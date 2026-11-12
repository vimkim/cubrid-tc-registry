"""cubrid-tcr: per-testcase knowledge for CUBRID CI analysis."""

import argparse
import getpass
import json
import os
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from . import __version__, chronicle, stack, verdict, view
from .store import RELATIONS, SUITE_REPOSITORY, RegistryError, Store, Testcase, default_path

EPILOG = """\
examples:
  cubrid-tcr show cbrd_27179
  cubrid-tcr check --case cbrd_27179 --suite shell --engine cd24a21 --stack-file core.txt
  cubrid-tcr check --job "shard sql_by_cci 01" --engine cd24a21
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


def cmd_check(args, store):
    stack_text = Path(args.stack_file).read_text(errors="replace") if args.stack_file else None
    if not args.offline:
        _require_engine_repo(args.engine_repo)
    tc = None
    if args.case:
        tc = resolve(store, args.case, args.suite, create=False)
        rules = store.rules(tc=tc)
    else:
        rules = store.job_rules()
    today = date.today()
    hits, issue_cache = [], {}
    for rule in rules:
        sig = store.signature(rule["signature_id"]) if rule["signature_id"] else None
        scope = verdict.scope_matches(rule, job=args.job, stack_text=stack_text, signature=sig)
        issue = fix = None
        if scope == "match" and rule["issue"]:
            if not args.offline:
                if rule["issue"] not in issue_cache:
                    issue_cache[rule["issue"]] = verdict.read_issue(rule["issue"])
                issue = issue_cache[rule["issue"]]
            if issue and issue.resolution == "Fixed":
                fix = verdict.read_fix(rule["issue"], args.engine, args.engine_repo)
        h = verdict.decide(rule, scope, today, issue, fix)
        if h:
            hits.append(h)
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
    p.add_argument("--database", type=Path,
                   help=f"SQLite path (default {default_path()}, env TCR_DB)")
    p.add_argument("--actor", default=os.environ.get("TCR_ACTOR") or getpass.getuser(),
                   help="who writes, recorded in the event log (env TCR_ACTOR)")
    p.add_argument("--json", action="store_true", help="print JSON")
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def case_args(sp, required=True):
        sp.add_argument("case", nargs=None if required else "?",
                        help="full testcase path, or a fragment Chronicle resolves")
        sp.add_argument("--suite", choices=sorted(SUITE_REPOSITORY))

    s = sub.add_parser("show", help="everything known about one testcase")
    case_args(s)
    g = s.add_mutually_exclusive_group()
    g.add_argument("--days", type=int, help="list only failures of the last N days")
    g.add_argument("--all", action="store_true", help="list every indexed failure")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("check", help="decide whether one failure is a known issue")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--case")
    g.add_argument("--job", help="CI job name, for a job-level failure")
    s.add_argument("--suite", choices=sorted(SUITE_REPOSITORY))
    s.add_argument("--engine", required=True, help="tested Engine commit")
    s.add_argument("--stack-file", help="core backtrace text")
    s.add_argument("--engine-repo", default=os.environ.get("TCR_ENGINE_REPO"),
                   help="engine clone with origin/develop (default: $TCR_ENGINE_REPO)")
    s.add_argument("--offline", action="store_true", help="skip the Jira read")
    s.add_argument("--no-context", action="store_true", help="skip the testcase view")
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("signature", help="add or attach a failure signature")
    ss = s.add_subparsers(dest="action", required=True)
    a = ss.add_parser("add")
    case_args(a)
    a.add_argument("--id", required=True)
    a.add_argument("--label", required=True)
    a.add_argument("--frames", required=True, help="comma-separated, innermost first")
    a.add_argument("--text", action="append", help="substring the stack must contain")
    a.set_defaults(func=cmd_signature_add)
    a = ss.add_parser("attach")
    case_args(a)
    a.add_argument("--run", type=int, required=True)
    a.add_argument("--shard", required=True)
    a.add_argument("--attempt", type=int)
    a.add_argument("--stack-file", required=True)
    a.add_argument("--signature")
    a.set_defaults(func=cmd_signature_attach)

    s = sub.add_parser("link", help="relate a testcase to an issue")
    case_args(s)
    s.add_argument("--issue", required=True)
    s.add_argument("--relation", required=True, choices=RELATIONS)
    s.add_argument("--signature")
    s.set_defaults(func=cmd_link)

    s = sub.add_parser("note", help="leave a note on a testcase")
    case_args(s)
    s.add_argument("--body", required=True)
    s.add_argument("--ref", action="append", help="a PR, run, issue, or document reference")
    s.set_defaults(func=cmd_note)

    s = sub.add_parser("rule", help="add or retire a known-issue rule")
    ss = s.add_subparsers(dest="action", required=True)
    a = ss.add_parser("add", help="dry run unless --yes and --approved-by")
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
    a = ss.add_parser("retire")
    a.add_argument("id")
    a.add_argument("--reason", required=True)
    a.set_defaults(func=cmd_rule_retire)

    s = sub.add_parser("validate", help="check cross-record invariants")
    s.set_defaults(func=cmd_validate)
    s = sub.add_parser("log", help="show the write history")
    case_args(s, required=False)
    s.add_argument("--limit", type=int, default=50)
    s.set_defaults(func=cmd_log)
    s = sub.add_parser("backup", help="save a consistent SQLite snapshot")
    s.add_argument("--out")
    s.set_defaults(func=cmd_backup)
    s = sub.add_parser("export", help="print every record as TOML for review")
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
