"""Classify org digest commits/tags as automation noise (first match wins)."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

ItemType = Literal["commit", "tag"]

LOCK = re.compile(r"(^|/)(package-lock\.json|npm-shrinkwrap\.json|pnpm-lock\.yaml|yarn\.lock)$")
NOISE_FILE = re.compile(r"(^|/)(package-lock\.json|package\.json|\.npmrc)$|^\.github/")
FLOAT_TAG = re.compile(r"^(.*/)?v\d+$")

FilesFn = Callable[[str, str, str], list[str] | None]


def lock_only(files: list[str]) -> bool:
    return bool(files) and all(LOCK.search(f) for f in files)


def noise_only(files: list[str]) -> bool:
    return bool(files) and all(NOISE_FILE.search(f) for f in files)


def wrangler_only(files: list[str]) -> bool:
    return bool(files) and all(
        f.endswith(("wrangler.toml", "wrangler.jsonc", "wrangler.json")) for f in files
    )


def package_json_only(files: list[str]) -> bool:
    return files == ["package.json"]


@dataclass(frozen=True)
class WindowItem:
    repo: str
    item_type: ItemType
    sha: str
    name: str
    author: str


RuleCheck = tuple[str, str, str | None, object | None]
# (reason, name_regex, optional author_regex, file_check)
# file_check: None | callable(files) | "DUP"

RULES: list[RuleCheck] = [
    (
        "R1 ci_action_pin_bump",
        r"^chore\(ci\): bump worker-actions (bundle|pins?|reusable workflows) to @?actions/v"
        r"|^chore: (bump )?actions bundle manifest (to )?actions/v"
        r"|^chore: align template pins with actions/v"
        r"|升级action版本"
        r"|^升级actions/checkout",
        None,
        None,
    ),
    (
        "R2 cf_deps_autobump",
        r"^chore: bump Cloudflare npm deps to latest|^chore: bump compatibility_date 至 ",
        None,
        None,
    ),
    ("R3 sdk_dep_bump", r"^chore\(deps\): bump framework_sdk_\w+ to \d", None, None),
    (
        "R4 lockfile_only",
        r"CI action 自动刷新 package-lock|^chore: sync package-lock|^chore: keep release package-lock|package-lock",
        None,
        lock_only,
    ),
    ("R4 lockfile_only", r".", None, lock_only),
    ("R5 dep_bot", r".", r"^(dependabot\[bot\]|renovate\[bot\])$", None),
    (
        "R5 dep_bot",
        r"^chore\(deps\): bump @cloudflare/workers-types",
        r"^dev-bot$",
        None,
    ),
    ("R5 dep_bot", r"^更新依赖版本", None, package_json_only),
    ("R6 ci_retrigger", r"^ci: retrigger", None, None),
    (
        "R7 tooling_ignore_config",
        r"^添加qodana配置|^(添加|更新)忽略配置|^update \.gitignore$|^chore: gitignore ",
        None,
        None,
    ),
    (
        "R8 trivial_app_config",
        r"^(update app config|更新应用配置。?)$",
        None,
        wrangler_only,
    ),
    (
        "R9 release_branch_align",
        r"^(chore|fix): align RELEASE_BRANCH with current dev line",
        None,
        None,
    ),
    (
        "R10a release_promote_empty",
        r"^release: (dev|destin)_\d+_\d+_\d+ → master",
        None,
        lambda fs: not fs,
    ),
    (
        "R10b release_promote_noise_only",
        r"^release: (dev|destin)_\d+_\d+_\d+ → master",
        None,
        noise_only,
    ),
    (
        "R10c release_promote_duplicate",
        r"^release: (dev|destin)_\d+_\d+_\d+ → master",
        None,
        "DUP",
    ),
]


def release_promote_duplicate(
    item: WindowItem,
    files: list[str],
    *,
    same_repo_commits: list[WindowItem],
    fetch_files: FilesFn,
    owner: str,
    repo_name: str,
) -> bool:
    rel = [f for f in files if not NOISE_FILE.search(f)]
    if not rel:
        return False
    others: set[str] = set()
    for other in same_repo_commits:
        if other.sha == item.sha:
            continue
        if other.name.startswith("release:"):
            continue
        other_files = fetch_files(owner, repo_name, other.sha)
        if other_files is None:
            return False
        others.update(other_files)
    return all(f in others for f in rel)


def classify_item(
    item: WindowItem,
    *,
    owner: str,
    repo_name: str,
    fetch_files: FilesFn,
    same_repo_commits: list[WindowItem],
) -> str | None:
    if item.item_type == "tag":
        if FLOAT_TAG.match(item.name):
            return "T1 floating_major_tag"
        return None

    for reason, name_re, author_re, file_check in RULES:
        if not re.search(name_re, item.name):
            continue
        if author_re and not re.search(author_re, item.author):
            continue
        if file_check is not None:
            files = fetch_files(owner, repo_name, item.sha)
            if files is None:
                continue
            if file_check == "DUP":
                if not release_promote_duplicate(
                    item,
                    files,
                    same_repo_commits=same_repo_commits,
                    fetch_files=fetch_files,
                    owner=owner,
                    repo_name=repo_name,
                ):
                    continue
            elif callable(file_check):
                if not file_check(files):
                    continue
            else:
                continue
        return reason
    return None


def filter_digest_rows(
    org: str,
    sections: list[tuple[str, list[dict[str, str]], list[dict[str, str]]]],
    *,
    fetch_files: FilesFn,
    repo_owner_name: Callable[[str, str], tuple[str, str]],
) -> tuple[list[tuple[str, list[dict[str, str]], list[dict[str, str]]]], Counter[str]]:
    """Return filtered sections and ignore_reason counts."""
    window_items: list[tuple[str, str, str, WindowItem]] = []
    for short_name, commits, tags in sections:
        owner, name = repo_owner_name(org, short_name)
        for row in commits:
            window_items.append(
                (
                    owner,
                    name,
                    short_name,
                    WindowItem(
                        repo=short_name,
                        item_type="commit",
                        sha=row["sha"],
                        name=row["message"],
                        author=row["author"],
                    ),
                )
            )
        for row in tags:
            window_items.append(
                (
                    owner,
                    name,
                    short_name,
                    WindowItem(
                        repo=short_name,
                        item_type="tag",
                        sha=row["sha"],
                        name=row["name"],
                        author="",
                    ),
                )
            )

    by_repo_commits: dict[str, list[WindowItem]] = {}
    for _, _, short_name, item in window_items:
        if item.item_type == "commit":
            by_repo_commits.setdefault(short_name, []).append(item)

    ignored: Counter[str] = Counter()
    drop_keys: set[tuple[str, ItemType, str]] = set()

    for owner, name, short_name, item in window_items:
        reason = classify_item(
            item,
            owner=owner,
            repo_name=name,
            fetch_files=fetch_files,
            same_repo_commits=by_repo_commits.get(short_name, []),
        )
        if reason:
            ignored[reason] += 1
            drop_keys.add((short_name, item.item_type, item.sha if item.item_type == "commit" else item.name))

    filtered: list[tuple[str, list[dict[str, str]], list[dict[str, str]]]] = []
    for short_name, commits, tags in sections:
        keep_commits = [r for r in commits if (short_name, "commit", r["sha"]) not in drop_keys]
        keep_tags = [r for r in tags if (short_name, "tag", r["name"]) not in drop_keys]
        if keep_commits or keep_tags:
            filtered.append((short_name, keep_commits, keep_tags))

    return filtered, ignored
