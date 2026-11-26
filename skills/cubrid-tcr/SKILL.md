---
name: cubrid-tcr
description: Check and maintain CUBRID CI testcase knowledge with cubrid-tcr: failure signatures, JIRA issue links, notes, human-approved known-issue rules, blockers, and PR unblock readiness. Use when checking whether a testcase or job failure is a known issue, viewing everything known about a testcase, recording signatures/links/notes, proposing or auditing known-issue rules, tracking CI blockers, or checking whether a PR is unblocked on develop.
---

# cubrid-tcr (CUBRID TC Registry)

`cubrid-tcr` stores curated per-testcase knowledge in `~/.local/share/cubrid-tc-registry/registry.sqlite3` (`$TCR_DB`) and evaluates failures alongside `cubrid-tcc` history, JIRA, GitHub, and local Git checkouts.

## Inspect a testcase

```bash
cubrid-tcr show cbrd_27179
cubrid-tcr show cbrd_27179 --days 30 --live --json
cubrid-tcr show sql/_36_guava/cbrd_27461/cases/cbrd_27461.sql --suite sql --all
```

- `show` reads failure history from `cubrid-tcc` and prints PR/branch/signature breakdowns alongside `links`, `signatures`, `blockers`, `rules`, and `notes`.
- If `history.status` is `NOT_INDEXED` or `history.freshness` is `STALE`/`UNKNOWN`, report the incomplete history. If `sync_hint` suggests a date window (`--since`/`--until`), report the gap rather than running a slow window sync inline.

## Check failures against known-issue rules

```bash
# Batch-check one cubrid-ci suite attempt directory (uses shard build.sha & message.txt)
cubrid-tcr --json check --ci-attempt "$SUITE_DIR" --record

# Check one testcase failure (core backtrace or NOK line in --stack-file)
cubrid-tcr --json check --case cbrd_27179 --suite shell --engine cd24a21 --stack-file core.txt

# Check a job-level failure
cubrid-tcr --json check --job "shard sql_by_cci 01" --engine cd24a21

# Offline check (skips JIRA and GitHub reads; still reads local Chronicle)
cubrid-tcr --json check --ci-attempt "$SUITE_DIR" --offline
```

- Online `check` requires `--engine-repo` or `$TCR_ENGINE_REPO` pointing to a `CUBRID/cubrid` clone (otherwise exits `2`). `check` does not run `git fetch`; a stale `origin/develop` can hide a recently merged fix commit unless GitHub Compare resolves it.
- `--ci-attempt` reads `summary.json` (schema 2 or 3) and `failures/*/metadata.json` (schema 2). If `summary.json` has `rerun.mode == "carried_forward"`, check the source attempt instead.
- For `test_sql_by_cci` (`sql_by_cci`), `check` evaluates matching `sql` signature rules and job rules while keeping `history.available: false` and skipping `--record`.
- `--record` tags `occurrence_signature` only when `message.txt` matches **exactly one** signature on that testcase (`added`, `unchanged`, `no_stack`, `no_single_match`, or `no_occurrence`).

### Verdicts and precedence

| Verdict | Meaning |
|---|---|
| `no_match` | No active rule matched; analyze as a new/untracked failure |
| `known` | Signature rule matched, unexpired, and fix is not yet merged/resolved |
| `known_job` | Job rule (`--job-contains`) matched; job is noisy, individual symptom still needs analysis (`known` outranks `known_job`) |
| `fix_missing` | Issue is `Fixed`/`Done` or required fix PR merged, but the tested commit lacks the fix |
| `fix_present` | Fix commit is already present in the tested commit yet the failure recurred — re-analyze |
| `expired` | Matched rule is past `--expires` — verify and extend or retire |
| `unknown` | Missing stack/text, `--offline`, JIRA/GitHub error, or non-`Fixed`/`Done` resolution |

When multiple rules match, `verdict` adopts the most conservative status: `fix_present > expired > unknown > fix_missing > known > known_job`.

## Record findings (idempotent, no approval needed)

`signature`, `link`, and `note` never change a `check` verdict, so record them freely (repeats report `unchanged`):

```bash
# Core signature (innermost frame first; omit line numbers and run-specific paths)
cubrid-tcr signature add cbrd_27179 --suite shell --id sig-27179-oom \
  --label "pgbuf_fix assertion" --frames pgbuf_fix_debug,heap_get_class_info --text "Assertion failed"

# Text-only signature for NOK failures without a core dump
cubrid-tcr signature add cbrd_27179 --suite shell --id sig-27179-nok \
  --label "NOK stat.log empty" --text "NOK stat.log is empty"

# Attach a signature to an indexed Chronicle occurrence (or omit --signature to test parsing)
cubrid-tcr signature attach cbrd_27179 --suite shell --run 38868725373 --shard 01 --stack-file message.txt

# Link an issue (origin | tracks | suspected | fixed_by) or append a note
cubrid-tcr link cbrd_27179 --suite shell --issue CBRD-28395 --relation suspected
cubrid-tcr note cbrd_27179 --suite shell --body "Observed on develop before PR #8537" --ref "run:38868725373"
```

For `test_sql_by_cci` failures, register signatures under `--suite sql` because `check` applies `sql` signature rules to `test_sql_by_cci`.

## Known-issue rules (human approval gate)

Only a `rule` alters `check` verdicts. **Never pass `--yes` without explicit human approval.**

1. Propose the rule without `--yes` (dry-run) and show the printed rule to the user:
   ```bash
   cubrid-tcr rule add --id rule-27179 --signature sig-27179-oom \
     --issue CBRD-28395 --blocker CBRD-28395 \
     --not-covered "Different assertion or stack frames"
   ```
2. After explicit user approval, rerun with `--yes --approved-by <NAME>`.
3. Verify with `check` that the motivating failure returns `known` and unrelated failure text returns `no_match`.
4. Job-level rules without an issue use `--job-contains TEXT --expires YYYY-MM-DD` and yield `known_job`.
5. Audit stale/resolved/expiring rules with `cubrid-tcr rules --audit` and retire finished ones with `cubrid-tcr rule retire <ID> --reason "..."`.

## Track blockers and PR unblock readiness

```bash
# Register or update a blocker (auto-hydrates culprit PR/commit metadata unless --offline)
cubrid-tcr blocker add CBRD-28395 --culprit engine#8537 --fix-issue CBRD-28395 --signature sig-27179-oom
cubrid-tcr blocker pr add CBRD-28395 engine#8610
cubrid-tcr blocker link CBRD-28395 --rule rule-27179

# Inspect live blocker readiness & affected open PRs
cubrid-tcr blockers --affected-days 7 --json
cubrid-tcr blockers --key CBRD-28395 --json

# Check whether a PR's latest failures are unblocked on develop
cubrid-tcr unblock --pr 8753 --json
```

`unblock_action` recommendations: `REBASE_ENGINE`, `REBASE_ENGINE_AND_SYNC_TC`, `RERUN_OR_SYNC_TC`, `RETIRE_RULE`.

## Suites, exit codes, and maintenance

- **Suites**: `sql`, `medium`, `shell` (and `test_*` aliases); `show` and `check` also accept `sql_by_cci` / `test_sql_by_cci`.
- **Exit codes**: `0` (any verdict or success), `1` (`validate` invariant violation), `2` (usage or operational error).
- **Maintenance**: `cubrid-tcr validate`, `cubrid-tcr log [CASE]`, `cubrid-tcr backup`, `cubrid-tcr export`.
- **Env vars**: `TCR_DB`, `TCR_ACTOR`, `TCR_ENGINE_REPO`, `TCR_TC_PUBLIC_REPO`, `TCR_TC_PRIVATE_REPO`, `CUBRID_JIRA_USER`, `CUBRID_JIRA_PASSWORD`.
