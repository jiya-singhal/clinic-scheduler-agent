"""Regression gate: pure function over two reports (D33). Scripted scenarios only."""
from __future__ import annotations

MAX_DROP = 0.03


def _scripted(report):
    return {r["id"]: r for r in report["scenarios"] if r["mode"] != "simulated"}


def _passed(r):
    return {c["name"] for c in r["checks"] if c["passed"]}


def _target_pass_count(scs, target):
    return sum(1 for r in scs.values() if target in _passed(r))


def gate(prev: dict, new: dict, target_check: str) -> dict:
    a, b = _scripted(prev), _scripted(new)
    reasons = []
    before, after = _target_pass_count(a, target_check), _target_pass_count(b, target_check)
    if not after > before:
        reasons.append(f"1. {target_check} pass count did not increase ({before} -> {after})")
    per = []
    for sid, ra in a.items():
        rb = b.get(sid)
        if rb is None:
            reasons.append(f"scenario {sid} missing from new report")
            continue
        delta = round(rb["score"] - ra["score"], 3)
        per.append({"id": sid, "before": ra["score"], "after": rb["score"], "delta": delta})
        if round(ra["score"] - rb["score"], 6) > MAX_DROP:  # avoid 0.030000000000000027 > 0.03
            reasons.append(f"2. {sid} dropped {ra['score']} -> {rb['score']} (more than {MAX_DROP})")
        regressed = sorted(_passed(ra) - _passed(rb))
        if regressed:
            reasons.append(f"3. {sid} newly fails {regressed}")
    gated = [f"{r['id']}: {r['gate_failed']}" for r in b.values() if r["gate_failed"]]
    if gated:
        reasons.append(f"4. safety gate failed: {gated}")
    m_a, m_b = prev["overall"]["mean_score"], new["overall"]["mean_score"]
    if m_b < m_a:
        reasons.append(f"5. mean decreased {m_a} -> {m_b}")
    return {"accepted": not reasons, "reasons": reasons, "target_check": target_check,
            "target_pass_before": before, "target_pass_after": after, "mean_before": m_a, "mean_after": m_b, "per_scenario": per}
