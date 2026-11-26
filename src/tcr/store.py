"""SQLite store for curated testcase knowledge. Chronicle owns the failure history."""

import json
import os
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

SUITE_REPOSITORY = {
    "sql": "CUBRID/cubrid-testcases",
    "medium": "CUBRID/cubrid-testcases",
    "shell": "CUBRID/cubrid-testcases-private-ex",
}
SUITE_ALIASES = {
    "sql": "sql",
    "medium": "medium",
    "shell": "shell",
    "test_sql": "sql",
    "test_medium": "medium",
    "test_shell": "shell",
}
CCI_SUITES = ("sql_by_cci", "test_sql_by_cci")
WRITE_SUITES = tuple(sorted(SUITE_ALIASES))
CHECK_SUITES = tuple(sorted((*SUITE_ALIASES, *CCI_SUITES)))
RELATIONS = ("origin", "tracks", "suspected", "fixed_by")
ISSUE_KEY = re.compile(r"^CBRD-\d+$")
KNOWN_REPOS = (
    "CUBRID/cubrid",
    "CUBRID/cubrid-testcases",
    "CUBRID/cubrid-testcases-private-ex",
)
REPO_ALIASES = {
    "engine": "CUBRID/cubrid",
    "cubrid": "CUBRID/cubrid",
    "cubrid/cubrid": "CUBRID/cubrid",
    "tc": "CUBRID/cubrid-testcases",
    "tc-public": "CUBRID/cubrid-testcases",
    "cubrid-testcases": "CUBRID/cubrid-testcases",
    "cubrid/cubrid-testcases": "CUBRID/cubrid-testcases",
    "tc-private": "CUBRID/cubrid-testcases-private-ex",
    "cubrid-testcases-private-ex": "CUBRID/cubrid-testcases-private-ex",
    "cubrid/cubrid-testcases-private-ex": "CUBRID/cubrid-testcases-private-ex",
}
CONFIDENCES = ("confirmed", "suspected")
BLOCKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
COMMIT_SHA = re.compile(r"^[0-9a-fA-F]{7,40}$")
_PR_URL = re.compile(
    r"^https?://github\.com/(CUBRID/[^/]+)/pull/(\d+)(?:[/?#].*)?$", re.IGNORECASE
)
SCHEMA_VERSION = 2


def is_cci_suite(suite: str | None) -> bool:
    return bool(suite and suite.strip().lower() in CCI_SUITES)


def normalize_suite(suite: str | None) -> str | None:
    if suite is None:
        return None
    key = suite.strip().lower()
    if key in SUITE_ALIASES:
        return SUITE_ALIASES[key]
    if key in CCI_SUITES:
        return "sql"
    raise RegistryError(f"suite must be one of {', '.join(CHECK_SUITES)}: {suite!r}")


def parse_pr_ref(ref: str, default_repo: str = "CUBRID/cubrid") -> tuple[str, int]:
    """Parse PR shorthand or URL into (repository, pr_number)."""
    raw = ref.strip()
    if m := _PR_URL.match(raw):
        owner_repo = m.group(1)
        repo = REPO_ALIASES.get(owner_repo.lower())
        if not repo:
            raise RegistryError(f"unsupported PR repository: {owner_repo!r}")
        num = int(m.group(2))
        if num < 1:
            raise RegistryError(f"PR number must be positive: {ref!r}")
        return repo, num
    if "#" in raw:
        prefix, num_s = raw.split("#", 1)
        repo = default_repo if not prefix else REPO_ALIASES.get(prefix.strip().lower())
        if not repo:
            raise RegistryError(
                f"unknown repository alias {prefix!r}; use engine#, tc#, tc-private#, or full repo"
            )
    else:
        repo, num_s = default_repo, raw
    if not num_s.isdigit() or int(num_s) < 1:
        raise RegistryError(f"PR reference must end with a positive number: {ref!r}")
    return repo, int(num_s)


def check_repo(repo: str) -> str:
    resolved = REPO_ALIASES.get(repo.strip().lower(), repo.strip())
    if resolved not in KNOWN_REPOS:
        raise RegistryError(f"repository must be one of {', '.join(KNOWN_REPOS)}: {repo!r}")
    return resolved


def check_commit_sha(sha: str) -> str:
    s = sha.strip().lower()
    if not COMMIT_SHA.match(s):
        raise RegistryError(f"commit SHA must be 7-40 hex chars: {sha!r}")
    return s


def check_blocker_id(bid: str) -> str:
    s = bid.strip()
    if not BLOCKER_ID.match(s):
        raise RegistryError(f"blocker id must match {BLOCKER_ID.pattern}: {bid!r}")
    return s


SIGNATURE_TABLE = """CREATE TABLE signature (
    id TEXT PRIMARY KEY,
    testcase_id INTEGER NOT NULL REFERENCES testcase(id),
    label TEXT NOT NULL,
    frames TEXT NOT NULL,
    required_text TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    CHECK (json_array_length(frames) > 0 OR json_array_length(required_text) > 0)
);"""

ISSUE_LINK_TABLE = """CREATE TABLE issue_link (
    testcase_id INTEGER NOT NULL REFERENCES testcase(id),
    issue TEXT NOT NULL,
    relation TEXT NOT NULL CHECK (relation IN ('origin', 'tracks', 'suspected', 'fixed_by')),
    signature_id TEXT REFERENCES signature(id),
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);"""

RULE_TABLE = """CREATE TABLE rule (
    id TEXT PRIMARY KEY,
    signature_id TEXT REFERENCES signature(id),
    job_contains TEXT,
    issue TEXT,
    expires TEXT,
    blocker_id TEXT REFERENCES blocker(id),
    not_covered TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    retired_at TEXT,
    retired_reason TEXT,
    CHECK ((signature_id IS NULL) <> (job_contains IS NULL)),
    CHECK (issue IS NOT NULL OR expires IS NOT NULL OR blocker_id IS NOT NULL)
);"""

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS testcase (
    id INTEGER PRIMARY KEY,
    repository TEXT NOT NULL,
    suite TEXT NOT NULL CHECK (suite IN ('sql', 'medium', 'shell')),
    path TEXT NOT NULL,
    variant TEXT NOT NULL DEFAULT '',
    UNIQUE (repository, suite, path, variant)
);
{SIGNATURE_TABLE.replace('CREATE TABLE', 'CREATE TABLE IF NOT EXISTS', 1)};
CREATE TABLE IF NOT EXISTS occurrence_signature (
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
{ISSUE_LINK_TABLE.replace('CREATE TABLE', 'CREATE TABLE IF NOT EXISTS', 1)}
CREATE UNIQUE INDEX IF NOT EXISTS issue_link_uniq
    ON issue_link(testcase_id, issue, relation, COALESCE(signature_id, ''));
CREATE TABLE IF NOT EXISTS note (
    id INTEGER PRIMARY KEY,
    testcase_id INTEGER NOT NULL REFERENCES testcase(id),
    body TEXT NOT NULL,
    refs TEXT NOT NULL DEFAULT '[]',
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    retired_at TEXT,
    retired_reason TEXT
);
CREATE TABLE IF NOT EXISTS blocker (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    culprit_repo TEXT CHECK (culprit_repo IS NULL OR culprit_repo IN (
        'CUBRID/cubrid', 'CUBRID/cubrid-testcases', 'CUBRID/cubrid-testcases-private-ex')),
    culprit_commit TEXT,
    culprit_pr INTEGER CHECK (culprit_pr IS NULL OR culprit_pr > 0),
    culprit_issue TEXT,
    culprit_author TEXT,
    culprit_confidence TEXT NOT NULL DEFAULT 'confirmed'
        CHECK (culprit_confidence IN ('confirmed', 'suspected')),
    culprit_note TEXT NOT NULL DEFAULT '',
    fix_issue TEXT,
    owner TEXT,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    retired_at TEXT,
    retired_reason TEXT
);
CREATE TABLE IF NOT EXISTS blocker_pr (
    blocker_id TEXT NOT NULL REFERENCES blocker(id),
    repository TEXT NOT NULL CHECK (repository IN (
        'CUBRID/cubrid', 'CUBRID/cubrid-testcases', 'CUBRID/cubrid-testcases-private-ex')),
    pr_number INTEGER NOT NULL CHECK (pr_number > 0),
    target_branch TEXT NOT NULL DEFAULT 'develop',
    required INTEGER NOT NULL DEFAULT 1 CHECK (required IN (0, 1)),
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    retired_at TEXT,
    retired_reason TEXT,
    PRIMARY KEY (blocker_id, repository, pr_number)
);
CREATE TABLE IF NOT EXISTS blocker_signature (
    blocker_id TEXT NOT NULL REFERENCES blocker(id),
    signature_id TEXT NOT NULL REFERENCES signature(id),
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (blocker_id, signature_id)
);
{RULE_TABLE.replace('CREATE TABLE', 'CREATE TABLE IF NOT EXISTS', 1)}
CREATE TABLE IF NOT EXISTS event (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    command TEXT NOT NULL,
    entity TEXT NOT NULL,
    entity_key TEXT NOT NULL,
    before TEXT,
    after TEXT
);
CREATE INDEX IF NOT EXISTS event_entity ON event(entity, entity_key);
"""


class RegistryError(Exception):
    """A request the registry refuses. The CLI reports it and exits with code 2."""


@dataclass(frozen=True)
class Testcase:
    id: int
    repository: str
    suite: str
    path: str
    variant: str


def default_path() -> Path:
    if env := os.environ.get("TCR_DB"):
        return Path(env)
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "cubrid-tc-registry" / "registry.sqlite3"


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def check_issue_key(key: str) -> str:
    if not ISSUE_KEY.match(key):
        raise RegistryError(f"issue key must look like CBRD-<digits>: {key!r}")
    return key


def check_date(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as e:
        raise RegistryError(f"date must be YYYY-MM-DD: {value!r}") from e


class Store:
    def __init__(self, path: Path, actor: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.actor = actor
        self.db = sqlite3.connect(path, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        self._migrate_signature()
        self._migrate_v2()

    def _migrate_signature(self):
        """Rebuild `signature` once when it still has the frames-only CHECK."""
        (sql,) = self.db.execute("SELECT sql FROM sqlite_master WHERE name='signature'").fetchone()
        if "required_text) > 0" in sql:
            return
        new = SIGNATURE_TABLE.replace("signature", "signature_new", 1)
        # foreign_keys cannot change inside a transaction. With it off, DROP keeps child rows.
        self.db.execute("PRAGMA foreign_keys=OFF")
        try:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                (sql,) = self.db.execute(
                    "SELECT sql FROM sqlite_master WHERE name='signature'").fetchone()
                if "required_text) > 0" not in sql:  # another process may have migrated first
                    self.db.execute(new)
                    self.db.execute("INSERT INTO signature_new SELECT id, testcase_id, label,"
                                    " frames, required_text, created_at FROM signature")
                    self.db.execute("DROP TABLE signature")
                    self.db.execute("ALTER TABLE signature_new RENAME TO signature")
                    if bad := self.db.execute("PRAGMA foreign_key_check").fetchall():
                        raise RegistryError(f"signature migration broke foreign keys: {bad}")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            self.db.execute("COMMIT")
        finally:
            self.db.execute("PRAGMA foreign_keys=ON")

    def _migrate_v2(self):
        """Upgrade v1 DBs to schema version 2 (blocker tables, rule.blocker_id, issue_link PK)."""
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version >= SCHEMA_VERSION:
            return
        rule_sql = self.db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='rule'").fetchone()[0]
        link_sql = self.db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='issue_link'").fetchone()[0]
        needs_rule_rebuild = "blocker_id" not in rule_sql
        needs_link_rebuild = "PRIMARY KEY" in link_sql

        self.db.execute("PRAGMA foreign_keys=OFF")
        try:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                version = self.db.execute("PRAGMA user_version").fetchone()[0]
                if version < SCHEMA_VERSION:
                    if needs_link_rebuild:
                        self.db.execute(
                            ISSUE_LINK_TABLE.replace("issue_link", "issue_link_new", 1))
                        self.db.execute(
                            "INSERT INTO issue_link_new"
                            " SELECT testcase_id, issue, relation, signature_id, actor, created_at"
                            " FROM issue_link")
                        self.db.execute("DROP TABLE issue_link")
                        self.db.execute("ALTER TABLE issue_link_new RENAME TO issue_link")
                        self.db.execute(
                            "CREATE UNIQUE INDEX IF NOT EXISTS issue_link_uniq"
                            " ON issue_link(testcase_id, issue, relation,"
                            " COALESCE(signature_id, ''))")
                    if needs_rule_rebuild:
                        self.db.execute(RULE_TABLE.replace("rule", "rule_new", 1))
                        self.db.execute(
                            "INSERT INTO rule_new(id, signature_id, job_contains, issue, expires,"
                            " blocker_id, not_covered, approved_by, created_at, retired_at,"
                            " retired_reason)"
                            " SELECT id, signature_id, job_contains, issue, expires, NULL,"
                            " not_covered, approved_by, created_at, retired_at, retired_reason"
                            " FROM rule")
                        self.db.execute("DROP TABLE rule")
                        self.db.execute("ALTER TABLE rule_new RENAME TO rule")
                    if bad := self.db.execute("PRAGMA foreign_key_check").fetchall():
                        raise RegistryError(f"v2 migration broke foreign keys: {bad}")
                    self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            self.db.execute("COMMIT")
        finally:
            self.db.execute("PRAGMA foreign_keys=ON")

    @contextmanager
    def write(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.db.execute("COMMIT")

    def _event(self, command, entity, key, before, after):
        self.db.execute(
            "INSERT INTO event(at, actor, command, entity, entity_key, before, after)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (now(), self.actor, command, entity, key, _dump(before), _dump(after)),
        )

    def _row(self, sql, *args):
        row = self.db.execute(sql, args).fetchone()
        return dict(row) if row else None

    # Testcase identity

    def testcase(self, suite, path, repository=None, variant="", create=False) -> Testcase | None:
        if suite not in SUITE_REPOSITORY:
            raise RegistryError(f"suite must be one of {', '.join(SUITE_REPOSITORY)}: {suite!r}")
        repository = repository or SUITE_REPOSITORY[suite]
        key = (repository, suite, path, variant or "")
        row = self._row(
            "SELECT * FROM testcase WHERE repository=? AND suite=? AND path=? AND variant=?", *key
        )
        if row is None and create:
            cur = self.db.execute(
                "INSERT INTO testcase(repository, suite, path, variant) VALUES (?, ?, ?, ?)", key
            )
            row = {"id": cur.lastrowid, "repository": key[0], "suite": suite, "path": path,
                   "variant": key[3]}
        return Testcase(**row) if row else None

    # Writes. Each returns "added" or "unchanged" so a repeated command converges.

    def add_signature(self, tc: Testcase, sig_id, label, frames, required_text) -> str:
        if not frames and not required_text:
            raise RegistryError("a signature needs at least one frame or one --text")
        after = {"testcase_id": tc.id, "label": label, "frames": frames,
                 "required_text": required_text}
        with self.write():
            existing = self.signature(sig_id)
            if existing:
                same = {k: existing[k] for k in after} == after
                if not same:
                    raise RegistryError(f"signature {sig_id} exists with other content")
                return "unchanged"
            self.db.execute(
                "INSERT INTO signature(id, testcase_id, label, frames, required_text, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (sig_id, tc.id, label, json.dumps(frames), json.dumps(required_text), now()),
            )
            self._event("signature add", "signature", sig_id, None, after)
        return "added"

    def signature(self, sig_id):
        row = self._row("SELECT * FROM signature WHERE id=?", sig_id)
        if row:
            row["frames"] = json.loads(row["frames"])
            row["required_text"] = json.loads(row["required_text"])
        return row

    def signatures(self, tc: Testcase):
        ids = self.db.execute("SELECT id FROM signature WHERE testcase_id=? ORDER BY id", (tc.id,))
        return [self.signature(r["id"]) for r in ids.fetchall()]

    def attach(self, tc: Testcase, occurrence, sig_id, stack_digest) -> str:
        sig = self.signature(sig_id)
        if sig is None:
            raise RegistryError(f"no signature {sig_id}")
        if sig["testcase_id"] != tc.id:
            raise RegistryError(f"signature {sig_id} belongs to another testcase")
        attempt = occurrence["attempt"] or 0  # Chronicle's null is unresolved; the key is NOT NULL
        key = (occurrence["repository"], occurrence["run_id"], attempt,
               occurrence["shard"], tc.id, occurrence["ordinal"])
        after = {"signature_id": sig_id, "stack_digest": stack_digest}
        with self.write():
            row = self._row(
                "SELECT signature_id, stack_digest FROM occurrence_signature WHERE repository=?"
                " AND run_id=? AND attempt=? AND shard=? AND testcase_id=? AND ordinal=?", *key)
            if row == after:
                return "unchanged"
            if row:
                raise RegistryError(
                    f"occurrence already tagged with {row['signature_id']}; retag is not supported")
            self.db.execute(
                "INSERT INTO occurrence_signature VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (*key, sig_id, stack_digest, self.actor, now()),
            )
            self._event("signature attach", "occurrence", _key(key), None, after)
        return "added"

    def occurrence_tags(self, tc: Testcase):
        rows = self.db.execute(
            "SELECT * FROM occurrence_signature WHERE testcase_id=?", (tc.id,)).fetchall()
        return {(r["repository"], r["run_id"], r["shard"], r["ordinal"]): dict(r) for r in rows}

    def link(self, tc: Testcase, issue, relation, sig_id=None) -> str:
        check_issue_key(issue)
        if relation not in RELATIONS:
            raise RegistryError(f"relation must be one of {', '.join(RELATIONS)}")
        if sig_id is not None:
            sig = self.signature(sig_id)
            if sig is None or sig["testcase_id"] != tc.id:
                raise RegistryError(f"signature {sig_id} does not belong to this testcase")
        with self.write():
            return self._link(tc, issue, relation, sig_id)

    def _link(self, tc, issue, relation, sig_id):
        exact = self._row(
            "SELECT signature_id FROM issue_link"
            " WHERE testcase_id=? AND issue=? AND relation=?"
            " AND COALESCE(signature_id, '') = COALESCE(?, '')",
            tc.id, issue, relation, sig_id)
        if exact is not None:
            return "unchanged"
        if sig_id is None:
            any_row = self._row(
                "SELECT 1 FROM issue_link WHERE testcase_id=? AND issue=? AND relation=?",
                tc.id, issue, relation)
            if any_row is not None:
                return "unchanged"
        else:
            null_row = self._row(
                "SELECT signature_id FROM issue_link"
                " WHERE testcase_id=? AND issue=? AND relation=? AND signature_id IS NULL",
                tc.id, issue, relation)
            if null_row is not None:
                self.db.execute(
                    "UPDATE issue_link SET signature_id=?"
                    " WHERE testcase_id=? AND issue=? AND relation=? AND signature_id IS NULL",
                    (sig_id, tc.id, issue, relation))
                self._event("link", "issue_link", _key((tc.id, issue, relation)),
                            {"signature_id": None}, {"signature_id": sig_id})
                return "updated"
        self.db.execute(
            "INSERT INTO issue_link VALUES (?, ?, ?, ?, ?, ?)",
            (tc.id, issue, relation, sig_id, self.actor, now()))
        self._event("link", "issue_link", _key((tc.id, issue, relation, sig_id or "")), None,
                    {"signature_id": sig_id})
        return "added"

    def links(self, tc: Testcase):
        return [dict(r) for r in self.db.execute(
            "SELECT issue, relation, signature_id, actor, created_at FROM issue_link"
            " WHERE testcase_id=? ORDER BY relation, issue, COALESCE(signature_id, '')",
            (tc.id,))]

    def add_note(self, tc: Testcase, body, refs) -> str:
        with self.write():
            exists = self._row(
                "SELECT id FROM note WHERE testcase_id=? AND body=? AND retired_at IS NULL",
                tc.id, body)
            if exists:
                return "unchanged"
            cur = self.db.execute(
                "INSERT INTO note(testcase_id, body, refs, actor, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (tc.id, body, json.dumps(refs), self.actor, now()))
            self._event("note", "note", str(cur.lastrowid), None, {"body": body, "refs": refs})
        return "added"

    def notes(self, tc: Testcase):
        rows = self.db.execute(
            "SELECT id, body, refs, actor, created_at FROM note"
            " WHERE testcase_id=? AND retired_at IS NULL ORDER BY created_at DESC, id DESC",
            (tc.id,))
        return [dict(r, refs=json.loads(r["refs"])) for r in rows]

    # Blockers

    def _validate_blocker_fields(self, *, culprit_repo, culprit_commit, culprit_pr,
                                 culprit_issue, culprit_confidence, fix_issue):
        if culprit_repo is not None:
            culprit_repo = check_repo(culprit_repo)
        if culprit_commit is not None:
            culprit_commit = check_commit_sha(culprit_commit)
        if culprit_pr is not None:
            if culprit_pr < 1:
                raise RegistryError(f"culprit PR must be positive: {culprit_pr!r}")
            culprit_repo = culprit_repo or "CUBRID/cubrid"
        if culprit_issue is not None:
            check_issue_key(culprit_issue)
        if culprit_confidence not in CONFIDENCES:
            raise RegistryError(
                f"culprit confidence must be one of {', '.join(CONFIDENCES)}:"
                f" {culprit_confidence!r}"
            )
        if fix_issue is not None:
            check_issue_key(fix_issue)
        return culprit_repo, culprit_commit

    def add_blocker(
        self,
        blocker_id: str,
        *,
        title: str = "",
        culprit_repo: str | None = None,
        culprit_commit: str | None = None,
        culprit_pr: int | None = None,
        culprit_issue: str | None = None,
        culprit_author: str | None = None,
        culprit_confidence: str = "confirmed",
        culprit_note: str = "",
        fix_issue: str | None = None,
        owner: str | None = None,
        prs: list[tuple[str, int, str, bool]] = (),
        signatures: list[str] = (),
        rules: list[str] = (),
    ) -> str:
        blocker_id = check_blocker_id(blocker_id)
        culprit_repo, culprit_commit = self._validate_blocker_fields(
            culprit_repo=culprit_repo, culprit_commit=culprit_commit, culprit_pr=culprit_pr,
            culprit_issue=culprit_issue, culprit_confidence=culprit_confidence,
            fix_issue=fix_issue,
        )
        for repo, pr_num, _, _ in prs:
            check_repo(repo)
            if pr_num < 1:
                raise RegistryError(f"PR number must be positive: {pr_num!r}")
        for sig_id in signatures:
            if self.signature(sig_id) is None:
                raise RegistryError(f"no signature {sig_id}")
        for rule_id in rules:
            r = self._row("SELECT id, retired_at FROM rule WHERE id=?", rule_id)
            if r is None:
                raise RegistryError(f"no rule {rule_id}")

        core = {
            "title": title,
            "culprit_repo": culprit_repo,
            "culprit_commit": culprit_commit,
            "culprit_pr": culprit_pr,
            "culprit_issue": culprit_issue,
            "culprit_author": culprit_author,
            "culprit_confidence": culprit_confidence,
            "culprit_note": culprit_note,
            "fix_issue": fix_issue,
            "owner": owner,
        }
        with self.write():
            existing = self._row("SELECT * FROM blocker WHERE id=?", blocker_id)
            changed = False
            if existing:
                if existing["retired_at"] is not None:
                    raise RegistryError(f"blocker {blocker_id} is retired")
                if {k: existing[k] for k in core} != core:
                    raise RegistryError(
                        f"blocker {blocker_id} exists with other content; use update"
                    )
                result = "unchanged"
            else:
                self.db.execute(
                    "INSERT INTO blocker(id, title, culprit_repo, culprit_commit, culprit_pr,"
                    " culprit_issue, culprit_author, culprit_confidence, culprit_note,"
                    " fix_issue, owner, actor, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (blocker_id, title, culprit_repo, culprit_commit, culprit_pr,
                     culprit_issue, culprit_author, culprit_confidence, culprit_note,
                     fix_issue, owner, self.actor, now()),
                )
                self._event("blocker add", "blocker", blocker_id, None, core)
                result = "added"
            for repo, pr_num, target_branch, required in prs:
                res_pr = self._add_blocker_pr(
                    blocker_id, check_repo(repo), pr_num,
                    target_branch=target_branch, required=required,
                )
                if res_pr != "unchanged":
                    changed = True
            for sig_id in signatures:
                if self._link_blocker_signature(blocker_id, sig_id) != "unchanged":
                    changed = True
            for rule_id in rules:
                if self._set_rule_blocker(rule_id, blocker_id) != "unchanged":
                    changed = True
        return "added" if result == "added" else ("updated" if changed else "unchanged")

    def update_blocker(self, blocker_id: str, **updates) -> str:
        blocker_id = check_blocker_id(blocker_id)
        allowed = (
            "title", "culprit_repo", "culprit_commit", "culprit_pr", "culprit_issue",
            "culprit_author", "culprit_confidence", "culprit_note", "fix_issue", "owner",
        )
        with self.write():
            existing = self._row("SELECT * FROM blocker WHERE id=?", blocker_id)
            if existing is None:
                raise RegistryError(f"no blocker {blocker_id}")
            if existing["retired_at"] is not None:
                raise RegistryError(f"blocker {blocker_id} is retired")
            merged = {k: existing[k] for k in allowed}
            for k, v in updates.items():
                if k in allowed and v is not None:
                    merged[k] = v
            culprit_repo, culprit_commit = self._validate_blocker_fields(
                culprit_repo=merged["culprit_repo"],
                culprit_commit=merged["culprit_commit"],
                culprit_pr=merged["culprit_pr"],
                culprit_issue=merged["culprit_issue"],
                culprit_confidence=merged["culprit_confidence"],
                fix_issue=merged["fix_issue"],
            )
            merged["culprit_repo"] = culprit_repo
            merged["culprit_commit"] = culprit_commit
            before = {k: existing[k] for k in allowed}
            if before == merged:
                return "unchanged"
            sets = ", ".join(f"{k}=?" for k in allowed)
            self.db.execute(f"UPDATE blocker SET {sets} WHERE id=?",
                            (*[merged[k] for k in allowed], blocker_id))
            self._event("blocker update", "blocker", blocker_id, before, merged)
            if merged["fix_issue"] and merged["fix_issue"] != existing["fix_issue"]:
                for sig_id in self._blocker_signature_ids(blocker_id):
                    sig = self.signature(sig_id)
                    if sig:
                        tc = self._row("SELECT * FROM testcase WHERE id=?", sig["testcase_id"])
                        self._link(Testcase(**tc), merged["fix_issue"], "tracks", sig_id)
        return "updated"

    def retire_blocker(self, blocker_id: str, reason: str) -> str:
        blocker_id = check_blocker_id(blocker_id)
        with self.write():
            row = self._row("SELECT * FROM blocker WHERE id=?", blocker_id)
            if row is None:
                raise RegistryError(f"no blocker {blocker_id}")
            if row["retired_at"]:
                return "unchanged"
            self.db.execute("UPDATE blocker SET retired_at=?, retired_reason=? WHERE id=?",
                            (now(), reason, blocker_id))
            self._event("blocker retire", "blocker", blocker_id, {"retired_at": None},
                        {"retired_reason": reason})
        return "retired"

    def _add_blocker_pr(self, blocker_id: str, repository: str, pr_number: int, *,
                        target_branch: str = "develop", required: bool = True) -> str:
        req_int = 1 if required else 0
        key = (blocker_id, repository, pr_number)
        row = self._row(
            "SELECT * FROM blocker_pr WHERE blocker_id=? AND repository=? AND pr_number=?", *key)
        after = {"target_branch": target_branch, "required": req_int, "retired_at": None}
        if row is not None:
            before = {"target_branch": row["target_branch"], "required": row["required"],
                      "retired_at": row["retired_at"]}
            if before == after:
                return "unchanged"
            self.db.execute(
                "UPDATE blocker_pr SET target_branch=?, required=?, retired_at=NULL,"
                " retired_reason=NULL WHERE blocker_id=? AND repository=? AND pr_number=?",
                (target_branch, req_int, *key),
            )
            self._event("blocker pr add", "blocker_pr", _key(key), before, after)
            return "updated"
        self.db.execute(
            "INSERT INTO blocker_pr(blocker_id, repository, pr_number, target_branch,"
            " required, actor, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (*key, target_branch, req_int, self.actor, now()),
        )
        self._event("blocker pr add", "blocker_pr", _key(key), None,
                    {"target_branch": target_branch, "required": req_int})
        return "added"

    def add_blocker_pr(self, blocker_id: str, repository: str, pr_number: int, *,
                       target_branch: str = "develop", required: bool = True) -> str:
        blocker_id = check_blocker_id(blocker_id)
        repository = check_repo(repository)
        if pr_number < 1:
            raise RegistryError(f"PR number must be positive: {pr_number!r}")
        with self.write():
            b = self._row("SELECT id, retired_at FROM blocker WHERE id=?", blocker_id)
            if b is None:
                raise RegistryError(f"no blocker {blocker_id}")
            if b["retired_at"] is not None:
                raise RegistryError(f"blocker {blocker_id} is retired")
            return self._add_blocker_pr(blocker_id, repository, pr_number,
                                        target_branch=target_branch, required=required)

    def retire_blocker_pr(self, blocker_id: str, repository: str, pr_number: int,
                          reason: str) -> str:
        blocker_id = check_blocker_id(blocker_id)
        repository = check_repo(repository)
        key = (blocker_id, repository, pr_number)
        with self.write():
            row = self._row(
                "SELECT * FROM blocker_pr WHERE blocker_id=? AND repository=? AND pr_number=?",
                *key,
            )
            if row is None:
                raise RegistryError(f"no blocker PR {repository}#{pr_number} on {blocker_id}")
            if row["retired_at"]:
                return "unchanged"
            self.db.execute(
                "UPDATE blocker_pr SET retired_at=?, retired_reason=?"
                " WHERE blocker_id=? AND repository=? AND pr_number=?",
                (now(), reason, *key),
            )
            self._event("blocker pr retire", "blocker_pr", _key(key),
                        {"retired_at": None}, {"retired_reason": reason})
        return "retired"

    def _link_blocker_signature(self, blocker_id: str, signature_id: str) -> str:
        b = self._row("SELECT * FROM blocker WHERE id=?", blocker_id)
        if b is None:
            raise RegistryError(f"no blocker {blocker_id}")
        sig = self.signature(signature_id)
        if sig is None:
            raise RegistryError(f"no signature {signature_id}")
        existing = self._row(
            "SELECT 1 FROM blocker_signature WHERE blocker_id=? AND signature_id=?",
            blocker_id, signature_id)
        if existing is not None:
            return "unchanged"
        self.db.execute(
            "INSERT INTO blocker_signature(blocker_id, signature_id, actor, created_at)"
            " VALUES (?, ?, ?, ?)",
            (blocker_id, signature_id, self.actor, now()),
        )
        self._event("blocker link", "blocker_signature", _key((blocker_id, signature_id)),
                    None, {"blocker_id": blocker_id, "signature_id": signature_id})
        if b["fix_issue"]:
            tc = self._row("SELECT * FROM testcase WHERE id=?", sig["testcase_id"])
            self._link(Testcase(**tc), b["fix_issue"], "tracks", signature_id)
        return "added"

    def link_blocker(self, blocker_id: str, *, signatures: list[str] = (),
                     rules: list[str] = ()) -> str:
        blocker_id = check_blocker_id(blocker_id)
        if not signatures and not rules:
            raise RegistryError("pass at least one --signature or --rule")
        with self.write():
            b = self._row("SELECT id FROM blocker WHERE id=?", blocker_id)
            if b is None:
                raise RegistryError(f"no blocker {blocker_id}")
            changed = False
            for sig_id in signatures:
                if self._link_blocker_signature(blocker_id, sig_id) != "unchanged":
                    changed = True
            for rule_id in rules:
                if self._set_rule_blocker(rule_id, blocker_id) != "unchanged":
                    changed = True
        return "updated" if changed else "unchanged"

    def _set_rule_blocker(self, rule_id: str, blocker_id: str) -> str:
        b = self._row("SELECT * FROM blocker WHERE id=?", blocker_id)
        if b is None:
            raise RegistryError(f"no blocker {blocker_id}")
        row = self._row("SELECT * FROM rule WHERE id=?", rule_id)
        if row is None:
            raise RegistryError(f"no rule {rule_id}")
        if row["blocker_id"] == blocker_id:
            return "unchanged"
        self.db.execute("UPDATE rule SET blocker_id=? WHERE id=?", (blocker_id, rule_id))
        self._event("blocker link", "rule", rule_id,
                    {"blocker_id": row["blocker_id"]}, {"blocker_id": blocker_id})
        if row["signature_id"]:
            self._link_blocker_signature(blocker_id, row["signature_id"])
        return "updated"

    def _blocker_signature_ids(self, blocker_id: str) -> list[str]:
        return [r[0] for r in self.db.execute(
            "SELECT signature_id FROM blocker_signature WHERE blocker_id=? ORDER BY signature_id",
            (blocker_id,))]

    def _hydrate_blocker(self, row: dict, *, include_retired_prs: bool = False) -> dict:
        b = dict(row)
        pr_sql = "SELECT * FROM blocker_pr WHERE blocker_id=?"
        if not include_retired_prs:
            pr_sql += " AND retired_at IS NULL"
        pr_sql += " ORDER BY required DESC, repository, pr_number"
        b["prs"] = [dict(r) for r in self.db.execute(pr_sql, (b["id"],))]
        sig_rows = self.db.execute(
            "SELECT s.id, s.testcase_id, s.label, t.suite, t.path"
            " FROM blocker_signature bs"
            " JOIN signature s ON s.id = bs.signature_id"
            " JOIN testcase t ON t.id = s.testcase_id"
            " WHERE bs.blocker_id=? ORDER BY t.suite, t.path, s.id",
            (b["id"],),
        ).fetchall()
        b["signatures"] = [dict(r) for r in sig_rows]
        b["rules"] = [dict(r) for r in self.db.execute(
            "SELECT id, signature_id, job_contains, issue, expires, retired_at"
            " FROM rule WHERE blocker_id=? ORDER BY id", (b["id"],))]
        return b

    def blocker(self, blocker_id: str, *, include_retired_prs: bool = False) -> dict | None:
        row = self._row("SELECT * FROM blocker WHERE id=?", blocker_id)
        if row is None:
            return None
        return self._hydrate_blocker(row, include_retired_prs=include_retired_prs)

    def blockers(self, *, active_only: bool = True) -> list[dict]:
        sql = "SELECT * FROM blocker"
        if active_only:
            sql += " WHERE retired_at IS NULL"
        sql += " ORDER BY id"
        return [self._hydrate_blocker(dict(r)) for r in self.db.execute(sql)]

    def blockers_for_testcase(self, tc: Testcase, *, active_only: bool = True) -> list[dict]:
        sql = (
            "SELECT DISTINCT b.* FROM blocker b"
            " LEFT JOIN blocker_signature bs ON bs.blocker_id = b.id"
            " LEFT JOIN signature s ON s.id = bs.signature_id"
            " LEFT JOIN rule r ON r.blocker_id = b.id"
            " LEFT JOIN signature rs ON rs.id = r.signature_id"
            " WHERE (s.testcase_id = ? OR rs.testcase_id = ?)"
        )
        if active_only:
            sql += " AND b.retired_at IS NULL"
        sql += " ORDER BY b.id"
        return [self._hydrate_blocker(dict(r)) for r in self.db.execute(sql, (tc.id, tc.id))]

    def blocker_for_rule(self, rule: dict) -> dict | None:
        """Resolve the blocker associated with a rule: explicit blocker_id, else via signature."""
        if rule.get("blocker_id"):
            return self.blocker(rule["blocker_id"])
        if rule.get("signature_id"):
            row = self._row(
                "SELECT b.* FROM blocker b"
                " JOIN blocker_signature bs ON bs.blocker_id = b.id"
                " WHERE bs.signature_id = ? AND b.retired_at IS NULL"
                " ORDER BY b.id LIMIT 1",
                rule["signature_id"],
            )
            if row:
                return self._hydrate_blocker(row)
        return None

    def add_rule(self, rule_id, *, sig_id, job_contains, issue, expires, not_covered,
                 approved_by, blocker_id=None) -> str:
        if (sig_id is None) == (job_contains is None):
            raise RegistryError("a rule needs exactly one of --signature and --job-contains")
        if issue is None and expires is None and blocker_id is None:
            raise RegistryError("a rule without --issue or --blocker needs --expires")
        if issue is not None:
            check_issue_key(issue)
        if expires is not None:
            expires = check_date(expires)
        if blocker_id is not None:
            blocker_id = check_blocker_id(blocker_id)
            b = self._row("SELECT * FROM blocker WHERE id=?", blocker_id)
            if b is None:
                raise RegistryError(f"no blocker {blocker_id}")
            if b["retired_at"] is not None:
                raise RegistryError(f"blocker {blocker_id} is retired")
            if issue is None and b["fix_issue"]:
                issue = b["fix_issue"]
        sig = None
        if sig_id is not None:
            sig = self.signature(sig_id)
            if sig is None:
                raise RegistryError(f"no signature {sig_id}")
        after = {"signature_id": sig_id, "job_contains": job_contains, "issue": issue,
                 "expires": expires, "blocker_id": blocker_id, "not_covered": not_covered}
        with self.write():
            existing = self._row("SELECT * FROM rule WHERE id=?", rule_id)
            if existing:
                if {k: existing[k] for k in after} == after and existing["retired_at"] is None:
                    return "unchanged"
                raise RegistryError(f"rule {rule_id} exists with other content or is retired")
            self.db.execute(
                "INSERT INTO rule(id, signature_id, job_contains, issue, expires, blocker_id,"
                " not_covered, approved_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (rule_id, sig_id, job_contains, issue, expires, blocker_id,
                 not_covered, approved_by, now()))
            self._event("rule add", "rule", rule_id, None, dict(after, approved_by=approved_by))
            if issue is not None and sig is not None:
                tc = self._row("SELECT * FROM testcase WHERE id=?", sig["testcase_id"])
                self._link(Testcase(**tc), issue, "tracks", sig_id)
            if blocker_id is not None and sig is not None:
                self._link_blocker_signature(blocker_id, sig_id)
        return "added"

    def retire_rule(self, rule_id, reason) -> str:
        with self.write():
            row = self._row("SELECT * FROM rule WHERE id=?", rule_id)
            if row is None:
                raise RegistryError(f"no rule {rule_id}")
            if row["retired_at"]:
                return "unchanged"
            self.db.execute("UPDATE rule SET retired_at=?, retired_reason=? WHERE id=?",
                            (now(), reason, rule_id))
            self._event("rule retire", "rule", rule_id, {"retired_at": None},
                        {"retired_reason": reason})
        return "retired"

    def rules(self, *, tc: Testcase | None = None, active_only=True):
        sql = "SELECT rule.* FROM rule LEFT JOIN signature ON signature.id = rule.signature_id"
        where, args = [], []
        if tc is not None:
            where.append("signature.testcase_id = ?")
            args.append(tc.id)
        if active_only:
            where.append("rule.retired_at IS NULL")
        if where:
            sql += " WHERE " + " AND ".join(where)
        return [dict(r) for r in self.db.execute(sql + " ORDER BY rule.id", args)]

    def job_rules(self):
        return [r for r in self.rules() if r["job_contains"] is not None]

    def events(self, tc: Testcase | None = None, limit=50):
        rows = [dict(r) for r in self.db.execute("SELECT * FROM event ORDER BY id DESC")]
        if tc is not None:
            sig_ids = {s["id"] for s in self.signatures(tc)}
            rule_ids = {r["id"] for r in self.rules(tc=tc, active_only=False)}
            blocker_ids = {b["id"] for b in self.blockers_for_testcase(tc, active_only=False)}
            note_ids = {str(r["id"]) for r in self.db.execute(
                "SELECT id FROM note WHERE testcase_id=?", (tc.id,))}

            def mine(e):
                key, parts = e["entity_key"], e["entity_key"].split("|")
                match e["entity"]:
                    case "signature":
                        return key in sig_ids
                    case "rule":
                        return key in rule_ids
                    case "note":
                        return key in note_ids
                    case "issue_link":
                        return parts[0] == str(tc.id)
                    case "occurrence":
                        return parts[4] == str(tc.id)
                    case "blocker":
                        return key in blocker_ids
                    case "blocker_pr":
                        return parts[0] in blocker_ids
                    case "blocker_signature":
                        return parts[0] in blocker_ids or parts[1] in sig_ids
                return False

            rows = [e for e in rows if mine(e)]
        return rows[:limit]

    def validate(self) -> list[str]:
        problems = []
        for row in self.db.execute("PRAGMA foreign_key_check"):
            problems.append(f"foreign key: {tuple(row)}")
        for r in self.db.execute(
            "SELECT o.run_id, o.signature_id FROM occurrence_signature o"
            " JOIN signature s ON s.id = o.signature_id WHERE s.testcase_id <> o.testcase_id"):
            problems.append(f"run {r[0]} is tagged with {r[1]} of another testcase")
        for r in self.rules():
            if r["issue"] and r["signature_id"]:
                sig = self.signature(r["signature_id"])
                has = self._row(
                    "SELECT 1 FROM issue_link"
                    " WHERE testcase_id=? AND issue=? AND relation='tracks'",
                    sig["testcase_id"], r["issue"])
                if not has:
                    problems.append(f"rule {r['id']} has no tracks link to {r['issue']}")
        for r in self.db.execute(
            "SELECT issue FROM issue_link UNION SELECT issue FROM rule"
            " UNION SELECT culprit_issue FROM blocker UNION SELECT fix_issue FROM blocker"
        ):
            if r[0] is not None and not ISSUE_KEY.match(r[0]):
                problems.append(f"bad issue key {r[0]!r}")
        for r in self.db.execute("SELECT id, culprit_commit FROM blocker"):
            if r[1] is not None and not COMMIT_SHA.match(r[1]):
                problems.append(f"blocker {r[0]} has bad culprit_commit {r[1]!r}")
        for r in self.db.execute("SELECT id, required_text FROM signature"):
            if not isinstance(json.loads(r[1]), list):
                problems.append(f"signature {r[0]} required_text is not a list")
        return problems

    def backup(self, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(dest)
        with target:
            self.db.backup(target)
        target.close()
        return dest


def _dump(value):
    return None if value is None else json.dumps(value, sort_keys=True, ensure_ascii=False)


def _key(parts) -> str:
    return "|".join(str(p) for p in parts)
