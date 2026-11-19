# cubrid-tc-registry

`cubrid-tcr` keeps curated knowledge about CUBRID CI testcases and shows it next to the testcase's failure history.

Chronicle (`cubrid-tcc`) owns the failure history. This tool stores only what Chronicle lacks:

- Failure signatures: ordered stack frames plus required text, or required text alone for NOK failures.
- Signature tags on Chronicle failure occurrences.
- Issue links. The relation is `origin`, `tracks`, `suspected`, or `fixed_by`.
- Notes.
- Known-issue rules. A rule is the only record that changes a CI verdict, and only a person can approve one.

The design is `docs/testcase-registry-design.md` in `my-cubrid-skills`.

## Commands

| Command | Purpose |
|---|---|
| `cubrid-tcr show CASE [--days N \| --all]` | Show failures by PR, branch, and signature, plus issue links, signatures, rules, and notes. The view lists every indexed failure. Past 50 failures, it lists the last 60 days and summarizes the older ones. |
| `cubrid-tcr check (--case CASE \| --job NAME) --engine SHA [--stack-file F]` | Return `no_match`, `expired`, `known`, `known_job`, `fix_missing`, `fix_present`, or `unknown`. `--stack-file` holds the failure text, a core backtrace or a failure message. With `--case`, the result also includes the `show` view. |
| `cubrid-tcr check --ci-attempt DIR [--record] [--offline]` | Check every failure of one suite directory that `cubrid-ci` collected (`.../attempts/N/test_shell`). One JSON document, one entry per failure. See below. |
| `cubrid-tcr signature add CASE --id ID --label TEXT [--frames F1,F2] [--text T]...` | Name a failure shape. List the frames innermost first. `--frames` is optional when `--text` is given; a text-only signature matches a NOK line or other failure message. |
| `cubrid-tcr signature attach CASE --run ID --shard N --stack-file F [--signature ID]` | Tag one Chronicle failure with the signature its stack matches. |
| `cubrid-tcr link CASE --issue KEY --relation R` | Relate the testcase to an issue. |
| `cubrid-tcr note CASE --body TEXT [--ref REF]` | Leave a note. |
| `cubrid-tcr rule add --id ID (--signature ID \| --job-contains TEXT) ...` | Print the proposed rule. With `--yes --approved-by NAME`, write it. |
| `cubrid-tcr rule retire ID --reason TEXT` | Stop applying a rule. The rule stays in history. |
| `cubrid-tcr validate`, `log`, `backup`, `export` | Check invariants, read the write history, save a snapshot, or print TOML for review. |

`CASE` is a full testcase path, or a fragment that `cubrid-tcc find` resolves to exactly one testcase.

`check` reads Jira anonymously (`/rest/api/2/issue/<KEY>?fields=status,resolution`) and reads Git in `--engine-repo`, which defaults to `$TCR_ENGINE_REPO`. Without `--offline`, `check` stops with code 2 when neither names a Git checkout. It does not fetch. A stale `origin/develop` there can hide a fix commit.

Every command exits with code 0 for any verdict and with code 2 for an operational error. Repeating a write command reports `unchanged`.

### Check a whole CI attempt

`check --ci-attempt DIR` reads `DIR/summary.json` and `DIR/failures/*/metadata.json` and exits 2 unless both have `schema_version` 2. For each failure it takes the suite, testcase path, run, attempt and shard from the bundle, and the tested Engine SHA from the shard's `build.sha` (not the workflow head). It passes `message.txt` to the stack matcher, which separates several cores. It evaluates the testcase's signature rules and the job rules for the failure's job (`shard <suite> <shard>`, such as `shard sql_by_cci 01`), so one call covers both. `test_sql_by_cci` has no Chronicle suite, so its entries get job rules only and `history.available` is false. Each testcase's history is read once, even when it failed in several shards. `--engine-repo`, `TCR_ENGINE_REPO` and `--offline` work as in single `check`; `--offline` still reads Chronicle.

Each entry holds `identity`, `verdict`, `hits`, `reason`, `stack_found` and `history` (counts only; `show` prints the full view). Without `--record` nothing is written. With it, an occurrence gets the testcase's signature when exactly one matches, the same write as `signature attach`; the entry's `record.result` says `added`, `unchanged`, `no_stack` (no failure text), `no_single_match` or `no_occurrence` (Chronicle has not indexed that run).

A job rule (`--job-contains`) never answers `known`. It answers `known_job`: the job is known noisy and each failure's symptom still needs analysis. A signature rule's `known` outranks it. `--ci-attempt` passes each failure's `message.txt` to every signature rule. A frames signature with no parsed stack answers `unknown`; so does a text-only signature when no text exists.

## Storage

Opening the database rebuilds its `signature` table once when it predates text-only signatures; every row is kept. The database is `~/.local/share/cubrid-tc-registry/registry.sqlite3`. Set `TCR_DB` or pass `--database` to use another file. `TCR_ACTOR` or `--actor` names the writer in the `event` table.

## Development

```bash
just verify   # unittest and ruff
just sync     # install cubrid-tcr from the primary main checkout
```
