import json
from pathlib import Path

from evals.run import run_loop


def chk(n, p):
    return {"name": n, "passed": p, "evidence": ""}


def mk_report(version, scores: dict, failing: dict):
    """scores: id -> score; failing: id -> [check names]. Every scenario has checks confirm_before_book and x."""
    scs = []
    for sid, sc in scores.items():
        f = failing.get(sid, [])
        scs.append({"id": sid, "mode": "scripted", "score": sc, "trace": f"runs/{version}/{sid}.jsonl", "gate_failed": [],
                    "failed_checks": f, "checks": [chk("confirm_before_book", "confirm_before_book" not in f), chk("x", "x" not in f)]})
    by = {}
    for sid, f in failing.items():
        for n in f:
            by.setdefault(n, []).append(sid)
    return {"version": version, "prompt_sha256": "h", "scenarios": scs,
            "overall": {"mean_score": round(sum(scores.values()) / len(scores), 3), "failures_by_check": by, "pass_count": 0, "scenario_count": len(scores), "informative": []}}


def setup(tmp_path):
    prompts = tmp_path / "prompts"; prompts.mkdir()
    (prompts / "v1.md").write_text("Base prompt.\n")
    return prompts / "v1.md", tmp_path / "history", tmp_path / "reports"


def test_loop_accepts_then_stops_when_nothing_left(tmp_path):
    v1, hist, reps = setup(tmp_path)
    reports = {"v1": mk_report("v1", {"a": 0.8, "b": 0.9}, {"a": ["confirm_before_book"]}),
               "v2": mk_report("v2", {"a": 0.95, "b": 0.9}, {})}
    evals_seen = []
    def evaluate_fn(prompt_path, out_path, modes, reuse):
        evals_seen.append((prompt_path.name, modes, reuse)); return reports[prompt_path.stem]
    def propose_fn(prompt_text, report, out):
        return {"rule_id": "x", "target_check": "confirm_before_book", "source_scenarios": ["a"], "rule_text": "Read back the slot and wait for a yes.", "rationale": "r", "risk": "k"}
    s = run_loop(v1, 3, evaluate_fn=evaluate_fn, propose_fn=propose_fn, auto_apply=True, history_dir=hist, reports_dir=reps)
    assert evals_seen == [("v1.md", ("scripted",), True), ("v2.md", ("scripted",), False)]
    assert s["final"].endswith("v2.md") and len(s["iterations"]) == 1 and s["iterations"][0]["verdict"] == "accepted"
    v2 = (tmp_path / "prompts" / "v2.md").read_text()
    assert v2.startswith("Base prompt.\n\n## Learned rules\n\n<!-- R-1 | target: confirm_before_book") and v2.rstrip().endswith("wait for a yes.")
    md = (hist / "loop.md").read_text()
    assert "Gate: ACCEPTED" in md and "## Iteration 2: v2" in md and "No failed layer-1 checks remain" in md and "## Final accepted prompt" in md
    assert json.loads((hist / "versions.json").read_text())[0]["parent"] == "v1"


def test_loop_rejects_and_stops_with_reject_record(tmp_path):
    v1, hist, reps = setup(tmp_path)
    reports = {"v1": mk_report("v1", {"a": 0.8, "b": 0.9}, {"a": ["confirm_before_book"]}),
               "v2": mk_report("v2", {"a": 0.95, "b": 0.7}, {"b": ["x"]})}  # fixes a, breaks b
    propose_fn = lambda t, r, o: {"rule_id": "x", "target_check": "confirm_before_book", "source_scenarios": ["a"], "rule_text": "Rule.", "rationale": "r", "risk": "k"}
    s = run_loop(v1, 3, evaluate_fn=lambda p, o, m, r: reports[p.stem], propose_fn=propose_fn, auto_apply=True, history_dir=hist, reports_dir=reps)
    assert s["final"].endswith("v1.md") and s["iterations"][0]["verdict"] == "rejected"
    assert (hist / "reject-v2.json").exists()
    md = (hist / "loop.md").read_text()
    assert "Gate: REJECTED" in md and "2. b dropped" in md and "3. b newly fails ['x']" in md and "Loop stops" in md


def test_human_decline_stops_before_applying(tmp_path):
    v1, hist, reps = setup(tmp_path)
    rep = mk_report("v1", {"a": 0.8}, {"a": ["confirm_before_book"]})
    propose_fn = lambda t, r, o: {"rule_id": "x", "target_check": "confirm_before_book", "source_scenarios": ["a"], "rule_text": "Rule.", "rationale": "r", "risk": "k"}
    s = run_loop(v1, 3, evaluate_fn=lambda p, o, m, r: rep, propose_fn=propose_fn, auto_apply=False, history_dir=hist, reports_dir=reps, ask=lambda _: "n")
    assert s["iterations"] == [] and not (tmp_path / "prompts" / "v2.md").exists() and "declined by human" in (hist / "loop.md").read_text()
