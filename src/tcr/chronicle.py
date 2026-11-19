"""Read testcase identity and failure history from Chronicle (`cubrid-tcc`)."""

import json
import shlex
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor

FAILED = ("FAIL", "ERROR")


class ChronicleError(Exception):
    pass


class AmbiguousTestcase(ChronicleError):
    """cubrid-tcc exit 3: the case matches several testcases."""


# The cubrid-tcc 0.3.0 contract: schema_version per command, and the keys we read without a default.
SCHEMA = {"find": (2, ("candidates",)), "history": (2, ("occurrences",)),
          "explain": (2, ()), "status": (1, ("status", "freshness")), "coverage": (1, ())}
TIMEOUT = 300  # seconds; a cold history or status read takes about 40 s
SHOWN_CANDIDATES = 5


def _document(text: str) -> dict | None:
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _ambiguity(cmd: str, data: dict) -> AmbiguousTestcase:
    cands = data.get("candidates") or []
    total = data.get("candidate_count", len(cands))
    first = "; ".join(f"{c.get('suite')} {c.get('path')}" for c in cands[:SHOWN_CANDIDATES])
    return AmbiguousTestcase(f"cubrid-tcc {cmd}: ambiguous testcase, {total} candidates."
                             f" First {min(SHOWN_CANDIDATES, len(cands))}: {first}")


def _run(*args: str) -> dict:
    cmd = args[0]
    exe = shutil.which("cubrid-tcc")
    if exe is None:
        raise ChronicleError("cubrid-tcc is not on PATH")
    try:
        proc = subprocess.run([exe, *args, "--json"], capture_output=True, text=True,
                              timeout=TIMEOUT)
    except subprocess.TimeoutExpired as e:
        raise ChronicleError(f"cubrid-tcc {cmd} timed out after {TIMEOUT} s") from e
    # Errors arrive as one JSON document on stdout, or in the older form on stderr.
    data = _document(proc.stdout) or (_document(proc.stderr) if proc.returncode else None)
    if data is None:
        raise ChronicleError(f"cubrid-tcc {cmd} exited {proc.returncode}: "
                             f"{(proc.stderr.strip() or proc.stdout.strip())[:300]}")
    if proc.returncode == 3 or data.get("error") == "ambiguous_identity":
        raise _ambiguity(cmd, data)
    if proc.returncode or data.get("error"):
        raise ChronicleError(f"cubrid-tcc {cmd}: "
                             f"{data.get('message') or data.get('error') or proc.returncode}")
    version, required = SCHEMA[cmd]
    if data.get("schema_version") != version:
        raise ChronicleError(f"cubrid-tcc {cmd} returned schema_version "
                             f"{data.get('schema_version')!r}, expected {version}")
    missing = [k for k in required if k not in data]
    if missing:
        raise ChronicleError(f"cubrid-tcc {cmd} output lacks {', '.join(missing)}")
    return data


def find(case: str, suite: str | None) -> list[dict]:
    args = ["find", case] + (["--suite", suite] if suite else [])
    return _run(*args)["candidates"]


def history_and_status(path: str, suite: str) -> tuple[dict, dict]:
    with ThreadPoolExecutor(max_workers=2) as pool:
        h = pool.submit(_run, "history", path, "--suite", suite)
        s = pool.submit(_run, "status", path, "--suite", suite)
        return h.result(), s.result()


def passes_recorded(occurrences: list[dict]) -> bool:
    """False when every occurrence comes from a failure list, which says nothing about passes."""
    return not occurrences or any(o["evidence"] != "failed.list" for o in occurrences)


def occurrence(o: dict) -> dict:
    """Keep the fields the view needs, with the target branch resolved."""
    pr = o.get("pull_request") or {}
    return {
        "repository": o["repository"],
        "run_id": o["run_id"],
        "attempt": o["attempt"],  # None means Chronicle could not resolve it
        "evidence": o.get("evidence"),
        "shard": str(o["shard"]),
        "ordinal": o["ordinal"],
        "outcome": o["outcome"],
        "completed_at": o.get("completed_at"),
        "engine_sha": o.get("engine_sha"),
        "build_mode": o.get("build_mode"),
        "pr": pr.get("number"),
        "branch": pr.get("base_ref") or o.get("engine_branch") or "unknown",
    }


def sync_hint(status: dict) -> str | None:
    """Chronicle's own refresh command for this testcase, or None when nothing a sync clears."""
    argv = (status.get("freshness") or {}).get("suggested_sync")
    return shlex.join(argv) if argv else None
