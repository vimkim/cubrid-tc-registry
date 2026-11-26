"""Human output rendering with lazy-imported rich on TTYs and plain fallback."""

from __future__ import annotations

import os
import sys

_READINESS_STYLE = {
    "READY": "bold green",
    "PARTIAL": "bold yellow",
    "IN_PROGRESS": "cyan",
    "NEEDS_FIX_PR": "bold red",
    "BRANCH_MISMATCH": "bold magenta",
    "UNTRACKED": "dim",
    "UNKNOWN": "yellow",
    "READY TO UNBLOCK": "bold green",
    "STILL BLOCKED": "bold red",
    "NEEDS ANALYSIS": "bold yellow",
    "PASSING / NO FAILURES": "bold green",
}


def use_rich(no_color: bool = False) -> bool:
    if no_color or os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def _format_culprit(c: dict) -> str:
    parts = []
    if c.get("pr"):
        repo = c.get("repository") or "CUBRID/cubrid"
        parts.append(f"{repo}#{c['pr']}")
    if c.get("commit"):
        parts.append(c["commit"][:10])
    meta = [x for x in (c.get("issue"), c.get("author")) if x]
    if meta:
        parts.append(f"({', '.join(meta)})")
    if parts and c.get("confidence") == "suspected":
        parts.append("[suspected]")
    return " ".join(parts) if parts else "-"


def _format_owner(owner: str | None, source: str | None) -> str:
    if not owner:
        return "-"
    return f"{owner} ({source})" if source and source != "unassigned" else owner


def _format_prs(prs: list[dict]) -> str:
    if not prs:
        return "none"
    parts = []
    for p in prs:
        tag = "" if p.get("required", True) else " (optional)"
        author = f", {p['author']}" if p.get("author") else ""
        base = f" -> {p['base_ref']}" if p.get("base_ref") else ""
        parts.append(f"{p['repository']}#{p['pr_number']} [{p['state']}{base}{author}]{tag}")
    return "; ".join(parts)


def render_blockers(report: dict, *, no_color: bool = False) -> str:
    items = report.get("blockers") or []
    untracked = report.get("untracked_rules") or []
    missing = report.get("missing_keys") or []
    if not items and not untracked and not missing:
        return "no blockers"

    if use_rich(no_color):
        from io import StringIO

        from rich.console import Console
        from rich.table import Table
        from rich.text import Text

        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=120)
        if items:
            table = Table(title="TCR Blockers", show_lines=False, pad_edge=False)
            table.add_column("State", no_wrap=True)
            table.add_column("Blocker", no_wrap=True)
            table.add_column("Owner")
            table.add_column("Culprit")
            table.add_column("Fix / PRs")
            for b in items:
                r = b["readiness"]
                state_txt = Text(r, style=_READINESS_STYLE.get(r, ""))
                owner_txt = _format_owner(b.get("owner"), b.get("owner_source"))
                culprit_txt = _format_culprit(b.get("culprit") or {})
                fix_lines = []
                if b.get("fix_issue"):
                    iss = b.get("issue") or {}
                    st = (
                        f"{iss.get('status')}/{iss.get('resolution') or 'Unresolved'}"
                        if iss.get("readable")
                        else (iss.get("error") or "not checked")
                    )
                    fix_lines.append(f"{b['fix_issue']} [{st}]")
                fix_lines.append(_format_prs(b.get("fix_prs") or []))
                if b.get("unblock_action"):
                    fix_lines.append(f"action: {b['unblock_action']}")
                for f in b.get("flags") or []:
                    fix_lines.append(f"FLAG: {f}")
                if b.get("affected_open_prs"):
                    prs_s = ", ".join(
                        f"#{ap['pr']}({ap.get('testcase_branch') or ap.get('base_ref') or '?'})"
                        for ap in b["affected_open_prs"]
                    )
                    fix_lines.append(f"affected open PRs: {prs_s}")
                table.add_row(state_txt, b["id"], owner_txt, culprit_txt, "\n".join(fix_lines))
            console.print(table)
        if untracked:
            console.print("[dim]Untracked active rules (no blocker):[/dim]")
            for r in untracked:
                console.print(
                    f"  - {r['id']} (issue {r.get('issue') or '-'},"
                    f" expires {r.get('expires') or '-'})"
                )
        if missing:
            console.print(f"[yellow]Missing keys:[/yellow] {', '.join(missing)}")
        return buf.getvalue().rstrip("\n")

    lines = []
    for b in items:
        cases = sorted({s["path"].rsplit("/", 1)[-1] for s in b.get("signatures") or []})
        case_s = f"  ({', '.join(cases)})" if cases else ""
        lines.append(f"[{b['readiness']}] {b['id']}{case_s}")
        lines.append(f"  culprit:  {_format_culprit(b.get('culprit') or {})}")
        owner_s = _format_owner(b.get("owner"), b.get("owner_source"))
        if b.get("fix_issue"):
            iss = b.get("issue") or {}
            st = (
                f"{iss.get('status')}/{iss.get('resolution') or 'Unresolved'}"
                if iss.get("readable")
                else (iss.get("error") or "not checked")
            )
            lines.append(f"  fix:      {b['fix_issue']} [{st}]  owner: {owner_s}")
        else:
            lines.append(f"  owner:    {owner_s}")
        lines.append(f"  fix PRs:  {_format_prs(b.get('fix_prs') or [])}")
        if b.get("unblock_action"):
            lines.append(f"  action:   {b['unblock_action']}")
        if b.get("rules"):
            lines.append(f"  rules:    {', '.join(b['rules'])}")
        for flag in b.get("flags") or []:
            lines.append(f"  FLAG:     {flag}")
        if b.get("affected_open_prs"):
            prs_s = ", ".join(
                f"#{ap['pr']} ({ap.get('testcase_branch') or ap.get('base_ref') or '?'})"
                for ap in b["affected_open_prs"]
            )
            lines.append(f"  open PRs: {prs_s}")
    if untracked:
        lines.append("untracked active rules:")
        for r in untracked:
            lines.append(
                f"  {r['id']} (issue {r.get('issue') or '-'}, expires {r.get('expires') or '-'})"
            )
    if missing:
        lines.append(f"missing keys: {', '.join(missing)}")
    return "\n".join(lines)


def render_unblock(report: dict, *, no_color: bool = False) -> str:
    pr = report["pull_request"]
    header = (
        f"PR #{pr['number']} ({pr.get('state') or 'UNKNOWN'}"
        f", base: {pr.get('base_ref') or '?'}"
        f", head: {pr.get('head_ref') or '?'})"
    )
    if not report["suites"]:
        return f"{header}\n  no indexed failures in latest runs"

    rich_on = use_rich(no_color)
    lines = [header]
    for suite_name, s in sorted(report["suites"].items()):
        eng = (s.get("engine_sha") or "?")[:12]
        tc_br = s.get("testcase_branch") or "?"
        tc_sha = (s.get("testcase_sha") or "?")[:12]
        status = s["status"]
        lines.append(
            f"\n{suite_name}: {status}"
            f" (run {s.get('run_id') or '?'}, engine {eng}, tc {tc_br}@{tc_sha})"
        )
        for item in s["failures"]:
            sig = item.get("signature") or "-"
            if item.get("blocker"):
                b = item["blocker"]
                pres = item.get("fix_presence")
                pres_s = f" [in tested: {pres}]" if pres is not None else ""
                owner_s = _format_owner(b.get("owner"), b.get("owner_source"))
                lines.append(
                    f"  - {item['path']}  (sig: {sig}) -> blocker {b['id']}"
                    f" [{b['readiness']}{pres_s}] owner: {owner_s}"
                )
            elif item.get("rule"):
                r = item["rule"]
                lines.append(
                    f"  - {item['path']}  (sig: {sig}) -> rule {r['id']}"
                    f" [verdict: {r['verdict']}]"
                )
            else:
                lines.append(
                    f"  - {item['path']}  (sig: {sig}) -> UNCLASSIFIED (needs analysis)"
                )
        lines.append(f"  => action: {s['action']}")

    if rich_on:
        from io import StringIO

        from rich.console import Console
        from rich.markup import escape

        buf = StringIO()
        console = Console(file=buf, force_terminal=True, highlight=False)
        ordered_styles = sorted(_READINESS_STYLE.items(), key=lambda kv: -len(kv[0]))
        for line in lines:
            styled = escape(line)
            for label, style in ordered_styles:
                if label in line:
                    styled = styled.replace(label, f"[{style}]{label}[/{style}]")
                    break
            console.print(styled)
        return buf.getvalue().rstrip("\n")
    return "\n".join(lines)
