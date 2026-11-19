"""Read one suite directory of a CI attempt collected by `cubrid-ci`."""

import json
from pathlib import Path

from .store import RegistryError

# summary.json 3 adds `commit` and `rerun` (the source attempt of a rerun) to version 2.
SUMMARY_VERSIONS = (2, 3)
METADATA_VERSIONS = (2,)
# CI suite name -> Chronicle suite. test_sql_by_cci has no Chronicle suite.
CHRONICLE_SUITE = {"test_sql": "sql", "test_medium": "medium", "test_shell": "shell",
                   "test_sql_by_cci": None}


def _read(path: Path, what: str, versions: tuple[int, ...]) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise RegistryError(f"cannot read {what} {path}: {e}") from e
    if data.get("schema_version") not in versions:
        raise RegistryError(f"{path} has schema_version {data.get('schema_version')!r};"
                            f" this cubrid-tcr reads {', '.join(map(str, versions))}")
    return data


def load(directory: str) -> dict:
    """Return the attempt header and one record per failure, with its message text.

    `engine_sha` is the shard's tested Engine commit (`shards[].build.sha`), not the workflow head.
    """
    root = Path(directory)
    summary = _read(root / "summary.json", "summary", SUMMARY_VERSIONS)
    suite = summary.get("suite")
    if suite not in CHRONICLE_SUITE:
        raise RegistryError(f"unknown CI suite {suite!r} in {root / 'summary.json'}")
    shas = {s["index"]: s["build"]["sha"] for s in summary["shards"]}
    failures = []
    for entry in summary["failures"]:
        folder = root / "failures" / entry["stable_id"]
        meta = _read(folder / "metadata.json", "failure metadata", METADATA_VERSIONS)
        if meta["shard"] not in shas:
            raise RegistryError(f"{folder}: shard {meta['shard']} is not in summary.json shards")
        message = root / meta["message_path"]
        failures.append({
            "suite": suite,
            "path": meta["name"],
            "run": summary["run_id"],
            "attempt": summary["attempt"],
            "shard": meta["shard"],
            "engine_sha": shas[meta["shard"]],
            "result": meta["result"],
            "message": message.read_text(errors="replace") if message.is_file() else "",
        })
    header = {"dir": str(root), "suite": suite, "run": summary["run_id"],
              "attempt": summary["attempt"], "schema_version": summary["schema_version"],
              "ci_state": summary.get("ci_state"),
              "collection_state": summary.get("collection_state")}
    if summary.get("rerun"):
        # A carried_forward rerun ran no cases. Its failures are in the source attempt.
        header["rerun"] = summary["rerun"]
    return {"header": header, "failures": failures}
