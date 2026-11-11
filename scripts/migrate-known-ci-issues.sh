#!/usr/bin/env bash
# Load the three entries of my-cubrid-skills cubrid-common/references/known-ci-issues.md
# as of main 0b0707d into the registry. The issue-backed rule drops the entry's expiry date,
# because the Jira state decides its lifetime.
# Every command is idempotent, so a rerun reports "unchanged".
set -euo pipefail

: "${APPROVED_BY:?set APPROVED_BY to the person who approved the two rules}"
tcr=(${TCR:-cubrid-tcr})
case_path=shell/_37_elderberry/cbrd_23842_cdc/bug/cbrd_27179/cases/cbrd_27179.sh

"${tcr[@]}" signature add "$case_path" --suite shell --id cbrd_27179-vacuum-deallocated-page \
	--label "vacuum fixes a deallocated heap page as OLD_PAGE (debug assertion)" \
	--frames pgbuf_fix_debug,vacuum_heap_page,vacuum_heap,vacuum_process_log_block \
	--text was_interrupted=false
"${tcr[@]}" link "$case_path" --suite shell --issue CBRD-27179 --relation origin
"${tcr[@]}" rule add --id cbrd-28018-vacuum-deallocated-page \
	--signature cbrd_27179-vacuum-deallocated-page --issue CBRD-28018 \
	--not-covered "Another stack in this case, the same assertion in another testcase, and the RQG dead_data_03_big_record cores (a build without CBRD-27151, plus index-scan reader cores)." \
	--yes --approved-by "$APPROVED_BY"
"${tcr[@]}" note "$case_path" --suite shell \
	--body "CBRD-28018 is owned by another team. Intermittent, about 2% of runs. Seen on develop PRs #8469 and #8404 and on feature/oos-merge PR #8299 run 42868959374. Not OOS-specific. The vacuum.c line differs by branch: 1678 on develop, 1776 on feature/oos-merge at cd24a21." \
	--ref CUBRID/cubrid#8469 --ref CUBRID/cubrid#8404 --ref "CUBRID/cubrid#8299 run 42868959374"
"${tcr[@]}" rule add --id sql-by-cci-every-branch --job-contains sql_by_cci --expires 2026-12-09 \
	--not-covered "test_sql and other suites. It also fails on develop; feature/oos-merge run 42763653366 after the 2026-11-10 develop sync had 0 unique failures (my-cubrid-docs cbrd-oos-sql-by-cci/report.md)." \
	--yes --approved-by "$APPROVED_BY"
"${tcr[@]}" validate
