"""cubrid-tcr: per-testcase knowledge for CUBRID CI analysis."""

import argparse
import getpass
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from pathlib import Path

from . import __version__, attempt, chronicle, stack, verdict, view
from .store import RELATIONS, SUITE_REPOSITORY, RegistryError, Store, Testcase, default_path

EPILOG = """\
examples:
  cubrid-tcr show cbrd_27179
  cubrid-tcr check --case cbrd_27179 --suite shell --engine cd24a21 --stack-file core.txt
  cubrid-tcr check --job "shard sql_by_cci 01" --engine cd24a21
  cubrid-tcr check --ci-attempt ~/.local/share/cubrid-ci-data/.../attempts/1/test_shell --record
  cubrid-tcr note cbrd_27179 --suite shell --body "Also failed on PR #8404"
"""


def resolve(store: Store, case: str, suite: str | None, *, create: bool) -> Testcase:
    """Turn a full path or a Chronicle fragment into a registry testcase identity.

    Without `create`, an identity the registry has not stored yet comes back with id -1, which
    matches no record. Read commands use that so they never write.
    """
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
    v = view.build(store, tc, days=args.days, show_all=args.all)
    emit(args, v, view.render(v))


def _hits(store, rules, *, job, stack_text, engine, engine_repo, offline, cache):
    """Evaluate rules for one failure. `cache` holds Jira and fix reads shared by a batch."""
    today = date.today()
    hits = []
    for rule in rules:
        sig = store.signature(rule["signature_id"]) if rule["signature_id"] else None
        scope = verdict.scope_matches(rule, job=job, stack_text=stack_text, signature=sig)
        issue = fix = None
        if scope == "match" and rule["issue"]:
            if not offline:
                key = ("issue", rule["issue"])
                if key not in cache:
                    cache[key] = verdict.read_issue(rule["issue"])
                issue = cache[key]
            if issue and issue.resolution == "Fixed":
                key = ("fix", rule["issue"], engine)
                if key not in cache:
                    cache[key] = verdict.read_fix(rule["issue"], engine, engine_repo)
                fix = cache[key]
        h = verdict.decide(rule, scope, today, issue, fix)
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
    if args.case:
        tc = resolve(store, args.case, args.suite, create=False)
        rules = store.rules(tc=tc)
    else:
        rules = store.job_rules()
    hits = _hits(store, rules, job=args.job, stack_text=stack_text, engine=args.engine,
                 engine_repo=args.engine_repo, offline=args.offline, cache={})
    result = {
        "verdict": _overall(hits),
        "hits": [h.as_dict() for h in hits],
        "engine": args.engine,
        "case": tc.path if tc else args.case,
        "job": args.job,
        "stack_given": stack_text is not None,
    }
    if tc and not args.no_context:
        result["context"] = view.build(store, tc)
    lines = [f"verdict: {result['verdict']}"]
    for h in hits:
        lines.append(f"  rule {h.rule_id} ({h.issue or 'no issue'}): {h.verdict}. {h.reason}.")
        lines.append(f"    not covered: {h.not_covered}")
    if "context" in result:
        lines += ["", view.render(result["context"])]
    emit(args, result, "\n".join(lines))


def check_attempt(args, store):
    """Check every failure of one collected CI attempt suite."""
    bundle = attempt.load(args.ci_attempt)
    failures = bundle["failures"]
    keys = sorted({(f["suite"], f["path"]) for f in failures
                   if attempt.CHRONICLE_SUITE[f["suite"]]})
    # One testcase view per testcase, even when it failed in several shards. tcc is the slow part.
    tcs = {k: resolve(store, k[1], attempt.CHRONICLE_SUITE[k[0]], create=False) for k in keys}

    def fetch(k):
        try:
            return chronicle.history_and_status(tcs[k].path, tcs[k].suite)
        except chronicle.ChronicleError as e:
            return e

    with ThreadPoolExecutor(max_workers=4) as pool:
        fetched = dict(zip(keys, pool.map(fetch, keys), strict=True))
    views = {k: view.build(store, tcs[k], fetched=fetched[k]) for k in keys}
    cache, entries = {}, []
    for f in failures:
        k = (f["suite"], f["path"])
        job = f"shard {f['suite'].removeprefix('test_')} {f['shard']}"
        stacks = stack.parse_stacks(f["message"])
        stack_text = f["message"] if stacks else None
        rules = store.job_rules()
        if k in tcs:
            rules = store.rules(tc=tcs[k]) + rules
        hits = _hits(store, rules, job=job, stack_text=stack_text, engine=f["engine_sha"],
                     engine_repo=args.engine_repo, offline=args.offline, cache=cache)
        overall = _overall(hits)
        winner = next((h for h in hits if h.verdict == overall), None)
        entry = {
            "identity": {"suite": f["suite"], "path": f["path"], "run": f["run"],
                         "attempt": f["attempt"], "shard": f["shard"],
                         "engine_sha": f["engine_sha"], "job": job},
            "verdict": overall,
            "hits": [h.as_dict() for h in hits],
            "reason": winner.reason if winner else "no rule matched",
            "stack_found": stack_text is not None,
            "history": _history_counts(views.get(k)),
        }
        if args.record and k in tcs:
            entry["record"] = _record(store, tcs[k], fetched[k], f, stack_text)
        entries.append(entry)
    counts = {}
    for e in entries:
        counts[e["verdict"]] = counts.get(e["verdict"], 0) + 1
    result = {**bundle["header"], "failure_count": len(entries), "verdicts": counts,
              "failures": entries}
    lines = [f"{result['suite']} run {result['run']} attempt {result['attempt']}: "
             f"{len(entries)} failures, " + (_counts_text(counts) or "none")]
    for e in entries:
        i, h = e["identity"], e["history"]
        past = (f"{h['failures']} failures, {h['passes']} passes"
                if h["available"] else h["reason"])
        lines.append(f"  {e['verdict']:11} shard {i['shard']} {i['path']} ({past})")
        lines.append(f"              {e['reason']}"
                     + (f" [record: {e['record']['result']}]" if "record" in e else ""))
    emit(args, result, "\n".join(lines))


def _counts_text(counts):
    return ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))


def _history_counts(v):
    if v is None:
        return {"available": False,
                "reason": "test_sql_by_cci has no Chronicle suite; history skipped"}
    h = v["history"]
    if not h["available"]:
        return {"available": False, "reason": h["reason"]}
    a = h["all_failures"]
    return {"available": True, "failures": a["count"], "passes": h["pass_count"],
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
    shard = f["shard"].lstrip("0")
    found = [o for o in found if o["run_id"] == f["run"] and o["attempt"] in (0, f["attempt"])
             and o["shard"].lstrip("0") == shard and o["outcome"] in chronicle.FAILED]
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
    order = ["fix_present", "expired", "unknown", "fix_missing", "known"]
    return min((h.verdict for h in hits), key=order.index)


def cmd_signature_add(args, store):
    tc = resolve(store, args.case, args.suite, create=True)
    frames = [f.strip() for f in args.frames.split(",") if f.strip()]
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
    found = [o for o in found if o["run_id"] == args.run and o["shard"] == str(args.shard)
             and o["outcome"] in chronicle.FAILED
             and (args.attempt is None or o["attempt"] == args.attempt)]
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


def cmd_rule_add(args, store):
    proposal = {k: getattr(args, k) for k in
                ("id", "signature", "job_contains", "issue", "expires", "not_covered")}
    if not args.yes:
        emit(args, {"result": "dry_run", "rule": proposal},
             "dry run, nothing written. Show this rule to the user, then rerun with"
             " --yes --approved-by <name>:\n" + json.dumps(proposal, indent=2))
        return
    if not args.approved_by:
        raise RegistryError("--yes needs --approved-by <name of the person who approved>")
    r = store.add_rule(args.id, sig_id=args.signature, job_contains=args.job_contains,
                       issue=args.issue, expires=args.expires, not_covered=args.not_covered,
                       approved_by=args.approved_by)
    emit(args, {"result": r}, f"rule {args.id}: {r}")


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
    for table in ("testcase", "signature", "issue_link", "rule", "note", "occurrence_signature"):
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

    global_options(p, lambda value: value)
    # The same options after the command. SUPPRESS keeps a value given before the command.
    common = argparse.ArgumentParser(add_help=False)
    global_options(common, lambda value: argparse.SUPPRESS)

    def add(subparsers, name, **kwargs):
        return subparsers.add_parser(name, parents=[common], **kwargs)

    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def case_args(sp, required=True):
        sp.add_argument("case", nargs=None if required else "?",
                        help="full testcase path, or a fragment Chronicle resolves")
        sp.add_argument("--suite", choices=sorted(SUITE_REPOSITORY))

    s = add(sub, "show", help="everything known about one testcase")
    case_args(s)
    g = s.add_mutually_exclusive_group()
    g.add_argument("--days", type=int, help="list only failures of the last N days")
    g.add_argument("--all", action="store_true", help="list every indexed failure")
    s.set_defaults(func=cmd_show)

    s = add(sub, "check", help="decide whether failures are known issues")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--case")
    g.add_argument("--job", help="CI job name, for a job-level failure")
    g.add_argument("--ci-attempt", metavar="DIR",
                   help="check every failure of one suite directory collected by cubrid-ci"
                        " (reads summary.json and failures/*; engine SHA comes from the shard)")
    s.add_argument("--suite", choices=sorted(SUITE_REPOSITORY))
    s.add_argument("--engine", help="tested Engine commit (--case and --job)")
    s.add_argument("--record", action="store_true",
                   help="with --ci-attempt, attach a signature to each occurrence that matches"
                        " exactly one; without it nothing is written")
    s.add_argument("--stack-file", help="core backtrace text")
    s.add_argument("--engine-repo", default=os.environ.get("TCR_ENGINE_REPO"),
                   help="engine clone with origin/develop (default: $TCR_ENGINE_REPO)")
    s.add_argument("--offline", action="store_true", help="skip the Jira read")
    s.add_argument("--no-context", action="store_true", help="skip the testcase view")
    s.set_defaults(func=cmd_check)

    s = add(sub, "signature", help="add or attach a failure signature")
    ss = s.add_subparsers(dest="action", required=True)
    a = add(ss, "add")
    case_args(a)
    a.add_argument("--id", required=True)
    a.add_argument("--label", required=True)
    a.add_argument("--frames", required=True, help="comma-separated, innermost first")
    a.add_argument("--text", action="append", help="substring the stack must contain")
    a.set_defaults(func=cmd_signature_add)
    a = add(ss, "attach")
    case_args(a)
    a.add_argument("--run", type=int, required=True)
    a.add_argument("--shard", required=True)
    a.add_argument("--attempt", type=int)
    a.add_argument("--stack-file", required=True)
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

    s = add(sub, "rule", help="add or retire a known-issue rule")
    ss = s.add_subparsers(dest="action", required=True)
    a = add(ss, "add", help="dry run unless --yes and --approved-by")
    a.add_argument("--id", required=True)
    g = a.add_mutually_exclusive_group(required=True)
    g.add_argument("--signature")
    g.add_argument("--job-contains")
    a.add_argument("--issue")
    a.add_argument("--expires", help="YYYY-MM-DD, required without --issue")
    a.add_argument("--not-covered", required=True)
    a.add_argument("--approved-by")
    a.add_argument("--yes", action="store_true")
    a.set_defaults(func=cmd_rule_add)
    a = add(ss, "retire")
    a.add_argument("id")
    a.add_argument("--reason", required=True)
    a.set_defaults(func=cmd_rule_retire)

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
    try:
        store = Store(args.database or default_path(), args.actor)
        args.func(args, store)
    except (RegistryError, OSError) as e:
        print(json.dumps({"error": str(e)}) if args.json else f"error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
