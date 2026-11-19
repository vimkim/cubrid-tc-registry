"""The cubrid-tcc contract: schema versions, error forms, exit 3, timeout, null attempt."""

import json
import os
import sqlite3
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

import test_registry

from tcr import chronicle, cli

# A fake cubrid-tcc that replays FAKE_OUT, FAKE_ERR and FAKE_RC from the environment.
FAKE = """#!PY
import os, sys, time
time.sleep(float(os.environ.get("FAKE_SLEEP", "0")))
sys.stdout.write(os.environ.get("FAKE_OUT", ""))
sys.stderr.write(os.environ.get("FAKE_ERR", ""))
sys.exit(int(os.environ.get("FAKE_RC", "0")))
"""

GOOD = {
    "find": {"schema_version": 2, "candidates": []},
    "history": {"schema_version": 2, "occurrences": []},
    "explain": {"schema_version": 2},
    "status": {"schema_version": 1, "status": "FAILING", "freshness": {}},
    "coverage": {"schema_version": 1},
}
CANDIDATES = [{"suite": "shell", "path": f"shell/a{i}/cases/a{i}.sh"} for i in range(8)]


class FakeTcc(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        exe = Path(tmp.name, "cubrid-tcc")
        exe.write_text(FAKE.replace("#!PY", f"#!{sys.executable}"))
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
        patch = mock.patch.dict(os.environ, {"PATH": f"{tmp.name}{os.pathsep}{os.environ['PATH']}"})
        patch.start()
        self.addCleanup(patch.stop)

    def tcc(self, cmd, out=None, err="", rc=0, **env):
        env = {"FAKE_OUT": json.dumps(out) if out is not None else "", "FAKE_ERR": err,
               "FAKE_RC": str(rc), **env}
        with mock.patch.dict(os.environ, env):
            return chronicle._run(cmd, "x")


class SchemaTest(FakeTcc):
    def test_each_command_accepts_its_version_and_refuses_any_other(self):
        for cmd, doc in GOOD.items():
            with self.subTest(cmd=cmd):
                self.assertEqual(self.tcc(cmd, doc), doc)
                for bad in (doc["schema_version"] + 1, None):
                    with self.assertRaisesRegex(chronicle.ChronicleError, "schema_version"):
                        self.tcc(cmd, {**doc, "schema_version": bad})

    def test_missing_required_key_is_an_error_not_a_default(self):
        with self.assertRaisesRegex(chronicle.ChronicleError, "lacks occurrences"):
            self.tcc("history", {"schema_version": 2})


class ErrorTest(FakeTcc):
    def test_error_document_on_stdout(self):
        doc = {"schema_version": 1, "error": "operational_error", "message": "index locked"}
        with self.assertRaisesRegex(chronicle.ChronicleError, "index locked"):
            self.tcc("history", doc, rc=2)

    def test_error_document_on_stderr(self):
        err = json.dumps({"schema_version": 1, "error": "operational_error", "message": "boom"})
        with self.assertRaisesRegex(chronicle.ChronicleError, "boom"):
            self.tcc("history", err=err, rc=2)

    def test_plain_text_failure(self):
        with self.assertRaisesRegex(chronicle.ChronicleError, "exited 1: oops"):
            self.tcc("status", err="oops", rc=1)

    def test_exit_3_on_stdout_names_the_first_candidates(self):
        doc = {"schema_version": 1, "error": "ambiguous_identity", "message": "m",
               "candidates": CANDIDATES, "candidate_count": 40}
        with self.assertRaises(chronicle.AmbiguousTestcase) as ctx:
            self.tcc("find", doc, rc=3)
        text = str(ctx.exception)
        self.assertIn("40 candidates", text)
        self.assertIn("shell/a4/cases/a4.sh", text)
        self.assertNotIn("shell/a5/cases/a5.sh", text)

    def test_exit_3_on_stderr(self):
        err = json.dumps({"schema_version": 1, "error": "ambiguous_identity",
                          "candidates": CANDIDATES[:2]})
        with self.assertRaisesRegex(chronicle.AmbiguousTestcase, "2 candidates"):
            self.tcc("history", err=err, rc=3)

    def test_timeout(self):
        with mock.patch.object(chronicle, "TIMEOUT", 0.2):
            with self.assertRaisesRegex(chronicle.ChronicleError, "timed out"):
                self.tcc("status", GOOD["status"], FAKE_SLEEP="5")


class ViewTest(test_registry.CliBase):
    def fake(self, history, status=None):
        status = status or {"schema_version": 1, "status": "FAILING", "freshness": {}}
        find = {"schema_version": 2, "candidates": [{
            "repository": "CUBRID/cubrid-testcases-private-ex", "suite": "shell",
            "path": test_registry.CASE, "variant": None}]}
        exe = Path(self.tmp.name, "cubrid-tcc")
        exe.write_text(f"""#!{sys.executable}
import json, sys
docs = {{"find": {find!r}, "history": {history!r}, "status": {status!r}}}
print(json.dumps(docs[sys.argv[1]]))
""")

    @staticmethod
    def occ(outcome, evidence, attempt=None):
        return {"repository": "CUBRID/cubrid", "run_id": 1, "attempt": attempt, "shard": "3",
                "ordinal": 0, "outcome": outcome, "completed_at": "2026-11-10T22:37:55Z",
                "engine_sha": "cd24a212aa81", "build_mode": "debug", "evidence": evidence,
                "pull_request": {"number": 1, "base_ref": "develop"}}

    def show_text(self):
        buf = StringIO()
        with redirect_stdout(buf):
            cli.main(["show", "cbrd_27179"])
        return buf.getvalue()

    def test_failure_list_evidence_says_passes_are_not_recorded(self):
        self.fake({"schema_version": 2, "occurrences": [self.occ("FAIL", "failed.list")]})
        h = self.run_cli("show", "cbrd_27179")["history"]
        self.assertIsNone(h["pass_count"])
        self.assertIn("1 failures and passes not recorded (failure-list evidence),",
                      self.show_text())

    def test_inventory_evidence_still_counts_passes(self):
        xml = "test-results/test-shell.xml"
        self.fake({"schema_version": 2, "occurrences": [
            self.occ("FAIL", "failed.list"), self.occ("PASS", xml), self.occ("PASS", xml)]})
        self.assertEqual(self.run_cli("show", "cbrd_27179")["history"]["pass_count"], 2)
        self.assertIn("2 passes indexed", self.show_text())

    def test_unresolved_attempt_stays_null(self):
        self.fake({"schema_version": 2, "occurrences": [self.occ("FAIL", "failed.list")]})
        h = self.run_cli("show", "cbrd_27179")["history"]
        self.assertIsNone(h["failures"][0]["attempt"])
        self.assertIn("attempt null (unresolved)", self.show_text())

    def test_unresolved_attempt_is_zero_only_in_the_signature_key(self):
        self.fake({"schema_version": 2, "occurrences": [self.occ("FAIL", "failed.list")]})
        self.run_cli("signature", "add", "cbrd_27179", "--id", "s", "--label", "l",
                     "--frames", ",".join(test_registry.FRAMES))
        stack_file = test_registry.FIXTURES / "reconstructed-cbrd_27179.txt"
        out = self.run_cli("signature", "attach", "cbrd_27179", "--run", "1", "--shard", "3",
                           "--stack-file", str(stack_file))
        self.assertIsNone(out["occurrence"]["attempt"])
        self.assertEqual(out["result"], "added")
        with sqlite3.connect(os.environ["TCR_DB"]) as db:
            self.assertEqual(db.execute("SELECT attempt FROM occurrence_signature").fetchall(),
                             [(0,)])

    def test_unknown_schema_version_reports_history_unavailable(self):
        self.fake({"schema_version": 3, "occurrences": []})
        h = self.run_cli("show", "cbrd_27179")["history"]
        self.assertFalse(h["available"])
        self.assertIn("schema_version 3, expected 2", h["reason"])
        self.assertIn("history: unavailable", self.show_text())


if __name__ == "__main__":
    unittest.main()
