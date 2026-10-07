#!/usr/bin/env python3
"""Scan GitHub org or single repo for commits and tags in a time window."""

from __future__ import annotations

import base64
import csv
import html
import io
import json
import os
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import noise_filter


META_FILE = ".digest-meta.json"
HISTORY_META_GLOB = "*.digest-meta.json"
SUBJECT_MAX_LEN = 72
TOP_REPOS_SUMMARY_N = 8
FANOUT_MIN_REPO_COUNT = 2
NOTABLE_COMMITS_CAP = 12
HIGHLIGHT_TAGS_CAP = 10
SPARKLINE_WEEKS = 8

# Inline palette for email-safe HTML digests
HTML_COLOR_UP = "#1a7f37"
HTML_COLOR_DOWN = "#cf222e"
HTML_COLOR_FLAT = "#656d76"
HTML_COLOR_TAG_BG = "#ddf4ff"
HTML_COLOR_TAG_FG = "#0550ae"
HTML_COLOR_CHART_COMMITS = "#0969da"
HTML_COLOR_CHART_REPOS = "#8250df"
HTML_TABLE_STYLE = (
    'style="border-collapse:collapse; width:100%; max-width:640px; '
    'font-size:0.9rem; border:1px solid #d0d7de;"'
)
HTML_TH_STYLE = (
    'style="background:#f6f8fa; border-bottom:1px solid #d0d7de; '
    'padding:6px 10px; font-weight:600;"'
)
HTML_TD_STYLE = 'style="border-bottom:1px solid #eaeef2; padding:6px 10px;"'
HTML_TD_RIGHT_STYLE = (
    'style="border-bottom:1px solid #eaeef2; padding:6px 10px; text-align:right;"'
)

COMMIT_TYPE_ORDER = ("feat", "fix", "chore", "ci", "docs", "release", "other")
COMMIT_TYPE_CHART_COLORS: dict[str, str] = {
    "feat": "#1a7f37",
    "fix": "#cf222e",
    "chore": "#656d76",
    "ci": "#8250df",
    "docs": "#0969da",
    "release": "#bf8700",
    "other": "#8b949e",
}
_COMMIT_TYPE_LABEL = re.compile(
    r"^(feat|fix|chore|ci|docs|refactor|test|build|perf|style|revert|release)(?:\([^)]+\))?!?\s*:",
    re.IGNORECASE,
)
_RELEASE_SUBJECT = re.compile(r"^release:", re.IGNORECASE)

# Fan-out subject matching (see README): conventional prefix, WW-N keys, (#PR) suffix.
_CONVENTIONAL_COMMIT_PREFIX = re.compile(
    r"^(?:fix|feat|chore|ci|docs|refactor|test|build|perf|style|revert)(?:\([^)]+\))?!?\s*:\s*",
    re.IGNORECASE,
)
_ISSUE_KEY_PATTERN = re.compile(r"\bWW-\d+\b", re.IGNORECASE)
_TRAILING_PR_REF = re.compile(r"\s*\(#\d+\)\s*$")
_FULLWIDTH_PAREN_QUALIFIER = re.compile(r"（[^）]*）")


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


def normalize_subject_key(message: str) -> str:
    """Normalize commit subject for cross-repo fan-out grouping.

    Heuristic: strip conventional-commit type/scope prefix, remove WW-N issue
    keys, fullwidth parenthetical repo qualifiers (e.g. ``（sch1 试点）``), trailing
    (#123) PR refs, collapse whitespace, lowercase, trim trailing periods. ASCII
    parentheses (e.g. ``(zizmor secrets-inherit)``) are kept so distinct CI themes
    do not merge. Two kept commits merge when keys match and they appear in at
    least FANOUT_MIN_REPO_COUNT distinct repos (see group_fanout_themes).
    """
    text = " ".join(message.split())
    text = _CONVENTIONAL_COMMIT_PREFIX.sub("", text)
    text = _ISSUE_KEY_PATTERN.sub("", text)
    text = _FULLWIDTH_PAREN_QUALIFIER.sub(" ", text)
    text = _TRAILING_PR_REF.sub("", text)
    text = re.sub(r"\s*\(\s*\)\s*", " ", text)
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text.rstrip(".")


@dataclass(frozen=True)
class FanoutTheme:
    display_subject: str
    repo_count: int
    commit_count: int
    author_count: int
    repos: tuple[str, ...]
    primary_author: str | None


def group_fanout_themes(
    sections: list[tuple[str, list[dict[str, str]], list[dict[str, str]]]],
    *,
    min_repo_count: int = FANOUT_MIN_REPO_COUNT,
) -> list[FanoutTheme]:
    buckets: dict[str, list[tuple[str, dict[str, str]]]] = {}
    for repo_name, commits, _tags in sections:
        for commit in commits:
            key = normalize_subject_key(commit["message"])
            if not key:
                continue
            buckets.setdefault(key, []).append((repo_name, commit))

    themes: list[FanoutTheme] = []
    for _key, entries in buckets.items():
        repos = sorted({repo for repo, _ in entries}, key=str.casefold)
        if len(repos) < min_repo_count:
            continue
        messages = [commit["message"] for _, commit in entries]
        display = max(messages, key=lambda msg: (messages.count(msg), len(msg)))
        authors = Counter(commit["author"] for _, commit in entries)
        primary_author: str | None = None
        if len(authors) == 1:
            primary_author = next(iter(authors))
        themes.append(
            FanoutTheme(
                display_subject=truncate_subject(display),
                repo_count=len(repos),
                commit_count=len(entries),
                author_count=len(authors),
                repos=tuple(repos),
                primary_author=primary_author,
            )
        )
    themes.sort(
        key=lambda theme: (-theme.repo_count, -theme.commit_count, theme.display_subject.casefold())
    )
    return themes


def fanout_subject_keys(
    sections: list[tuple[str, list[dict[str, str]], list[dict[str, str]]]],
    *,
    min_repo_count: int = FANOUT_MIN_REPO_COUNT,
) -> set[str]:
    buckets: dict[str, set[str]] = {}
    for repo_name, commits, _tags in sections:
        for commit in commits:
            key = normalize_subject_key(commit["message"])
            if not key:
                continue
            buckets.setdefault(key, set()).add(repo_name)
    return {key for key, repos in buckets.items() if len(repos) >= min_repo_count}


def collect_notable_commits(
    sections: list[tuple[str, list[dict[str, str]], list[dict[str, str]]]],
    *,
    folded_keys: set[str],
    cap: int = NOTABLE_COMMITS_CAP,
) -> list[tuple[str, dict[str, str]]]:
    """Non-fan-out commits for the highlights section (repo order = sort_sections)."""
    rows: list[tuple[str, dict[str, str]]] = []
    for repo_name, commits, _tags in sort_sections(sections):
        for commit in commits:
            if normalize_subject_key(commit["message"]) in folded_keys:
                continue
            rows.append((repo_name, commit))
            if len(rows) >= cap:
                return rows
    return rows


def collect_highlight_tags(
    sections: list[tuple[str, list[dict[str, str]], list[dict[str, str]]]],
    *,
    cap: int = HIGHLIGHT_TAGS_CAP,
) -> tuple[list[tuple[str, dict[str, str]]], int]:
    tags: list[tuple[str, dict[str, str]]] = []
    for repo_name, _commits, repo_tags in sort_sections(sections):
        for tag in repo_tags:
            tags.append((repo_name, tag))
    if len(tags) <= cap:
        return tags, 0
    return tags[:cap], len(tags) - cap


def split_summary_rows_top_n(
    summary_rows: list[tuple[str, int, int, str]],
    n: int = TOP_REPOS_SUMMARY_N,
) -> tuple[list[tuple[str, int, int, str]], int, int, int]:
    """Return (top rows, extra repo count, extra commits, extra tags)."""
    if len(summary_rows) <= n:
        return summary_rows, 0, 0, 0
    top = summary_rows[:n]
    rest = summary_rows[n:]
    extra_commits = sum(c for _, c, _, _ in rest)
    extra_tags = sum(t for _, _, t, _ in rest)
    return top, len(rest), extra_commits, extra_tags


def build_tldr_sentence(
    scope: str,
    since_label: str,
    until_label: str,
    summary_rows: list[tuple[str, int, int, str]],
    themes: list[FanoutTheme],
    *,
    org_noise_ratio: str | None = None,
) -> str:
    org_label = scope.split(":", 1)[-1]
    total_commits = sum(c for _, c, _, _ in summary_rows)
    total_tags = sum(t for _, _, t, _ in summary_rows)
    sentence = (
        f"**{org_label}** — **{len(summary_rows)}** active repos, "
        f"**{total_commits}** kept commits, **{total_tags}** tags "
        f"({since_label} .. {until_label})."
    )
    if themes:
        named = ", ".join(f"“{theme.display_subject}” ({theme.repo_count} repos)" for theme in themes[:3])
        if len(themes) > 3:
            named += f", +{len(themes) - 3} more themes"
        sentence += f" Cross-repo fan-out: {named}."
    if org_noise_ratio is not None:
        sentence += f" Noise filter dropped **{org_noise_ratio}** of raw commit/tag volume org-wide."
    return sentence


def format_fanout_theme_line(theme: FanoutTheme) -> str:
    line = f"- **{theme.display_subject}** — **{theme.repo_count}** repos ({theme.commit_count} commits)"
    if theme.primary_author:
        line += f", {theme.primary_author}"
    return line


def format_summary_table_md(summary_rows: list[tuple[str, int, int, str]]) -> list[str]:
    lines = [
        "| Repo | Commits | Tags | Noise |",
        "| --- | ---: | ---: | ---: |",
    ]
    for repo_name, commit_count, tag_count, noise in summary_rows:
        lines.append(f"| {repo_name} | {commit_count} | {tag_count} | {noise} |")
    return lines


def build_layered_summary_md(
    since_label: str,
    until_label: str,
    tz_name: str,
    summary_rows: list[tuple[str, int, int, str]],
    sections: list[tuple[str, list[dict[str, str]], list[dict[str, str]]]],
    *,
    scope: str = "",
    org_noise_ratio: str | None = None,
    top_n: int = TOP_REPOS_SUMMARY_N,
    trend: SummaryTrendContext | None = None,
    type_histogram: Counter[str] | None = None,
) -> str:
    themes = group_fanout_themes(sections)
    folded_keys = fanout_subject_keys(sections)
    notable = collect_notable_commits(sections, folded_keys=folded_keys)
    highlight_tags, extra_highlight_tags = collect_highlight_tags(sections)
    top_rows, extra_repos, extra_commits, extra_tags = split_summary_rows_top_n(summary_rows, top_n)

    activity_line = format_activity_line_with_wow(
        summary_rows,
        org_noise_ratio,
        trend=trend,
    )

    lines = [
        "## Summary",
        "",
    ]
    if scope:
        lines.extend([f"**TL;DR:** {build_tldr_sentence(scope, since_label, until_label, summary_rows, themes, org_noise_ratio=org_noise_ratio)}", ""])
    lines.extend(
        [
            f"Window: **{since_label} .. {until_label}** ({tz_name})",
            activity_line,
            "",
        ]
    )

    spark_lines = format_sparkline_block_md(trend)
    if spark_lines:
        lines.extend(spark_lines)
        lines.append("")

    if type_histogram is not None:
        lines.extend(["### Commit types (kept)", "", format_type_rollup_line(type_histogram), ""])

    if themes:
        lines.extend(["### Cross-repo themes", ""])
        for theme in themes:
            lines.append(format_fanout_theme_line(theme))
        lines.append("")

    lines.extend(["### Top repositories", ""])
    lines.extend(format_summary_table_md(top_rows))
    if extra_repos:
        lines.append("")
        lines.append(
            f"*+{extra_repos} more repos* ({extra_commits} commits, {extra_tags} tags) — see **Full detail**."
        )
    lines.append("")

    if highlight_tags or notable:
        lines.append("### Highlights")
        lines.append("")
        if highlight_tags:
            lines.append("#### Tags / releases")
            for repo_name, tag in highlight_tags:
                lines.append(
                    f"- `{repo_name}` `{tag['name']}` → `{tag['sha']}` ({tag['date']})"
                )
            if extra_highlight_tags:
                lines.append(f"- *+{extra_highlight_tags} more tags in Full detail / CSV*")
            lines.append("")
        if notable:
            lines.append("#### Notable commits")
            for repo_name, commit in notable:
                subject = truncate_subject(commit["message"])
                lines.append(
                    f"- `{repo_name}` `{commit['sha']}` {subject} — {commit['author']} ({commit['date']})"
                )
            lines.append("")

    lines.extend(["---", "", "## Full detail", ""])
    lines.extend(["### All repositories", ""])
    lines.extend(format_summary_table_md(summary_rows))
    lines.extend(["", "---", ""])
    return "\n".join(lines)


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


def format_noise_ratio(*, raw: int, kept: int) -> str:
    if raw <= 0:
        return "0.0%"
    dropped = raw - kept
    return f"{100.0 * dropped / raw:.1f}%"


def repo_raw_total(counts: tuple[int, int, int, int]) -> int:
    return counts[0] + counts[1]


def repo_kept_total(counts: tuple[int, int, int, int]) -> int:
    return counts[2] + counts[3]


def build_summary_rows(
    sections: list[tuple[str, list[dict[str, str]], list[dict[str, str]]]],
    *,
    per_repo_counts: dict[str, tuple[int, int, int, int]] | None = None,
) -> list[tuple[str, int, int, str]]:
    """Summary table rows: repo name, kept commits/tags, noise ratio string."""
    if per_repo_counts is None:
        rows = [
            (name, len(commits), len(tags), "0.0%")
            for name, commits, tags in sort_sections(sections)
        ]
        return rows

    summary: list[tuple[str, int, int, str]] = []
    for repo_name, raw_c, raw_t, kept_c, kept_t in (
        (name, *counts) for name, counts in per_repo_counts.items()
    ):
        raw = raw_c + raw_t
        if raw <= 0:
            continue
        kept = kept_c + kept_t
        summary.append(
            (
                repo_name,
                kept_c,
                kept_t,
                format_noise_ratio(raw=raw, kept=kept),
            )
        )
    return sorted(
        summary,
        key=lambda item: (-item[1], -item[2], item[0].casefold()),
    )


def org_wide_noise_ratio(
    per_repo_counts: dict[str, tuple[int, int, int, int]],
) -> str:
    raw = sum(repo_raw_total(c) for c in per_repo_counts.values())
    kept = sum(repo_kept_total(c) for c in per_repo_counts.values())
    return format_noise_ratio(raw=raw, kept=kept)


def parse_noise_pct(ratio: str | None) -> float | None:
    if not ratio:
        return None
    text = ratio.strip().rstrip("%")
    try:
        return float(text)
    except ValueError:
        return None


def normalize_window_label(window: object) -> str | None:
    if isinstance(window, str):
        text = window.strip()
        return text if text else None
    if isinstance(window, dict):
        since = str(window.get("since") or "").strip()
        until = str(window.get("until") or "").strip()
        if since and until:
            return f"{since}..{until}"
    return None


def normalize_kpi_embedded(embedded: dict[str, object]) -> dict[str, object]:
    """Accept slim seed metas (snake_case keys) and canonical hyphenated KPI fields."""
    out = dict(embedded)
    alias = (
        ("active_repos", "active-repos"),
        ("kept_commits", "kept-commits"),
        ("org_noise_ratio", "noise-pct"),
    )
    for old_key, new_key in alias:
        if old_key in out and new_key not in out:
            out[new_key] = out.pop(old_key)
    return out


def parse_window_until(window: str) -> str | None:
    if ".." not in window:
        return None
    return window.split("..", 1)[1].strip()


def parse_window_since(window: str) -> str | None:
    if ".." not in window:
        return None
    return window.split("..", 1)[0].strip()


def count_kept_rows_from_csv(csv_path: Path) -> tuple[int, int]:
    commits = 0
    tags = 0
    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            item_type = (row.get("type") or "").strip()
            if item_type == "commit":
                commits += 1
            elif item_type == "tag":
                tags += 1
    return commits, tags


def resolve_digest_csv_path(data: dict[str, object], meta_path: Path) -> Path | None:
    candidates: list[Path] = []
    raw = data.get("digest-csv-file")
    if raw:
        path = Path(str(raw))
        candidates.extend([path, meta_path.parent / path.name])
    candidates.append(meta_path.parent / "digest.csv")
    seen: set[str] = set()
    for candidate in candidates:
        try:
            key = str(candidate.resolve())
        except OSError:
            key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if candidate.is_file():
            return candidate
    return None


def bucket_commit_type(message: str) -> str:
    if _RELEASE_SUBJECT.match(message.strip()):
        return "release"
    match = _COMMIT_TYPE_LABEL.match(message.strip())
    if not match:
        return "other"
    label = match.group(1).lower()
    if label == "release":
        return "release"
    if label in COMMIT_TYPE_ORDER:
        return label
    return "other"


def build_commit_type_histogram(
    sections: list[tuple[str, list[dict[str, str]], list[dict[str, str]]]],
) -> Counter[str]:
    counts: Counter[str] = Counter()
    for _repo, commits, _tags in sections:
        for row in commits:
            counts[bucket_commit_type(row["message"])] += 1
    return counts


def format_type_rollup_line(type_counts: Counter[str]) -> str:
    parts: list[str] = []
    for key in COMMIT_TYPE_ORDER:
        count = type_counts.get(key, 0)
        if count:
            parts.append(f"**{key}** {count}")
    return " · ".join(parts) if parts else "_no kept commits_"


def summarize_noise_rules(ignored: Counter[str]) -> str:
    if not ignored:
        return ""
    groups: Counter[str] = Counter()
    for reason, count in ignored.items():
        prefix = reason.split()[0] if reason else reason
        groups[prefix] += count
    parts = [f"{rule} ({groups[rule]})" for rule in sorted(groups)]
    return ", ".join(parts)


def build_kpi_snapshot(
    summary_rows: list[tuple[str, int, int, str]],
    *,
    scope: str,
    window: str,
    org_noise_ratio: str | None,
) -> dict[str, object]:
    total_commits = sum(c for _, c, _, _ in summary_rows)
    total_tags = sum(t for _, _, t, _ in summary_rows)
    return {
        "scope": scope,
        "window": window,
        "active-repos": len(summary_rows),
        "kept-commits": total_commits,
        "tags": total_tags,
        "noise-pct": parse_noise_pct(org_noise_ratio),
    }


def resolve_history_dir(out_file: str, history_dir_input: str) -> Path:
    if history_dir_input.strip():
        return Path(history_dir_input.strip())
    out_path = Path(out_file)
    if out_path.parent != Path("."):
        return out_path.parent
    return Path(".")


def history_meta_path(history_dir: Path, until_label: str) -> Path:
    return history_dir / f"{until_label}.digest-meta.json"


def week_dir_meta_path(history_dir: Path, since_label: str, until_label: str) -> Path:
    return history_dir / "weeks" / f"{since_label}_{until_label}" / META_FILE


def iter_history_meta_paths(history_dir: Path) -> list[Path]:
    """Discover archived meta: flat `{until}.digest-meta.json` and batch `weeks/*/.digest-meta.json`."""
    if not history_dir.is_dir():
        return []
    found: dict[str, Path] = {}
    patterns = (
        HISTORY_META_GLOB,
        f"weeks/*/{META_FILE}",
        f"weeks/*/*{META_FILE}",
        f"**/{META_FILE}",
        "weeks/*/digest-meta.json",
        "**/digest-meta.json",
    )
    for pattern in patterns:
        for path in history_dir.glob(pattern):
            if not path.is_file():
                continue
            if path.name == META_FILE and path.parent.resolve() == history_dir.resolve():
                continue
            found[str(path.resolve())] = path
    return sorted(found.values(), key=lambda item: str(item))


def kpi_from_meta_record(data: dict[str, object], *, meta_path: Path) -> dict[str, object] | None:
    scope = str(data.get("scope") or "")
    window = normalize_window_label(data.get("window"))
    if not scope or not window:
        return None
    until = parse_window_until(window)
    if not until:
        return None

    embedded = data.get("kpi")
    if isinstance(embedded, dict):
        embedded = normalize_kpi_embedded(embedded)
    if isinstance(embedded, dict) and embedded.get("active-repos") is not None:
        merged: dict[str, object] = dict(embedded)
    else:
        has_activity = bool(data.get("has-activity"))
        active = int(data.get("active-count") or 0)
        kept_commits = 0
        tags = 0
        if has_activity:
            csv_path = resolve_digest_csv_path(data, meta_path)
            if csv_path is not None:
                kept_commits, tags = count_kept_rows_from_csv(csv_path)
        noise_pct: float | None = None
        if isinstance(embedded, dict) and embedded.get("noise-pct") is not None:
            raw_noise = embedded.get("noise-pct")
            if isinstance(raw_noise, (int, float)):
                noise_pct = float(raw_noise)
            elif isinstance(raw_noise, str):
                noise_pct = parse_noise_pct(raw_noise)
        merged = {
            "scope": scope,
            "window": window,
            "active-repos": active,
            "kept-commits": kept_commits,
            "tags": tags,
            "noise-pct": noise_pct,
        }

    merged["window"] = window
    merged["scope"] = scope
    merged["_until"] = until
    merged["_since"] = parse_window_since(window)
    return merged


def load_kpi_history(
    history_dir: Path,
    scope: str,
    *,
    exclude_window: str | None = None,
) -> list[dict[str, object]]:
    snapshots: list[dict[str, object]] = []
    seen_windows: set[str] = set()
    for path in iter_history_meta_paths(history_dir):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        if data.get("scope") != scope:
            continue
        merged = kpi_from_meta_record(data, meta_path=path)
        if merged is None:
            continue
        window = str(merged.get("window") or "")
        if exclude_window and window == exclude_window:
            continue
        if window in seen_windows:
            continue
        seen_windows.add(window)
        snapshots.append(merged)
    snapshots.sort(key=lambda item: str(item.get("_until", "")))
    return snapshots


def find_prior_week_kpi(
    history: list[dict[str, object]],
    since_label: str,
) -> dict[str, object] | None:
    """Prior Mon–Mon window: snapshot whose window ends on this run's ``since`` date."""
    for snap in history:
        if str(snap.get("_until")) == since_label:
            return snap
    return None


def select_prior_week_kpi(history: list[dict[str, object]]) -> dict[str, object] | None:
    if not history:
        return None
    return history[-1]


def persist_history_meta(
    history_dir: Path,
    since_label: str,
    until_label: str,
    payload: dict[str, object],
) -> None:
    history_dir.mkdir(parents=True, exist_ok=True)
    write_meta(history_meta_path(history_dir, until_label), payload)
    week_path = week_dir_meta_path(history_dir, since_label, until_label)
    week_path.parent.mkdir(parents=True, exist_ok=True)
    write_meta(week_path, payload)


def wow_delta_pct(current: float | int, previous: float | int | None) -> float | None:
    if previous is None:
        return None
    if previous == 0:
        if current == 0:
            return 0.0
        return None
    return 100.0 * (float(current) - float(previous)) / float(previous)


def format_wow_suffix(delta_pct: float | None) -> str:
    if delta_pct is None:
        return ""
    if delta_pct == 0:
        return " (→0%)"
    arrow = "↑" if delta_pct > 0 else "↓"
    return f" ({arrow}{abs(delta_pct):.1f}% vs last week)"


def sparkline_ascii(values: list[int], *, width: int = SPARKLINE_WEEKS) -> str:
    if not values:
        return ""
    vals = values[-width:]
    if len(vals) < width:
        vals = [0] * (width - len(vals)) + vals
    mn, mx = min(vals), max(vals)
    chars = "▁▂▃▄▅▆▇█"
    if mx == mn:
        return chars[0] * len(vals) if mx == 0 else chars[len(chars) // 2] * len(vals)
    out: list[str] = []
    for value in vals:
        idx = int((value - mn) / (mx - mn) * (len(chars) - 1))
        out.append(chars[idx])
    return "".join(out)


def sparkline_svg_data_uri(values: list[int], *, width: int = 72, height: int = 18) -> str:
    if not values:
        values = [0]
    vals = values[-SPARKLINE_WEEKS:]
    if len(vals) < 2:
        vals = [vals[0], vals[0]]
    mn, mx = min(vals), max(vals)
    pad = 2
    inner_w = max(width - 2 * pad, 1)
    inner_h = max(height - 2 * pad, 1)
    points: list[str] = []
    for i, value in enumerate(vals):
        x = pad + (i / (len(vals) - 1)) * inner_w
        if mx == mn:
            y = pad + inner_h / 2
        else:
            y = pad + (1 - (value - mn) / (mx - mn)) * inner_h
        points.append(f"{x:.1f},{y:.1f}")
    polyline = " ".join(points)
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">'
        f'<polyline fill="none" stroke="#0969da" stroke-width="1.5" points="{polyline}"/>'
        "</svg>"
    )
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


def build_sparkline_series(
    history: list[dict[str, object]],
    current: dict[str, object],
    field: str,
) -> list[int]:
    series: list[int] = []
    for snap in history:
        raw = snap.get(field)
        if isinstance(raw, (int, float)):
            series.append(int(raw))
    raw_current = current.get(field)
    if isinstance(raw_current, (int, float)):
        series.append(int(raw_current))
    return series[-SPARKLINE_WEEKS:]


@dataclass(frozen=True)
class SummaryTrendContext:
    prior_kpi: dict[str, object] | None
    history: list[dict[str, object]]
    current_kpi: dict[str, object]
    noise_rule_summary: str | None = None


def format_activity_line_with_wow(
    summary_rows: list[tuple[str, int, int, str]],
    org_noise_ratio: str | None,
    *,
    trend: SummaryTrendContext | None,
) -> str:
    total_commits = sum(c for _, c, _, _ in summary_rows)
    total_tags = sum(t for _, _, t, _ in summary_rows)
    active = len(summary_rows)
    prior = trend.prior_kpi if trend else None

    active_suffix = format_wow_suffix(
        wow_delta_pct(active, prior.get("active-repos") if prior else None)  # type: ignore[arg-type]
    )
    commits_suffix = format_wow_suffix(
        wow_delta_pct(total_commits, prior.get("kept-commits") if prior else None)  # type: ignore[arg-type]
    )
    tags_suffix = format_wow_suffix(
        wow_delta_pct(total_tags, prior.get("tags") if prior else None)  # type: ignore[arg-type]
    )

    activity_line = (
        f"Repos with activity: **{active}**{active_suffix} · "
        f"Commits: **{total_commits}**{commits_suffix} · Tags: **{total_tags}**{tags_suffix}"
    )
    if org_noise_ratio is not None:
        noise_val = parse_noise_pct(org_noise_ratio)
        prior_noise = prior.get("noise-pct") if prior else None
        noise_suffix = ""
        if isinstance(prior_noise, (int, float)) and noise_val is not None:
            noise_suffix = format_wow_suffix(wow_delta_pct(noise_val, prior_noise))
        activity_line += f" · Noise: **{org_noise_ratio}**{noise_suffix}"
        if trend and trend.noise_rule_summary:
            activity_line += f" — _{trend.noise_rule_summary}_"
    return activity_line


def markdown_inline_to_html(text: str) -> str:
    parts: list[str] = []
    rest = text
    while rest:
        bold = re.search(r"\*\*([^*]+)\*\*", rest)
        italic = re.search(r"_([^_]+)_", rest)
        candidates = [(m, "bold") for m in [bold] if m] + [(m, "italic") for m in [italic] if m]
        if not candidates:
            parts.append(html.escape(rest))
            break
        match, kind = min(candidates, key=lambda item: item[0].start())
        parts.append(html.escape(rest[: match.start()]))
        if kind == "bold":
            parts.append(f"<strong>{html.escape(match.group(1))}</strong>")
        else:
            parts.append(f"<em>{html.escape(match.group(1))}</em>")
        rest = rest[match.end() :]
    return "".join(parts)


def format_sparkline_block_md(trend: SummaryTrendContext | None) -> list[str]:
    if trend is None or not trend.history:
        return []
    commit_series = build_sparkline_series(trend.history, trend.current_kpi, "kept-commits")
    repo_series = build_sparkline_series(trend.history, trend.current_kpi, "active-repos")
    if not any(commit_series) and not any(repo_series):
        return []
    return [
        f"Trend ({min(SPARKLINE_WEEKS, len(commit_series))}w): "
        f"commits `{sparkline_ascii(commit_series)}` · "
        f"repos `{sparkline_ascii(repo_series)}`",
    ]


def format_wow_suffix_html(delta_pct: float | None) -> str:
    text = format_wow_suffix(delta_pct)
    if not text:
        return ""
    if delta_pct is None:
        return html.escape(text)
    color = HTML_COLOR_FLAT
    if delta_pct > 0:
        color = HTML_COLOR_UP
    elif delta_pct < 0:
        color = HTML_COLOR_DOWN
    return f'<span style="color:{color};">{html.escape(text)}</span>'


def format_noise_value_html(noise_str: str) -> str:
    pct = parse_noise_pct(noise_str)
    color = "#1f2328"
    if pct is not None:
        if pct >= 60:
            color = HTML_COLOR_DOWN
        elif pct >= 30:
            color = "#bf8700"
    return f'<strong style="color:{color};">{html.escape(noise_str)}</strong>'


def format_activity_line_html(
    summary_rows: list[tuple[str, int, int, str]],
    org_noise_ratio: str | None,
    *,
    trend: SummaryTrendContext | None,
) -> str:
    total_commits = sum(c for _, c, _, _ in summary_rows)
    total_tags = sum(t for _, _, t, _ in summary_rows)
    active = len(summary_rows)
    prior = trend.prior_kpi if trend else None

    active_suffix = format_wow_suffix_html(
        wow_delta_pct(active, prior.get("active-repos") if prior else None)  # type: ignore[arg-type]
    )
    commits_suffix = format_wow_suffix_html(
        wow_delta_pct(total_commits, prior.get("kept-commits") if prior else None)  # type: ignore[arg-type]
    )
    tags_suffix = format_wow_suffix_html(
        wow_delta_pct(total_tags, prior.get("tags") if prior else None)  # type: ignore[arg-type]
    )

    parts = [
        f"Repos with activity: <strong>{active}</strong>{active_suffix}",
        f"Commits: <strong>{total_commits}</strong>{commits_suffix}",
        f"Tags: <strong>{total_tags}</strong>{tags_suffix}",
    ]
    if org_noise_ratio is not None:
        noise_val = parse_noise_pct(org_noise_ratio)
        prior_noise = prior.get("noise-pct") if prior else None
        noise_suffix = ""
        if isinstance(prior_noise, (int, float)) and noise_val is not None:
            noise_suffix = format_wow_suffix_html(wow_delta_pct(noise_val, prior_noise))
        parts.append(f"Noise: {format_noise_value_html(org_noise_ratio)}{noise_suffix}")
        if trend and trend.noise_rule_summary:
            parts.append(
                f'<em style="color:{HTML_COLOR_FLAT};">{html.escape(trend.noise_rule_summary)}</em>'
            )
    return " · ".join(parts)


def html_inline_code(text: str, *, variant: str = "default") -> str:
    if variant == "tag":
        style = (
            f"background:{HTML_COLOR_TAG_BG}; color:{HTML_COLOR_TAG_FG}; "
            "padding:1px 5px; border-radius:3px; font-size:0.85em;"
        )
    else:
        style = "background:#f6f8fa; padding:1px 4px; border-radius:3px; font-size:0.85em;"
    return f'<code style="{style}">{html.escape(text)}</code>'


def format_type_rollup_html_table(type_counts: Counter[str]) -> str:
    rows: list[tuple[str, int]] = []
    for key in COMMIT_TYPE_ORDER:
        count = type_counts.get(key, 0)
        if count:
            rows.append((key, count))
    if not rows:
        return "<p><em>no kept commits</em></p>"
    body: list[str] = [
        f"<table {HTML_TABLE_STYLE}>",
        (
            f"<thead><tr>"
            f'<th align="left" {HTML_TH_STYLE}>Type</th>'
            f'<th align="right" {HTML_TH_STYLE}>Count</th>'
            f"</tr></thead>"
        ),
        "<tbody>",
    ]
    for idx, (key, count) in enumerate(rows):
        zebra = ' style="background:#f6f8fa;"' if idx % 2 else ""
        body.append(
            f"<tr{zebra}>"
            f'<td {HTML_TD_STYLE}><strong>{html.escape(key)}</strong></td>'
            f'<td {HTML_TD_RIGHT_STYLE}>{count}</td>'
            f"</tr>"
        )
    body.extend(["</tbody>", "</table>"])
    return "\n".join(body)


def format_chart_date_label(until_label: str) -> str:
    parts = until_label.split("-")
    if len(parts) == 3:
        try:
            return f"{int(parts[1])}/{int(parts[2])}"
        except ValueError:
            pass
    return until_label


def build_trend_datasets(
    history: list[dict[str, object]],
    current_kpi: dict[str, object],
    *,
    current_until: str,
) -> tuple[list[str], list[int], list[int]]:
    dates: list[str] = []
    commits: list[int] = []
    repos: list[int] = []
    for snap in history:
        until = snap.get("_until")
        if not isinstance(until, str) or not until:
            continue
        dates.append(until)
        raw_c = snap.get("kept-commits")
        raw_r = snap.get("active-repos")
        commits.append(int(raw_c) if isinstance(raw_c, (int, float)) else 0)
        repos.append(int(raw_r) if isinstance(raw_r, (int, float)) else 0)
    dates.append(current_until)
    raw_c = current_kpi.get("kept-commits")
    raw_r = current_kpi.get("active-repos")
    commits.append(int(raw_c) if isinstance(raw_c, (int, float)) else 0)
    repos.append(int(raw_r) if isinstance(raw_r, (int, float)) else 0)
    if len(dates) >= 2 and dates[-1] == dates[-2]:
        dates.pop(-2)
        commits.pop(-2)
        repos.pop(-2)
    if len(dates) > SPARKLINE_WEEKS:
        dates = dates[-SPARKLINE_WEEKS:]
        commits = commits[-SPARKLINE_WEEKS:]
        repos = repos[-SPARKLINE_WEEKS:]
    return dates, commits, repos


def _trend_axis_ticks(values: list[int], *, tick_count: int = 4) -> list[int]:
    if not values:
        return [0]
    peak = max(values)
    if peak == 0:
        return [0]
    step = max(1, (peak + tick_count - 2) // max(tick_count - 1, 1))
    top = ((peak + step - 1) // step) * step
    ticks = list(range(0, top + 1, step))
    if ticks[-1] != top:
        ticks.append(top)
    return ticks


def _series_polyline_points(
    values: list[int],
    *,
    x0: float,
    y0: float,
    width: float,
    height: float,
    y_max: float,
) -> str:
    if len(values) < 2:
        return ""
    points: list[str] = []
    for i, value in enumerate(values):
        x = x0 + (i / (len(values) - 1)) * width
        if y_max <= 0:
            y = y0 + height
        else:
            y = y0 + height - (value / y_max) * height
        points.append(f"{x:.1f},{y:.1f}")
    return " ".join(points)


def render_trend_chart_svg(
    dates: list[str],
    commits: list[int],
    repos: list[int],
) -> str:
    if len(dates) < 2 or not (len(dates) == len(commits) == len(repos)):
        return ""
    chart_width = 520
    chart_height = 172
    margin_left = 38
    margin_right = 10
    margin_top = 20
    axis_gap = 10
    x_label_h = 16
    panel_height = (chart_height - margin_top - axis_gap - x_label_h) / 2
    plot_width = chart_width - margin_left - margin_right

    fragments: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{chart_width}" height="{chart_height}" '
        f'viewBox="0 0 {chart_width} {chart_height}" role="img" '
        f'aria-label="Weekly trend of kept commits and active repositories">',
        (
            f'<text x="{margin_left}" y="13" fill="{HTML_COLOR_FLAT}" '
            f'font-size="11" font-family="system-ui,sans-serif">'
            f'<tspan fill="{HTML_COLOR_CHART_COMMITS}">●</tspan> commits  '
            f'<tspan fill="{HTML_COLOR_CHART_REPOS}">●</tspan> active repos</text>'
        ),
    ]

    panels = (
        ("Commits", commits, HTML_COLOR_CHART_COMMITS, margin_top),
        ("Active repos", repos, HTML_COLOR_CHART_REPOS, margin_top + panel_height + axis_gap),
    )
    for panel_idx, (label, values, stroke, panel_y) in enumerate(panels):
        tick_max = max(_trend_axis_ticks(values))
        fragments.append(
            f'<rect x="{margin_left}" y="{panel_y}" width="{plot_width}" height="{panel_height}" '
            f'fill="#fafbfc" stroke="#eaeef2" stroke-width="1"/>'
        )
        fragments.append(
            f'<text x="{margin_left + 6}" y="{panel_y + 12}" fill="#1f2328" font-size="10" '
            f'font-weight="600" font-family="system-ui,sans-serif">{html.escape(label)}</text>'
        )
        for tick in _trend_axis_ticks(values):
            if tick_max == 0:
                ty = panel_y + panel_height
            else:
                ty = panel_y + panel_height - (tick / tick_max) * panel_height
            fragments.append(
                f'<line x1="{margin_left}" y1="{ty:.1f}" x2="{margin_left + plot_width}" '
                f'y2="{ty:.1f}" stroke="#eaeef2" stroke-width="1"/>'
            )
            fragments.append(
                f'<text x="{margin_left - 5}" y="{ty + 3:.1f}" text-anchor="end" '
                f'fill="{HTML_COLOR_FLAT}" font-size="9" font-family="system-ui,sans-serif">'
                f"{tick}</text>"
            )
        polyline = _series_polyline_points(
            values,
            x0=margin_left,
            y0=panel_y,
            width=plot_width,
            height=panel_height,
            y_max=tick_max,
        )
        if polyline:
            fragments.append(
                f'<polyline fill="none" stroke="{stroke}" stroke-width="2" points="{polyline}"/>'
            )
        for i, value in enumerate(values):
            x = margin_left + (i / (len(values) - 1)) * plot_width
            if tick_max == 0:
                y = panel_y + panel_height
            else:
                y = panel_y + panel_height - (value / tick_max) * panel_height
            fragments.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.5" fill="{stroke}"/>')
        if panel_idx == 1:
            for i, date_label in enumerate(dates):
                x = margin_left + (i / (len(dates) - 1)) * plot_width
                fragments.append(
                    f'<text x="{x:.1f}" y="{panel_y + panel_height + 13}" text-anchor="middle" '
                    f'fill="{HTML_COLOR_FLAT}" font-size="9" font-family="system-ui,sans-serif">'
                    f"{html.escape(format_chart_date_label(date_label))}</text>"
                )

    fragments.append("</svg>")
    return "\n".join(fragments)


def _truncate_chart_label(text: str, max_len: int = 28) -> str:
    if len(text) <= max_len:
        return text
    if max_len <= 1:
        return text[:max_len]
    return text[: max_len - 1].rstrip() + "…"


def render_kpi_card_strip_html(
    summary_rows: list[tuple[str, int, int, str]],
    org_noise_ratio: str | None,
    *,
    trend: SummaryTrendContext | None,
) -> str:
    total_commits = sum(c for _, c, _, _ in summary_rows)
    total_tags = sum(t for _, _, t, _ in summary_rows)
    active = len(summary_rows)
    prior = trend.prior_kpi if trend else None

    cards: list[tuple[str, str, str]] = [
        (
            "Active repos",
            str(active),
            format_wow_suffix_html(
                wow_delta_pct(active, prior.get("active-repos") if prior else None)  # type: ignore[arg-type]
            ),
        ),
        (
            "Kept commits",
            str(total_commits),
            format_wow_suffix_html(
                wow_delta_pct(total_commits, prior.get("kept-commits") if prior else None)  # type: ignore[arg-type]
            ),
        ),
        (
            "Tags",
            str(total_tags),
            format_wow_suffix_html(
                wow_delta_pct(total_tags, prior.get("tags") if prior else None)  # type: ignore[arg-type]
            ),
        ),
    ]
    if org_noise_ratio is not None:
        noise_val = parse_noise_pct(org_noise_ratio)
        prior_noise = prior.get("noise-pct") if prior else None
        noise_suffix = ""
        if isinstance(prior_noise, (int, float)) and noise_val is not None:
            noise_suffix = format_wow_suffix_html(wow_delta_pct(noise_val, prior_noise))
        cards.append(
            (
                "Noise %",
                format_noise_value_html(org_noise_ratio),
                noise_suffix,
            ),
        )
    else:
        cards.append(("Noise %", "—", ""))

    cell_style = (
        "background:#f6f8fa; border:1px solid #d0d7de; border-radius:6px; "
        "padding:10px 12px; vertical-align:top; width:25%;"
    )
    label_style = (
        f"display:block; font-size:0.75rem; color:{HTML_COLOR_FLAT}; "
        "text-transform:uppercase; letter-spacing:0.02em; margin-bottom:4px;"
    )
    value_style = "display:block; font-size:1.35rem; font-weight:600; line-height:1.2;"
    delta_style = f"display:block; font-size:0.8rem; margin-top:4px; color:{HTML_COLOR_FLAT};"

    cells: list[str] = []
    for label, value, delta in cards:
        delta_block = delta if delta else f'<span style="color:{HTML_COLOR_FLAT};">&nbsp;</span>'
        cells.append(
            f'<td style="{cell_style}">'
            f'<span style="{label_style}">{html.escape(label)}</span>'
            f'<span style="{value_style}">{value}</span>'
            f'<span style="{delta_style}">{delta_block}</span>'
            f"</td>"
        )
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" '
        'style="width:100%; max-width:640px; border-collapse:separate; '
        'border-spacing:8px 0; margin:0 0 12px 0;">'
        f"<tr>{''.join(cells)}</tr></table>"
    )


def render_top_repos_bar_chart_svg(
    summary_rows: list[tuple[str, int, int, str]],
    *,
    width: int = 520,
    max_bars: int = TOP_REPOS_SUMMARY_N,
) -> str:
    rows = summary_rows[:max_bars]
    if not rows:
        return ""
    max_commits = max((c for _, c, _, _ in rows), default=0)
    if max_commits <= 0:
        return ""

    label_col = 148
    count_col = 36
    bar_area = width - label_col - count_col - 16
    row_h = 22
    pad_top = 8
    height = pad_top + len(rows) * row_h + 4

    fragments: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Top repositories by kept commits">',
    ]
    for idx, (repo_name, commit_count, _tags, _noise) in enumerate(rows):
        y = pad_top + idx * row_h
        label = html.escape(_truncate_chart_label(repo_name, 22))
        bar_w = (commit_count / max_commits) * bar_area if max_commits else 0
        fragments.append(
            f'<text x="0" y="{y + 14}" fill="#1f2328" font-size="11" '
            f'font-family="system-ui,sans-serif">{label}</text>'
        )
        fragments.append(
            f'<rect x="{label_col}" y="{y + 4}" width="{bar_area:.1f}" height="14" '
            f'fill="#eaeef2" rx="2"/>'
        )
        if bar_w > 0:
            fragments.append(
                f'<rect x="{label_col}" y="{y + 4}" width="{bar_w:.1f}" height="14" '
                f'fill="{HTML_COLOR_CHART_COMMITS}" rx="2"/>'
            )
        fragments.append(
            f'<text x="{width - 4}" y="{y + 14}" text-anchor="end" fill="#656d76" '
            f'font-size="11" font-family="system-ui,sans-serif">{commit_count}</text>'
        )
    fragments.append("</svg>")
    return "\n".join(fragments)


def render_commit_type_chart_svg(type_counts: Counter[str]) -> str:
    segments: list[tuple[str, int, str]] = []
    for key in COMMIT_TYPE_ORDER:
        count = type_counts.get(key, 0)
        if count:
            segments.append((key, count, COMMIT_TYPE_CHART_COLORS.get(key, "#8b949e")))
    total = sum(c for _, c, _ in segments)
    if total <= 0:
        return ""

    width = 520
    bar_h = 22
    legend_h = 14 + 8 * ((len(segments) + 3) // 4)
    height = 36 + bar_h + legend_h
    margin = 8
    bar_w = width - 2 * margin

    fragments: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="Commit types breakdown">',
        f'<text x="{margin}" y="14" fill="#1f2328" font-size="10" font-weight="600" '
        f'font-family="system-ui,sans-serif">{total} kept commits</text>',
    ]
    x = margin
    y_bar = 22
    for key, count, color in segments:
        seg_w = (count / total) * bar_w
        if seg_w <= 0:
            continue
        fragments.append(
            f'<rect x="{x:.1f}" y="{y_bar}" width="{seg_w:.1f}" height="{bar_h}" fill="{color}"/>'
        )
        x += seg_w
    fragments.append(
        f'<rect x="{margin}" y="{y_bar}" width="{bar_w:.1f}" height="{bar_h}" '
        f'fill="none" stroke="#d0d7de" stroke-width="1" rx="3"/>'
    )

    legend_y = y_bar + bar_h + 16
    col_w = bar_w / 4
    for idx, (key, count, color) in enumerate(segments):
        col = idx % 4
        row = idx // 4
        lx = margin + col * col_w
        ly = legend_y + row * 16
        pct = 100.0 * count / total
        fragments.append(f'<rect x="{lx:.1f}" y="{ly - 8}" width="8" height="8" fill="{color}" rx="1"/>')
        fragments.append(
            f'<text x="{lx + 12:.1f}" y="{ly}" fill="#656d76" font-size="10" '
            f'font-family="system-ui,sans-serif">'
            f"{html.escape(key)} {count} ({pct:.0f}%)</text>"
        )
    fragments.append("</svg>")
    return "\n".join(fragments)


def format_fanout_themes_html_table(themes: list[FanoutTheme]) -> str:
    body: list[str] = [
        f"<table {HTML_TABLE_STYLE}>",
        (
            f"<thead><tr>"
            f'<th align="left" {HTML_TH_STYLE}>Theme</th>'
            f'<th align="right" {HTML_TH_STYLE}>Repos</th>'
            f'<th align="right" {HTML_TH_STYLE}>Authors</th>'
            f'<th align="right" {HTML_TH_STYLE}>Commits</th>'
            f"</tr></thead>"
        ),
        "<tbody>",
    ]
    for idx, theme in enumerate(themes):
        zebra = ' style="background:#f6f8fa;"' if idx % 2 else ""
        author_cell = str(theme.author_count)
        if theme.primary_author:
            author_cell = (
                f"{theme.author_count} "
                f'<span style="color:#656d76;">({html.escape(theme.primary_author)})</span>'
            )
        body.append(
            f"<tr{zebra}>"
            f'<td {HTML_TD_STYLE}><strong>{html.escape(theme.display_subject)}</strong></td>'
            f'<td {HTML_TD_RIGHT_STYLE}>{theme.repo_count}</td>'
            f'<td {HTML_TD_RIGHT_STYLE}>{author_cell}</td>'
            f'<td {HTML_TD_RIGHT_STYLE}>{theme.commit_count}</td>'
            f"</tr>"
        )
    body.extend(["</tbody>", "</table>"])
    return "\n".join(body)


def build_summary_text(
    since_label: str,
    until_label: str,
    tz_name: str,
    summary_rows: list[tuple[str, int, int, str]],
    *,
    org_noise_ratio: str | None = None,
) -> str:
    total_commits = sum(c for _, c, _, _ in summary_rows)
    total_tags = sum(t for _, _, t, _ in summary_rows)
    activity_line = (
        f"Repos with activity: **{len(summary_rows)}** · "
        f"Commits: **{total_commits}** · Tags: **{total_tags}**"
    )
    if org_noise_ratio is not None:
        activity_line += f" · Noise: **{org_noise_ratio}**"
    lines = [
        "## Summary",
        "",
        f"Window: **{since_label} .. {until_label}** ({tz_name})",
        activity_line,
        "",
        "| Repo | Commits | Tags | Noise |",
        "| --- | ---: | ---: | ---: |",
    ]
    for repo_name, commit_count, tag_count, noise in summary_rows:
        lines.append(f"| {repo_name} | {commit_count} | {tag_count} | {noise} |")
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
    *,
    per_repo_counts: dict[str, tuple[int, int, int, int]] | None = None,
    trend: SummaryTrendContext | None = None,
    type_histogram: Counter[str] | None = None,
) -> str:
    summary_rows = build_summary_rows(sections, per_repo_counts=per_repo_counts)
    org_noise = org_wide_noise_ratio(per_repo_counts) if per_repo_counts else None
    if type_histogram is None:
        type_histogram = build_commit_type_histogram(sections)
    parts = [
        f"# {digest_title(scope, since_label, until_label, tz_name)}",
        "",
        build_layered_summary_md(
            since_label,
            until_label,
            tz_name,
            summary_rows,
            sections,
            scope=scope,
            org_noise_ratio=org_noise,
            trend=trend,
            type_histogram=type_histogram,
        ).rstrip(),
        build_repo_sections_text(sections).rstrip(),
    ]
    return "\n".join(parts).rstrip() + "\n"


def _html_summary_table_rows(
    summary_rows: list[tuple[str, int, int, str]],
    *,
    include_noise: bool = True,
) -> list[str]:
    header = (
        f"<thead><tr>"
        f'<th align="left" {HTML_TH_STYLE}>Repo</th>'
        f'<th align="right" {HTML_TH_STYLE}>Commits</th>'
        f'<th align="right" {HTML_TH_STYLE}>Tags</th>'
    )
    if include_noise:
        header += f'<th align="right" {HTML_TH_STYLE}>Noise</th>'
    header += "</tr></thead>"
    rows = [f"<table {HTML_TABLE_STYLE}>", header, "<tbody>"]
    for idx, (repo_name, commit_count, tag_count, noise) in enumerate(summary_rows):
        zebra = ' style="background:#f6f8fa;"' if idx % 2 else ""
        row = (
            f"<tr{zebra}>"
            f"<td {HTML_TD_STYLE}>{html.escape(repo_name)}</td>"
            f"<td {HTML_TD_RIGHT_STYLE}>{commit_count}</td>"
            f"<td {HTML_TD_RIGHT_STYLE}>{tag_count}</td>"
        )
        if include_noise:
            row += f"<td {HTML_TD_RIGHT_STYLE}>{format_noise_value_html(noise)}</td>"
        row += "</tr>"
        rows.append(row)
    rows.extend(["</tbody>", "</table>"])
    return rows


def build_digest_html(
    scope: str,
    since_label: str,
    until_label: str,
    tz_name: str,
    sections: list[tuple[str, list[dict[str, str]], list[dict[str, str]]]],
    *,
    per_repo_counts: dict[str, tuple[int, int, int, int]] | None = None,
    trend: SummaryTrendContext | None = None,
    type_histogram: Counter[str] | None = None,
) -> str:
    summary_rows = build_summary_rows(sections, per_repo_counts=per_repo_counts)
    org_noise_raw = org_wide_noise_ratio(per_repo_counts) if per_repo_counts else None
    if type_histogram is None:
        type_histogram = build_commit_type_histogram(sections)
    themes = group_fanout_themes(sections)
    folded_keys = fanout_subject_keys(sections)
    notable = collect_notable_commits(sections, folded_keys=folded_keys)
    highlight_tags, extra_highlight_tags = collect_highlight_tags(sections)
    top_rows, extra_repos, extra_commits, extra_tags = split_summary_rows_top_n(summary_rows)

    title = html.escape(digest_title(scope, since_label, until_label, tz_name))
    window = html.escape(f"{since_label} .. {until_label} ({tz_name})")
    tldr = markdown_inline_to_html(
        build_tldr_sentence(
            scope,
            since_label,
            until_label,
            summary_rows,
            themes,
            org_noise_ratio=org_noise_raw,
        )
    )

    noise_rules_line = ""
    if trend and trend.noise_rule_summary:
        noise_rules_line = (
            f'<p style="margin:0 0 12px 0; font-size:0.9rem;">'
            f'<em style="color:{HTML_COLOR_FLAT};">Noise rules: '
            f"{html.escape(trend.noise_rule_summary)}</em></p>"
        )

    body_parts = [
        "<!DOCTYPE html>",
        "<html>",
        "<head><meta charset=\"utf-8\"></head>",
        (
            "<body style=\"font-family: system-ui, -apple-system, Segoe UI, sans-serif; "
            "line-height: 1.45; color: #1f2328; max-width: 720px;\">"
        ),
        f"<h1 style=\"font-size: 1.25rem; margin-bottom: 0.75rem;\">{title}</h1>",
        (
            "<p style=\"background:#f6f8fa; border-left:4px solid #0969da; "
            "padding:10px 12px; border-radius:4px; margin:0 0 12px 0;\">"
            f"<strong>TL;DR:</strong> {tldr}</p>"
        ),
        f'<p style="margin:0 0 8px 0;"><strong>Window:</strong> {window}</p>',
        render_kpi_card_strip_html(summary_rows, org_noise_raw, trend=trend),
    ]
    if noise_rules_line:
        body_parts.append(noise_rules_line)

    if trend and trend.history:
        dates, commit_series, repo_series = build_trend_datasets(
            trend.history,
            trend.current_kpi,
            current_until=until_label,
        )
        chart = render_trend_chart_svg(dates, commit_series, repo_series)
        if chart:
            weeks = len(dates)
            body_parts.append(
                f'<h2 style="font-size: 1.05rem; margin:16px 0 8px 0;">Trend ({weeks}w)</h2>'
            )
            body_parts.append(chart)

    if type_histogram:
        body_parts.append("<h2 style=\"font-size: 1.05rem; margin:16px 0 8px 0;\">Commit types (kept)</h2>")
        type_chart = render_commit_type_chart_svg(type_histogram)
        if type_chart:
            body_parts.append(type_chart)
        body_parts.append(format_type_rollup_html_table(type_histogram))

    body_parts.append("<h2 style=\"font-size: 1.05rem;\">Cross-repo themes</h2>")

    if themes:
        body_parts.append(format_fanout_themes_html_table(themes))
    else:
        body_parts.append("<p><em>None this window.</em></p>")

    body_parts.append("<h2 style=\"font-size: 1.05rem;\">Top repositories</h2>")
    top_chart = render_top_repos_bar_chart_svg(top_rows)
    if top_chart:
        body_parts.append(top_chart)
    body_parts.append("\n".join(_html_summary_table_rows(top_rows, include_noise=False)))
    if extra_repos:
        body_parts.append(
            f"<p><em>+{extra_repos} more repos ({extra_commits} commits, {extra_tags} tags) — "
            "see attached Markdown or CSV for full detail.</em></p>"
        )

    if highlight_tags or notable:
        body_parts.append("<h2 style=\"font-size: 1.05rem;\">Highlights</h2>")
        if highlight_tags:
            body_parts.append(
                "<h3 style=\"font-size: 0.95rem; color:#0550ae;\">Tags / releases</h3><ul>"
            )
            for repo_name, tag in highlight_tags:
                body_parts.append(
                    f"<li>{html_inline_code(repo_name)} "
                    f"{html_inline_code(tag['name'], variant='tag')} → "
                    f"{html_inline_code(tag['sha'])} "
                    f"({html.escape(tag['date'])})</li>"
                )
            if extra_highlight_tags:
                body_parts.append(
                    f"<li><em>+{extra_highlight_tags} more tags in Markdown / CSV</em></li>"
                )
            body_parts.append("</ul>")
        if notable:
            body_parts.append("<h3 style=\"font-size: 0.95rem;\">Notable commits</h3><ul>")
            for repo_name, commit in notable:
                subject = html.escape(truncate_subject(commit["message"]))
                body_parts.append(
                    f"<li>{html_inline_code(repo_name)} "
                    f"{html_inline_code(commit['sha'])} {subject} — "
                    f"{html.escape(commit['author'])} ({html.escape(commit['date'])})</li>"
                )
            body_parts.append("</ul>")

    body_parts.extend(
        [
            "<hr>",
            "<p><em>Per-repo commit lists are in <code>digest.md</code> (Full detail) and "
            "<code>commit-digest.csv</code>; this HTML is the summary view only.</em></p>",
            "</body>",
            "</html>",
        ]
    )
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

    had_raw_activity = bool(sections)
    per_repo_counts: dict[str, tuple[int, int, int, int]] | None = None
    ignored_counts: Counter[str] = Counter()
    if noise_filter_enabled and sections:
        files_cache = CommitFilesCache()
        sections, ignored_counts, per_repo_counts = noise_filter.filter_digest_rows(
            org,
            sections,
            fetch_files=files_cache.get,
            repo_owner_name=repo_owner_name,
        )
        log_noise_filter_stats(ignored_counts, verbose=verbose)
        active = sum(1 for counts in per_repo_counts.values() if repo_kept_total(counts) > 0)

    history_dir_input = os.environ.get("INPUT_HISTORY_DIR", "")
    meta_path = Path(META_FILE)
    history_dir = resolve_history_dir(out_file, history_dir_input)
    window_label = f"{since_label}..{until_label}"
    if not had_raw_activity:
        message = f"no activity in window {since_label}..{until_label} {tz_name} ({scanned} repos scanned)"
        print(message)
        empty_kpi: dict[str, object] = {
            "scope": scope,
            "window": window_label,
            "active-repos": 0,
            "kept-commits": 0,
            "tags": 0,
            "noise-pct": None,
        }
        meta_payload = {
            "digest-file": "",
            "subject": "",
            "repo-count": scanned,
            "active-count": 0,
            "scope": scope,
            "has-activity": False,
            "window": window_label,
            "timezone": tz_name,
            "kpi": empty_kpi,
            "history-dir": str(history_dir),
        }
        write_meta(meta_path, meta_payload)
        persist_history_meta(history_dir, since_label, until_label, meta_payload)
        return 0

    summary_rows = build_summary_rows(sections, per_repo_counts=per_repo_counts)
    org_noise = org_wide_noise_ratio(per_repo_counts) if per_repo_counts else None
    current_kpi = build_kpi_snapshot(
        summary_rows,
        scope=scope,
        window=window_label,
        org_noise_ratio=org_noise,
    )
    kpi_history = load_kpi_history(history_dir, scope, exclude_window=window_label)
    prior_kpi = find_prior_week_kpi(kpi_history, since_label)
    noise_rule_summary = summarize_noise_rules(ignored_counts) or None
    trend = SummaryTrendContext(
        prior_kpi=prior_kpi,
        history=kpi_history,
        current_kpi=current_kpi,
        noise_rule_summary=noise_rule_summary,
    )

    digest_text = build_digest(
        scope,
        since_label,
        until_label,
        tz_name,
        sections,
        per_repo_counts=per_repo_counts,
        trend=trend,
    )
    html_file = html_path_for_digest(out_file)
    csv_file = csv_path_for_digest(out_file)
    digest_html = build_digest_html(
        scope,
        since_label,
        until_label,
        tz_name,
        sections,
        per_repo_counts=per_repo_counts,
        trend=trend,
    )
    digest_csv = build_digest_csv(since_label, until_label, tz_name, sections)
    Path(out_file).write_text(digest_text, encoding="utf-8")
    html_file.write_text(digest_html, encoding="utf-8")
    csv_file.write_text(digest_csv, encoding="utf-8")
    subject = f"[{scope.split(':', 1)[-1]}] weekly digest {since_label}..{until_label}"
    meta_payload: dict[str, object] = {
        "digest-file": out_file,
        "digest-html-file": str(html_file),
        "digest-csv-file": str(csv_file),
        "subject": subject,
        "repo-count": scanned,
        "active-count": active,
        "scope": scope,
        "has-activity": True,
        "window": window_label,
        "timezone": tz_name,
        "kpi": current_kpi,
        "history-dir": str(history_dir),
    }
    write_meta(meta_path, meta_payload)
    persist_history_meta(history_dir, since_label, until_label, meta_payload)
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
