#!/usr/bin/env python3
"""Scan GitHub org or single repo for commits and tags in a time window."""

from __future__ import annotations

import csv
import html
import io
import json
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import noise_filter


META_FILE = ".digest-meta.json"
SUBJECT_MAX_LEN = 72


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


def build_gh_json_args(
    path: str,
    *,
    paginate: bool = False,
    params: dict[str, str] | None = None,
) -> list[str]:
    """Build `gh api` args for a REST GET.

    `gh api` silently switches to POST as soon as any -f/-F field is passed, so
    query params MUST be paired with an explicit `--method GET`; otherwise
    list endpoints such as /repos/{o}/{r}/commits return 404.
    """
    args = ["api", "--method", "GET", path, "--jq", "."]
    if paginate:
        args.insert(1, "--paginate")
    if params:
        for key, value in params.items():
            args.extend(["-f", f"{key}={value}"])
    return args


def gh_json(path: str, *, paginate: bool = False, params: dict[str, str] | None = None) -> object:
    args = build_gh_json_args(path, paginate=paginate, params=params)
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
        # GitHub follows renamed branches here (e.g. master -> dev_00_01_00) and
        # returns the *current* name; use it, since /commits?sha=<old name> 404s.
        actual = parse_gh_jq_scalar(
            run_gh(["api", f"repos/{owner}/{name}/branches/{requested}", "--jq", ".name"])
        )
        return actual or requested
    except RuntimeError:
        default = parse_gh_jq_scalar(
            run_gh(["api", f"repos/{owner}/{name}", "--jq", ".default_branch"])
        )
        return default


class CommitListError(RuntimeError):
    """Listing commits failed for a reason other than an empty repository."""


def is_empty_repo_error(message: str) -> bool:
    return "Git Repository is empty" in message or "HTTP 409" in message


def list_commits(
    owner: str,
    name: str,
    branch: str,
    since: datetime,
    until: datetime,
    tz: ZoneInfo | timezone = timezone.utc,
    include_merges: bool = False,
) -> list[dict[str, str]]:
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    until_iso = until.strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        commits = gh_json(
            f"repos/{owner}/{name}/commits",
            paginate=True,
            params={"sha": branch, "since": since_iso, "until": until_iso, "per_page": "100"},
        )
    except RuntimeError as exc:
        if is_empty_repo_error(str(exc)):
            return []
        raise CommitListError(str(exc)) from exc
    return parse_commit_rows(commits, until, tz, include_merges=include_merges)


def parse_commit_rows(
    commits: object,
    until: datetime,
    tz: ZoneInfo | timezone = timezone.utc,
    *,
    include_merges: bool = False,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    if not isinstance(commits, list):
        return rows
    for item in commits:
        if not isinstance(item, dict):
            continue
        commit = item.get("commit")
        if not isinstance(commit, dict):
            continue
        parents = item.get("parents")
        if not include_merges and isinstance(parents, list) and len(parents) > 1:
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
        message = str(((commit.get("message") or "").splitlines() or [""])[0])
        author = str(((commit.get("author") or {}).get("name") or "unknown"))
        rows.append(
            {
                "sha": sha,
                "message": message,
                "author": author,
                "date": committed.astimezone(tz).strftime("%Y-%m-%d"),
            }
        )
    return rows


def list_tags(
    owner: str,
    name: str,
    since: datetime,
    until: datetime,
    tz: ZoneInfo | timezone = timezone.utc,
) -> list[dict[str, str]]:
    try:
        return _list_tags_graphql(owner, name, since, until, tz)
    except RuntimeError:
        return []


def _list_tags_graphql(
    owner: str,
    name: str,
    since: datetime,
    until: datetime,
    tz: ZoneInfo | timezone = timezone.utc,
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
                        "date": committed.astimezone(tz).strftime("%Y-%m-%d"),
                    }
                )

        page_info = refs.get("pageInfo") or {}
        if stop or not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
        if not cursor:
            break

    return rows


def truncate_subject(message: str, max_len: int = SUBJECT_MAX_LEN) -> str:
    one_line = " ".join(message.split())
    if len(one_line) <= max_len:
        return one_line
    if max_len <= 1:
        return one_line[:max_len]
    return one_line[: max_len - 1].rstrip() + "…"


def active_sections(
    sections: list[tuple[str, list[dict[str, str]], list[dict[str, str]]]],
) -> list[tuple[str, list[dict[str, str]], list[dict[str, str]]]]:
    return [(name, commits, tags) for name, commits, tags in sections if commits or tags]


def sort_sections(
    sections: list[tuple[str, list[dict[str, str]], list[dict[str, str]]]],
) -> list[tuple[str, list[dict[str, str]], list[dict[str, str]]]]:
    """Order repos by commit count (desc), then tag count (desc), then name."""
    return sorted(
        active_sections(sections),
        key=lambda item: (-len(item[1]), -len(item[2]), item[0].casefold()),
    )


def format_commit_line(row: dict[str, str]) -> str:
    subject = truncate_subject(row["message"])
    return f"- `{row['sha']}` {subject} — {row['author']} ({row['date']})"


def digest_title(scope: str, since_label: str, until_label: str, tz_name: str) -> str:
    return f"Commit digest: {scope} ({since_label} .. {until_label} {tz_name})"


def build_summary_rows(
    sections: list[tuple[str, list[dict[str, str]], list[dict[str, str]]]],
) -> list[tuple[str, int, int]]:
    return [(name, len(commits), len(tags)) for name, commits, tags in sort_sections(sections)]


def build_summary_text(
    since_label: str,
    until_label: str,
    tz_name: str,
    summary_rows: list[tuple[str, int, int]],
) -> str:
    total_commits = sum(c for _, c, _ in summary_rows)
    total_tags = sum(t for _, _, t in summary_rows)
    lines = [
        "## Summary",
        "",
        f"Window: **{since_label} .. {until_label}** ({tz_name})",
        f"Repos with activity: **{len(summary_rows)}** · Commits: **{total_commits}** · Tags: **{total_tags}**",
        "",
        "| Repo | Commits | Tags |",
        "| --- | ---: | ---: |",
    ]
    for repo_name, commit_count, tag_count in summary_rows:
        lines.append(f"| {repo_name} | {commit_count} | {tag_count} |")
    lines.extend(["", "---", ""])
    return "\n".join(lines)


def build_repo_sections_text(
    sections: list[tuple[str, list[dict[str, str]], list[dict[str, str]]]],
) -> str:
    lines: list[str] = []
    for repo_name, commits, tags in sort_sections(sections):
        lines.append(f"## {repo_name}")
        lines.append("")
        if commits:
            lines.append(f"### Commits ({len(commits)})")
            for row in commits:
                lines.append(format_commit_line(row))
            lines.append("")
        if tags:
            lines.append(f"### Tags ({len(tags)})")
            for row in tags:
                lines.append(f"- `{row['name']}` → `{row['sha']}` ({row['date']})")
            lines.append("")
    return "\n".join(lines).rstrip() + ("\n" if lines else "")


def build_digest(
    scope: str,
    since_label: str,
    until_label: str,
    tz_name: str,
    sections: list[tuple[str, list[dict[str, str]], list[dict[str, str]]]],
) -> str:
    summary_rows = build_summary_rows(sections)
    parts = [
        f"# {digest_title(scope, since_label, until_label, tz_name)}",
        "",
        build_summary_text(since_label, until_label, tz_name, summary_rows).rstrip(),
        build_repo_sections_text(sections).rstrip(),
    ]
    return "\n".join(parts).rstrip() + "\n"


def build_digest_html(
    scope: str,
    since_label: str,
    until_label: str,
    tz_name: str,
    sections: list[tuple[str, list[dict[str, str]], list[dict[str, str]]]],
) -> str:
    summary_rows = build_summary_rows(sections)
    total_commits = sum(c for _, c, _ in summary_rows)
    total_tags = sum(t for _, _, t in summary_rows)
    title = html.escape(digest_title(scope, since_label, until_label, tz_name))
    window = html.escape(f"{since_label} .. {until_label} ({tz_name})")

    summary_table = [
        "<table>",
        "<thead><tr><th align=\"left\">Repo</th><th align=\"right\">Commits</th><th align=\"right\">Tags</th></tr></thead>",
        "<tbody>",
    ]
    for repo_name, commit_count, tag_count in summary_rows:
        summary_table.append(
            f"<tr><td>{html.escape(repo_name)}</td>"
            f"<td align=\"right\">{commit_count}</td>"
            f"<td align=\"right\">{tag_count}</td></tr>"
        )
    summary_table.extend(["</tbody>", "</table>"])

    body_parts = [
        "<!DOCTYPE html>",
        "<html>",
        "<head><meta charset=\"utf-8\"></head>",
        "<body style=\"font-family: system-ui, -apple-system, Segoe UI, sans-serif; line-height: 1.45; color: #1f2328;\">",
        f"<h1 style=\"font-size: 1.25rem;\">{title}</h1>",
        f"<p><strong>Window:</strong> {window}<br>",
        f"<strong>Repos with activity:</strong> {len(summary_rows)} · "
        f"<strong>Commits:</strong> {total_commits} · <strong>Tags:</strong> {total_tags}</p>",
        "\n".join(summary_table),
        "<hr>",
    ]

    for repo_name, commits, tags in sort_sections(sections):
        body_parts.append(f"<h2 style=\"font-size: 1.05rem; margin-top: 1.25rem;\">{html.escape(repo_name)}</h2>")
        if commits:
            body_parts.append(f"<h3 style=\"font-size: 0.95rem;\">Commits ({len(commits)})</h3><ul>")
            for row in commits:
                subject = html.escape(truncate_subject(row["message"]))
                author = html.escape(row["author"])
                sha = html.escape(row["sha"])
                date = html.escape(row["date"])
                body_parts.append(
                    f"<li><code>{sha}</code> {subject} — {author} ({date})</li>"
                )
            body_parts.append("</ul>")
        if tags:
            body_parts.append(f"<h3 style=\"font-size: 0.95rem;\">Tags ({len(tags)})</h3><ul>")
            for row in tags:
                name = html.escape(row["name"])
                sha = html.escape(row["sha"])
                date = html.escape(row["date"])
                body_parts.append(f"<li><code>{name}</code> → <code>{sha}</code> ({date})</li>")
            body_parts.append("</ul>")

    body_parts.extend(["</body>", "</html>"])
    return "\n".join(body_parts) + "\n"


def html_path_for_digest(out_file: str) -> Path:
    path = Path(out_file)
    if path.suffix:
        return path.with_suffix(".html")
    return path.with_name(f"{path.name}.html")


def csv_path_for_digest(out_file: str) -> Path:
    path = Path(out_file)
    if path.suffix:
        return path.with_suffix(".csv")
    return path.with_name(f"{path.name}.csv")


class CommitFilesCache:
    """Per-run cache for GET /repos/{o}/{r}/commits/{sha} file lists."""

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str, str], list[str] | None] = {}

    def get(self, owner: str, name: str, sha: str) -> list[str] | None:
        key = (owner, name, sha)
        if key in self._cache:
            return self._cache[key]
        try:
            data = gh_json(f"repos/{owner}/{name}/commits/{sha}")
        except RuntimeError:
            self._cache[key] = None
            return None
        files: list[str] = []
        if isinstance(data, dict):
            for entry in data.get("files") or []:
                if isinstance(entry, dict) and entry.get("filename"):
                    files.append(str(entry["filename"]))
        self._cache[key] = files
        return files


def log_noise_filter_stats(ignored: Counter[str], *, verbose: bool) -> None:
    total = sum(ignored.values())
    if total == 0:
        return
    commits_dropped = total  # tags included in total; stderr stays subject-free
    eprint(f"noise-filter: dropped {commits_dropped} items")
    if verbose:
        for reason in sorted(ignored):
            eprint(f"noise-filter:   {ignored[reason]} {reason}")


def build_digest_csv(
    since_label: str,
    until_label: str,
    tz_name: str,
    sections: list[tuple[str, list[dict[str, str]], list[dict[str, str]]]],
) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        ["window_since", "window_until", "timezone", "repo", "type", "sha", "name", "author", "date"]
    )
    window_since = since_label
    window_until = until_label
    for repo_name, commits, tags in sort_sections(sections):
        for row in commits:
            writer.writerow(
                [
                    window_since,
                    window_until,
                    tz_name,
                    repo_name,
                    "commit",
                    row["sha"],
                    row["message"],
                    row["author"],
                    row["date"],
                ]
            )
        for row in tags:
            writer.writerow(
                [
                    window_since,
                    window_until,
                    tz_name,
                    repo_name,
                    "tag",
                    row["sha"],
                    row["name"],
                    "",
                    row["date"],
                ]
            )
    return buffer.getvalue()


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
    include_merges = os.environ.get("INPUT_INCLUDE_MERGES", "false").lower() == "true"
    noise_filter_enabled = os.environ.get("INPUT_NOISE_FILTER", "true").lower() != "false"

    since, until, since_label, until_label = resolve_window(since_raw, until_raw, tz_name)
    scope, repo_names = resolve_repos(org, repo)
    excludes = load_excludes(exclude_file, exclude_csv)

    tz = ZoneInfo(tz_name)
    scanned = 0
    active = 0
    commit_failures = 0
    sections: list[tuple[str, list[dict[str, str]], list[dict[str, str]]]] = []

    for entry in repo_names:
        owner, name = repo_owner_name(org, entry)
        short_name = name if owner == org or not org else f"{owner}/{name}"
        if excludes and short_name in excludes:
            continue
        scanned += 1
        use_branch = resolve_branch(owner, name, branch)
        try:
            commits = list_commits(owner, name, use_branch, since, until, tz, include_merges)
        except CommitListError as exc:
            commit_failures += 1
            commits = []
            if verbose:
                eprint(f"warn: listing commits failed for {short_name}: {exc}")
        tags = list_tags(owner, name, since, until, tz)
        if commits or tags:
            active += 1
            sections.append((short_name, commits, tags))
            if verbose:
                eprint(f"active {short_name}: {len(commits)} commits, {len(tags)} tags")

    if commit_failures:
        # Count only: repo names may be private and this log can be public.
        eprint(f"warn: commit listing failed for {commit_failures}/{scanned} repos")
        if commit_failures == scanned:
            raise SystemExit("commit listing failed for every scanned repo; refusing to send a tag-only digest")

    if noise_filter_enabled and sections:
        files_cache = CommitFilesCache()
        sections, ignored_counts = noise_filter.filter_digest_rows(
            org,
            sections,
            fetch_files=files_cache.get,
            repo_owner_name=repo_owner_name,
        )
        log_noise_filter_stats(ignored_counts, verbose=verbose)
        active = len(sections)

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
    html_file = html_path_for_digest(out_file)
    csv_file = csv_path_for_digest(out_file)
    digest_html = build_digest_html(scope, since_label, until_label, tz_name, sections)
    digest_csv = build_digest_csv(since_label, until_label, tz_name, sections)
    Path(out_file).write_text(digest_text, encoding="utf-8")
    html_file.write_text(digest_html, encoding="utf-8")
    csv_file.write_text(digest_csv, encoding="utf-8")
    subject = f"[{scope.split(':', 1)[-1]}] weekly digest {since_label}..{until_label}"
    write_meta(
        meta_path,
        {
            "digest-file": out_file,
            "digest-html-file": str(html_file),
            "digest-csv-file": str(csv_file),
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
        if isinstance(exc.code, str) and exc.code:
            eprint(exc.code)
        raise
