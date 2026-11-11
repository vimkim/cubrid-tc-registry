"""Read testcase identity and failure history from Chronicle (`cubrid-tcc`)."""

import json
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor

FAILED = ("FAIL", "ERROR")


class ChronicleError(Exception):
    pass


def _run(*args: str) -> dict:
    exe = shutil.which("cubrid-tcc")
    if exe is None:
        raise ChronicleError("cubrid-tcc is not on PATH")
    proc = subprocess.run([exe, *args, "--json"], capture_output=True, text=True)
    try:
        data = json.loads(proc.stdout)
    except ValueError as e:
        raise ChronicleError(f"cubrid-tcc {args[0]} exited {proc.returncode}: "
                             f"{proc.stderr.strip()[:300]}") from e
    if proc.returncode == 2 or data.get("error"):
        raise ChronicleError(f"cubrid-tcc {args[0]}: {data.get('message', proc.returncode)}")
    return data


def find(case: str, suite: str | None) -> list[dict]:
    args = ["find", case] + (["--suite", suite] if suite else [])
    return _run(*args)["candidates"]


def history_and_status(path: str, suite: str) -> tuple[dict, dict]:
    with ThreadPoolExecutor(max_workers=2) as pool:
        h = pool.submit(_run, "history", path, "--suite", suite)
        s = pool.submit(_run, "status", path, "--suite", suite)
        return h.result(), s.result()


def occurrence(o: dict) -> dict:
    """Keep the fields the view needs, with the target branch resolved."""
    pr = o.get("pull_request") or {}
    return {
        "repository": o["repository"],
        "run_id": o["run_id"],
        "attempt": o.get("attempt") or 0,
        "shard": str(o["shard"]),
        "ordinal": o["ordinal"],
        "outcome": o["outcome"],
        "completed_at": o.get("completed_at"),
        "engine_sha": o.get("engine_sha"),
        "build_mode": o.get("build_mode"),
        "pr": pr.get("number"),
        "branch": pr.get("base_ref") or o.get("engine_branch") or "unknown",
    }


def sync_hint(status: dict, suite: str, today: str) -> str | None:
    fresh = status.get("freshness") or {}
    if not fresh.get("needs_refresh"):
        return None
    since = (fresh.get("discovery_through") or "")[:10] or today
    return (f"cubrid-tcc sync-data --since {since} --until {today} --suite {suite}"
            " --revisit-limit 0 --json")
