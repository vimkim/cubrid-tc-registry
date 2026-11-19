"""Build the testcase view: Chronicle failures merged with the curated records."""

from collections import Counter
from datetime import UTC, datetime, timedelta

from . import chronicle

DETAIL_LIMIT = 50
SHORT_DAYS = 60


def window(failures: list[dict], *, days: int | None, show_all: bool, now: datetime):
    """Return (shown, older, mode). `older` is summarized instead of listed."""
    failures = sorted(failures, key=lambda f: f["completed_at"] or "", reverse=True)
    if show_all:
        return failures, [], "all"
    if days is None:
        if len(failures) <= DETAIL_LIMIT:
            return failures, [], "all"
        days, mode = SHORT_DAYS, f"shortened to {SHORT_DAYS} days past {DETAIL_LIMIT} failures"
    else:
        mode = f"last {days} days"
    cut = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    shown = [f for f in failures if (f["completed_at"] or "") >= cut]
    older = [f for f in failures if (f["completed_at"] or "") < cut]
    return shown, older, mode


def summarize(failures: list[dict]) -> dict:
    dates = sorted(f["completed_at"] for f in failures if f["completed_at"])
    return {
        "count": len(failures),
        "first": dates[0] if dates else None,
        "last": dates[-1] if dates else None,
        "by_branch": dict(Counter(f["branch"] for f in failures)),
        "by_signature": dict(Counter(f["signature"] or "unknown" for f in failures)),
    }


def passes_text(passes: int | None) -> str:
    return "passes not recorded (failure-list evidence)" if passes is None else f"{passes} passes"


def build(store, tc, *, days=None, show_all=False, now=None, fetched=None) -> dict:
    """`fetched` is a (history, status) pair or a ChronicleError already read for this testcase."""
    now = now or datetime.now(UTC)
    tags = store.occurrence_tags(tc)
    history = {"available": True}
    failures, passes, recorded = [], 0, True
    try:
        if isinstance(fetched, Exception):
            raise fetched
        hist, status = fetched or chronicle.history_and_status(tc.path, tc.suite)
    except chronicle.ChronicleError as e:
        history = {"available": False, "reason": str(e)}
    else:
        raws = hist["occurrences"]
        recorded = chronicle.passes_recorded(raws)
        for raw in raws:
            occ = chronicle.occurrence(raw)
            if occ["outcome"] not in chronicle.FAILED:
                passes += occ["outcome"] == "PASS"
                continue
            tag = tags.get((occ["repository"], occ["run_id"], occ["shard"], occ["ordinal"]))
            occ["signature"] = tag["signature_id"] if tag else None
            failures.append(occ)
        fresh = status.get("freshness") or {}
        history.update(
            status=status.get("status"),
            freshness=fresh.get("state"),
            discovery_through=fresh.get("discovery_through"),
            sync_hint=chronicle.sync_hint(status),
        )
    shown, older, mode = window(failures, days=days, show_all=show_all, now=now)
    history.update(
        window=mode,
        pass_count=passes if recorded else None,
        failures=shown,
        older=summarize(older) if older else None,
        all_failures=summarize(failures),
    )
    return {
        "testcase": {"repository": tc.repository, "suite": tc.suite, "path": tc.path,
                     "variant": tc.variant},
        "history": history,
        "links": store.links(tc),
        "signatures": store.signatures(tc),
        "rules": store.rules(tc=tc),
        "notes": store.notes(tc),
    }


def render(view: dict) -> str:
    tc, h = view["testcase"], view["history"]
    out = [f"{tc['suite']} {tc['path']}"]
    if not h["available"]:
        out.append(f"history: unavailable ({h['reason']})")
    else:
        a = h["all_failures"]
        out.append(
            f"history: {h['status']}, {a['count']} failures and "
            f"{passes_text(h['pass_count'])}{'' if h['pass_count'] is None else ' indexed'},"
            f" freshness {h['freshness']}, window {h['window']}")
        if a["count"]:
            out.append(f"  first failure {a['first']}, last failure {a['last']}")
            out.append("  by branch: " + _counts(a["by_branch"]))
            out.append("  by signature: " + _counts(a["by_signature"]))
        for f in h["failures"]:
            pr = f"PR #{f['pr']}" if f["pr"] else "no PR"
            out.append(
                f"  {f['completed_at']}  {pr:9} {f['branch']:20} {f['outcome']:5}"
                f" run {f['run_id']} attempt {_attempt(f['attempt'])} shard {f['shard']}"
                f" engine {(f['engine_sha'] or '?')[:10]}"
                f" signature {f['signature'] or 'unknown'}")
        if h["older"]:
            o = h["older"]
            out.append(f"  older: {o['count']} failures from {o['first']} to {o['last']},"
                       f" by branch {_counts(o['by_branch'])}")
        if h.get("sync_hint"):
            out.append(f"  coverage is stale. Refresh with: {h['sync_hint']}")
        if not a["count"]:
            out.append("  no indexed failure. That does not prove the testcase never failed.")
    out.append("issues:" if view["links"] else "issues: none")
    for link in view["links"]:
        sig = f" (signature {link['signature_id']})" if link["signature_id"] else ""
        out.append(f"  {link['relation']:9} {link['issue']}{sig}")
    out.append("signatures:" if view["signatures"] else "signatures: none")
    for s in view["signatures"]:
        out.append(f"  {s['id']}: {s['label']} [{' > '.join(s['frames'])}]")
    out.append("rules:" if view["rules"] else "rules: none")
    for r in view["rules"]:
        out.append(f"  {r['id']} issue {r['issue'] or '-'} expires {r['expires'] or '-'}"
                   f" approved by {r['approved_by']}")
    out.append("notes:" if view["notes"] else "notes: none")
    for n in view["notes"]:
        out.append(f"  {n['created_at']} {n['actor']}: {n['body']}")
    return "\n".join(out)


def _counts(c: dict) -> str:
    return ", ".join(f"{k} {v}" for k, v in sorted(c.items(), key=lambda kv: -kv[1]))


def _attempt(a: int | None) -> str:
    return "null (unresolved)" if a is None else str(a)
