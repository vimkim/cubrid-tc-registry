"""cubrid-tcr: per-testcase knowledge for CUBRID CI analysis."""

import argparse
import getpass
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from pathlib import Path

from . import __version__, attempt, chronicle, live, readiness, render, stack, verdict, view
from .store import (
    CHECK_SUITES,
    CONFIDENCES,
    RELATIONS,
    SUITE_REPOSITORY,
    WRITE_SUITES,
    RegistryError,
    Store,
    Testcase,
    check_repo,
    default_path,
    is_cci_suite,
    normalize_suite,
    parse_pr_ref,
)

CCI_HISTORY_REASON = "test_sql_by_cci has no Chronicle suite; history skipped"
_HEX_SHA = re.compile(r"^[0-9a-fA-F]{7,40}$")


def _shard_key(shard: str | int) -> str:
    return str(shard).lstrip("0") or "0"


EPILOG = """\
examples:
  cubrid-tcr show cbrd_27179
  cubrid-tcr check --case cbrd_27179 --suite shell --engine cd24a21 --stack-file core.txt
  cubrid-tcr check --job "shard sql_by_cci 01" --engine cd24a21
  cubrid-tcr check --ci-attempt ~/.local/share/cubrid-ci-data/.../attempts/1/test_shell --record
  cubrid-tcr blocker add CBRD-28395 --culprit engine#8537 --fix-issue CBRD-28395 --signature s
  cubrid-tcr blockers --affected-days 7
  cubrid-tcr unblock --pr 8753
  cubrid-tcr rules --audit
"""


def resolve(store: Store, case: str, suite: str | None, *, create: bool) -> Testcase:
    """Turn a full path or a Chronicle fragment into a registry testcase identity.

    Without `create`, an identity the registry has not stored yet comes back with id -1, which
    matches no record. Read commands use that so they never write.
    """
    case = case.strip()
    while case.startswith("./"):
        case = case[2:]
    suite = normalize_suite(suite)
    if "/" in case:
        suite = suite or case.split("/", 1)[0]
        if suite not in SUITE_REPOSITORY:
            raise RegistryError(f"pass --suite for {case}")
        repository, path, variant = None, case, ""
    else:
        try:
            found = chronicle.find(case, suite)
        except chronicle.ChronicleError as e:
            raise RegistryError(f"cannot resolve {case!r} without Chronicle ({e});"
                                " pass the full testcase path") from e
        if len(found) != 1:
            names = ", ".join(f"{c['suite']}:{c['path']}" for c in found[:10]) or "none"
            raise RegistryError(f"{case!r} matches {len(found)} testcases ({names});"
                                " pass the full testcase path")
        c = found[0]
        suite, repository, path, variant = c["suite"], c["repository"], c["path"], c["variant"]
    repository = repository or SUITE_REPOSITORY[suite]
    if create:
        with store.write():
            return store.testcase(suite, path, repository, variant or "", create=True)
    tc = store.testcase(suite, path, repository, variant or "")
    return tc or Testcase(-1, repository, suite, path, variant or "")


def emit(args, data, text):
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    else:
        print(text)


def cmd_show(args, store):
    tc = resolve(store, args.case, args.suite, create=False)
    fetched = chronicle.ChronicleError(CCI_HISTORY_REASON) if is_cci_suite(args.suite) else None
    v = view.build(store, tc, days=args.days, show_all=args.all, fetched=fetched,
                   live_lookup=args.live)
    emit(args, v, view.render(v))


def _hits(store, rules, *, job, stack_text, engine, engine_repo, offline, cache,
          tc_sha=None, tc_repo=None):
    """Evaluate rules for one failure. `cache` holds Jira, PR, and fix reads shared by a batch."""
    today = date.today()
    hits = []
    for rule in rules:
        sig = store.signature(rule["signature_id"]) if rule["signature_id"] else None
        scope = verdict.scope_matches(rule, job=job, stack_text=stack_text, signature=sig)
        blocker = store.blocker_for_rule(rule)
        issue = fix = blocker_fix = open_reason = None
        if scope == "match":
            if not offline and blocker and blocker.get("prs"):
                req_refs = tuple(sorted(
                    (p["repository"], p["pr_number"])
                    for p in blocker["prs"]
                    if not p.get("retired_at") and p.get("required", 1)
                ))
                if req_refs:
                    if ("prs", req_refs) not in cache:
                        cache[("prs", req_refs)] = live.read_prs_batch(req_refs)
                    blocker_fix, open_reason = verdict.read_blocker_fix(
                        blocker,
                        engine_sha=engine,
                        tc_sha=tc_sha,
                        tc_repo=tc_repo,
                        engine_repo=engine_repo,
                        pr_facts_map=cache[("prs", req_refs)],
                    )
            if not offline and rule["issue"]:
                key = ("issue", rule["issue"])
                if key not in cache:
                    cache[key] = verdict.read_issue(rule["issue"])
                issue = cache[key]
            if blocker_fix is None and issue and issue.resolution in verdict.FIXED_RESOLUTIONS:
                key = ("fix", rule["issue"], engine)
                if key not in cache:
                    cache[key] = verdict.read_fix(rule["issue"], engine, engine_repo)
                fix = cache[key]
        h = verdict.decide(
            rule, scope, today, issue, fix,
            blocker_id=blocker["id"] if blocker else None,
            blocker_fix=blocker_fix,
            open_blocker_reason=open_reason,
        )
        if h:
            hits.append(h)
    return hits


def cmd_check(args, store):
    if args.record and not args.ci_attempt:
        raise RegistryError("--record works only with --ci-attempt")
    if not args.ci_attempt and not args.engine:
        raise RegistryError("--engine is required with --case or --job")
    if not args.offline:
        _require_engine_repo(args.engine_repo)
    if args.ci_attempt:
        return check_attempt(args, store)
    stack_text = Path(args.stack_file).read_text(errors="replace") if args.stack_file else None
    tc = None
    cci = is_cci_suite(args.suite)
    job = "sql_by_cci" if (args.case and cci) else args.job
    if args.case:
        tc = resolve(store, args.case, args.suite, create=False)
        rules = store.rules(tc=tc) + (store.job_rules() if cci else [])
    else:
        rules = store.job_rules()
    hits = _hits(store, rules, job=job, stack_text=stack_text, engine=args.engine,
                 engine_repo=args.engine_repo, offline=args.offline, cache={},
                 tc_sha=args.tc_sha, tc_repo=tc.repository if tc else None)
    result = {
        "verdict": _overall(hits),
        "hits": [h.as_dict() for h in hits],
        "engine": args.engine,
        "case": tc.path if tc else args.case,
        "job": job,
        "stack_given": stack_text is not None,
    }
    if tc and not args.no_context:
        fetched = chronicle.ChronicleError(CCI_HISTORY_REASON) if cci else None
        result["context"] = view.build(store, tc, fetched=fetched)
    lines = [f"verdict: {result['verdict']}"]
    for h in hits:
        b_tag = f" [blocker {h.blocker_id}]" if h.blocker_id else ""
        lines.append(
            f"  rule {h.rule_id} ({h.issue or 'no issue'}{b_tag}): {h.verdict}. {h.reason}."
        )
        lines.append(f"    not covered: {h.not_covered}")
    if "context" in result:
        lines += ["", view.render(result["context"])]
    emit(args, result, "\n".join(lines))


def check_attempt(args, store):
    """Check every failure of one collected CI attempt suite."""
    bundle = attempt.load(args.ci_attempt)
    failures = bundle["failures"]
    all_keys = sorted({(f["suite"], f["path"]) for f in failures})
    # One testcase view per testcase, even when it failed in several shards. tcc is the slow part.
    tcs = {k: resolve(store, k[1], attempt.RULE_SUITE[k[0]], create=False) for k in all_keys}
    chronicle_keys = [k for k in all_keys if attempt.CHRONICLE_SUITE[k[0]]]

    def fetch(k):
        try:
            return chronicle.history_and_status(tcs[k].path, tcs[k].suite)
        except chronicle.ChronicleError as e:
            return e

    with ThreadPoolExecutor(max_workers=4) as pool:
        fetched = dict(zip(chronicle_keys, pool.map(fetch, chronicle_keys), strict=True))
    views = {k: view.build(store, tcs[k], fetched=fetched[k]) for k in chronicle_keys}
    cache, entries = {}, []
    for f in failures:
        k = (f["suite"], f["path"])
        job = f"shard {f['suite'].removeprefix('test_')} {f['shard']}"
        stack_text = f["message"] or None  # the failure text, whether or not a stack parsed
        rules = store.rules(tc=tcs[k]) + store.job_rules()
        hits = _hits(store, rules, job=job, stack_text=stack_text, engine=f["engine_sha"],
                     engine_repo=args.engine_repo, offline=args.offline, cache=cache,
                     tc_sha=f.get("testcase_sha"), tc_repo=tcs[k].repository)
        overall = _overall(hits)
        winner = next((h for h in hits if h.verdict == overall), None)
        entry = {
            "identity": {"suite": f["suite"], "path": f["path"], "run": f["run"],
                         "attempt": f["attempt"], "shard": f["shard"],
                         "engine_sha": f["engine_sha"], "job": job},
            "verdict": overall,
            "hits": [h.as_dict() for h in hits],
            "reason": winner.reason if winner else "no rule matched",
            "stack_found": bool(stack_text and stack.parse_stacks(stack_text)),
            "history": _history_counts(views.get(k)),
        }
        if args.record and k in fetched:
            entry["record"] = _record(store, tcs[k], fetched[k], f, stack_text)
        entries.append(entry)
    counts = {}
    for e in entries:
        counts[e["verdict"]] = counts.get(e["verdict"], 0) + 1
    result = {**bundle["header"], "failure_count": len(entries), "verdicts": counts,
              "failures": entries}
    lines = [f"{result['suite']} run {result['run']} attempt {result['attempt']}: "
             f"{len(entries)} failures, " + (_counts_text(counts) or "none")]
    if rerun := result.get("rerun"):
        lines.append(f"  {rerun['mode']} rerun of run {rerun['run_id']} attempt {rerun['attempt']}"
                     + ("; its failures are in that attempt" if not entries else ""))
    for e in entries:
        i, h = e["identity"], e["history"]
        if not h["available"]:
            past = h["reason"]
        elif h.get("status") == "NOT_INDEXED":
            past = f"NOT_INDEXED, freshness {h.get('freshness') or 'UNKNOWN'}"
        else:
            past = f"{h['failures']} failures, {view.passes_text(h['passes'])}"
            if h.get("freshness") in ("STALE", "UNKNOWN"):
                past += f", {h['freshness'].lower()}"
        lines.append(f"  {e['verdict']:11} shard {i['shard']} {i['path']} ({past})")
        lines.append(f"              {e['reason']}"
                     + (f" [record: {e['record']['result']}]" if "record" in e else ""))
    emit(args, result, "\n".join(lines))


def _counts_text(counts):
    return ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))


def _history_counts(v):
    if v is None:
        return {"available": False, "reason": CCI_HISTORY_REASON}
    h = v["history"]
    if not h["available"]:
        return {"available": False, "reason": h["reason"]}
    a = h["all_failures"]
    return {"available": True, "status": h.get("status"), "freshness": h.get("freshness"),
            "sync_hint": h.get("sync_hint"), "failures": a["count"], "passes": h["pass_count"],
            "first": a["first"], "last": a["last"], "by_branch": a["by_branch"],
            "by_signature": a["by_signature"]}


def _record(store, tc, fetched, f, stack_text):
    """Attach the one signature that matches. Anything else writes nothing."""
    if stack_text is None:
        return {"result": "no_stack"}
    cands = [s["id"] for s in store.signatures(tc)
             if stack.matches(stack_text, s["frames"], s["required_text"])]
    if len(cands) != 1:
        return {"result": "no_single_match", "candidates": cands}
    if isinstance(fetched, Exception):
        return {"result": "no_occurrence", "reason": str(fetched)}
    found = [chronicle.occurrence(o) for o in fetched[0].get("occurrences", [])]
    shard = _shard_key(f["shard"])
    found = [o for o in found if o["run_id"] == f["run"] and o["attempt"] in (None, f["attempt"])
             and _shard_key(o["shard"]) == shard and o["outcome"] in chronicle.FAILED]
    if len(found) != 1:
        return {"result": "no_occurrence", "reason": f"Chronicle has {len(found)} failed "
                f"occurrences for run {f['run']} shard {f['shard']}; sync the run first"}
    r = store.attach(tc, found[0], cands[0], stack.digest(stack_text))
    return {"result": r, "signature": cands[0]}


def _require_engine_repo(repo):
    if not repo:
        raise RegistryError("set TCR_ENGINE_REPO or pass --engine-repo to a CUBRID engine clone;"
                            " pass --offline to skip the Jira and Git reads")
    if not Path(repo, ".git").exists():
        raise RegistryError(f"engine repository {repo} is not a Git checkout")


def _overall(hits) -> str:
    """One verdict for the failure. A rule that says "analyze as new" wins over "known"."""
    if not hits:
        return "no_match"
    order = ["fix_present", "expired", "unknown", "fix_missing", "known", "known_job"]
    return min((h.verdict for h in hits), key=order.index)


def cmd_signature_add(args, store):
    tc = resolve(store, args.case, args.suite, create=True)
    frames = [f.strip() for f in (args.frames or "").split(",") if f.strip()]
    r = store.add_signature(tc, args.id, args.label, frames, args.text or [])
    emit(args, {"result": r, "signature": args.id}, f"signature {args.id}: {r}")


def cmd_signature_attach(args, store):
    tc = resolve(store, args.case, args.suite, create=True)
    text = Path(args.stack_file).read_text(errors="replace")
    occ = _find_occurrence(tc, args)
    sig_id = args.signature
    if sig_id is None:
        cands = [s["id"] for s in store.signatures(tc)
                 if stack.matches(text, s["frames"], s["required_text"])]
        if len(cands) != 1:
            stacks = stack.parse_stacks(text)
            data = {"result": "no_single_match", "candidates": cands,
                    "parsed_frames": stacks[0] if stacks else []}
            msg = (f"{len(cands)} signatures match ({', '.join(cands) or 'none'})."
                   " Pass --signature, or add a signature from these frames:\n  "
                   + " > ".join(data["parsed_frames"][:15]))
            emit(args, data, msg)
            return
        sig_id = cands[0]
    else:
        sig = store.signature(sig_id)
        if sig is None:
            raise RegistryError(f"no signature {sig_id}")
        if not stack.matches(text, sig["frames"], sig["required_text"]):
            raise RegistryError(f"the stack does not match signature {sig_id}")
    r = store.attach(tc, occ, sig_id, stack.digest(text))
    emit(args, {"result": r, "signature": sig_id, "occurrence": occ},
         f"run {occ['run_id']} shard {occ['shard']}: {sig_id} {r}")


def _find_occurrence(tc, args):
    try:
        hist, _ = chronicle.history_and_status(tc.path, tc.suite)
    except chronicle.ChronicleError as e:
        raise RegistryError(f"cannot read the occurrence from Chronicle: {e}") from e
    found = [chronicle.occurrence(o) for o in hist.get("occurrences", [])]
    shard = _shard_key(args.shard)
    found = [o for o in found if o["run_id"] == args.run and _shard_key(o["shard"]) == shard
             and o["outcome"] in chronicle.FAILED
             and (args.attempt is None or o["attempt"] in (None, args.attempt))]
    if len(found) != 1:
        raise RegistryError(
            f"Chronicle has {len(found)} failed occurrences for run {args.run} shard {args.shard}."
            f" Sync the run first: cubrid-tcc sync-data --run {args.run} --suite {tc.suite}"
            " --revisit-limit 0 --json")
    return found[0]


def cmd_link(args, store):
    tc = resolve(store, args.case, args.suite, create=True)
    r = store.link(tc, args.issue, args.relation, args.signature)
    emit(args, {"result": r}, f"{args.relation} {args.issue}: {r}")


def cmd_note(args, store):
    tc = resolve(store, args.case, args.suite, create=True)
    r = store.add_note(tc, args.body, args.ref or [])
    emit(args, {"result": r}, f"note: {r}")


def _resolve_culprit_args(args) -> dict:
    culprit_repo = check_repo(args.culprit_repo) if args.culprit_repo else None
    culprit_commit = args.culprit_commit
    culprit_pr = args.culprit_pr
    culprit_issue = args.culprit_issue
    culprit_author = args.culprit_author

    if args.culprit:
        raw = args.culprit.strip()
        if _HEX_SHA.match(raw):
            hydrated = ({"culprit_commit": raw} if args.offline
                        else live.hydrate_culprit_from_commit(raw, args.engine_repo))
        else:
            repo, pr_num = parse_pr_ref(raw)
            hydrated = ({"culprit_repo": repo, "culprit_pr": pr_num} if args.offline
                        else live.hydrate_culprit_from_pr(repo, pr_num))
        culprit_repo = culprit_repo or hydrated.get("culprit_repo")
        culprit_pr = culprit_pr if culprit_pr is not None else hydrated.get("culprit_pr")
        culprit_commit = culprit_commit or hydrated.get("culprit_commit")
        culprit_issue = culprit_issue or hydrated.get("culprit_issue")
        culprit_author = culprit_author or hydrated.get("culprit_author")

    return {
        "culprit_repo": culprit_repo,
        "culprit_commit": culprit_commit,
        "culprit_pr": culprit_pr,
        "culprit_issue": culprit_issue,
        "culprit_author": culprit_author,
    }


def cmd_blocker_add(args, store):
    culprit = _resolve_culprit_args(args)
    prs = []
    for ref in args.pr or ():
        repo, num = parse_pr_ref(ref)
        prs.append((repo, num, args.target_branch, True))
    for ref in args.pr_optional or ():
        repo, num = parse_pr_ref(ref)
        prs.append((repo, num, args.target_branch, False))
    r = store.add_blocker(
        args.id,
        title=args.title or "",
        **culprit,
        culprit_confidence=args.culprit_confidence,
        culprit_note=args.culprit_note or "",
        fix_issue=args.fix_issue,
        owner=args.owner,
        prs=prs,
        signatures=args.signature or [],
        rules=args.rule or [],
    )
    emit(args, {"result": r, "blocker": args.id}, f"blocker {args.id}: {r}")


def cmd_blocker_update(args, store):
    culprit = _resolve_culprit_args(args)
    updates = {
        "title": args.title,
        "culprit_repo": culprit["culprit_repo"],
        "culprit_commit": culprit["culprit_commit"],
        "culprit_pr": culprit["culprit_pr"],
        "culprit_issue": culprit["culprit_issue"],
        "culprit_author": culprit["culprit_author"],
        "culprit_confidence": args.culprit_confidence,
        "culprit_note": args.culprit_note,
        "fix_issue": args.fix_issue,
        "owner": args.owner,
    }
    r = store.update_blocker(args.id, **updates)
    emit(args, {"result": r, "blocker": args.id}, f"blocker {args.id}: {r}")


def cmd_blocker_retire(args, store):
    r = store.retire_blocker(args.id, args.reason)
    emit(args, {"result": r, "blocker": args.id}, f"blocker {args.id}: {r}")


def cmd_blocker_pr_add(args, store):
    repo, num = parse_pr_ref(args.pr_ref)
    r = store.add_blocker_pr(
        args.id, repo, num, target_branch=args.target_branch, required=not args.optional)
    emit(args, {"result": r, "blocker": args.id, "repository": repo, "pr_number": num},
         f"blocker {args.id} PR {repo}#{num}: {r}")


def cmd_blocker_pr_retire(args, store):
    repo, num = parse_pr_ref(args.pr_ref)
    r = store.retire_blocker_pr(args.id, repo, num, args.reason)
    emit(args, {"result": r, "blocker": args.id, "repository": repo, "pr_number": num},
         f"blocker {args.id} PR {repo}#{num}: {r}")


def cmd_blocker_link(args, store):
    r = store.link_blocker(args.id, signatures=args.signature or [], rules=args.rule or [])
    emit(args, {"result": r, "blocker": args.id}, f"blocker {args.id}: {r}")


def cmd_blocker_show(args, store):
    b = store.blocker(args.id, include_retired_prs=True)
    if b is None:
        raise RegistryError(f"no blocker {args.id}")
    issue_facts = None
    pr_map = {}
    if args.live:
        pr_refs = [(p["repository"], p["pr_number"]) for p in b["prs"] if not p["retired_at"]]
        if pr_refs:
            pr_map = live.read_prs_batch(pr_refs)
        if b["fix_issue"]:
            issue_facts = verdict.read_issue(b["fix_issue"])
    ev = readiness.evaluate_blocker(
        b, issue_facts=issue_facts, pr_facts_map=pr_map,
        active_rules=[r for r in b["rules"] if not r["retired_at"]],
    )
    emit(args, ev, render.render_blockers({"blockers": [ev], "untracked_rules": []},
                                         no_color=args.no_color))


def _local_develop_fix_commits(issue_key: str, engine_repo: str | None) -> tuple[str, ...]:
    if not engine_repo or not Path(engine_repo, ".git").exists():
        return ()
    proc = verdict._git(engine_repo, "log", "--format=%H", f"--grep=\\[{issue_key}\\]",
                        verdict.FIX_REF)
    return tuple(proc.stdout.split()) if proc.returncode == 0 else ()


def cmd_blockers(args, store):
    requested_keys: list[str] = []
    for raw_k in args.key or ():
        k = raw_k.strip()
        if k and k not in requested_keys:
            requested_keys.append(k)
    missing_keys: list[str] = []
    if requested_keys:
        all_blockers = store.blockers(active_only=False)
        wanted_lower = {k.lower() for k in requested_keys}
        blockers = [
            b for b in all_blockers
            if b["id"].lower() in wanted_lower
            or (b.get("fix_issue") and b["fix_issue"].lower() in wanted_lower)
        ]
        matched_lower: set[str] = set()
        for b in blockers:
            matched_lower.add(b["id"].lower())
            if b.get("fix_issue"):
                matched_lower.add(b["fix_issue"].lower())
        missing_keys = [k for k in requested_keys if k.lower() not in matched_lower]
    else:
        blockers = store.blockers(active_only=not args.all)
    active_rules = store.rules(active_only=True)
    rules_by_blocker: dict[str, list[dict]] = {}
    untracked_rules: list[dict] = []
    for r in active_rules:
        b = store.blocker_for_rule(r)
        if b is not None:
            rules_by_blocker.setdefault(b["id"], []).append(r)
        else:
            untracked_rules.append(r)

    # Affected open PRs from cubrid-tcc failures --days N (if requested).
    failures_by_tc: dict[tuple[str, str], list[dict]] = {}
    affected_pr_refs: set[tuple[str, int]] = set()
    if args.affected_days is not None:
        rep = chronicle.failures(days=args.affected_days, latest_run=True)
        for item in rep.get("items") or []:
            pr = item.get("pull_request") or {}
            pr_num = pr.get("number")
            if pr_num is None:
                continue
            tc = item["testcase"]
            key = (tc["suite"], tc["path"])
            failures_by_tc.setdefault(key, []).append(item)
            affected_pr_refs.add(("CUBRID/cubrid", int(pr_num)))

    pr_refs = {
        (p["repository"], p["pr_number"])
        for b in blockers
        for p in b["prs"]
        if not p["retired_at"]
    } | affected_pr_refs

    pr_map: dict[tuple[str, int], live.PrFacts] = {}
    issues: dict[str, live.IssueFacts] = {}
    discovered: dict[str, list[live.PrFacts]] = {}

    if not args.offline:
        if pr_refs:
            pr_map = live.read_prs_batch(pr_refs)
        issue_keys = sorted({b["fix_issue"] for b in blockers if b.get("fix_issue")})
        if issue_keys:
            with ThreadPoolExecutor(max_workers=min(6, len(issue_keys))) as pool:
                issues = dict(
                    zip(issue_keys, pool.map(verdict.read_issue, issue_keys), strict=True)
                )
        if not args.no_discover:
            # Discover PRs only for blockers that have a fix_issue and no active required PRs.
            tbd = [
                b for b in blockers
                if b.get("fix_issue")
                and not [p for p in b["prs"] if not p["retired_at"] and p["required"]]
            ]
            if tbd:
                tbd_keys = sorted({b["fix_issue"] for b in tbd})
                with ThreadPoolExecutor(max_workers=min(4, len(tbd_keys))) as pool:
                    for k, (prs_found, _) in zip(
                        tbd_keys, pool.map(live.discover_prs_for_issue, tbd_keys), strict=True
                    ):
                        discovered[k] = prs_found

    evaluated = []
    for b in blockers:
        fix_iss = b.get("fix_issue")
        iss_facts = issues.get(fix_iss) if fix_iss else None
        develop_commits = ()
        if (not args.offline and fix_iss and iss_facts and iss_facts.readable
                and iss_facts.resolution in verdict.FIXED_RESOLUTIONS
                and not [p for p in b["prs"] if not p["retired_at"] and p["required"]]):
            develop_commits = _local_develop_fix_commits(fix_iss, args.engine_repo)
        ev = readiness.evaluate_blocker(
            b,
            target_branch=args.branch,
            issue_facts=iss_facts,
            pr_facts_map=pr_map,
            discovered_prs=discovered.get(fix_iss, ()) if fix_iss else (),
            fix_on_develop_commits=develop_commits,
            active_rules=rules_by_blocker.get(b["id"], []),
        )
        if args.affected_days is not None:
            seen_prs: dict[int, dict] = {}
            for sig in b.get("signatures") or []:
                for item in failures_by_tc.get((sig["suite"], sig["path"]), []):
                    pr_info = item.get("pull_request") or {}
                    num = int(pr_info["number"])
                    pf = pr_map.get(("CUBRID/cubrid", num))
                    if pf and pf.readable and pf.state != "OPEN":
                        continue
                    seen_prs[num] = {
                        "pr": num,
                        "base_ref": pr_info.get("base_ref"),
                        "testcase_branch": item.get("testcase_branch"),
                        "engine_sha": item.get("engine_sha"),
                    }
            ev["affected_open_prs"] = [seen_prs[n] for n in sorted(seen_prs)]
        if args.ready_only and ev["readiness"] != "READY":
            continue
        evaluated.append(ev)

    report = {
        "schema_version": 1,
        "target_branch": args.branch,
        "blockers": evaluated,
        "missing_keys": missing_keys,
        "untracked_rules": [] if (args.ready_only or requested_keys) else [
            {"id": r["id"], "issue": r["issue"], "expires": r["expires"],
             "signature_id": r["signature_id"], "job_contains": r["job_contains"]}
            for r in untracked_rules
        ],
    }
    emit(args, report, render.render_blockers(report, no_color=args.no_color))


def cmd_unblock(args, store):
    rep = chronicle.failures(pr=args.pr, latest_run=True)
    items = rep.get("items") or []
    latest_runs = rep.get("latest_runs") or {}

    # Resolve testcase identities and their occurrence tags / blockers / rules.
    tc_cache: dict[tuple[str, str], Testcase] = {}
    tags_cache: dict[int, dict] = {}
    item_matches: list[tuple[dict, Testcase, list[dict], list[dict]]] = []
    all_blockers: dict[str, dict] = {}

    for item in items:
        tc_info = item["testcase"]
        t_key = (tc_info["suite"], tc_info["path"])
        if t_key not in tc_cache:
            tc = store.testcase(tc_info["suite"], tc_info["path"],
                                tc_info["repository"], tc_info.get("variant") or "")
            tc_cache[t_key] = tc or Testcase(
                -1, tc_info["repository"], tc_info["suite"], tc_info["path"],
                tc_info.get("variant") or "")
        tc = tc_cache[t_key]
        if tc.id not in tags_cache:
            tags_cache[tc.id] = store.occurrence_tags(tc) if tc.id != -1 else {}
        tag = tags_cache[tc.id].get(
            (item["repository"], item["run_id"], str(item["shard"]), item["ordinal"])
        )
        sig_id = tag["signature_id"] if tag else None
        tc_rules = store.rules(tc=tc) if tc.id != -1 else []
        if sig_id:
            tc_rules = [r for r in tc_rules if r["signature_id"] == sig_id] or tc_rules
        tc_blockers = store.blockers_for_testcase(tc) if tc.id != -1 else []
        if sig_id:
            filtered = [
                b for b in tc_blockers
                if any(s["id"] == sig_id for s in b.get("signatures") or ())
            ]
            tc_blockers = filtered or tc_blockers
        for b in tc_blockers:
            all_blockers[b["id"]] = b
        for r in tc_rules:
            if b := store.blocker_for_rule(r):
                all_blockers[b["id"]] = b
                if b["id"] not in {x["id"] for x in tc_blockers}:
                    tc_blockers.append(b)
        item_matches.append((item, tc, tc_blockers, tc_rules))

    pr_refs = {("CUBRID/cubrid", args.pr)} | {
        (p["repository"], p["pr_number"])
        for b in all_blockers.values()
        for p in b["prs"]
        if not p["retired_at"]
    }
    pr_map: dict[tuple[str, int], live.PrFacts] = {}
    issues: dict[str, live.IssueFacts] = {}
    if not args.offline:
        pr_map = live.read_prs_batch(pr_refs)
        issue_keys = sorted({b["fix_issue"] for b in all_blockers.values() if b.get("fix_issue")})
        if issue_keys:
            with ThreadPoolExecutor(max_workers=min(6, len(issue_keys))) as pool:
                issues = dict(
                    zip(issue_keys, pool.map(verdict.read_issue, issue_keys), strict=True)
                )

    target_pr = pr_map.get(("CUBRID/cubrid", args.pr))
    pr_meta = {
        "number": args.pr,
        "state": target_pr.display_state if target_pr else "UNKNOWN",
        "base_ref": (target_pr.base_ref if target_pr and target_pr.base_ref
                     else next((i["pull_request"]["base_ref"] for i in items
                                if i.get("pull_request") and i["pull_request"].get("base_ref")),
                               None)),
        "head_ref": (target_pr.head_ref if target_pr and target_pr.head_ref
                     else next((i["pull_request"]["head_ref"] for i in items
                                if i.get("pull_request") and i["pull_request"].get("head_ref")),
                               None)),
    }

    eval_map: dict[str, dict] = {}
    for bid, b in all_blockers.items():
        fix_iss = b.get("fix_issue")
        iss_facts = issues.get(fix_iss) if fix_iss else None
        develop_commits = ()
        if (not args.offline and fix_iss and iss_facts and iss_facts.readable
                and iss_facts.resolution in verdict.FIXED_RESOLUTIONS
                and not [p for p in b["prs"] if not p["retired_at"] and p["required"]]):
            develop_commits = _local_develop_fix_commits(fix_iss, args.engine_repo)
        eval_map[bid] = readiness.evaluate_blocker(
            b, target_branch=args.branch, issue_facts=iss_facts,
            pr_facts_map=pr_map, fix_on_develop_commits=develop_commits,
        )

    suites_out: dict[str, dict] = {}
    for item, tc, tc_blockers, tc_rules in item_matches:
        suite_name = item["suite"]
        lr = latest_runs.get(suite_name) or {}
        s_entry = suites_out.setdefault(suite_name, {
            "run_id": lr.get("run_id") or item["run_id"],
            "engine_sha": lr.get("engine_sha") or item.get("engine_sha"),
            "testcase_sha": lr.get("testcase_sha") or item.get("testcase_sha"),
            "testcase_branch": lr.get("testcase_branch") or item.get("testcase_branch"),
            "failures": [],
        })
        eng_sha = item.get("engine_sha") or s_entry["engine_sha"]
        tc_sha = item.get("testcase_sha") or s_entry["testcase_sha"]
        tag = tags_cache[tc.id].get(
            (item["repository"], item["run_id"], str(item["shard"]), item["ordinal"])
        )
        sig_id = tag["signature_id"] if tag else None

        if tc_blockers:
            b_raw = tc_blockers[0]
            b_ev = eval_map[b_raw["id"]]
            fix_presence = None
            if not args.offline and b_ev["readiness"] == "READY" and eng_sha:
                bfix, _ = verdict.read_blocker_fix(
                    b_raw, engine_sha=eng_sha, tc_sha=tc_sha, tc_repo=tc.repository,
                    engine_repo=args.engine_repo, pr_facts_map=pr_map,
                )
                if bfix is not None:
                    fix_presence = ("present" if bfix.present
                                    else ("missing" if bfix.present is False else "unknown"))
            s_entry["failures"].append({
                "path": tc.path,
                "shard": item["shard"],
                "signature": sig_id,
                "blocker": b_ev,
                "fix_presence": fix_presence,
            })
        elif tc_rules:
            r0 = tc_rules[0]
            s_entry["failures"].append({
                "path": tc.path,
                "shard": item["shard"],
                "signature": sig_id,
                "rule": {"id": r0["id"], "issue": r0["issue"], "expires": r0["expires"],
                         "verdict": "known" if r0["expires"] or r0["issue"] else "unknown"},
            })
        else:
            s_entry["failures"].append({
                "path": tc.path,
                "shard": item["shard"],
                "signature": sig_id,
            })

    for suite_name, s_entry in suites_out.items():
        fs = s_entry["failures"]
        unclassified = [f for f in fs if not f.get("blocker") and not f.get("rule")]
        fix_already_in = [f for f in fs if f.get("fix_presence") == "present"]
        unready = [
            f for f in fs
            if (f.get("blocker") and f["blocker"]["readiness"] != "READY") or f.get("rule")
        ]
        if unclassified or fix_already_in:
            s_entry["status"] = "NEEDS ANALYSIS"
            reasons = []
            if unclassified:
                reasons.append(f"{len(unclassified)} unclassified failure(s)")
            if fix_already_in:
                reasons.append(f"{len(fix_already_in)} failure(s) already contain fix")
            s_entry["action"] = "analyze: " + ", ".join(reasons)
        elif unready:
            s_entry["status"] = "STILL BLOCKED"
            wait_items = []
            for f in unready:
                if f.get("blocker"):
                    b = f["blocker"]
                    wait_items.append(
                        f"{b['id']} ({b['readiness']}, owner: {b.get('owner') or '-'})"
                    )
                else:
                    wait_items.append(f"rule {f['rule']['id']}")
            s_entry["action"] = "WAIT on " + ", ".join(dict.fromkeys(wait_items))
        else:
            s_entry["status"] = "READY TO UNBLOCK"
            actions = {
                f["blocker"]["unblock_action"]
                for f in fs if f.get("blocker") and f["blocker"].get("unblock_action")
            }
            need_engine = bool(actions & {"REBASE_ENGINE", "REBASE_ENGINE_AND_SYNC_TC"})
            need_tc = bool(actions & {"RERUN_OR_SYNC_TC", "REBASE_ENGINE_AND_SYNC_TC"})
            tc_br = s_entry.get("testcase_branch") or ""
            steps = []
            if need_engine:
                steps.append(f"rebase/merge {args.branch} into engine PR #{args.pr}")
            if need_tc:
                if tc_br.startswith("tc/pr-"):
                    steps.append(f"sync {tc_br} with {args.branch}")
                else:
                    steps.append(f"rerun {suite_name} immediately (TC uses {args.branch})")
            if not steps:
                steps.append(f"rerun {suite_name}")
            s_entry["action"] = "; ".join(steps)

    report = {
        "schema_version": 1,
        "pull_request": pr_meta,
        "target_branch": args.branch,
        "suites": suites_out,
    }
    emit(args, report, render.render_unblock(report, no_color=args.no_color))


def cmd_rule_add(args, store):
    proposal = {k: getattr(args, k) for k in
                ("id", "signature", "job_contains", "issue", "expires", "blocker", "not_covered")}
    if not args.yes:
        emit(args, {"result": "dry_run", "rule": proposal},
             "dry run, nothing written. Show this rule to the user, then rerun with"
             " --yes --approved-by <name>:\n" + json.dumps(proposal, indent=2))
        return
    if not args.approved_by:
        raise RegistryError("--yes needs --approved-by <name of the person who approved>")
    r = store.add_rule(args.id, sig_id=args.signature, job_contains=args.job_contains,
                       issue=args.issue, expires=args.expires, not_covered=args.not_covered,
                       approved_by=args.approved_by, blocker_id=args.blocker)
    emit(args, {"result": r}, f"rule {args.id}: {r}")


EXPIRY_WARN_DAYS = 14


def audit_rule(rule, issue, today, *, blocker_eval: dict | None = None):
    """Add live issue state, days to expiry, blocker readiness, and flags to one rule row."""
    out = {"id": rule["id"], "kind": "job" if rule["job_contains"] is not None else "signature",
           "signature": rule["signature_id"], "job_contains": rule["job_contains"],
           "issue": rule["issue"], "expires": rule["expires"], "approved_by": rule["approved_by"],
           "created": rule["created_at"], "retired": rule["retired_at"]}
    if issue is not None:
        out["issue_status"] = issue.status if issue.readable else None
        out["issue_resolution"] = issue.resolution if issue.readable else None
        if issue.readable and issue.assignee:
            out["issue_assignee"] = issue.assignee
        if not issue.readable:
            out["issue_error"] = issue.error
    flags = []
    if rule["expires"]:
        days = (date.fromisoformat(rule["expires"]) - today).days
        out["days_to_expiry"] = days
        if days < 0:
            flags.append("expired")
        elif days <= EXPIRY_WARN_DAYS:
            flags.append(f"expires within {EXPIRY_WARN_DAYS} days")
    if issue is not None and issue.readable and issue.resolution in verdict.FIXED_RESOLUTIONS:
        flags.append(f"issue resolved {issue.resolution}: retire the rule or expect fix_* verdicts")
    if blocker_eval is not None:
        out["blocker_id"] = blocker_eval["id"]
        out["readiness"] = blocker_eval["readiness"]
        out["unblock_action"] = blocker_eval["unblock_action"]
        out["owner"] = blocker_eval["owner"]
        out["culprit"] = blocker_eval["culprit"]
        out["fix_prs"] = blocker_eval["fix_prs"]
        if blocker_eval["readiness"] == "READY":
            flags.append(
                f"blocker {blocker_eval['id']} READY ({blocker_eval['unblock_action']}):"
                " consider retiring rule"
            )
        for bf in blocker_eval.get("flags") or []:
            if bf not in flags:
                flags.append(bf)
    out["flags"] = flags
    return out


def cmd_rules(args, store):
    rows = store.rules(active_only=not args.all)
    today = date.today()
    issues = {}
    blocker_evals: dict[str, dict] = {}
    if args.audit:
        blockers_by_rule = {r["id"]: store.blocker_for_rule(r) for r in rows}
        all_blockers = [b for b in blockers_by_rule.values() if b is not None]
        issue_keys = sorted({
            *{r["issue"] for r in rows if r["issue"]},
            *{b["fix_issue"] for b in all_blockers if b.get("fix_issue")},
        })
        for key in issue_keys:
            issues[key] = verdict.read_issue(key)
        pr_refs = {
            (p["repository"], p["pr_number"])
            for b in all_blockers
            for p in b["prs"]
            if not p["retired_at"]
        }
        pr_map = live.read_prs_batch(pr_refs) if pr_refs else {}
        for r in rows:
            b = blockers_by_rule.get(r["id"])
            if b is not None:
                fix_iss = b.get("fix_issue")
                blocker_evals[r["id"]] = readiness.evaluate_blocker(
                    b,
                    issue_facts=issues.get(fix_iss) if fix_iss else None,
                    pr_facts_map=pr_map,
                )
    data = [
        audit_rule(r, issues.get(r["issue"]), today, blocker_eval=blocker_evals.get(r["id"]))
        for r in rows
    ]
    lines = []
    for d in data:
        scope = f"job contains {d['job_contains']!r}" if d["kind"] == "job" else \
            f"signature {d['signature']}"
        b_suffix = f" [blocker {d['blocker_id']}]" if d.get("blocker_id") else ""
        lines.append(f"{d['id']} [{d['kind']}] {scope}{b_suffix}"
                     + (" (retired)" if d["retired"] else ""))
        lines.append(f"  issue {d['issue'] or '-'}, expires {d['expires'] or '-'},"
                     f" approved by {d['approved_by'] or '-'}, created {d['created']}")
        if args.audit:
            if "issue_status" in d:
                state = d["issue_status"] or "unreadable"
                assignee = f", assignee {d['issue_assignee']}" if d.get("issue_assignee") else ""
                lines.append(f"  {d['issue']}: {state} / {d['issue_resolution'] or 'unresolved'}"
                             f"{assignee}"
                             + (f" ({d['issue_error']})" if "issue_error" in d else ""))
            if "readiness" in d:
                lines.append(f"  blocker {d['blocker_id']}: {d['readiness']}"
                             f" (owner {d.get('owner') or '-'})"
                             + (f", action {d['unblock_action']}"
                                if d.get("unblock_action") else ""))
            if "days_to_expiry" in d:
                lines.append(f"  expires in {d['days_to_expiry']} days")
            lines += [f"  FLAG {f}" for f in d["flags"]]
    emit(args, data, "\n".join(lines) or "no rules")


def cmd_rule_retire(args, store):
    r = store.retire_rule(args.id, args.reason)
    emit(args, {"result": r}, f"rule {args.id}: {r}")


def cmd_validate(args, store):
    problems = store.validate()
    emit(args, {"ok": not problems, "problems": problems},
         "\n".join(problems) if problems else "ok")
    if problems:
        sys.exit(1)


def cmd_log(args, store):
    tc = resolve(store, args.case, args.suite, create=False) if args.case else None
    events = store.events(tc, limit=args.limit)
    emit(args, events, "\n".join(
        f"{e['at']} {e['actor']} {e['command']} {e['entity']} {e['entity_key']} {e['after']}"
        for e in events) or "no events")


def cmd_backup(args, store):
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    default = store.path.parent / "backups" / f"registry-{stamp}.sqlite3"
    dest = Path(args.out) if args.out else default
    store.backup(dest)
    emit(args, {"path": str(dest)}, str(dest))


def cmd_export(args, store):
    out = ["# cubrid-tcr export. Generated; edit the database through cubrid-tcr instead.\n"]
    tables = ("testcase", "signature", "issue_link", "blocker", "blocker_pr",
              "blocker_signature", "rule", "note", "occurrence_signature")
    for table in tables:
        for row in store.db.execute(f"SELECT * FROM {table} ORDER BY rowid"):
            out.append(f"[[{table}]]")
            for k in row.keys():
                if row[k] is not None:
                    out.append(f"{k} = {json.dumps(row[k], ensure_ascii=False)}")
            out.append("")
    print("\n".join(out))


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cubrid-tcr", description=__doc__, epilog=EPILOG,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"cubrid-tcr {__version__}")

    def global_options(target, default):
        target.add_argument("--database", type=Path, default=default(None),
                            help=f"SQLite path (default {default_path()}, env TCR_DB)")
        target.add_argument("--actor",
                            default=default(os.environ.get("TCR_ACTOR") or getpass.getuser()),
                            help="who writes, recorded in the event log (env TCR_ACTOR)")
        target.add_argument("--json", action="store_true", default=default(False),
                            help="print JSON")
        target.add_argument("--no-color", action="store_true", default=default(False),
                            help="disable colorized terminal output")

    global_options(p, lambda value: value)
    # The same options after the command. SUPPRESS keeps a value given before the command.
    common = argparse.ArgumentParser(add_help=False)
    global_options(common, lambda value: argparse.SUPPRESS)

    def add(subparsers, name, **kwargs):
        return subparsers.add_parser(name, parents=[common], **kwargs)

    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def case_args(sp, required=True, suites=WRITE_SUITES):
        sp.add_argument("case", nargs=None if required else "?",
                        help="full testcase path, or a fragment Chronicle resolves")
        sp.add_argument("--suite", choices=suites)

    s = add(sub, "show", help="everything known about one testcase")
    case_args(s, suites=CHECK_SUITES)
    g = s.add_mutually_exclusive_group()
    g.add_argument("--days", type=int, help="list only failures of the last N days")
    g.add_argument("--all", action="store_true", help="list every indexed failure")
    s.add_argument("--live", action="store_true",
                   help="fetch live JIRA and PR status for linked blockers")
    s.set_defaults(func=cmd_show)

    s = add(sub, "check", help="decide whether failures are known issues")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--case")
    g.add_argument("--job", help="CI job name, for a job-level failure")
    g.add_argument("--ci-attempt", metavar="DIR",
                   help="check every failure of one suite directory collected by cubrid-ci"
                        " (reads summary.json and failures/*; engine SHA comes from the shard)")
    s.add_argument("--suite", choices=CHECK_SUITES)
    s.add_argument("--engine", help="tested Engine commit (--case and --job)")
    s.add_argument("--tc-sha", help="tested testcase repository commit SHA (--case)")
    s.add_argument("--record", action="store_true",
                   help="with --ci-attempt, attach a signature to each occurrence that matches"
                        " exactly one; without it nothing is written")
    s.add_argument("--stack-file",
                   help="failure text: a core backtrace or a failure message (NOK line)")
    s.add_argument("--engine-repo", default=os.environ.get("TCR_ENGINE_REPO"),
                   help="engine clone with origin/develop (default: $TCR_ENGINE_REPO)")
    s.add_argument("--offline", action="store_true", help="skip the Jira and GitHub reads")
    s.add_argument("--no-context", action="store_true", help="skip the testcase view")
    s.set_defaults(func=cmd_check)

    s = add(sub, "signature", help="add or attach a failure signature")
    ss = s.add_subparsers(dest="action", required=True)
    a = add(ss, "add")
    case_args(a)
    a.add_argument("--id", required=True)
    a.add_argument("--label", required=True)
    a.add_argument("--frames", help="comma-separated, innermost first; optional with --text")
    a.add_argument("--text", action="append",
                   help="substring the failure text must contain; with no --frames the signature"
                        " matches NOK lines and other failure messages")
    a.set_defaults(func=cmd_signature_add)
    a = add(ss, "attach")
    case_args(a)
    a.add_argument("--run", type=int, required=True)
    a.add_argument("--shard", required=True)
    a.add_argument("--attempt", type=int)
    a.add_argument("--stack-file", required=True, help="failure text, as in check")
    a.add_argument("--signature")
    a.set_defaults(func=cmd_signature_attach)

    s = add(sub, "link", help="relate a testcase to an issue")
    case_args(s)
    s.add_argument("--issue", required=True)
    s.add_argument("--relation", required=True, choices=RELATIONS)
    s.add_argument("--signature")
    s.set_defaults(func=cmd_link)

    s = add(sub, "note", help="leave a note on a testcase")
    case_args(s)
    s.add_argument("--body", required=True)
    s.add_argument("--ref", action="append", help="a PR, run, issue, or document reference")
    s.set_defaults(func=cmd_note)

    # First-class blocker management
    s = add(sub, "blocker", help="track culprit, fix issue, fix PRs, and owner for a blocker")
    bs = s.add_subparsers(dest="action", required=True)

    def add_culprit_opts(sp, *, for_update=False):
        sp.add_argument("--title", help="short blocker summary")
        sp.add_argument("--culprit",
                        help="culprit PR ref (engine#8537, tc#4474) or commit SHA (auto-hydrated)")
        sp.add_argument("--culprit-repo", help="culprit repository")
        sp.add_argument("--culprit-commit", help="culprit commit SHA")
        sp.add_argument("--culprit-pr", type=int, help="culprit PR number")
        sp.add_argument("--culprit-issue", help="culprit CBRD issue key")
        sp.add_argument("--culprit-author", help="culprit author login or name")
        sp.add_argument("--culprit-confidence",
                        choices=CONFIDENCES,
                        default=None if for_update else "confirmed")
        sp.add_argument("--culprit-note", help="bisect or root-cause note")
        sp.add_argument("--fix-issue", help="CBRD issue tracking the fix")
        sp.add_argument("--owner", help="local fallback owner when no JIRA assignee / PR author")
        sp.add_argument("--engine-repo", default=os.environ.get("TCR_ENGINE_REPO"),
                        help="local engine clone for culprit SHA inspection")
        sp.add_argument("--offline", action="store_true", help="skip GitHub auto-hydration")

    a = add(bs, "add")
    a.add_argument("id", help="blocker id (e.g. CBRD-28395 or tc-4474)")
    add_culprit_opts(a)
    a.add_argument("--pr", action="append", help="required fix PR ref (repeatable)")
    a.add_argument("--pr-optional", action="append", help="non-blocking fix/TC PR ref")
    a.add_argument("--target-branch", default="develop", help="target branch (default: develop)")
    a.add_argument("--signature", action="append", help="signature ID to attach (repeatable)")
    a.add_argument("--rule", action="append", help="existing rule ID to link (repeatable)")
    a.set_defaults(func=cmd_blocker_add)

    a = add(bs, "update")
    a.add_argument("id")
    add_culprit_opts(a, for_update=True)
    a.set_defaults(func=cmd_blocker_update)

    a = add(bs, "show")
    a.add_argument("id")
    a.add_argument("--live", action="store_true", help="query live GitHub PR and JIRA state")
    a.set_defaults(func=cmd_blocker_show)

    a = add(bs, "retire")
    a.add_argument("id")
    a.add_argument("--reason", required=True)
    a.set_defaults(func=cmd_blocker_retire)

    a = add(bs, "link", help="link signatures or existing rules to a blocker")
    a.add_argument("id")
    a.add_argument("--signature", action="append")
    a.add_argument("--rule", action="append")
    a.set_defaults(func=cmd_blocker_link)

    bp = add(bs, "pr", help="add or retire fix PRs on a blocker")
    bps = bp.add_subparsers(dest="pr_action", required=True)
    a = add(bps, "add")
    a.add_argument("id")
    a.add_argument("pr_ref", help="PR ref: engine#N, tc#N, tc-private#N, or URL")
    a.add_argument("--target-branch", default="develop")
    a.add_argument("--optional", action="store_true",
                   help="mark PR as optional (does not gate READY)")
    a.set_defaults(func=cmd_blocker_pr_add)
    a = add(bps, "retire")
    a.add_argument("id")
    a.add_argument("pr_ref")
    a.add_argument("--reason", required=True)
    a.set_defaults(func=cmd_blocker_pr_retire)

    s = add(sub, "blockers", help="list blockers with live fix PR merge & owner readiness")
    s.add_argument("--key", action="append",
                   help="filter blockers by ID or fix CBRD key (repeatable; includes retired)")
    s.add_argument("--branch", default="develop", help="target branch (default: develop)")
    s.add_argument("--ready-only", action="store_true", help="show only READY blockers")
    s.add_argument("--all", action="store_true", help="include retired blockers")
    s.add_argument("--no-discover", action="store_true",
                   help="skip cubrid-xref discovery for PR-TBD blockers")
    s.add_argument("--affected-days", type=int,
                   help="also list open PRs still failing in last N days via cubrid-tcc")
    s.add_argument("--engine-repo", default=os.environ.get("TCR_ENGINE_REPO"))
    s.add_argument("--offline", action="store_true", help="skip GitHub, JIRA, and cubrid-xref")
    s.set_defaults(func=cmd_blockers)

    s = add(sub, "unblock", help="check whether a PR's latest failures are unblocked on develop")
    s.add_argument("--pr", type=int, required=True, help="pull request number to check")
    s.add_argument("--branch", default="develop", help="target branch (default: develop)")
    s.add_argument("--engine-repo", default=os.environ.get("TCR_ENGINE_REPO"))
    s.add_argument("--offline", action="store_true", help="skip GitHub and JIRA queries")
    s.set_defaults(func=cmd_unblock)

    s = add(sub, "rule", help="add or retire a known-issue rule")
    ss = s.add_subparsers(dest="action", required=True)
    a = add(ss, "add", help="dry run unless --yes and --approved-by")
    a.add_argument("--id", required=True)
    g = a.add_mutually_exclusive_group(required=True)
    g.add_argument("--signature")
    g.add_argument("--job-contains")
    a.add_argument("--issue")
    a.add_argument("--expires", help="YYYY-MM-DD, required without --issue or --blocker")
    a.add_argument("--blocker", help="blocker ID backing this rule")
    a.add_argument("--not-covered", required=True)
    a.add_argument("--approved-by")
    a.add_argument("--yes", action="store_true")
    a.set_defaults(func=cmd_rule_add)
    a = add(ss, "retire")
    a.add_argument("id")
    a.add_argument("--reason", required=True)
    a.set_defaults(func=cmd_rule_retire)

    s = add(sub, "rules", help="list known-issue rules, optionally with live issue/blocker state")
    s.add_argument("--audit", action="store_true",
                   help="read each issue/blocker live and flag Fixed, merged PR, or expiring rules")
    s.add_argument("--all", action="store_true", help="include retired rules")
    s.set_defaults(func=cmd_rules)

    s = add(sub, "validate", help="check cross-record invariants")
    s.set_defaults(func=cmd_validate)
    s = add(sub, "log", help="show the write history")
    case_args(s, required=False)
    s.add_argument("--limit", type=int, default=50)
    s.set_defaults(func=cmd_log)
    s = add(sub, "backup", help="save a consistent SQLite snapshot")
    s.add_argument("--out")
    s.set_defaults(func=cmd_backup)
    s = add(sub, "export", help="print every record as TOML for review")
    s.set_defaults(func=cmd_export)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    store = None
    try:
        store = Store(args.database or default_path(), args.actor)
        args.func(args, store)
    except (RegistryError, OSError, chronicle.ChronicleError) as e:
        print(json.dumps({"error": str(e)}) if args.json else f"error: {e}", file=sys.stderr)
        sys.exit(2)
    finally:
        if store is not None:
            store.db.close()


if __name__ == "__main__":
    main()
