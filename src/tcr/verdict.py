"""Decide whether a failure is a known issue.

`decide` is pure. `IssueFacts` and `FixFacts` come from the Jira, GitHub, and Git readers,
which are the only parts that touch the network or a repository.
"""

import functools
import json
import subprocess
from dataclasses import asdict, dataclass
from datetime import date

from . import live, stack
from .live import IssueFacts

JIRA_URL = "https://jira.cubrid.org/rest/api/2/issue/{key}?fields=status,resolution"
FIX_REF = "origin/develop"
ENGINE_GITHUB = "CUBRID/cubrid"

VERDICTS = ("no_match", "expired", "known", "known_job", "fix_missing", "fix_present", "unknown")
FIXED_RESOLUTIONS = frozenset({"Fixed", "Done"})


@dataclass(frozen=True)
class FixFacts:
    """Whether the tested commit(s) contain the fix. `present` is None when unknown."""

    present: bool | None
    basis: str
    fix_commits: tuple[str, ...] = ()


@dataclass(frozen=True)
class Hit:
    rule_id: str
    issue: str | None
    verdict: str
    reason: str
    not_covered: str
    fix_commits: tuple[str, ...] = ()
    blocker_id: str | None = None

    def as_dict(self):
        d = asdict(self)
        d["fix_commits"] = list(self.fix_commits)
        return d


def scope_matches(rule, *, job: str | None, stack_text: str | None, signature) -> str:
    """Return "match", "miss", or "no_stack" for one rule against one failure.

    `stack_text` is the failure text, a core backtrace or a failure message. A frames signature
    needs a parsed stack in it. A text-only signature needs any text.
    """
    if rule["job_contains"] is not None:
        return "match" if job is not None and rule["job_contains"] in job else "miss"
    if stack_text is None or (signature["frames"] and not stack.parse_stacks(stack_text)):
        return "no_stack"
    return "match" if stack.matches(stack_text, signature["frames"],
                                     signature["required_text"]) else "miss"


def decide(rule, scope: str, today: date, issue: IssueFacts | None,
           fix: FixFacts | None, *, blocker_id: str | None = None,
           blocker_fix: FixFacts | None = None,
           open_blocker_reason: str | None = None) -> Hit | None:
    """Map one rule and its gathered facts to a verdict. None means the rule does not apply."""
    effective_blocker = blocker_id or rule.get("blocker_id")

    def hit(verdict, reason, commits=()):
        if verdict == "known" and rule["job_contains"] is not None:
            verdict = "known_job"
            reason = ("the job is known noisy and each failure's symptom still needs analysis;"
                      f" {reason}")
        return Hit(rule["id"], rule["issue"], verdict, reason, rule["not_covered"],
                   tuple(commits), effective_blocker)

    if scope == "miss":
        return None
    if rule["expires"] is not None and date.fromisoformat(rule["expires"]) < today:
        return hit("expired", f"the rule expired on {rule['expires']}")
    if scope == "no_stack":
        return hit("unknown",
                   "no stack or failure text was given, so the signature could not be compared")
    if blocker_fix is not None:
        # Merged required blocker PRs decide fix_present / fix_missing / unknown directly,
        # unless JIRA explicitly resolved the issue with a non-Fixed resolution (e.g. Won't Fix).
        if issue and issue.readable and issue.resolution is not None and (
                issue.resolution not in FIXED_RESOLUTIONS):
            return hit("unknown",
                       f"{rule['issue']} is resolved as {issue.resolution}, not Fixed or Done")
        if blocker_fix.present is None:
            return hit("unknown", f"fix presence is unknown: {blocker_fix.basis}",
                       blocker_fix.fix_commits)
        if blocker_fix.present:
            return hit("fix_present", blocker_fix.basis, blocker_fix.fix_commits)
        return hit("fix_missing", blocker_fix.basis, blocker_fix.fix_commits)
    if rule["issue"] is None:
        if open_blocker_reason:
            return hit("known", open_blocker_reason)
        if effective_blocker:
            return hit("known", f"blocker {effective_blocker} has no merged fix PR yet")
        return hit("known", "the rule has no tracking issue and has not expired")
    if issue is None or not issue.readable:
        return hit("unknown", f"the issue state is unknown: {issue.error if issue else 'offline'}")
    if issue.resolution is None:
        return hit("known", f"{rule['issue']} is {issue.status} and unresolved")
    if issue.resolution not in FIXED_RESOLUTIONS:
        return hit("unknown",
                   f"{rule['issue']} is resolved as {issue.resolution}, not Fixed or Done")
    if fix is None or fix.present is None:
        return hit("unknown", f"fix presence is unknown: {fix.basis if fix else 'not checked'}")
    if fix.present:
        return hit("fix_present", fix.basis, fix.fix_commits)
    return hit("fix_missing", fix.basis, fix.fix_commits)


def read_issue(key: str, timeout: float = 30) -> IssueFacts:
    return live.read_issue_live(key, timeout=timeout)


def _git(repo: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)


@functools.cache
def _gh_behind_by(fix: str, engine: str) -> int:
    """Commits of `fix` that `engine` lacks, from GitHub's compare API. Read-only."""
    r = subprocess.run(["gh", "api", f"repos/{ENGINE_GITHUB}/compare/{fix}...{engine}"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise OSError((r.stderr.strip() or f"gh exited {r.returncode}").splitlines()[0])
    try:
        return int(json.loads(r.stdout)["behind_by"])
    except (KeyError, ValueError, TypeError) as e:
        raise OSError(f"unexpected compare reply: {e}") from e


def _read_fix_remote(key: str, engine_sha: str, repo: str) -> FixFacts:
    """The tested commit is not in the clone: ask GitHub about each fix commit."""
    found = _git(repo, "log", "--format=%H", f"--grep=\\[{key}\\]", FIX_REF)
    commits = tuple(found.stdout.split()) if found.returncode == 0 else ()
    if not commits:
        return FixFacts(None, f"the tested commit {engine_sha} is not in {repo}"
                              f" and no commit on {FIX_REF} names {key}")
    missing = []
    for c in commits:
        try:
            behind = _gh_behind_by(c, engine_sha)
        except OSError as e:
            return FixFacts(None, f"GitHub compare of {c[:12]}...{engine_sha} failed: {e}",
                            commits)
        if behind:
            missing.append(c)
    if missing:
        return FixFacts(False, f"GitHub compare: {engine_sha} lacks fix commit"
                               f" {missing[0][:12]} from {FIX_REF}", commits)
    return FixFacts(True, f"GitHub compare: every fix commit is an ancestor of {engine_sha}",
                    commits)


def read_fix(key: str, engine_sha: str, repo: str) -> FixFacts:
    if _git(repo, "cat-file", "-e", f"{engine_sha}^{{commit}}").returncode != 0:
        return _read_fix_remote(key, engine_sha, repo)
    pattern = f"\\[{key}\\]"
    found = _git(repo, "log", "--format=%H", f"--grep={pattern}", FIX_REF)
    if found.returncode != 0:
        return FixFacts(None, f"git log on {FIX_REF} failed in {repo}")
    commits = tuple(found.stdout.split())
    if commits and all(
        _git(repo, "merge-base", "--is-ancestor", c, engine_sha).returncode == 0 for c in commits
    ):
        return FixFacts(True, f"every fix commit on {FIX_REF} is an ancestor of {engine_sha}",
                        commits)
    own = _git(repo, "log", "--format=%H", "-1", f"--grep={pattern}", engine_sha)
    if own.returncode == 0 and own.stdout.strip():
        return FixFacts(True, f"the tested history has {own.stdout.strip()[:12]} naming {key}",
                        commits)
    if not commits:
        return FixFacts(None, f"no commit on {FIX_REF} names {key}")
    return FixFacts(False, f"{engine_sha} lacks fix commits from {FIX_REF}", commits)


def read_blocker_fix(
    blocker: dict,
    *,
    engine_sha: str,
    tc_sha: str | None,
    tc_repo: str | None,
    engine_repo: str | None,
    pr_facts_map: dict[tuple[str, int], live.PrFacts],
) -> tuple[FixFacts | None, str | None]:
    """Evaluate a blocker's pinned required PRs against the tested engine/TC commits.

    Returns (blocker_fix, open_blocker_reason).
    `blocker_fix` is non-None when all required PRs are merged on their target branch.
    """
    required_prs = [
        p for p in blocker.get("prs") or () if not p.get("retired_at") and p.get("required", 1)
    ]
    if not required_prs:
        return None, None
    facts = [pr_facts_map.get((p["repository"], p["pr_number"])) for p in required_prs]
    if any(pf is None or not pf.readable for pf in facts):
        return None, None
    unmerged = [
        (p, pf) for p, pf in zip(required_prs, facts, strict=True)
        if not (pf.merged and pf.base_ref == (p.get("target_branch") or "develop"))
    ]
    if unmerged:
        p0, pf0 = unmerged[0]
        return None, (
            f"blocker {blocker['id']} fix PR {p0['repository']}#{p0['pr_number']}"
            f" is {pf0.display_state} (target {p0.get('target_branch') or 'develop'})"
        )

    commits: list[str] = []
    missing_reasons: list[str] = []
    unknown_reasons: list[str] = []
    present_reasons: list[str] = []
    for p, pf in zip(required_prs, facts, strict=True):
        assert pf is not None
        sha = pf.merge_commit_sha
        if not sha:
            unknown_reasons.append(
                f"{p['repository']}#{p['pr_number']} merged without merge_commit_sha")
            continue
        commits.append(sha)
        repo = p["repository"]
        if repo == "CUBRID/cubrid":
            present, basis = live.check_commit_in_target(repo, sha, engine_sha, engine_repo)
        else:
            if tc_repo and repo != tc_repo:
                continue
            if not tc_sha:
                unknown_reasons.append(
                    f"TC fix {repo}#{p['pr_number']} merged on {pf.base_ref};"
                    " pass --tc-sha to verify fix presence"
                )
                continue
            present, basis = live.check_commit_in_target(repo, sha, tc_sha)
        if present is False:
            missing_reasons.append(
                f"{repo}#{p['pr_number']} ({sha[:12]}) merged on {pf.base_ref}: {basis}"
            )
        elif present is None:
            unknown_reasons.append(basis)
        else:
            present_reasons.append(f"{repo}#{p['pr_number']} ({sha[:12]}) is in tested commit")

    if missing_reasons:
        return FixFacts(False, "; ".join(missing_reasons), tuple(commits)), None
    if unknown_reasons:
        return FixFacts(None, "; ".join(unknown_reasons), tuple(commits)), None
    if not present_reasons:
        return FixFacts(None, f"blocker {blocker['id']} has no applicable repo check",
                        tuple(commits)), None
    return FixFacts(True, "; ".join(present_reasons), tuple(commits)), None
