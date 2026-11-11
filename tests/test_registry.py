import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import UTC, date, datetime, timedelta
from io import StringIO
from pathlib import Path

from tcr import cli, stack, verdict, view
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


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        exe = d / "cubrid-tcc"
        exe.write_text(FAKE_TCC.replace("/usr/bin/env python3", sys.executable))
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
        self.env = {"PATH": f"{d}{os.pathsep}{os.environ['PATH']}",
                    "TCR_DB": str(d / "r.sqlite3"), "TCR_ACTOR": "tester"}
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
        self.assertEqual(job["verdict"], "known")


if __name__ == "__main__":
    unittest.main()
