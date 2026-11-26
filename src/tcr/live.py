"""Live readers for JIRA (via httpx), GitHub PRs (via gh GraphQL/REST), and cubrid-xref."""

from __future__ import annotations

import functools
import json
import netrc
import os
import re
import shutil
import subprocess
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from .store import check_commit_sha

JIRA_BASE = "https://jira.cubrid.org/rest/api/2/issue/{key}?fields=status,resolution,assignee,summary"
_ISSUE_IN_TEXT = re.compile(r"\b(CBRD-\d+)\b")
_PR_IN_SUBJECT = re.compile(r"\(#(\d+)\)\s*$")


@dataclass(frozen=True)
class IssueFacts:
    readable: bool
    status: str | None = None
    resolution: str | None = None
    error: str | None = None
    assignee: str | None = None
    summary: str | None = None


@dataclass(frozen=True)
class PrFacts:
    repository: str
    number: int
    readable: bool
    state: str | None = None
    is_draft: bool = False
    merged: bool = False
    merged_at: str | None = None
    merge_commit_sha: str | None = None
    base_ref: str | None = None
    head_ref: str | None = None
    author: str | None = None
    title: str | None = None
    url: str | None = None
    error: str | None = None

    @property
    def display_state(self) -> str:
        if not self.readable:
            return "UNREADABLE"
        if self.merged or self.state == "MERGED":
            return "MERGED"
        if self.state == "CLOSED":
            return "CLOSED/DRAFT" if self.is_draft else "CLOSED"
        return "DRAFT" if self.is_draft else "OPEN"


def _jira_auth() -> tuple[str, str] | None:
    user = os.environ.get("CUBRID_JIRA_USER")
    password = os.environ.get("CUBRID_JIRA_PASSWORD")
    if user and password:
        return (user, password)
    try:
        auth = netrc.netrc().authenticators("jira.cubrid.org")
    except (OSError, netrc.NetrcParseError):
        return None
    if auth and auth[0] and auth[2]:
        return (auth[0], auth[2])
    return None


def _parse_issue_json(payload: dict) -> IssueFacts:
    fields = payload["fields"]
    resolution = fields.get("resolution")
    assignee = fields.get("assignee") or {}
    return IssueFacts(
        readable=True,
        status=(fields.get("status") or {}).get("name"),
        resolution=resolution.get("name") if resolution else None,
        assignee=assignee.get("displayName") or assignee.get("name"),
        summary=fields.get("summary"),
    )


def read_issue_live(key: str, timeout: float = 30.0, client=None) -> IssueFacts:
    import httpx

    url = JIRA_BASE.format(key=key)
    auth = _jira_auth()
    try:
        if client is not None:
            resp = client.get(url, timeout=timeout)
        else:
            with httpx.Client(auth=auth, timeout=timeout, follow_redirects=True) as c:
                resp = c.get(url)
        # Never retry 401 with different credentials to avoid JIRA CAPTCHA lockout;
        # only fall back to anonymous if unauthenticated wasn't already tried.
        if resp.status_code in (401, 403) and auth is not None and client is None:
            with httpx.Client(timeout=timeout, follow_redirects=True) as anon:
                resp = anon.get(url)
        resp.raise_for_status()
        return _parse_issue_json(resp.json())
    except (httpx.HTTPError, KeyError, ValueError, TypeError) as e:
        return IssueFacts(readable=False, error=str(e))


def read_issues_batch(keys: Iterable[str], timeout: float = 30.0) -> dict[str, IssueFacts]:
    unique = sorted({k for k in keys if k})
    if not unique:
        return {}
    import httpx

    auth = _jira_auth()
    out: dict[str, IssueFacts] = {}
    with httpx.Client(auth=auth, timeout=timeout, follow_redirects=True) as client:
        def fetch(k: str) -> tuple[str, IssueFacts]:
            return k, read_issue_live(k, timeout=timeout, client=client)

        with ThreadPoolExecutor(max_workers=min(6, len(unique))) as pool:
            for k, facts in pool.map(fetch, unique):
                out[k] = facts
    return out


def _graphql_chunk(chunk: list[tuple[str, int]]) -> dict[tuple[str, int], PrFacts]:
    lines = ["query {"]
    alias_map: dict[str, tuple[str, int]] = {}
    for idx, (repo, number) in enumerate(chunk):
        owner, name = repo.split("/", 1)
        alias = f"p{idx}"
        alias_map[alias] = (repo, number)
        lines.append(
            f'  {alias}: repository(owner: "{owner}", name: "{name}") {{'
            f" pullRequest(number: {number}) {{"
            " number title url state isDraft merged mergedAt baseRefName headRefName"
            " mergeCommit { oid } author { login }"
            " } }"
        )
    lines.append("}")
    query = "\n".join(lines)
    proc = subprocess.run(
        ["gh", "api", "graphql", "-f", f"query={query}"],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0 and not proc.stdout.strip():
        err = (proc.stderr.strip() or f"gh exited {proc.returncode}").splitlines()[0]
        return {ref: PrFacts(ref[0], ref[1], readable=False, error=err) for ref in chunk}
    try:
        payload = json.loads(proc.stdout)
    except ValueError as e:
        return {ref: PrFacts(ref[0], ref[1], readable=False, error=str(e)) for ref in chunk}

    data = payload.get("data") or {}
    errors = payload.get("errors") or []
    error_msg = errors[0].get("message") if errors else None
    out: dict[tuple[str, int], PrFacts] = {}
    for alias, (repo, number) in alias_map.items():
        pr = (data.get(alias) or {}).get("pullRequest")
        if not pr:
            out[(repo, number)] = PrFacts(
                repo, number, readable=False, error=error_msg or "PR not found")
            continue
        merge_commit = pr.get("mergeCommit") or {}
        author = pr.get("author") or {}
        out[(repo, number)] = PrFacts(
            repository=repo,
            number=int(pr["number"]),
            readable=True,
            state=pr.get("state"),
            is_draft=bool(pr.get("isDraft")),
            merged=bool(pr.get("merged")),
            merged_at=pr.get("mergedAt"),
            merge_commit_sha=merge_commit.get("oid"),
            base_ref=pr.get("baseRefName"),
            head_ref=pr.get("headRefName"),
            author=author.get("login"),
            title=pr.get("title"),
            url=pr.get("url"),
        )
    return out


def read_prs_batch(pr_refs: Iterable[tuple[str, int]]) -> dict[tuple[str, int], PrFacts]:
    unique = sorted({(repo, int(num)) for repo, num in pr_refs})
    out: dict[tuple[str, int], PrFacts] = {}
    for i in range(0, len(unique), 40):
        out.update(_graphql_chunk(unique[i:i + 40]))
    return out


def read_pr_single(repo: str, number: int) -> PrFacts:
    return read_prs_batch([(repo, number)])[(repo, int(number))]


def hydrate_culprit_from_pr(repo: str, number: int) -> dict:
    pr = read_pr_single(repo, number)
    if not pr.readable:
        return {"culprit_repo": repo, "culprit_pr": number, "error": pr.error}
    issue = None
    if pr.title and (m := _ISSUE_IN_TEXT.search(pr.title)):
        issue = m.group(1)
    return {
        "culprit_repo": repo,
        "culprit_pr": pr.number,
        "culprit_commit": pr.merge_commit_sha if pr.merged else None,
        "culprit_issue": issue,
        "culprit_author": pr.author,
    }


def hydrate_culprit_from_commit(sha: str, engine_repo: str | None) -> dict:
    sha_norm = check_commit_sha(sha)
    out = {"culprit_commit": sha_norm}
    if not engine_repo or not Path(engine_repo, ".git").exists():
        return out
    proc = subprocess.run(
        ["git", "-C", engine_repo, "show", "-s", "--format=%H%n%an%n%s", sha_norm],
        capture_output=True, text=True, timeout=15,
    )
    if proc.returncode != 0:
        return out
    lines = proc.stdout.splitlines()
    if len(lines) >= 3:
        full_sha, author_name, subject = lines[0].strip(), lines[1].strip(), lines[2].strip()
        out["culprit_repo"] = "CUBRID/cubrid"
        out["culprit_commit"] = full_sha
        if m := _ISSUE_IN_TEXT.search(subject):
            out["culprit_issue"] = m.group(1)
        if m := _PR_IN_SUBJECT.search(subject):
            out["culprit_pr"] = int(m.group(1))
        if author_name:
            out["culprit_author"] = author_name
    return out


def discover_prs_for_issue(issue_key: str) -> tuple[list[PrFacts], str | None]:
    exe = shutil.which("cubrid-xref")
    if exe is None:
        return [], "cubrid-xref not found"
    try:
        proc = subprocess.run(
            [exe, "--refresh", "--json", issue_key],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return [], str(e)
    if proc.returncode != 0 or not proc.stdout.strip():
        return [], (proc.stderr.strip() or f"cubrid-xref exited {proc.returncode}").splitlines()[0]
    try:
        payload = json.loads(proc.stdout)
    except ValueError as e:
        return [], f"invalid cubrid-xref JSON: {e}"
    if not payload.get("ok"):
        return [], payload.get("error") or "cubrid-xref returned not ok"
    data = payload.get("data") or {}
    raw_prs: list[dict] = []
    for bundle in data.get("bundles") or []:
        if bundle.get("engine"):
            raw_prs.append(bundle["engine"])
        raw_prs.extend(bundle.get("tc_private") or [])
        raw_prs.extend(bundle.get("tc_public") or [])
    raw_prs.extend(data.get("standalone_tc_private") or [])
    raw_prs.extend(data.get("standalone_tc_public") or [])
    raw_prs.extend(data.get("other_prs") or [])

    seen: set[tuple[str, int]] = set()
    result: list[PrFacts] = []
    for pr in raw_prs:
        repo = pr.get("repo")
        num = pr.get("number")
        if not repo or not num or (repo, int(num)) in seen:
            continue
        seen.add((repo, int(num)))
        author = pr.get("author") or ""
        is_draft = bool(pr.get("is_draft"))
        merged_at = pr.get("merged_at")
        state = (pr.get("state") or "").upper()
        # Filter closed unmerged bot drafts (auto-closed when engine PR merges without TC changes).
        if is_draft and not merged_at and state == "CLOSED" and author.startswith(
                "cubrid-tc-sync-bot"):
            continue
        merged = bool(merged_at)
        result.append(PrFacts(
            repository=repo,
            number=int(num),
            readable=True,
            state="MERGED" if merged else state,
            is_draft=is_draft,
            merged=merged,
            merged_at=merged_at,
            merge_commit_sha=None,
            base_ref=pr.get("base_ref"),
            head_ref=pr.get("head_ref"),
            author=author or None,
            title=pr.get("title"),
            url=pr.get("url"),
        ))
    return result, None


def default_repo_checkout(repo: str, engine_repo: str | None = None) -> str | None:
    candidates: list[Path | str | None] = []
    if repo == "CUBRID/cubrid":
        candidates = [engine_repo, os.environ.get("TCR_ENGINE_REPO"),
                      Path.home() / "gh" / "cb" / "develop"]
    elif repo == "CUBRID/cubrid-testcases":
        candidates = [os.environ.get("TCR_TC_PUBLIC_REPO"),
                      Path.home() / "gh" / "cubrid-testcases" / "develop"]
    elif repo == "CUBRID/cubrid-testcases-private-ex":
        candidates = [os.environ.get("TCR_TC_PRIVATE_REPO"),
                      Path.home() / "gh" / "cubrid-testcases-private-ex" / "develop"]
    for cand in candidates:
        if cand and Path(cand, ".git").exists():
            return str(cand)
    return None


@functools.cache
def gh_compare_behind_by(repo: str, base_sha: str, head_sha: str) -> int:
    """Commits of `base_sha` that `head_sha` lacks, via GitHub's compare API."""
    proc = subprocess.run(
        ["gh", "api", f"repos/{repo}/compare/{base_sha}...{head_sha}"],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        raise OSError((proc.stderr.strip() or f"gh exited {proc.returncode}").splitlines()[0])
    try:
        return int(json.loads(proc.stdout)["behind_by"])
    except (KeyError, ValueError, TypeError) as e:
        raise OSError(f"unexpected compare reply: {e}") from e


def check_commit_in_target(
    repo: str, fix_sha: str, target_sha: str, local_repo: str | None = None,
) -> tuple[bool | None, str]:
    """Check whether `fix_sha` is an ancestor of `target_sha` in `repo`."""
    if not fix_sha or not target_sha:
        return None, "missing commit SHA"
    if fix_sha == target_sha or target_sha.startswith(fix_sha) or fix_sha.startswith(target_sha):
        return True, f"tested commit {target_sha[:12]} matches fix {fix_sha[:12]}"
    checkout = local_repo or default_repo_checkout(repo)
    if checkout:
        has_fix = subprocess.run(
            ["git", "-C", checkout, "cat-file", "-e", f"{fix_sha}^{{commit}}"],
            capture_output=True, text=True,
        ).returncode == 0
        has_target = subprocess.run(
            ["git", "-C", checkout, "cat-file", "-e", f"{target_sha}^{{commit}}"],
            capture_output=True, text=True,
        ).returncode == 0
        if has_fix and has_target:
            rc = subprocess.run(
                ["git", "-C", checkout, "merge-base", "--is-ancestor", fix_sha, target_sha],
                capture_output=True, text=True,
            ).returncode
            if rc == 0:
                return True, f"{repo} {fix_sha[:12]} is an ancestor of {target_sha[:12]}"
            if rc == 1:
                return False, f"{target_sha[:12]} lacks {repo} fix {fix_sha[:12]}"
    try:
        behind = gh_compare_behind_by(repo, fix_sha, target_sha)
    except OSError as e:
        return None, f"GitHub compare {repo} {fix_sha[:12]}...{target_sha[:12]} failed: {e}"
    if behind == 0:
        return True, f"GitHub compare: {target_sha[:12]} contains {repo} fix {fix_sha[:12]}"
    return False, f"GitHub compare: {target_sha[:12]} lacks {repo} fix {fix_sha[:12]}"
