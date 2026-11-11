# cubrid-tc-registry

`cubrid-tcr` keeps curated knowledge about CUBRID CI testcases and shows it next to the testcase's failure history.

Chronicle (`cubrid-tcc`) owns the failure history. This tool stores only what Chronicle lacks:

- Failure signatures, which are ordered stack frames plus required text.
- Signature tags on Chronicle failure occurrences.
- Issue links. The relation is `origin`, `tracks`, `suspected`, or `fixed_by`.
- Notes.
- Known-issue rules. A rule is the only record that changes a CI verdict, and only a person can approve one.

The design is `docs/testcase-registry-design.md` in `my-cubrid-skills`.

## Commands

| Command | Purpose |
|---|---|
| `cubrid-tcr show CASE [--days N \| --all]` | Show failures by PR, branch, and signature, plus issue links, signatures, rules, and notes. The view lists every indexed failure. Past 50 failures, it lists the last 60 days and summarizes the older ones. |
| `cubrid-tcr check (--case CASE \| --job NAME) --engine SHA [--stack-file F]` | Return `no_match`, `expired`, `known`, `fix_missing`, `fix_present`, or `unknown`. With `--case`, the result also includes the `show` view. |
| `cubrid-tcr signature add CASE --id ID --label TEXT --frames F1,F2 [--text T]` | Name a failure shape. List the frames innermost first. |
| `cubrid-tcr signature attach CASE --run ID --shard N --stack-file F [--signature ID]` | Tag one Chronicle failure with the signature its stack matches. |
| `cubrid-tcr link CASE --issue KEY --relation R` | Relate the testcase to an issue. |
| `cubrid-tcr note CASE --body TEXT [--ref REF]` | Leave a note. |
| `cubrid-tcr rule add --id ID (--signature ID \| --job-contains TEXT) ...` | Print the proposed rule. With `--yes --approved-by NAME`, write it. |
| `cubrid-tcr rule retire ID --reason TEXT` | Stop applying a rule. The rule stays in history. |
| `cubrid-tcr validate`, `log`, `backup`, `export` | Check invariants, read the write history, save a snapshot, or print TOML for review. |

`CASE` is a full testcase path, or a fragment that `cubrid-tcc find` resolves to exactly one testcase.

`check` reads Jira anonymously (`/rest/api/2/issue/<KEY>?fields=status,resolution`) and reads Git in `--engine-repo`, which defaults to `/home/vimkim/gh/cb/develop`. It does not fetch. A stale `origin/develop` there can hide a fix commit.

Every command exits with code 0 for any verdict and with code 2 for an operational error. Repeating a write command reports `unchanged`.

## Storage

The database is `~/.local/share/cubrid-tc-registry/registry.sqlite3`. Set `TCR_DB` or pass `--database` to use another file. `TCR_ACTOR` or `--actor` names the writer in the `event` table.

## Development

```bash
just verify   # unittest and ruff
just sync     # install cubrid-tcr from the primary main checkout
```
