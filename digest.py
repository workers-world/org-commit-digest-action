#!/usr/bin/env python3
"""Scan GitHub org or single repo for commits and tags in a time window."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


META_FILE = ".digest-meta.json"


def eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def run_gh(args: list[str]) -> str:
    env = os.environ.copy()
    token = env.get("GH_TOKEN") or env.get("GITHUB_TOKEN")
    if token:
        env["GH_TOKEN"] = token
    proc = subprocess.run(
        ["gh", *args],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"gh {' '.join(args)} failed: {err}")
    return proc.stdout


def parse_repo_list_json(raw: str) -> list[str]:
    """Parse `gh repo list --json name` output (a JSON array of objects)."""
    payload = json.loads(raw or "[]")
    if not isinstance(payload, list):
        raise RuntimeError(f"unexpected repo list response: {raw[:200]!r}")
    names: list[str] = []
    for item in payload:
        if isinstance(item, dict) and item.get("name"):
            names.append(str(item["name"]))
    return names


def parse_gh_jq_scalar(raw: str) -> str:
    """Parse scalar output from `gh ... --jq .field` (plain text, not JSON)."""
    return raw.strip()


def gh_graphql(query: str, **variables: str | None) -> dict[str, object]:
    args = ["api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        args.extend(["-f", f"{key}={value or ''}"])
    raw = run_gh(args)
    payload = json.loads(raw or "{}")
    errors = payload.get("errors")
    if errors:
        raise RuntimeError(f"graphql failed: {errors}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("graphql returned no data")
    return data


def gh_json(path: str, *, paginate: bool = False, params: dict[str, str] | None = None) -> object:
    args = ["api", path, "--jq", "."]
    if paginate:
        args.insert(2, "--paginate")
    if params:
        for key, value in params.items():
            args.extend(["-f", f"{key}={value}"])
    raw = run_gh(args)
    if not raw.strip():
        return []
    chunks = [chunk.strip() for chunk in re.split(r"\n(?=\[|\{)", raw.strip()) if chunk.strip()]
    if len(chunks) == 1:
        return json.loads(chunks[0])
    merged: list[object] = []
    for chunk in chunks:
        data = json.loads(chunk)
        if isinstance(data, list):
            merged.extend(data)
        else:
            merged.append(data)
    return merged


def parse_github_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def has_explicit_offset(value: str) -> bool:
    return bool(re.search(r"(?:[Zz])|(?:[+-]\d{2}:?\d{2})$", value.strip()))


def parse_window_value(value: str, tz_name: str) -> datetime:
    value = value.strip()
    if has_explicit_offset(value):
        return parse_github_dt(value)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        local = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=ZoneInfo(tz_name))
        return local.astimezone(timezone.utc)
    local = datetime.fromisoformat(value).replace(tzinfo=ZoneInfo(tz_name))
    return local.astimezone(timezone.utc)


def resolve_window(
    since_raw: str,
    until_raw: str,
    tz_name: str,
) -> tuple[datetime, datetime, str, str]:
    tz = ZoneInfo(tz_name)
    now_local = datetime.now(tz)
    until = parse_window_value(until_raw, tz_name) if until_raw.strip() else now_local.astimezone(timezone.utc)
    if since_raw.strip():
        since = parse_window_value(since_raw, tz_name)
    else:
        since_local = now_local - timedelta(days=7)
        since = since_local.astimezone(timezone.utc)

    if since > until:
        raise SystemExit("since must be before until")

    since_label = since.astimezone(tz).strftime("%Y-%m-%d")
    until_label = until.astimezone(tz).strftime("%Y-%m-%d")
    return since, until, since_label, until_label


def load_excludes(exclude_file: str, exclude_csv: str) -> set[str]:
    names: set[str] = set()
    for part in exclude_csv.split(","):
        part = part.strip()
        if part:
            names.add(part)
    if exclude_file.strip():
        path = Path(exclude_file)
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.split("#", 1)[0].strip()
                if line:
                    names.add(line)
    return names


def resolve_repos(org: str, repo: str) -> tuple[str, list[str]]:
    org = org.strip()
    repo = repo.strip()
    if repo:
        if "/" in repo:
            owner, name = repo.split("/", 1)
            return f"repo:{owner}/{name}", [f"{owner}/{name}"]
        if not org:
            raise SystemExit("repo short name requires org input")
        return f"repo:{org}/{repo}", [repo]
    if not org:
        raise SystemExit("org or repo is required")
    raw = run_gh(["repo", "list", org, "--json", "name", "--limit", "500"])
    names = parse_repo_list_json(raw)
    if not names:
        raise SystemExit(
            f"no repositories found for org {org!r} (verify org name and that GH_TOKEN can list org repos)"
        )
    return f"org:{org}", names


def repo_owner_name(org: str, repo_entry: str) -> tuple[str, str]:
    if "/" in repo_entry:
        owner, name = repo_entry.split("/", 1)
        return owner, name
    return org, repo_entry


def resolve_branch(owner: str, name: str, branch: str) -> str:
    requested = branch.strip() or "master"
    try:
        run_gh(["api", f"repos/{owner}/{name}/branches/{requested}", "--jq", ".name"])
        return requested
    except RuntimeError:
        default = parse_gh_jq_scalar(
            run_gh(["api", f"repos/{owner}/{name}", "--jq", ".default_branch"])
        )
        return default


def list_commits(
    owner: str,
    name: str,
    branch: str,
    since: datetime,
    until: datetime,
) -> list[dict[str, str]]:
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        commits = gh_json(
            f"repos/{owner}/{name}/commits",
            paginate=True,
            params={"sha": branch, "since": since_iso, "per_page": "100"},
        )
    except RuntimeError:
        return []

    rows: list[dict[str, str]] = []
    if not isinstance(commits, list):
        return rows
    for item in commits:
        if not isinstance(item, dict):
            continue
        commit = item.get("commit")
        if not isinstance(commit, dict):
            continue
        date_raw = (
            (commit.get("committer") or {}).get("date")
            or (commit.get("author") or {}).get("date")
            or ""
        )
        if not date_raw:
            continue
        committed = parse_github_dt(date_raw)
        if committed > until:
            continue
        sha = str(item.get("sha") or "")[:7]
        message = str((commit.get("message") or "").splitlines()[0])
        author = str(((commit.get("author") or {}).get("name") or "unknown"))
        rows.append(
            {
                "sha": sha,
                "message": message,
                "author": author,
                "date": committed.astimezone(timezone.utc).strftime("%Y-%m-%d"),
            }
        )
    return rows


def list_tags(
    owner: str,
    name: str,
    since: datetime,
    until: datetime,
) -> list[dict[str, str]]:
    try:
        return _list_tags_graphql(owner, name, since, until)
    except RuntimeError:
        return []


def _list_tags_graphql(
    owner: str,
    name: str,
    since: datetime,
    until: datetime,
) -> list[dict[str, str]]:
    query = """
    query($owner: String!, $name: String!, $cursor: String) {
      repository(owner: $owner, name: $name) {
        refs(
          refPrefix: "refs/tags/"
          first: 100
          after: $cursor
          orderBy: { field: TAG_COMMIT_DATE, direction: DESC }
        ) {
          pageInfo { hasNextPage endCursor }
          nodes {
            name
            target {
              ... on Commit { oid committedDate }
              ... on Tag {
                tagger { date }
                target { ... on Commit { oid committedDate } }
              }
            }
          }
        }
      }
    }
    """
    rows: list[dict[str, str]] = []
    cursor: str | None = None
    stop = False

    while not stop:
        data = gh_graphql(query, owner=owner, name=name, cursor=cursor or "")
        refs = data.get("repository", {})
        if isinstance(refs, dict):
            refs = refs.get("refs") or {}
        if not isinstance(refs, dict):
            break
        nodes = refs.get("nodes") or []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            tag_name = str(node.get("name") or "")
            target = node.get("target") or {}
            date_raw = ""
            sha = ""
            if isinstance(target, dict):
                if target.get("committedDate"):
                    date_raw = str(target["committedDate"])
                    sha = str(target.get("oid") or "")[:7]
                else:
                    tagger = target.get("tagger") or {}
                    if isinstance(tagger, dict) and tagger.get("date"):
                        date_raw = str(tagger["date"])
                    inner = target.get("target") or {}
                    if isinstance(inner, dict):
                        if not date_raw and inner.get("committedDate"):
                            date_raw = str(inner["committedDate"])
                        sha = str(inner.get("oid") or "")[:7]
            if not tag_name or not date_raw:
                continue
            committed = parse_github_dt(date_raw)
            if committed < since:
                stop = True
                break
            if since <= committed <= until:
                rows.append(
                    {
                        "name": tag_name,
                        "sha": sha or "???????",
                        "date": committed.astimezone(timezone.utc).strftime("%Y-%m-%d"),
                    }
                )

        page_info = refs.get("pageInfo") or {}
        if stop or not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
        if not cursor:
            break

    return rows


def build_digest(
    scope: str,
    since_label: str,
    until_label: str,
    tz_name: str,
    sections: list[tuple[str, list[dict[str, str]], list[dict[str, str]]]],
) -> str:
    lines = [
        f"# Commit digest: {scope} ({since_label} .. {until_label} {tz_name})",
        "",
    ]
    for repo_name, commits, tags in sections:
        if not commits and not tags:
            continue
        lines.append(f"## {repo_name}")
        lines.append("")
        if commits:
            lines.append(f"### Commits ({len(commits)})")
            for row in commits:
                lines.append(f"- `{row['sha']}` {row['message']} — {row['author']} ({row['date']})")
            lines.append("")
        if tags:
            lines.append(f"### Tags ({len(tags)})")
            for row in tags:
                lines.append(f"- `{row['name']}` → `{row['sha']}` ({row['date']})")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_meta(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def maybe_write_summary(text: str, verbose: bool) -> None:
    if not verbose:
        return
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "").strip()
    if summary_path:
        Path(summary_path).write_text(text + "\n", encoding="utf-8")


def main() -> int:
    org = os.environ.get("INPUT_ORG", "")
    repo = os.environ.get("INPUT_REPO", "")
    branch = os.environ.get("INPUT_BRANCH", "master")
    tz_name = os.environ.get("INPUT_TIMEZONE", "UTC") or "UTC"
    since_raw = os.environ.get("INPUT_SINCE", "")
    until_raw = os.environ.get("INPUT_UNTIL", "")
    exclude_file = os.environ.get("INPUT_EXCLUDE_FILE", "")
    exclude_csv = os.environ.get("INPUT_EXCLUDE", "")
    out_file = os.environ.get("INPUT_OUT", "digest.md") or "digest.md"
    verbose = os.environ.get("INPUT_VERBOSE", "false").lower() == "true"

    since, until, since_label, until_label = resolve_window(since_raw, until_raw, tz_name)
    scope, repo_names = resolve_repos(org, repo)
    excludes = load_excludes(exclude_file, exclude_csv)

    scanned = 0
    active = 0
    sections: list[tuple[str, list[dict[str, str]], list[dict[str, str]]]] = []

    for entry in repo_names:
        owner, name = repo_owner_name(org, entry)
        short_name = name if owner == org or not org else f"{owner}/{name}"
        if excludes and short_name in excludes:
            continue
        scanned += 1
        use_branch = resolve_branch(owner, name, branch)
        commits = list_commits(owner, name, use_branch, since, until)
        tags = list_tags(owner, name, since, until)
        if commits or tags:
            active += 1
            sections.append((short_name, commits, tags))
            if verbose:
                eprint(f"active {short_name}: {len(commits)} commits, {len(tags)} tags")

    meta_path = Path(META_FILE)
    if active == 0:
        message = f"no activity in window {since_label}..{until_label} {tz_name} ({scanned} repos scanned)"
        print(message)
        write_meta(
            meta_path,
            {
                "digest-file": "",
                "subject": "",
                "repo-count": scanned,
                "active-count": 0,
                "scope": scope,
                "has-activity": False,
                "window": f"{since_label}..{until_label}",
                "timezone": tz_name,
            },
        )
        return 0

    digest_text = build_digest(scope, since_label, until_label, tz_name, sections)
    Path(out_file).write_text(digest_text, encoding="utf-8")
    subject = f"[{scope.split(':', 1)[-1]}] weekly digest {since_label}..{until_label}"
    write_meta(
        meta_path,
        {
            "digest-file": out_file,
            "subject": subject,
            "repo-count": scanned,
            "active-count": active,
            "scope": scope,
            "has-activity": True,
            "window": f"{since_label}..{until_label}",
            "timezone": tz_name,
        },
    )
    maybe_write_summary(digest_text, verbose)
    print(f"digest written: {out_file} ({active} active / {scanned} scanned)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit as exc:
        if str(exc):
            eprint(str(exc))
        raise
