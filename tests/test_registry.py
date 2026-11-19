import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, date, datetime, timedelta
from io import StringIO
from pathlib import Path

from tcr import cli, stack, verdict, view
from tcr import store as store_module
from tcr.store import RegistryError, Store

FIXTURES = Path(__file__).parent / "fixtures"
CASE = "shell/_37_elderberry/cbrd_23842_cdc/bug/cbrd_27179/cases/cbrd_27179.sh"
FRAMES = ["pgbuf_fix_debug", "vacuum_heap_page", "vacuum_heap", "vacuum_process_log_block"]


class StackTest(unittest.TestCase):
    def test_real_ctp_block_parses_one_gdb_stack(self):
        stacks = stack.parse_stacks((FIXTURES / "ctp-core-cbrd_27064.txt").read_text())
        self.assertEqual(len(stacks), 1, "the gdb detail stack wins over the CTP summary")
        self.assertEqual(stacks[0][:3], ["raise", "abort", "abort_handler"])
        self.assertNotIn("<signal", stacks[0])
        self.assertTrue(stack.is_subsequence(
            ["__assert_fail", "oos_check_head_header", "oos_read", "cdc_log_extract"], stacks[0]))
        self.assertIn("cubthread::daemon::loop_with_context", stacks[0])

    def test_ctp_summary_parses_when_no_gdb_stack(self):
        text = "#0\traise\t<EXTERNAL LIBRARY>\n#1\tpgbuf_fix_debug\tsrc/storage/page_buffer.c\n"
        self.assertEqual(stack.parse_stacks(text), [["raise", "pgbuf_fix_debug"]])

    def test_signature_matches_frames_in_order_with_text(self):
        text = (FIXTURES / "reconstructed-cbrd_27179.txt").read_text()
        self.assertTrue(stack.matches(text, FRAMES, ["was_interrupted=false"]))
        self.assertFalse(stack.matches(text, list(reversed(FRAMES)), []), "order matters")
        self.assertFalse(stack.matches(text, FRAMES, ["was_interrupted=true"]))

    def test_other_crash_in_same_directory_does_not_match(self):
        text = (FIXTURES / "ctp-core-cbrd_27064.txt").read_text()
        self.assertFalse(stack.matches(text, FRAMES, []))

    def test_frames_never_match_across_two_cores(self):
        text = "#0  0x1 in vacuum_heap_page (x=1)\n#0  0x2 in pgbuf_fix_debug (y=2)\n"
        self.assertFalse(stack.matches(text, ["pgbuf_fix_debug", "vacuum_heap_page"], []))


NOK = "cbrd_21737-3 : NOK stat.log is empty\nother lines\n"


class TextSignatureTest(unittest.TestCase):
    def test_text_only_matches_when_every_text_appears(self):
        self.assertTrue(stack.matches(NOK, [], ["NOK stat.log is empty"]))
        self.assertTrue(stack.matches(NOK, [], ["NOK", "other lines"]))
        self.assertFalse(stack.matches(NOK, [], ["NOK", "absent"]))

    def test_scope_without_text_or_stack_is_unknown(self):
        text_sig = {"frames": [], "required_text": ["NOK"]}
        frames_sig = {"frames": FRAMES, "required_text": []}
        r = rule()

        def scope(text, sig):
            return verdict.scope_matches(r, job=None, stack_text=text, signature=sig)

        self.assertEqual(scope(None, text_sig), "no_stack")
        self.assertEqual(scope(NOK, text_sig), "match")
        self.assertEqual(scope("unrelated", text_sig), "miss")
        self.assertEqual(scope(NOK, frames_sig), "no_stack", "text without a parsed stack")
        self.assertEqual(scope(None, frames_sig), "no_stack")


def rule(**kw):
    base = {"id": "r", "issue": "CBRD-1", "expires": None, "not_covered": "-",
            "job_contains": None, "signature_id": "s"}
    return base | kw


OPEN = verdict.IssueFacts(True, "Open", None)
FIXED = verdict.IssueFacts(True, "Resolved", "Fixed")
TODAY = date(2026, 11, 11)


class DecideTest(unittest.TestCase):
    def v(self, r, scope="match", issue=OPEN, fix=None):
        h = verdict.decide(r, scope, TODAY, issue, fix)
        return h.verdict if h else None

    def test_table(self):
        fix_in = verdict.FixFacts(True, "ancestor")
        fix_out = verdict.FixFacts(False, "missing")
        fix_unk = verdict.FixFacts(None, "no commit")
        cases = [
            (rule(), "miss", OPEN, None, None),
            (rule(expires="2026-11-10"), "match", OPEN, None, "expired"),
            (rule(), "no_stack", OPEN, None, "unknown"),
            (rule(issue=None, expires="2026-12-09"), "match", None, None, "known"),
            (rule(), "match", OPEN, None, "known"),
            (rule(job_contains="x", signature_id=None), "match", OPEN, None, "known_job"),
            (rule(job_contains="x", signature_id=None, issue=None, expires="2026-12-09"),
             "match", None, None, "known_job"),
            (rule(), "match", verdict.IssueFacts(False, error="timeout"), None, "unknown"),
            (rule(), "match", None, None, "unknown"),
            (rule(), "match", verdict.IssueFacts(True, "Closed", "Won't Fix"), None, "unknown"),
            (rule(), "match", FIXED, fix_in, "fix_present"),
            (rule(), "match", FIXED, fix_out, "fix_missing"),
            (rule(), "match", FIXED, fix_unk, "unknown"),
        ]
        for r, scope, issue, fix, want in cases:
            with self.subTest(scope=scope, issue=issue, fix=fix, want=want):
                self.assertEqual(self.v(r, scope, issue, fix), want)

    def test_overall_prefers_analyze_as_new(self):
        hits = [verdict.Hit("a", None, "known", "", ""), verdict.Hit("b", None, "fix_present",
                                                                     "", "")]
        self.assertEqual(cli._overall(hits), "fix_present")
        self.assertEqual(cli._overall([]), "no_match")

    def test_signature_known_outranks_job_known(self):
        hits = [verdict.Hit("job", None, "known_job", "", ""),
                verdict.Hit("sig", None, "known", "", "")]
        self.assertEqual(cli._overall(hits), "known")
        self.assertEqual(cli._overall(hits[:1]), "known_job")
        self.assertEqual(cli._overall(hits[:1] + [verdict.Hit("x", None, "unknown", "", "")]),
                         "unknown")


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


class ReadFixTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        git(self.repo, "init", "-q", "-b", "develop")
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "t")

        def commit(msg):
            git(self.repo, "commit", "-q", "--allow-empty", "-m", msg)
            return git(self.repo, "rev-parse", "HEAD")

        self.commit = commit
        self.base = commit("[CBRD-2] unrelated")
        self.fix = commit("[CBRD-1] fix the vacuum page fix (#1)")
        git(self.repo, "update-ref", "refs/remotes/origin/develop", self.fix)

    def tearDown(self):
        self.tmp.cleanup()

    def test_ancestry(self):
        self.assertTrue(verdict.read_fix("CBRD-1", self.fix, str(self.repo)).present)
        self.assertIs(verdict.read_fix("CBRD-1", self.base, str(self.repo)).present, False)

    def test_cherry_pick_counts_by_subject(self):
        git(self.repo, "checkout", "-q", "-b", "feature", self.base)
        picked = self.commit("[CBRD-1] fix the vacuum page fix (#1) (cherry picked)")
        facts = verdict.read_fix("CBRD-1", picked, str(self.repo))
        self.assertTrue(facts.present)
        self.assertIn("tested history", facts.basis)

    def test_unknown_when_no_fix_or_no_commit(self):
        self.assertIsNone(verdict.read_fix("CBRD-9", self.fix, str(self.repo)).present)
        self.assertIsNone(verdict.read_fix("CBRD-1", "0" * 40, str(self.repo)).present)


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "r.sqlite3", "tester")
        with self.store.write():
            self.tc = self.store.testcase("shell", CASE, create=True)
        self.store.add_signature(self.tc, "vacuum", "vacuum fixes a freed page", FRAMES,
                                 ["was_interrupted=false"])

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_signature_needs_frames_or_text(self):
        with self.assertRaises(RegistryError):
            self.store.add_signature(self.tc, "empty", "l", [], [])
        self.assertEqual(self.store.add_signature(self.tc, "nok", "l", [], ["NOK"]), "added")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute(
                "INSERT INTO signature VALUES ('raw', ?, 'l', '[]', '[]', 'now')", (self.tc.id,))

    def test_rule_scope_and_lifetime_are_enforced(self):
        with self.assertRaises(RegistryError):
            self.store.add_rule("x", sig_id="vacuum", job_contains="sql_by_cci", issue=None,
                                expires="2026-12-09", not_covered="-", approved_by="u")
        with self.assertRaises(RegistryError):
            self.store.add_rule("x", sig_id=None, job_contains="sql_by_cci", issue=None,
                                expires=None, not_covered="-", approved_by="u")
        with self.assertRaises(RegistryError):
            self.store.add_rule("x", sig_id="vacuum", job_contains=None, issue="28018",
                                expires=None, not_covered="-", approved_by="u")

    def test_rule_add_is_idempotent_and_creates_tracks_link(self):
        kw = dict(sig_id="vacuum", job_contains=None, issue="CBRD-28018", expires=None,
                  not_covered="-", approved_by="u")
        self.assertEqual(self.store.add_rule("r1", **kw), "added")
        self.assertEqual(self.store.add_rule("r1", **kw), "unchanged")
        self.assertEqual(self.store.links(self.tc)[0]["relation"], "tracks")
        self.assertEqual(self.store.validate(), [])
        events = [e["command"] for e in self.store.events(self.tc)]
        self.assertEqual(events.count("rule add"), 1, "a repeated command adds no event")

    def test_links_and_notes_converge(self):
        self.assertEqual(self.store.link(self.tc, "CBRD-27179", "origin"), "added")
        self.assertEqual(self.store.link(self.tc, "CBRD-27179", "origin"), "unchanged")
        self.assertEqual(self.store.add_note(self.tc, "fails on #8404", []), "added")
        self.assertEqual(self.store.add_note(self.tc, "fails on #8404", []), "unchanged")
        with self.assertRaises(RegistryError):
            self.store.link(self.tc, "CBRD-1", "duplicate")

    def test_attach_rejects_signature_of_another_testcase(self):
        with self.store.write():
            other = self.store.testcase("shell", "shell/x/cases/x.sh", create=True)
        occ = {"repository": "CUBRID/cubrid", "run_id": 1, "attempt": 0, "shard": "39",
               "ordinal": 0}
        with self.assertRaises(RegistryError):
            self.store.attach(other, occ, "vacuum", "d")
        self.assertEqual(self.store.attach(self.tc, occ, "vacuum", "d"), "added")
        self.assertEqual(self.store.attach(self.tc, occ, "vacuum", "d"), "unchanged")


def failure(day, branch="develop"):
    return {"completed_at": day.strftime("%Y-%m-%dT%H:%M:%SZ"), "branch": branch,
            "signature": None}


OLD_SIGNATURE = """CREATE TABLE signature (
    id TEXT PRIMARY KEY,
    testcase_id INTEGER NOT NULL REFERENCES testcase(id),
    label TEXT NOT NULL,
    frames TEXT NOT NULL CHECK (json_array_length(frames) > 0),
    required_text TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);"""


class MigrationTest(unittest.TestCase):
    def build_old(self, path):
        schema = store_module.SCHEMA.replace(
            store_module.SIGNATURE_TABLE.replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS", 1),
            OLD_SIGNATURE.replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS", 1))
        self.assertIn("frames) > 0),", schema)
        db = sqlite3.connect(path)
        db.executescript(schema)
        db.executescript("""
            INSERT INTO testcase VALUES (1, 'r', 'shell', 'p', '');
            INSERT INTO signature VALUES ('s', 1, 'l', '["a"]', '["t"]', 'then');
            INSERT INTO occurrence_signature
                VALUES ('r', 7, 1, '3', 1, 0, 's', 'dg', 'a', 'then');
            INSERT INTO issue_link VALUES (1, 'CBRD-1', 'tracks', 's', 'a', 'then');
            INSERT INTO rule(id, signature_id, issue, not_covered, approved_by, created_at)
                VALUES ('ru', 's', 'CBRD-1', '-', 'u', 'then');
            INSERT INTO event(at, actor, command, entity, entity_key)
                VALUES ('then', 'a', 'signature add', 'signature', 's');
        """)
        db.commit()
        db.close()

    def counts(self, db):
        return {t: db.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in
                ("testcase", "signature", "occurrence_signature", "issue_link", "rule", "note",
                 "event")}

    def test_old_database_is_rebuilt_once_keeping_every_row(self):
        path = Path(tempfile.mkdtemp()) / "old.sqlite3"
        self.addCleanup(shutil.rmtree, path.parent)
        self.build_old(path)
        raw = sqlite3.connect(path)
        before = self.counts(raw)
        raw.close()
        store = Store(path, "tester")
        self.assertEqual(self.counts(store.db), before)
        self.assertEqual(store.signature("s")["frames"], ["a"])
        self.assertEqual(store.db.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(store.db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(store.validate(), [])
        tc = store.testcase("shell", "p", "r")
        self.assertEqual(store.add_signature(tc, "nok", "l", [], ["NOK"]), "added")
        events = store.db.execute("SELECT count(*) FROM event").fetchone()[0]
        store.db.close()
        again = Store(path, "tester")  # second open changes nothing
        self.assertEqual(again.db.execute("SELECT count(*) FROM event").fetchone()[0], events)
        self.assertEqual(again.signature("nok")["frames"], [])
        again.db.close()


class WindowTest(unittest.TestCase):
    NOW = datetime(2026, 11, 11, tzinfo=UTC)

    def test_unlimited_until_more_than_fifty(self):
        old = [failure(self.NOW - timedelta(days=200 + i)) for i in range(50)]
        shown, older, mode = view.window(old, days=None, show_all=False, now=self.NOW)
        self.assertEqual((len(shown), len(older), mode), (50, 0, "all"))

    def test_shortens_to_sixty_days_past_fifty(self):
        fs = [failure(self.NOW - timedelta(days=i)) for i in range(0, 102, 2)]
        shown, older, mode = view.window(fs, days=None, show_all=False, now=self.NOW)
        self.assertEqual(len(shown) + len(older), 51)
        self.assertTrue(all(f["completed_at"] >= "2026-09-12" for f in shown))
        self.assertIn("60 days", mode)
        self.assertEqual(view.window(fs, days=None, show_all=True, now=self.NOW)[2], "all")


FAKE_TCC = """#!/usr/bin/env python3
import json, sys
cmd = sys.argv[1]
case = "__CASE__"
if cmd == "find":
    print(json.dumps({"candidates": [{"repository": "CUBRID/cubrid-testcases-private-ex",
        "suite": "shell", "path": case, "variant": None}]}))
elif cmd == "history":
    occ = lambda run, out, day, base: {"repository": "CUBRID/cubrid", "run_id": run,
        "attempt": None, "shard": "39", "ordinal": 0, "outcome": out,
        "completed_at": day, "engine_sha": "cd24a212aa81", "build_mode": "debug",
        "pull_request": {"number": 8299, "base_ref": base}, "engine_branch": None}
    print(json.dumps({"occurrences": [
        occ(42868959374, "FAIL", "2026-11-10T22:37:55Z", "feature/oos-merge"),
        occ(42000000000, "PASS", "2026-11-09T22:37:55Z", "develop")]}))
elif cmd == "status":
    print(json.dumps({"status": "FAILING", "freshness": {"state": "STALE",
        "needs_refresh": True, "discovery_through": "2026-10-27T12:20:00Z"}}))
""".replace("__CASE__", CASE)


class CliBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        exe = d / "cubrid-tcc"
        exe.write_text(FAKE_TCC.replace("/usr/bin/env python3", sys.executable))
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
        (d / "engine" / ".git").mkdir(parents=True)
        self.env = {"PATH": f"{d}{os.pathsep}{os.environ['PATH']}",
                    "TCR_DB": str(d / "r.sqlite3"), "TCR_ACTOR": "tester",
                    "TCR_ENGINE_REPO": str(d / "engine")}
        self.old = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)

    def tearDown(self):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def run_cli(self, *argv):
        buf = StringIO()
        with redirect_stdout(buf):
            cli.main(["--json", *argv])
        return json.loads(buf.getvalue())


class CliTest(CliBase):
    def test_migrated_records_drive_show_and_check(self):
        stack_file = str(FIXTURES / "reconstructed-cbrd_27179.txt")
        self.run_cli("signature", "add", "cbrd_27179", "--id", "vacuum", "--label", "l",
                     "--frames", ",".join(FRAMES), "--text", "was_interrupted=false")
        dry = self.run_cli("rule", "add", "--id", "r", "--signature", "vacuum", "--issue",
                           "CBRD-28018", "--not-covered", "-")
        self.assertEqual(dry["result"], "dry_run")
        self.run_cli("rule", "add", "--id", "r", "--signature", "vacuum", "--issue",
                     "CBRD-28018", "--not-covered", "-", "--yes", "--approved-by", "user")
        self.run_cli("rule", "add", "--id", "cci", "--job-contains", "sql_by_cci",
                     "--expires", "2099-01-01", "--not-covered", "-", "--yes",
                     "--approved-by", "user")
        tagged = self.run_cli("signature", "attach", "cbrd_27179", "--run", "42868959374",
                              "--shard", "39", "--stack-file", stack_file)
        self.assertEqual(tagged["signature"], "vacuum")

        shown = self.run_cli("show", "cbrd_27179")
        h = shown["history"]
        self.assertEqual(h["all_failures"]["by_branch"], {"feature/oos-merge": 1})
        self.assertEqual(h["failures"][0]["signature"], "vacuum")
        self.assertEqual(h["pass_count"], 1)
        self.assertIn("--since 2026-10-27", h["sync_hint"])
        self.assertEqual([link["relation"] for link in shown["links"]], ["tracks"])

        offline = self.run_cli("check", "--case", "cbrd_27179", "--engine", "cd24a21",
                               "--stack-file", stack_file, "--offline", "--no-context")
        self.assertEqual(offline["verdict"], "unknown")
        other = self.run_cli("check", "--case", "cbrd_27179", "--engine", "cd24a21",
                             "--stack-file", str(FIXTURES / "ctp-core-cbrd_27064.txt"),
                             "--offline", "--no-context")
        self.assertEqual(other["verdict"], "no_match")
        job = self.run_cli("check", "--job", "shard sql_by_cci 01", "--engine", "cd24a21")
        self.assertEqual(job["verdict"], "known_job")

    def test_global_options_work_after_the_command(self):
        db = str(Path(self.tmp.name) / "other.sqlite3")
        argv = ["rule", "add", "--id", "cci", "--job-contains", "sql_by_cci",
                "--expires", "2099-01-01", "--not-covered", "-", "--yes",
                "--approved-by", "user", "--database", db, "--actor", "late"]
        with redirect_stdout(StringIO()):
            cli.main(argv)
        buf = StringIO()
        with redirect_stdout(buf):
            cli.main(["check", "--job", "shard sql_by_cci 01", "--engine", "cd24a21",
                      "--offline", "--database", db, "--json"])
        self.assertEqual(json.loads(buf.getvalue())["verdict"], "known_job")
        buf = StringIO()
        with redirect_stdout(buf):
            cli.main(["--database", db, "--json", "log"])
        self.assertEqual(json.loads(buf.getvalue())[0]["actor"], "late")
        self.assertFalse(Path(os.environ["TCR_DB"]).exists())

    def test_check_without_engine_repo_stops(self):
        os.environ.pop("TCR_ENGINE_REPO")
        with self.assertRaises(SystemExit) as stopped, redirect_stderr(StringIO()) as err:
            cli.main(["check", "--job", "shard sql_by_cci 01", "--engine", "cd24a21"])
        self.assertEqual(stopped.exception.code, 2)
        self.assertIn("TCR_ENGINE_REPO", err.getvalue())
        missing = str(Path(self.tmp.name) / "absent")
        with self.assertRaises(SystemExit), redirect_stderr(StringIO()) as err:
            cli.main(["check", "--job", "x", "--engine", "cd24a21", "--engine-repo", missing])
        self.assertIn("not a Git checkout", err.getvalue())
        offline = self.run_cli("check", "--job", "x", "--engine", "cd24a21", "--offline")
        self.assertEqual(offline["verdict"], "no_match")


CORE_CASE = "shell/_06_issues/_26_2h/cbrd_26916/cases/cbrd_26916.sh"
CORE_FRAMES = ["aj_execquery_get_exec_time", "aj_execquery_handler", "aj_thread_r"]
ATTEMPT = FIXTURES / "ci-attempt" / "test_shell"
# Every `history` call is logged, so a test can count them.
ATTEMPT_TCC = """#!/usr/bin/env python3
import json, sys
cmd, path = sys.argv[1], sys.argv[2]
if cmd == "history":
    open("__LOG__", "a").write(path + "\\n")
    occ = lambda shard, out: {"repository": "CUBRID/cubrid", "run_id": 43197154411,
        "attempt": 1, "shard": shard, "ordinal": 0, "outcome": out,
        "completed_at": "2026-11-12T22:00:00Z", "engine_sha": "fbb4f250576a",
        "build_mode": "debug", "pull_request": {"number": 8494, "base_ref": "develop"}}
    print(json.dumps({"occurrences": [occ("15", "ERROR"), occ("17", "ERROR")]}))
elif cmd == "status":
    print(json.dumps({"status": "FAILING", "freshness": {"state": "FRESH"}}))
"""


class CiAttemptTest(CliBase):
    def setUp(self):
        super().setUp()
        d = Path(self.tmp.name)
        self.log = d / "history.log"
        exe = d / "cubrid-tcc"
        exe.write_text(ATTEMPT_TCC.replace("__LOG__", str(self.log))
                       .replace("/usr/bin/env python3", sys.executable))
        self.attempt = d / "test_shell"
        shutil.copytree(ATTEMPT, self.attempt)

    def calls(self):
        return self.log.read_text().split() if self.log.exists() else []

    def by_path(self, out):
        return {f["identity"]["path"]: f for f in out["failures"]}

    def test_every_failure_is_checked_with_case_and_job_rules(self):
        self.run_cli("signature", "add", CORE_CASE, "--id", "manager-core", "--label", "l",
                     "--frames", ",".join(CORE_FRAMES))
        for args in (["--id", "r", "--signature", "manager-core", "--expires", "2099-01-01"],
                     ["--id", "job", "--job-contains", "shard shell 17",
                      "--expires", "2099-01-01"]):
            self.run_cli("rule", "add", *args, "--not-covered", "-", "--yes",
                         "--approved-by", "user")
        out = self.run_cli("check", "--ci-attempt", str(self.attempt), "--offline")
        self.assertEqual((out["suite"], out["run"], out["attempt"], out["failure_count"]),
                         ("test_shell", 43197154411, 1, 3))
        self.assertEqual(out["verdicts"], {"known": 1, "known_job": 1, "no_match": 1})
        core = self.by_path(out)[CORE_CASE]
        self.assertEqual(core["identity"], {
            "suite": "test_shell", "path": CORE_CASE, "run": 43197154411, "attempt": 1,
            "shard": "15", "job": "shard shell 15",
            "engine_sha": "fbb4f250576a1d39a5202ab2b5ddb9965560d6f1"})
        self.assertEqual((core["verdict"], core["stack_found"]), ("known", True))
        self.assertEqual(core["hits"][0]["rule_id"], "r")
        self.assertEqual(core["history"]["failures"], 2)
        jobbed = self.by_path(out)["shell/_06_issues/_26_2h/cbrd_26861/cases/cbrd_26861.sh"]
        self.assertEqual((jobbed["verdict"], jobbed["hits"][0]["rule_id"]), ("known_job", "job"))
        self.assertFalse(jobbed["stack_found"])
        self.assertTrue(all("record" not in f for f in out["failures"]))

    def test_history_is_read_once_per_testcase(self):
        data = json.loads((self.attempt / "summary.json").read_text())
        twin = dict(data["failures"][1], shard="17", stable_id="twin")
        data["failures"].append(twin)
        data["shards"].append(dict(data["shards"][0], index="17"))
        (self.attempt / "summary.json").write_text(json.dumps(data))
        folder = self.attempt / "failures" / "twin"
        shutil.copytree(self.attempt / "failures" / data["failures"][1]["stable_id"], folder)
        meta = json.loads((folder / "metadata.json").read_text())
        (folder / "metadata.json").write_text(json.dumps(meta | {"shard": "17"}))
        out = self.run_cli("check", "--ci-attempt", str(self.attempt), "--offline")
        self.assertEqual(out["failure_count"], 4)
        self.assertEqual(sorted(self.calls()), sorted({f["identity"]["path"]
                                                       for f in out["failures"]}))

    def test_record_attaches_only_the_single_match(self):
        self.run_cli("signature", "add", CORE_CASE, "--id", "manager-core", "--label", "l",
                     "--frames", ",".join(CORE_FRAMES))
        argv = ["check", "--ci-attempt", str(self.attempt), "--offline"]
        self.run_cli(*argv)
        self.assertEqual(self.run_cli("log")[0]["command"], "signature add")
        out = self.run_cli(*argv, "--record")
        recs = {f["identity"]["shard"]: f["record"] for f in out["failures"]}
        self.assertEqual(recs["15"], {"result": "added", "signature": "manager-core"})
        self.assertEqual(recs["17"]["result"], "no_single_match")
        again = self.by_path(self.run_cli(*argv, "--record"))[CORE_CASE]
        self.assertEqual(again["record"]["result"], "unchanged")
        self.assertEqual(len([e for e in self.run_cli("log") if e["command"] ==
                              "signature attach"]), 1)
        shown = self.run_cli("show", CORE_CASE)
        self.assertEqual(shown["history"]["all_failures"]["by_signature"],
                         {"manager-core": 1, "unknown": 1})

    def test_text_signature_matches_ci_attempt_without_a_stack(self):
        case = "shell/_06_issues/_26_2h/cbrd_26861/cases/cbrd_26861.sh"  # message has no stack
        for sig, extra in (("nok", ["--text", "cbrd_26861-1 : NOK"]),
                           ("frames", ["--frames", "no_such_frame"])):
            self.run_cli("signature", "add", case, "--id", sig, "--label", "l", *extra)
            self.run_cli("rule", "add", "--id", f"r-{sig}", "--signature", sig,
                         "--expires", "2099-01-01", "--not-covered", "-", "--yes",
                         "--approved-by", "user")
        out = self.run_cli("check", "--ci-attempt", str(self.attempt), "--offline", "--record")
        f = self.by_path(out)[case]
        by_rule = {h["rule_id"]: h["verdict"] for h in f["hits"]}
        self.assertEqual(by_rule, {"r-nok": "known", "r-frames": "unknown"})
        self.assertEqual((f["verdict"], f["stack_found"]), ("unknown", False))
        # The frames signature cannot match without a stack, so the text one is the single match.
        self.assertEqual(f["record"], {"result": "added", "signature": "nok"})
        self.assertEqual(self.by_path(out)[CORE_CASE]["verdict"], "no_match")

    def test_text_only_check_with_stack_file_and_no_text(self):
        self.run_cli("signature", "add", CORE_CASE, "--id", "nok", "--label", "l",
                     "--text", "NOK stat.log")
        self.run_cli("rule", "add", "--id", "r", "--signature", "nok", "--expires", "2099-01-01",
                     "--not-covered", "-", "--yes", "--approved-by", "user")
        base = ["check", "--case", CORE_CASE, "--engine", "e", "--offline", "--no-context"]
        self.assertEqual(self.run_cli(*base)["verdict"], "unknown")
        text = Path(self.tmp.name) / "m.txt"
        text.write_text(NOK)
        self.assertEqual(self.run_cli(*base, "--stack-file", str(text))["verdict"], "known")
        text.write_text("something else")
        self.assertEqual(self.run_cli(*base, "--stack-file", str(text))["verdict"], "no_match")
        self.assertRaises(SystemExit, cli.main, ["signature", "add", CORE_CASE, "--id", "x",
                                                  "--label", "l"])

    def test_by_cci_has_no_history_and_uses_job_rules(self):
        self.run_cli("rule", "add", "--id", "cci", "--job-contains", "shard sql_by_cci 15",
                     "--expires", "2099-01-01", "--not-covered", "-", "--yes",
                     "--approved-by", "user")
        data = json.loads((self.attempt / "summary.json").read_text())
        (self.attempt / "summary.json").write_text(
            json.dumps(data | {"suite": "test_sql_by_cci"}))
        out = self.run_cli("check", "--ci-attempt", str(self.attempt), "--offline", "--record")
        self.assertEqual(self.calls(), [])
        core = self.by_path(out)[CORE_CASE]
        self.assertEqual((core["verdict"], core["hits"][0]["rule_id"]), ("known_job", "cci"))
        self.assertFalse(core["history"]["available"])
        self.assertIn("no Chronicle suite", core["history"]["reason"])
        self.assertNotIn("record", core)

    def test_unknown_schema_version_exits_2(self):
        for name in ("summary.json", "failures/*/metadata.json"):
            shutil.rmtree(self.attempt)
            shutil.copytree(ATTEMPT, self.attempt)
            target = next(self.attempt.glob(name))
            data = json.loads(target.read_text()) | {"schema_version": 4}
            target.write_text(json.dumps(data))
            with self.assertRaises(SystemExit) as stopped, redirect_stderr(StringIO()) as err:
                cli.main(["check", "--ci-attempt", str(self.attempt), "--offline"])
            self.assertEqual(stopped.exception.code, 2)
            self.assertIn("schema_version 4", err.getvalue())

    def rerun_summary(self, mode, **changes):
        target = self.attempt / "summary.json"
        source = {"mode": mode, "run_id": 1, "attempt": 1,
                  "summary": "providers/github-actions/runs/1/attempts/1/test_shell/summary.json"}
        data = json.loads(target.read_text()) | {"schema_version": 3, "rerun": source} | changes
        target.write_text(json.dumps(data))
        return source

    def test_partial_rerun_summary_is_checked(self):
        source = self.rerun_summary("partial")
        out = self.run_cli("check", "--ci-attempt", str(self.attempt), "--offline")
        self.assertEqual(out["rerun"], source)
        self.assertEqual(out["failure_count"], 3)

    def test_carried_forward_rerun_points_at_its_source(self):
        source = self.rerun_summary("carried_forward", shards=[], failures=[])
        out = self.run_cli("check", "--ci-attempt", str(self.attempt), "--offline")
        self.assertEqual((out["failure_count"], out["rerun"]), (0, source))
        self.assertEqual(self.calls(), [])

    def test_options_are_validated(self):
        for argv in (["--case", "x", "--offline", "--record"], ["--case", "x", "--offline"]):
            with self.assertRaises(SystemExit) as stopped, redirect_stderr(StringIO()):
                cli.main(["check", *argv])
            self.assertEqual(stopped.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
