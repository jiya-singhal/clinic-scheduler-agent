import json
from pathlib import Path

from evals.run import load_scenarios, run_scenario, write_markdown
from evals.score import aggregate
from tests.test_loop import fake_model


def sc(id):
    return json.loads(Path(f"scenarios/{id}.json").read_text())


def fake_judge(sc_, events, checks):
    return {c: {"score": 1.0, "evidence": "fine"} for c in
            ["resolves_request", "clarity", "tone_appropriate_for_patients", "no_medical_advice", "no_phi_leak", "handles_errors_gracefully"]}


def test_all_scenario_files_load_and_have_contract_fields():
    scs = load_scenarios()
    assert len(scs) == 14
    for s in scs:
        assert {"id", "category", "mode", "patient", "expect", "max_turns"} <= set(s)
        assert ("turns" in s) if s["mode"] == "scripted" else ("brief" in s)


def test_identity_fail_with_polite_refusals_scores_full(tmp_path):
    s = sc("identity-fail")
    model = fake_model([
        "Sure, may I have your full name and date of birth?",
        [("verify_patient", {"full_name": "Maria Gonzalez", "dob": "1985-03-03"})], "I couldn't find a record matching that date of birth. Could you check it?",
        [("verify_patient", {"full_name": "Maria Gonzalez", "dob": "1986-03-14"})], "That still doesn't match our records. I can connect you with the front desk if you like.",
        "I'm sorry, I can't share availability until your identity is verified.",
    ])
    r = run_scenario(s, prompt_text="P", trace_path=tmp_path / "t.jsonl", model_fn=model, judge_fn=fake_judge)
    assert r["gate_failed"] == [] and r["failed_checks"] == [] and r["score"] == 1.0
    assert {c["name"] for c in r["checks"]} >= {"no_booking_when_forbidden", "booking_count", "verify_before_phi"}


def test_fault_injection_makes_first_offered_slot_taken_and_setup_books(tmp_path):
    s = sc("slot-taken-race")
    model = fake_model([
        "Name and date of birth?",
        # eager week-wide fetch before the patient says when, as the real agent does: the fault must survive it
        [("verify_patient", {"full_name": "Sarah Whitfield", "dob": "2001-09-09"}),
         ("get_available_slots", {"date_from": "2026-10-12", "date_to": "2026-10-16", "reason": "x"})], "Verified. Today 9:30 with Dr. Lee?",
        [("get_available_slots", {"date_from": "2026-10-15", "date_to": "2026-10-15", "reason": "x", "time_from": "13:00"})], "Thursday October 15th 1:00 PM with Dr. Patel?",
        [("book_appointment", {"patient_id": "PT005", "slot_id": "P1-20261015-1300", "reason": "x"})], "You're all set!",
        [("get_available_slots", {"date_from": "2026-10-15", "date_to": "2026-10-15", "reason": "x", "time_from": "13:00"})], "Dr. Lee at 1:00 PM?",
        "ok",
    ])
    r = run_scenario(s, prompt_text="P", trace_path=tmp_path / "t.jsonl", model_fn=model, judge_fn=None)
    events = [json.loads(l) for l in (tmp_path / "t.jsonl").read_text().splitlines()]
    faults = [e["slot_id"] for e in events if e["type"] == "fault"]
    assert faults == ["P2-20261012-0930", "P1-20261015-1300"]  # fired on both fetches, then disarmed after slot_taken
    results = [e["result"] for e in events if e["type"] == "tool_result" and e["name"] == "book_appointment"]
    assert results == [{"ok": False, "error": "slot_taken"}]
    post = [e for e in events if e["type"] == "tool_result" and e["name"] == "get_available_slots"][-1]["result"]["slots"]
    assert post[0]["slot_id"] == "P2-20261015-1300" and "P2-20261015-1300" not in faults  # the re-fetch is left alone
    assert {"booking_count", "no_tool_errors_swallowed"} <= set(r["failed_checks"]) and "retry_on_slot_taken" not in r["failed_checks"]  # swallowed "all set", but did re-fetch
    s2 = sc("reschedule-existing")
    r2 = run_scenario(s2, prompt_text="P", trace_path=tmp_path / "t2.jsonl", model_fn=fake_model(["hi"] * 5), judge_fn=None)
    be = next(e for e in map(json.loads, (tmp_path / "t2.jsonl").read_text().splitlines()) if e["type"] == "backend")
    assert "P1-20261014-1000" in be["taken"] and r2["failed_checks"]  # seeded appointment exists; nothing was done


def test_markdown_report_renders(tmp_path):
    s = sc("identity-fail")
    r = run_scenario(s, prompt_text="P", trace_path=tmp_path / "t.jsonl", model_fn=fake_model(["no"] * 4), judge_fn=fake_judge)
    report = {"version": "vtest", "prompt_path": "p", "prompt_sha256": "abc" * 10, "models": {"agent": "a", "judge": "j", "simulator": "s"},
              "generated_at": "now", "scenarios": [r], "overall": aggregate([r])}
    write_markdown(report, tmp_path / "r.md")
    md = (tmp_path / "r.md").read_text()
    assert "| identity-fail |" in md and "Failures by check" in md
