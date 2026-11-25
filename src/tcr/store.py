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

SIGNATURE_TABLE = """CREATE TABLE signature (
    id TEXT PRIMARY KEY,
    testcase_id INTEGER NOT NULL REFERENCES testcase(id),
    label TEXT NOT NULL,
    frames TEXT NOT NULL,
    required_text TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    CHECK (json_array_length(frames) > 0 OR json_array_length(required_text) > 0)
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
CREATE TABLE IF NOT EXISTS issue_link (
    testcase_id INTEGER NOT NULL REFERENCES testcase(id),
    issue TEXT NOT NULL,
    relation TEXT NOT NULL CHECK (relation IN ('origin', 'tracks', 'suspected', 'fixed_by')),
    signature_id TEXT REFERENCES signature(id),
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (testcase_id, issue, relation)
);
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
CREATE TABLE IF NOT EXISTS rule (
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
        row = self._row(
            "SELECT signature_id FROM issue_link WHERE testcase_id=? AND issue=? AND relation=?",
            tc.id, issue, relation)
        if row is not None:
            if row["signature_id"] == sig_id or sig_id is None:
                return "unchanged"
            self.db.execute(
                "UPDATE issue_link SET signature_id=?"
                " WHERE testcase_id=? AND issue=? AND relation=?",
                (sig_id, tc.id, issue, relation))
            self._event("link", "issue_link", _key((tc.id, issue, relation)), row,
                        {"signature_id": sig_id})
            return "updated"
        self.db.execute(
            "INSERT INTO issue_link VALUES (?, ?, ?, ?, ?, ?)",
            (tc.id, issue, relation, sig_id, self.actor, now()))
        self._event("link", "issue_link", _key((tc.id, issue, relation)), None,
                    {"signature_id": sig_id})
        return "added"

    def links(self, tc: Testcase):
        return [dict(r) for r in self.db.execute(
            "SELECT issue, relation, signature_id, actor, created_at FROM issue_link"
            " WHERE testcase_id=? ORDER BY relation, issue", (tc.id,))]

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

    def add_rule(self, rule_id, *, sig_id, job_contains, issue, expires, not_covered,
                 approved_by) -> str:
        if (sig_id is None) == (job_contains is None):
            raise RegistryError("a rule needs exactly one of --signature and --job-contains")
        if issue is None and expires is None:
            raise RegistryError("a rule without --issue needs --expires")
        if issue is not None:
            check_issue_key(issue)
        if expires is not None:
            expires = check_date(expires)
        sig = None
        if sig_id is not None:
            sig = self.signature(sig_id)
            if sig is None:
                raise RegistryError(f"no signature {sig_id}")
        after = {"signature_id": sig_id, "job_contains": job_contains, "issue": issue,
                 "expires": expires, "not_covered": not_covered}
        with self.write():
            existing = self._row("SELECT * FROM rule WHERE id=?", rule_id)
            if existing:
                if {k: existing[k] for k in after} == after and existing["retired_at"] is None:
                    return "unchanged"
                raise RegistryError(f"rule {rule_id} exists with other content or is retired")
            self.db.execute(
                "INSERT INTO rule(id, signature_id, job_contains, issue, expires, not_covered,"
                " approved_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (rule_id, sig_id, job_contains, issue, expires, not_covered, approved_by, now()))
            self._event("rule add", "rule", rule_id, None, dict(after, approved_by=approved_by))
            if issue is not None and sig is not None:
                tc = self._row("SELECT * FROM testcase WHERE id=?", sig["testcase_id"])
                self._link(Testcase(**tc), issue, "tracks", sig_id)
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
        for r in self.db.execute("SELECT issue FROM issue_link UNION SELECT issue FROM rule"):
            if r[0] is not None and not ISSUE_KEY.match(r[0]):
                problems.append(f"bad issue key {r[0]!r}")
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
