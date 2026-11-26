"""Pure blocker readiness state machine and owner resolution."""

from __future__ import annotations

from .live import IssueFacts, PrFacts
from .verdict import FIXED_RESOLUTIONS


def resolve_owner(
    blocker: dict,
    issue_facts: IssueFacts | None,
    pr_facts: list[PrFacts],
) -> tuple[str | None, str]:
    """Resolve owner with precedence: JIRA assignee > non-bot fix PR author > local > culprit."""
    if issue_facts and issue_facts.readable and issue_facts.assignee:
        if issue_facts.assignee.lower() != "unassigned":
            return issue_facts.assignee, "jira"
    for pr in pr_facts:
        if pr.readable and pr.author and not pr.author.startswith("cubrid-tc-sync-bot"):
            return pr.author, "fix_pr"
    if blocker.get("owner"):
        return blocker["owner"], "local"
    if blocker.get("culprit_author"):
        return blocker["culprit_author"], "culprit"
    return None, "unassigned"


def unblock_action_for_repos(repos: set[str]) -> str:
    has_engine = "CUBRID/cubrid" in repos
    has_tc = bool(repos - {"CUBRID/cubrid"})
    if has_engine and has_tc:
        return "REBASE_ENGINE_AND_SYNC_TC"
    if has_engine:
        return "REBASE_ENGINE"
    if has_tc:
        return "RERUN_OR_SYNC_TC"
    return "RETIRE_RULE"


def evaluate_blocker(
    blocker: dict,
    *,
    target_branch: str = "develop",
    issue_facts: IssueFacts | None = None,
    pr_facts_map: dict[tuple[str, int], PrFacts] | None = None,
    discovered_prs: list[PrFacts] = (),
    fix_on_develop_commits: tuple[str, ...] = (),
    active_rules: list[dict] = (),
) -> dict:
    """Evaluate a blocker's unblock readiness, action, owner, and drift flags."""
    pr_facts_map = pr_facts_map or {}
    active_prs = [p for p in blocker.get("prs") or () if not p.get("retired_at")]
    required_prs = [p for p in active_prs if p.get("required", 1)]
    optional_prs = [p for p in active_prs if not p.get("required", 1)]

    req_facts: list[tuple[dict, PrFacts | None]] = [
        (p, pr_facts_map.get((p["repository"], p["pr_number"]))) for p in required_prs
    ]
    opt_facts: list[tuple[dict, PrFacts | None]] = [
        (p, pr_facts_map.get((p["repository"], p["pr_number"]))) for p in optional_prs
    ]
    all_facts = [pf for _, pf in req_facts + opt_facts if pf is not None]
    owner, owner_source = resolve_owner(blocker, issue_facts, all_facts)

    flags: list[str] = []
    unblock_action: str | None = None
    readiness = "UNTRACKED"

    if required_prs:
        if any(pf is None or not pf.readable for _, pf in req_facts):
            readiness = "UNKNOWN"
            for p, pf in req_facts:
                if pf is None:
                    flags.append(f"PR {p['repository']}#{p['pr_number']} state unknown (offline)")
                elif not pf.readable:
                    flags.append(
                        f"PR {p['repository']}#{p['pr_number']} unreadable: {pf.error}"
                    )
        else:
            merged_target = []
            merged_other = []
            open_or_draft = []
            closed_unmerged = []
            for p, pf in req_facts:
                assert pf is not None
                want_branch = p.get("target_branch") or target_branch
                if pf.merged:
                    if pf.base_ref == want_branch:
                        merged_target.append((p, pf))
                    else:
                        merged_other.append((p, pf))
                elif pf.state == "OPEN":
                    open_or_draft.append((p, pf))
                else:
                    closed_unmerged.append((p, pf))

            if len(merged_target) == len(req_facts):
                readiness = "READY"
                unblock_action = unblock_action_for_repos(
                    {p["repository"] for p in required_prs}
                )
            elif merged_target and open_or_draft and not merged_other:
                readiness = "PARTIAL"
            elif merged_other and not open_or_draft:
                readiness = "BRANCH_MISMATCH"
                for p, pf in merged_other:
                    want = p.get("target_branch") or target_branch
                    flags.append(
                        f"{p['repository']}#{p['pr_number']} merged into {pf.base_ref}, not {want}"
                    )
            elif open_or_draft:
                readiness = "IN_PROGRESS"
            else:
                readiness = "NEEDS_FIX_PR"
                flags.append("all required fix PRs closed without merge")
    else:
        if (
            issue_facts
            and issue_facts.readable
            and issue_facts.resolution in FIXED_RESOLUTIONS
            and fix_on_develop_commits
        ):
            readiness = "READY"
            unblock_action = "REBASE_ENGINE"
        elif discovered_prs:
            merged_disc = [
                d for d in discovered_prs if d.merged and d.base_ref == target_branch
            ]
            open_disc = [d for d in discovered_prs if d.state == "OPEN"]
            if open_disc:
                readiness = "IN_PROGRESS"
            elif merged_disc:
                readiness = "NEEDS_FIX_PR"
            else:
                readiness = "NEEDS_FIX_PR" if blocker.get("fix_issue") else "UNTRACKED"
            for d in discovered_prs:
                flags.append(
                    f"discovered unpinned PR {d.repository}#{d.number}"
                    f" ({d.display_state} -> {d.base_ref or '?'})"
                )
        elif blocker.get("fix_issue"):
            readiness = "NEEDS_FIX_PR"
        else:
            readiness = "UNTRACKED"

    # Drift checks between JIRA issue and pinned PRs.
    fix_issue = blocker.get("fix_issue")
    if fix_issue and issue_facts:
        if not issue_facts.readable:
            flags.append(f"{fix_issue} unreadable: {issue_facts.error}")
        elif issue_facts.resolution is None:
            if readiness == "READY":
                flags.append(
                    f"PR merged on {target_branch}, but {fix_issue}"
                    f" is still {issue_facts.status}/Unresolved"
                )
        elif issue_facts.resolution in FIXED_RESOLUTIONS:
            if required_prs and readiness not in ("READY", "UNKNOWN"):
                flags.append(
                    f"{fix_issue} is resolved {issue_facts.resolution},"
                    f" but fix PR(s) not merged on {target_branch}"
                )
        else:
            flags.append(
                f"{fix_issue} is resolved as {issue_facts.resolution}"
                " (not Fixed/Done; check returns unknown)"
            )

    if blocker.get("retired_at") and active_rules:
        flags.append("blocker is retired, but active rule(s) still reference it")

    pr_entries = []
    for p, pf in req_facts + opt_facts:
        entry = {
            "repository": p["repository"],
            "pr_number": p["pr_number"],
            "target_branch": p.get("target_branch") or target_branch,
            "required": bool(p.get("required", 1)),
            "state": pf.display_state if pf else "UNKNOWN",
            "merged": pf.merged if pf else None,
            "merged_at": pf.merged_at if pf else None,
            "merge_commit_sha": pf.merge_commit_sha if pf else None,
            "base_ref": pf.base_ref if pf else None,
            "head_ref": pf.head_ref if pf else None,
            "author": pf.author if pf else None,
            "url": pf.url if pf else None,
        }
        pr_entries.append(entry)

    wait_keys: list[str] = []
    for k in (fix_issue, blocker["id"]):
        if k and k not in wait_keys:
            wait_keys.append(k)

    return {
        "id": blocker["id"],
        "wait_keys": wait_keys,
        "title": blocker.get("title") or "",
        "readiness": readiness,
        "unblock_action": unblock_action,
        "owner": owner,
        "owner_source": owner_source,
        "culprit": {
            "repository": blocker.get("culprit_repo"),
            "commit": blocker.get("culprit_commit"),
            "pr": blocker.get("culprit_pr"),
            "issue": blocker.get("culprit_issue"),
            "author": blocker.get("culprit_author"),
            "confidence": blocker.get("culprit_confidence"),
            "note": blocker.get("culprit_note") or "",
        },
        "fix_issue": fix_issue,
        "issue": (
            {
                "key": fix_issue,
                "readable": issue_facts.readable,
                "status": issue_facts.status,
                "resolution": issue_facts.resolution,
                "assignee": issue_facts.assignee,
                "summary": issue_facts.summary,
                "error": issue_facts.error,
            }
            if fix_issue and issue_facts
            else None
        ),
        "fix_prs": pr_entries,
        "discovered_prs": [
            {
                "repository": d.repository,
                "pr_number": d.number,
                "state": d.display_state,
                "base_ref": d.base_ref,
                "author": d.author,
                "url": d.url,
            }
            for d in discovered_prs
        ],
        "signatures": blocker.get("signatures") or [],
        "rules": [r["id"] for r in active_rules] or [r["id"] for r in blocker.get("rules") or ()],
        "retired_at": blocker.get("retired_at"),
        "flags": flags,
    }
