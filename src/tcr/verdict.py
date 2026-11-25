"""Decide whether a failure is a known issue.

`decide` is pure. `IssueFacts` and `FixFacts` come from the Jira and Git readers below, which
are the only parts that touch the network or a repository.
"""

import functools
import json
import subprocess
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import date

from . import stack

JIRA_URL = "https://jira.cubrid.org/rest/api/2/issue/{key}?fields=status,resolution"
FIX_REF = "origin/develop"
ENGINE_GITHUB = "CUBRID/cubrid"

VERDICTS = ("no_match", "expired", "known", "known_job", "fix_missing", "fix_present", "unknown")
FIXED_RESOLUTIONS = frozenset({"Fixed", "Done"})


@dataclass(frozen=True)
class IssueFacts:
    readable: bool
    status: str | None = None
    resolution: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class FixFacts:
    """Whether the tested Engine commit contains the fix. `present` is None when unknown."""

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
           fix: FixFacts | None) -> Hit | None:
    """Map one rule and its gathered facts to a verdict. None means the rule does not apply."""

    def hit(verdict, reason, commits=()):
        if verdict == "known" and rule["job_contains"] is not None:
            verdict = "known_job"
            reason = ("the job is known noisy and each failure's symptom still needs analysis;"
                      f" {reason}")
        return Hit(rule["id"], rule["issue"], verdict, reason, rule["not_covered"], tuple(commits))

    if scope == "miss":
        return None
    if rule["expires"] is not None and date.fromisoformat(rule["expires"]) < today:
        return hit("expired", f"the rule expired on {rule['expires']}")
    if scope == "no_stack":
        return hit("unknown",
                   "no stack or failure text was given, so the signature could not be compared")
    if rule["issue"] is None:
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
    try:
        with urllib.request.urlopen(JIRA_URL.format(key=key), timeout=timeout) as resp:
            fields = json.load(resp)["fields"]
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError) as e:
        return IssueFacts(readable=False, error=str(e))
    resolution = fields.get("resolution")
    return IssueFacts(
        readable=True,
        status=(fields.get("status") or {}).get("name"),
        resolution=resolution.get("name") if resolution else None,
    )


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
