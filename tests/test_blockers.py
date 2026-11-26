"""Tests for first-class blockers, readiness state machine, live lookups, and unblock."""

import io
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from unittest import mock

import test_registry

from tcr import cli, live, readiness, render, verdict
from tcr.store import RegistryError, Store, parse_pr_ref

V1_SCHEMA = """
CREATE TABLE testcase (
    id INTEGER PRIMARY KEY,
    repository TEXT NOT NULL,
    suite TEXT NOT NULL CHECK (suite IN ('sql', 'medium', 'shell')),
    path TEXT NOT NULL,
    variant TEXT NOT NULL DEFAULT '',
    UNIQUE (repository, suite, path, variant)
);
CREATE TABLE signature (
    id TEXT PRIMARY KEY,
    testcase_id INTEGER NOT NULL REFERENCES testcase(id),
    label TEXT NOT NULL,
    frames TEXT NOT NULL,
    required_text TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    CHECK (json_array_length(frames) > 0 OR json_array_length(required_text) > 0)
);
CREATE TABLE occurrence_signature (
    repository TEXT NOT NULL,
    run_id INTEGER NOT NULL,
    attempt INTEGER NOT NULL,
    shard TEXT NOT NULL,
    testcase_id INTEGER NOT NULL REFERENCES testcase(id),
    ordinal INTEGER NOT NULL,
    signature_id TEXT NOT NULL REFERENCES signature(id),
    stack_digest TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (repository, run_id, attempt, shard, testcase_id, ordinal)
);
CREATE TABLE issue_link (
    testcase_id INTEGER NOT NULL REFERENCES testcase(id),
    issue TEXT NOT NULL,
    relation TEXT NOT NULL CHECK (relation IN ('origin', 'tracks', 'suspected', 'fixed_by')),
    signature_id TEXT REFERENCES signature(id),
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (testcase_id, issue, relation)
);
CREATE TABLE note (
    id INTEGER PRIMARY KEY,
    testcase_id INTEGER NOT NULL REFERENCES testcase(id),
    body TEXT NOT NULL,
    refs TEXT NOT NULL DEFAULT '[]',
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    retired_at TEXT,
    retired_reason TEXT
);
CREATE TABLE rule (
    id TEXT PRIMARY KEY,
    signature_id TEXT REFERENCES signature(id),
    job_contains TEXT,
    issue TEXT,
    expires TEXT,
    not_covered TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    retired_at TEXT,
    retired_reason TEXT,
    CHECK ((signature_id IS NULL) <> (job_contains IS NULL)),
    CHECK (issue IS NOT NULL OR expires IS NOT NULL)
);
CREATE TABLE event (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    command TEXT NOT NULL,
    entity TEXT NOT NULL,
    entity_key TEXT NOT NULL,
    before TEXT,
    after TEXT
);
"""


def pr_facts(
    repo: str,
    number: int,
    *,
    state: str = "OPEN",
    merged: bool = False,
    base_ref: str = "develop",
    head_ref: str = "topic",
    author: str = "dev",
    merge_commit_sha: str | None = None,
    is_draft: bool = False,
    readable: bool = True,
    error: str | None = None,
) -> live.PrFacts:
    return live.PrFacts(
        repository=repo,
        number=number,
        readable=readable,
        state="MERGED" if merged else state,
        is_draft=is_draft,
        merged=merged,
        merged_at="2026-11-26T00:00:00Z" if merged else None,
        merge_commit_sha=merge_commit_sha or ("f" * 40 if merged else None),
        base_ref=base_ref,
        head_ref=head_ref,
        author=author,
        title=f"PR #{number}",
        url=f"https://github.com/{repo}/pull/{number}",
        error=error,
    )


class PrRefTest(unittest.TestCase):
    def test_shorthand_and_urls(self):
        self.assertEqual(parse_pr_ref("8537"), ("CUBRID/cubrid", 8537))
        self.assertEqual(parse_pr_ref("#8537"), ("CUBRID/cubrid", 8537))
        self.assertEqual(parse_pr_ref("engine#8693"), ("CUBRID/cubrid", 8693))
        self.assertEqual(parse_pr_ref("tc#4474"), ("CUBRID/cubrid-testcases", 4474))
        self.assertEqual(
            parse_pr_ref("tc-private#5098"),
            ("CUBRID/cubrid-testcases-private-ex", 5098),
        )
        self.assertEqual(
            parse_pr_ref("https://github.com/CUBRID/cubrid-testcases/pull/4475/files"),
            ("CUBRID/cubrid-testcases", 4475),
        )
        with self.assertRaises(RegistryError):
            parse_pr_ref("unknown#12")
        with self.assertRaises(RegistryError):
            parse_pr_ref("engine#0")


class V1ToV2MigrationTest(unittest.TestCase):
    def test_v1_migrates_cleanly_and_fixes_issue_link_multi_sig(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = Path(tmp.name) / "v1.sqlite3"
        raw = sqlite3.connect(db_path)
        raw.executescript(V1_SCHEMA)
        raw.executescript("""
            INSERT INTO testcase VALUES
              (5, 'CUBRID/cubrid-testcases-private-ex', 'shell',
               'shell/_06_issues/_26_2h/cbrd_27780/cases/cbrd_27780.sh', '');
            INSERT INTO signature VALUES
              ('sig-a', 5, 'leak a', '[]', '["chk2"]', 't'),
              ('sig-b', 5, 'leak b', '[]', '["chk4"]', 't');
            INSERT INTO issue_link VALUES
              (5, 'CBRD-27780', 'origin', NULL, 'vimkim', 't'),
              (5, 'CBRD-28257', 'tracks', 'sig-a', 'vimkim', 't');
            INSERT INTO rule(id, signature_id, issue, not_covered, approved_by, created_at)
              VALUES ('r-a', 'sig-a', 'CBRD-28257', '-', 'DK', 't');
        """)
        raw.commit()
        raw.close()

        store = Store(db_path, "tester")
        self.addCleanup(store.db.close)
        self.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertEqual(store.db.execute("PRAGMA foreign_key_check").fetchall(), [])
        tc = store.testcase(
            "shell", "shell/_06_issues/_26_2h/cbrd_27780/cases/cbrd_27780.sh"
        )
        # Linking a second signature to CBRD-28257 no longer overwrites sig-a.
        self.assertEqual(store.link(tc, "CBRD-28257", "tracks", "sig-b"), "added")
        tracks_sigs = {
            row["signature_id"]
            for row in store.links(tc)
            if row["issue"] == "CBRD-28257" and row["relation"] == "tracks"
        }
        self.assertEqual(tracks_sigs, {"sig-a", "sig-b"})
        self.assertEqual(store.validate(), [])


class ReadinessStateMachineTest(unittest.TestCase):
    def _blocker(self, *, prs=(), fix_issue=None, owner=None, culprit_author=None):
        return {
            "id": "b1",
            "title": "test blocker",
            "culprit_repo": "CUBRID/cubrid",
            "culprit_commit": "e74826c46c",
            "culprit_pr": 8537,
            "culprit_issue": "CBRD-27562",
            "culprit_author": culprit_author,
            "culprit_confidence": "confirmed",
            "culprit_note": "",
            "fix_issue": fix_issue,
            "owner": owner,
            "prs": list(prs),
            "signatures": [],
            "rules": [],
            "retired_at": None,
        }

    def test_all_six_readiness_states_and_owner_precedence(self):
        # 1. READY (TC-only) + fix_pr owner fallback over culprit_author
        b = self._blocker(
            prs=[{"repository": "CUBRID/cubrid-testcases", "pr_number": 4474,
                  "target_branch": "develop", "required": 1, "retired_at": None}],
            culprit_author="vimkim",
        )
        ev = readiness.evaluate_blocker(
            b,
            pr_facts_map={
                ("CUBRID/cubrid-testcases", 4474): pr_facts(
                    "CUBRID/cubrid-testcases", 4474, merged=True, author="ssihil"
                )
            },
        )
        self.assertEqual(ev["readiness"], "READY")
        self.assertEqual(ev["unblock_action"], "RERUN_OR_SYNC_TC")
        self.assertEqual((ev["owner"], ev["owner_source"]), ("ssihil", "fix_pr"))

        # 2. READY (engine + optional open TC PR ignored) + JIRA assignee wins + drift flag
        b2 = self._blocker(
            fix_issue="CBRD-28257",
            prs=[
                {"repository": "CUBRID/cubrid", "pr_number": 8693,
                 "target_branch": "develop", "required": 1, "retired_at": None},
                {"repository": "CUBRID/cubrid-testcases-private-ex", "pr_number": 5098,
                 "target_branch": "develop", "required": 0, "retired_at": None},
            ],
            owner="local-owner",
        )
        ev2 = readiness.evaluate_blocker(
            b2,
            issue_facts=verdict.IssueFacts(True, "Develop", None, assignee="Yeunjun Lee"),
            pr_facts_map={
                ("CUBRID/cubrid", 8693): pr_facts(
                    "CUBRID/cubrid", 8693, merged=True, author="YeunjunLee"
                ),
                ("CUBRID/cubrid-testcases-private-ex", 5098): pr_facts(
                    "CUBRID/cubrid-testcases-private-ex", 5098, state="OPEN",
                    author="cubrid-tc-sync-bot"
                ),
            },
        )
        self.assertEqual(ev2["readiness"], "READY")
        self.assertEqual(ev2["unblock_action"], "REBASE_ENGINE")
        self.assertEqual((ev2["owner"], ev2["owner_source"]), ("Yeunjun Lee", "jira"))
        self.assertTrue(any("still Develop/Unresolved" in f for f in ev2["flags"]))

        # 3. PARTIAL (engine merged, required TC still open)
        b3 = self._blocker(
            prs=[
                {"repository": "CUBRID/cubrid", "pr_number": 1,
                 "target_branch": "develop", "required": 1, "retired_at": None},
                {"repository": "CUBRID/cubrid-testcases", "pr_number": 2,
                 "target_branch": "develop", "required": 1, "retired_at": None},
            ]
        )
        ev3 = readiness.evaluate_blocker(
            b3,
            pr_facts_map={
                ("CUBRID/cubrid", 1): pr_facts("CUBRID/cubrid", 1, merged=True),
                ("CUBRID/cubrid-testcases", 2): pr_facts(
                    "CUBRID/cubrid-testcases", 2, state="OPEN"
                ),
            },
        )
        self.assertEqual(ev3["readiness"], "PARTIAL")

        # 4. IN_PROGRESS (bot author skipped -> falls back to local owner / culprit)
        b4 = self._blocker(
            prs=[{"repository": "CUBRID/cubrid-testcases-private-ex", "pr_number": 5098,
                  "target_branch": "develop", "required": 1, "retired_at": None}],
            owner="fallback-dev",
            culprit_author="hornetmj",
        )
        ev4 = readiness.evaluate_blocker(
            b4,
            pr_facts_map={
                ("CUBRID/cubrid-testcases-private-ex", 5098): pr_facts(
                    "CUBRID/cubrid-testcases-private-ex", 5098, state="OPEN",
                    author="cubrid-tc-sync-bot"
                ),
            },
        )
        self.assertEqual(ev4["readiness"], "IN_PROGRESS")
        self.assertEqual((ev4["owner"], ev4["owner_source"]), ("fallback-dev", "local"))

        # 5. BRANCH_MISMATCH
        ev5 = readiness.evaluate_blocker(
            self._blocker(
                prs=[{"repository": "CUBRID/cubrid", "pr_number": 9,
                      "target_branch": "develop", "required": 1, "retired_at": None}]
            ),
            pr_facts_map={
                ("CUBRID/cubrid", 9): pr_facts(
                    "CUBRID/cubrid", 9, merged=True, base_ref="feature/oos"
                )
            },
        )
        self.assertEqual(ev5["readiness"], "BRANCH_MISMATCH")

        # 6. NEEDS_FIX_PR and UNTRACKED + culprit fallback
        ev6 = readiness.evaluate_blocker(
            self._blocker(fix_issue="CBRD-28395", culprit_author="hornetmj"),
            issue_facts=verdict.IssueFacts(True, "Confirmed", None, assignee="Joon Min"),
        )
        self.assertEqual(ev6["readiness"], "NEEDS_FIX_PR")
        self.assertEqual((ev6["owner"], ev6["owner_source"]), ("Joon Min", "jira"))

        ev7 = readiness.evaluate_blocker(self._blocker(culprit_author="hornetmj"))
        self.assertEqual(ev7["readiness"], "UNTRACKED")
        self.assertEqual((ev7["owner"], ev7["owner_source"]), ("hornetmj", "culprit"))

        # Both engine + TC required PRs merged -> REBASE_ENGINE_AND_SYNC_TC
        ev8 = readiness.evaluate_blocker(
            b3,
            pr_facts_map={
                ("CUBRID/cubrid", 1): pr_facts("CUBRID/cubrid", 1, merged=True),
                ("CUBRID/cubrid-testcases", 2): pr_facts(
                    "CUBRID/cubrid-testcases", 2, merged=True
                ),
            },
        )
        self.assertEqual(ev8["unblock_action"], "REBASE_ENGINE_AND_SYNC_TC")


class BlockerCliAndCheckTest(test_registry.CliBase):
    def _init_git(self, name: str) -> tuple[Path, str, str]:
        repo = Path(self.tmp.name) / name
        repo.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "develop"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True
        )
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Tester"], check=True)

        def commit(msg: str) -> str:
            subprocess.run(
                ["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", msg], check=True
            )
            return subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                check=True, capture_output=True, text=True,
            ).stdout.strip()

        base = commit("[CBRD-10] base")
        fix = commit("[CBRD-28257] fix leak (#8693)")
        subprocess.run(["git", "-C", str(repo), "update-ref", "refs/remotes/origin/develop", fix],
                       check=True)
        return repo, base, fix

    def test_blocker_crud_culprit_hydration_and_export(self):
        self.run_cli("signature", "add", test_registry.CASE, "--suite", "shell",
                     "--id", "sig1", "--label", "l", "--text", "NOK 1")
        with mock.patch.object(
            live, "hydrate_culprit_from_pr",
            return_value={
                "culprit_repo": "CUBRID/cubrid",
                "culprit_pr": 8537,
                "culprit_commit": "e74826c46c" + "0" * 30,
                "culprit_issue": "CBRD-27562",
                "culprit_author": "hornetmj",
            },
        ) as m_hyd:
            res = self.run_cli(
                "blocker", "add", "CBRD-28395",
                "--culprit", "engine#8537",
                "--fix-issue", "CBRD-28395",
                "--signature", "sig1",
            )
        m_hyd.assert_called_once_with("CUBRID/cubrid", 8537)
        self.assertEqual(res["result"], "added")
        # Idempotent repeat offline
        res2 = self.run_cli(
            "blocker", "add", "CBRD-28395",
            "--culprit-repo", "CUBRID/cubrid",
            "--culprit-pr", "8537",
            "--culprit-commit", "e74826c46c" + "0" * 30,
            "--culprit-issue", "CBRD-27562",
            "--culprit-author", "hornetmj",
            "--fix-issue", "CBRD-28395",
            "--signature", "sig1",
            "--offline",
        )
        self.assertEqual(res2["result"], "unchanged")

        self.assertEqual(
            self.run_cli("blocker", "pr", "add", "CBRD-28395", "engine#8810")["result"],
            "added",
        )
        self.assertEqual(
            self.run_cli("blocker", "update", "CBRD-28395", "--owner", "Joon Min")["result"],
            "updated",
        )
        shown = self.run_cli("blocker", "show", "CBRD-28395")
        self.assertEqual(shown["culprit"]["author"], "hornetmj")
        self.assertEqual(shown["fix_prs"][0]["pr_number"], 8810)

        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["export"])
        self.assertIn("[[blocker]]", buf.getvalue())
        self.assertIn("[[blocker_pr]]", buf.getvalue())
        self.assertIn("[[blocker_signature]]", buf.getvalue())

        self.assertEqual(
            self.run_cli("blocker", "pr", "retire", "CBRD-28395", "engine#8810",
                         "--reason", "superseded")["result"],
            "retired",
        )
        self.assertEqual(
            self.run_cli("blocker", "retire", "CBRD-28395", "--reason", "done")["result"],
            "retired",
        )

    def test_check_uses_merged_blocker_prs_for_engine_and_tc(self):
        eng_repo, eng_base, eng_fix = self._init_git("eng")
        tc_repo, tc_base, tc_fix = self._init_git("tc")
        os.environ["TCR_ENGINE_REPO"] = str(eng_repo)
        os.environ["TCR_TC_PUBLIC_REPO"] = str(tc_repo)
        self.addCleanup(os.environ.pop, "TCR_TC_PUBLIC_REPO", None)

        sql_case = "sql/_36_guava/cbrd_26795/cases/cbrd_26795.sql"
        self.run_cli("signature", "add", sql_case, "--suite", "sql",
                     "--id", "sig-tc", "--label", "oos trace", "--text", "(oos time:")
        self.run_cli("blocker", "add", "tc-4474", "--pr", "tc#4474", "--signature", "sig-tc",
                     "--offline")
        self.run_cli("rule", "add", "--id", "r-tc", "--signature", "sig-tc",
                     "--blocker", "tc-4474", "--not-covered", "-", "--yes", "--approved-by", "DK")
        msg = Path(self.tmp.name) / "msg.txt"
        msg.write_text("(oos time: ?)")

        tc_merged = pr_facts(
            "CUBRID/cubrid-testcases", 4474, merged=True, merge_commit_sha=tc_fix, author="ssihil"
        )
        with mock.patch.object(
            live, "read_prs_batch", return_value={("CUBRID/cubrid-testcases", 4474): tc_merged}
        ):
            # No --tc-sha -> unknown with helpful message
            unk = self.run_cli("check", "--case", sql_case, "--suite", "sql",
                               "--engine", eng_base, "--stack-file", str(msg), "--no-context")
            self.assertEqual(unk["verdict"], "unknown")
            self.assertIn("--tc-sha", unk["hits"][0]["reason"])
            # Old TC SHA -> fix_missing
            miss = self.run_cli("check", "--case", sql_case, "--suite", "sql",
                                "--engine", eng_base, "--tc-sha", tc_base,
                                "--stack-file", str(msg), "--no-context")
            self.assertEqual(miss["verdict"], "fix_missing")
            # Merged TC SHA -> fix_present
            pres = self.run_cli("check", "--case", sql_case, "--suite", "sql",
                                "--engine", eng_base, "--tc-sha", tc_fix,
                                "--stack-file", str(msg), "--no-context")
            self.assertEqual(pres["verdict"], "fix_present")

        # Engine blocker PR merged while JIRA still Unresolved -> fix_missing / fix_present!
        self.run_cli("signature", "add", test_registry.CASE, "--suite", "shell",
                     "--id", "sig-eng", "--label", "leak", "--text", "NOK leak")
        self.run_cli("blocker", "add", "CBRD-28257", "--fix-issue", "CBRD-28257",
                     "--pr", "engine#8693", "--signature", "sig-eng", "--offline")
        self.run_cli("rule", "add", "--id", "r-eng", "--signature", "sig-eng",
                     "--blocker", "CBRD-28257", "--not-covered", "-", "--yes",
                     "--approved-by", "DK")
        msg_sh = Path(self.tmp.name) / "sh.txt"
        msg_sh.write_text("NOK leak")
        eng_merged = pr_facts(
            "CUBRID/cubrid", 8693, merged=True, merge_commit_sha=eng_fix, author="YeunjunLee"
        )
        stale_jira = verdict.IssueFacts(True, "Develop", None, assignee="Yeunjun Lee")
        with (mock.patch.object(live, "read_prs_batch",
                                return_value={("CUBRID/cubrid", 8693): eng_merged}),
              mock.patch.object(verdict, "read_issue", return_value=stale_jira)):
            r_miss = self.run_cli("check", "--case", test_registry.CASE, "--suite", "shell",
                                  "--engine", eng_base, "--stack-file", str(msg_sh),
                                  "--no-context")
            self.assertEqual(r_miss["verdict"], "fix_missing")
            r_pres = self.run_cli("check", "--case", test_registry.CASE, "--suite", "shell",
                                  "--engine", eng_fix, "--stack-file", str(msg_sh),
                                  "--no-context")
            self.assertEqual(r_pres["verdict"], "fix_present")

    def test_ci_attempt_reads_testcases_sha_automatically(self):
        eng_repo, eng_base, _ = self._init_git("eng2")
        tc_repo, tc_base, tc_fix = self._init_git("tc2")
        os.environ["TCR_ENGINE_REPO"] = str(eng_repo)
        os.environ["TCR_TC_PUBLIC_REPO"] = str(tc_repo)
        self.addCleanup(os.environ.pop, "TCR_TC_PUBLIC_REPO", None)

        attempt_dir = Path(self.tmp.name) / "attempt_sql"
        shutil.copytree(test_registry.FIXTURES / "ci-attempt" / "test_shell", attempt_dir)
        data = json.loads((attempt_dir / "summary.json").read_text())
        sql_case = "sql/_36_guava/cbrd_26795/cases/cbrd_26795.sql"
        f0 = data["failures"][0]
        meta_path = attempt_dir / "failures" / f0["stable_id"] / "metadata.json"
        meta = json.loads(meta_path.read_text())
        meta["name"] = sql_case
        meta_path.write_text(json.dumps(meta))
        (attempt_dir / meta["message_path"]).write_text("(oos time: ?)")
        data["suite"] = "test_sql"
        data["failures"] = [f0]
        data["shards"] = [{**data["shards"][0], "build": {"sha": eng_base},
                           "testcases": {"sha": tc_base, "branch": "tc/pr-8753"}}]
        (attempt_dir / "summary.json").write_text(json.dumps(data))

        self.run_cli("signature", "add", sql_case, "--suite", "sql",
                     "--id", "sig-tc", "--label", "l", "--text", "(oos time:")
        self.run_cli("blocker", "add", "tc-4474", "--pr", "tc#4474", "--signature", "sig-tc",
                     "--offline")
        self.run_cli("rule", "add", "--id", "r-tc", "--signature", "sig-tc",
                     "--blocker", "tc-4474", "--not-covered", "-", "--yes", "--approved-by", "DK")

        tc_merged = pr_facts("CUBRID/cubrid-testcases", 4474, merged=True, merge_commit_sha=tc_fix)
        with mock.patch.object(
            live, "read_prs_batch", return_value={("CUBRID/cubrid-testcases", 4474): tc_merged}
        ):
            out = self.run_cli("check", "--ci-attempt", str(attempt_dir))
        self.assertEqual(out["verdicts"], {"fix_missing": 1})

    def test_blockers_discover_affected_and_unblock_pr(self):
        eng_repo, eng_base, eng_fix = self._init_git("eng3")
        tc_repo, tc_base, tc_fix = self._init_git("tc3")
        os.environ["TCR_ENGINE_REPO"] = str(eng_repo)
        os.environ["TCR_TC_PUBLIC_REPO"] = str(tc_repo)
        self.addCleanup(os.environ.pop, "TCR_TC_PUBLIC_REPO", None)

        sql_case = "sql/_36_guava/cbrd_26795/cases/cbrd_26795.sql"
        sh_case = "shell/_06_issues/_26_2h/cbrd_27744_sa/cases/cbrd_27744_sa.sh"
        self.run_cli("signature", "add", sql_case, "--suite", "sql",
                     "--id", "sig-sql", "--label", "l", "--text", "(oos time:")
        self.run_cli("signature", "add", sh_case, "--suite", "shell",
                     "--id", "sig-sh", "--label", "l", "--text", "NOK sa")
        self.run_cli("blocker", "add", "tc-4474", "--culprit-repo", "CUBRID/cubrid",
                     "--culprit-pr", "7990", "--culprit-issue", "CBRD-26357",
                     "--culprit-author", "vimkim", "--pr", "tc#4474",
                     "--signature", "sig-sql", "--offline")
        self.run_cli("blocker", "add", "CBRD-28395", "--culprit-repo", "CUBRID/cubrid",
                     "--culprit-pr", "8537", "--culprit-issue", "CBRD-27562",
                     "--culprit-author", "hornetmj", "--fix-issue", "CBRD-28395",
                     "--signature", "sig-sh", "--offline")
        self.run_cli("rule", "add", "--id", "r-sql", "--signature", "sig-sql",
                     "--blocker", "tc-4474", "--not-covered", "-", "--yes", "--approved-by", "DK")
        self.run_cli("rule", "add", "--id", "r-sh", "--signature", "sig-sh",
                     "--blocker", "CBRD-28395", "--not-covered", "-", "--yes",
                     "--approved-by", "DK")

        tcc_failures = {
            "schema_version": 1,
            "latest_runs": {
                "sql": {"run_id": 100, "engine_sha": eng_base, "testcase_sha": tc_base,
                        "testcase_branch": "tc/pr-8753"},
                "shell": {"run_id": 100, "engine_sha": eng_base, "testcase_sha": tc_base,
                          "testcase_branch": "tc/pr-8753"},
            },
            "count": 2,
            "items": [
                {"repository": "CUBRID/cubrid", "run_id": 100, "attempt": 1, "suite": "sql",
                 "shard": "09", "ordinal": 0, "outcome": "FAIL", "evidence": "summary_info",
                 "completed_at": "2026-11-26T00:00:00Z", "engine_sha": eng_base,
                 "testcase_sha": tc_base, "testcase_branch": "tc/pr-8753",
                 "testcase": {"repository": "CUBRID/cubrid-testcases", "suite": "sql",
                              "path": sql_case, "variant": ""},
                 "pull_request": {"number": 8753, "base_ref": "develop", "head_ref": "topic"},
                 "is_latest_pr_suite_run": True},
                {"repository": "CUBRID/cubrid", "run_id": 100, "attempt": 1, "suite": "shell",
                 "shard": "16", "ordinal": 0, "outcome": "FAIL", "evidence": "junit",
                 "completed_at": "2026-11-26T00:00:00Z", "engine_sha": eng_base,
                 "testcase_sha": tc_base, "testcase_branch": "tc/pr-8753",
                 "testcase": {"repository": "CUBRID/cubrid-testcases-private-ex",
                              "suite": "shell", "path": sh_case, "variant": ""},
                 "pull_request": {"number": 8753, "base_ref": "develop", "head_ref": "topic"},
                 "is_latest_pr_suite_run": True},
            ],
        }
        pr_batch = {
            ("CUBRID/cubrid-testcases", 4474): pr_facts(
                "CUBRID/cubrid-testcases", 4474, merged=True, merge_commit_sha=tc_fix,
                author="ssihil"
            ),
            ("CUBRID/cubrid", 8753): pr_facts(
                "CUBRID/cubrid", 8753, state="OPEN", base_ref="develop", head_ref="CBRD-x"
            ),
        }
        jira_28395 = verdict.IssueFacts(True, "Confirmed", None, assignee="Joon Min")
        disc_pr = pr_facts("CUBRID/cubrid", 8810, state="OPEN", author="JoonMin")

        with (mock.patch.object(live, "read_prs_batch", return_value=pr_batch),
              mock.patch.object(verdict, "read_issue", return_value=jira_28395),
              mock.patch.object(live, "discover_prs_for_issue", return_value=([disc_pr], None)),
              mock.patch.object(cli.chronicle, "failures", return_value=tcc_failures)):
            rep = self.run_cli("blockers", "--affected-days", "7")
            by_id = {b["id"]: b for b in rep["blockers"]}
            self.assertEqual(by_id["tc-4474"]["readiness"], "READY")
            self.assertEqual(by_id["tc-4474"]["affected_open_prs"][0]["pr"], 8753)
            self.assertEqual(by_id["CBRD-28395"]["readiness"], "IN_PROGRESS")
            self.assertEqual(by_id["CBRD-28395"]["discovered_prs"][0]["pr_number"], 8810)

            audit = self.run_cli("rules", "--audit")
            by_rule = {r["id"]: r for r in audit}
            self.assertEqual(by_rule["r-sql"]["readiness"], "READY")
            self.assertEqual(by_rule["r-sh"]["issue_assignee"], "Joon Min")

            unblock = self.run_cli("unblock", "--pr", "8753")
            self.assertEqual(unblock["suites"]["sql"]["status"], "READY TO UNBLOCK")
            self.assertIn("sync tc/pr-8753", unblock["suites"]["sql"]["action"])
            self.assertEqual(unblock["suites"]["shell"]["status"], "STILL BLOCKED")
            self.assertIn("CBRD-28395", unblock["suites"]["shell"]["action"])

            # Verify plain render output works cleanly
            text = render.render_unblock(unblock, no_color=True)
            self.assertIn("READY TO UNBLOCK", text)
            self.assertIn("STILL BLOCKED", text)
            self.assertTrue(date.today())

    def test_unblock_needs_analysis_on_unclassified_or_fix_present(self):
        eng_repo, eng_base, eng_fix = self._init_git("eng4")
        tc_repo, tc_base, tc_fix = self._init_git("tc4")
        os.environ["TCR_ENGINE_REPO"] = str(eng_repo)
        os.environ["TCR_TC_PUBLIC_REPO"] = str(tc_repo)
        self.addCleanup(os.environ.pop, "TCR_TC_PUBLIC_REPO", None)

        sql_known = "sql/_36_guava/cbrd_26795/cases/cbrd_26795.sql"
        sql_unknown = "sql/_36_guava/cbrd_99999/cases/cbrd_99999.sql"
        self.run_cli("signature", "add", sql_known, "--suite", "sql",
                     "--id", "sig-k", "--label", "l", "--text", "(oos time:")
        self.run_cli("blocker", "add", "tc-4474", "--pr", "tc#4474", "--signature", "sig-k",
                     "--offline")
        self.run_cli("rule", "add", "--id", "r-k", "--signature", "sig-k",
                     "--blocker", "tc-4474", "--not-covered", "-", "--yes", "--approved-by", "DK")

        # Case 1: fix already present in tested tc_fix -> NEEDS ANALYSIS
        # Case 2: unclassified failure in medium -> NEEDS ANALYSIS
        tcc_failures = {
            "schema_version": 1,
            "latest_runs": {
                "sql": {"run_id": 200, "engine_sha": eng_base, "testcase_sha": tc_fix,
                        "testcase_branch": "tc/pr-9000"},
                "medium": {"run_id": 200, "engine_sha": eng_base, "testcase_sha": tc_fix,
                           "testcase_branch": "tc/pr-9000"},
            },
            "count": 2,
            "items": [
                {"repository": "CUBRID/cubrid", "run_id": 200, "attempt": 1, "suite": "sql",
                 "shard": "09", "ordinal": 0, "outcome": "FAIL", "evidence": "summary_info",
                 "completed_at": "2026-11-26T00:00:00Z", "engine_sha": eng_base,
                 "testcase_sha": tc_fix, "testcase_branch": "tc/pr-9000",
                 "testcase": {"repository": "CUBRID/cubrid-testcases", "suite": "sql",
                              "path": sql_known, "variant": ""},
                 "pull_request": {"number": 9000, "base_ref": "develop", "head_ref": "topic"},
                 "is_latest_pr_suite_run": True},
                {"repository": "CUBRID/cubrid", "run_id": 200, "attempt": 1, "suite": "medium",
                 "shard": "01", "ordinal": 0, "outcome": "FAIL", "evidence": "summary_info",
                 "completed_at": "2026-11-26T00:00:00Z", "engine_sha": eng_base,
                 "testcase_sha": tc_fix, "testcase_branch": "tc/pr-9000",
                 "testcase": {"repository": "CUBRID/cubrid-testcases", "suite": "medium",
                              "path": sql_unknown, "variant": ""},
                 "pull_request": {"number": 9000, "base_ref": "develop", "head_ref": "topic"},
                 "is_latest_pr_suite_run": True},
            ],
        }
        pr_batch = {
            ("CUBRID/cubrid-testcases", 4474): pr_facts(
                "CUBRID/cubrid-testcases", 4474, merged=True, merge_commit_sha=tc_fix
            ),
            ("CUBRID/cubrid", 9000): pr_facts("CUBRID/cubrid", 9000, state="OPEN"),
        }
        with (mock.patch.object(live, "read_prs_batch", return_value=pr_batch),
              mock.patch.object(cli.chronicle, "failures", return_value=tcc_failures)):
            unblock = self.run_cli("unblock", "--pr", "9000")
        self.assertEqual(unblock["suites"]["sql"]["status"], "NEEDS ANALYSIS")
        self.assertIn("already contain fix", unblock["suites"]["sql"]["action"])
        self.assertEqual(unblock["suites"]["medium"]["status"], "NEEDS ANALYSIS")
        self.assertIn("unclassified", unblock["suites"]["medium"]["action"])


if __name__ == "__main__":
    unittest.main()
