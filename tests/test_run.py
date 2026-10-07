import json
from pathlib import Path

from evals.run import load_scenarios, run_scenario, stand_in_reply, write_markdown
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


def test_fault_at_booking_time_takes_the_requested_slot_once_and_setup_books(tmp_path):
    s = sc("slot-taken-race")
    model = fake_model([
        "Name and date of birth?",
        [("verify_patient", {"full_name": "Sarah Whitfield", "dob": "2001-09-09"})], "Verified. When?",
        [("get_available_slots", {"date_from": "2026-10-15", "date_to": "2026-10-15", "reason": "x", "time_from": "13:00"})], "Thursday October 15th 1:00 PM with Dr. Patel?",
        [("book_appointment", {"patient_id": "PT005", "slot_id": "P1-20261015-1300", "reason": "x"}),
         ("get_available_slots", {"date_from": "2026-10-15", "date_to": "2026-10-15", "reason": "x", "time_from": "13:00"})],
        "Sorry, that slot was just taken. Dr. Lee at 1:00 PM on Thursday October 15th instead?",
        [("book_appointment", {"patient_id": "PT005", "slot_id": "P2-20261015-1300", "reason": "x"})], "Booked.",
    ])
    r = run_scenario(s, prompt_text="P", trace_path=tmp_path / "t.jsonl", model_fn=model, judge_fn=None)
    events = [json.loads(l) for l in (tmp_path / "t.jsonl").read_text().splitlines()]
    assert [(e["slot_id"], e["when"]) for e in events if e["type"] == "fault"] == [("P1-20261015-1300", "before_book")]
    results = [e["result"] for e in events if e["type"] == "tool_result" and e["name"] == "book_appointment"]
    assert results == [{"ok": False, "error": "slot_taken"}, {"ok": True, "appointment_id": "A-0001"}]
    assert [e["text"] for e in events if e["type"] == "auto_affirm"] == ["Yes, that's right."]
    assert r["failed_checks"] == [], r["failed_checks"]
    s2 = sc("reschedule-existing")
    r2 = run_scenario(s2, prompt_text="P", trace_path=tmp_path / "t2.jsonl", model_fn=fake_model(["hi"] * 5), judge_fn=None)
    be = next(e for e in map(json.loads, (tmp_path / "t2.jsonl").read_text().splitlines()) if e["type"] == "backend")
    assert "P1-20261014-1000" in be["taken"] and r2["failed_checks"]  # seeded appointment exists; nothing was done


def test_after_tool_fault_stays_armed_until_slot_taken(tmp_path):
    s = {**sc("slot-taken-race"), "faults": [{"after_tool": "get_available_slots", "action": "take_slot", "which": "first_offered"}]}
    model = fake_model([
        "Name?",
        [("verify_patient", {"full_name": "Sarah Whitfield", "dob": "2001-09-09"}),
         ("get_available_slots", {"date_from": "2026-10-12", "date_to": "2026-10-16", "reason": "x"})], "Today 9:30?",
        [("get_available_slots", {"date_from": "2026-10-15", "date_to": "2026-10-15", "reason": "x", "time_from": "13:00"})], "Thursday 1:00 PM Dr. Patel?",
        [("book_appointment", {"patient_id": "PT005", "slot_id": "P1-20261015-1300", "reason": "x"}),
         ("get_available_slots", {"date_from": "2026-10-15", "date_to": "2026-10-15", "reason": "x", "time_from": "13:00"})], "Taken. Dr. Lee at 1:00 PM Thursday October 15th?",
        [("book_appointment", {"patient_id": "PT005", "slot_id": "P2-20261015-1300", "reason": "x"})], "Booked.",
    ])
    run_scenario(s, prompt_text="P", trace_path=tmp_path / "t.jsonl", model_fn=model, judge_fn=None)
    events = [json.loads(l) for l in (tmp_path / "t.jsonl").read_text().splitlines()]
    assert [e["slot_id"] for e in events if e["type"] == "fault"] == ["P2-20261012-0930", "P1-20261015-1300"]  # disarmed after slot_taken


def test_markdown_report_renders(tmp_path):
    s = sc("identity-fail")
    r = run_scenario(s, prompt_text="P", trace_path=tmp_path / "t.jsonl", model_fn=fake_model(["no"] * 4), judge_fn=fake_judge)
    report = {"version": "vtest", "prompt_path": "p", "prompt_sha256": "abc" * 10, "models": {"agent": "a", "judge": "j", "simulator": "s"},
              "generated_at": "now", "scenarios": [r], "overall": aggregate([r])}
    write_markdown(report, tmp_path / "r.md")
    md = (tmp_path / "r.md").read_text()
    assert "| identity-fail |" in md and "Failures by check" in md


def test_auto_affirm_only_when_script_ends_on_an_unbooked_slot_question(tmp_path):
    s = sc("happy-path-book")
    # agent reads back a slot and asks after the script's last line; the harness must answer yes, then the agent books
    model = fake_model([
        "Name and date of birth?",
        [("verify_patient", {"full_name": "Priya Raman", "dob": "1990-07-21"})], "Verified. When?",
        [("get_available_slots", {"date_from": "2026-10-19", "date_to": "2026-10-23", "reason": "x"})], "Monday October 19th 9:00 AM with Dr. Patel, shall I book it?",
        "Just to confirm: Dr. Patel, Monday October 19th at 9:00 AM. Is that right?",
        [("book_appointment", {"patient_id": "PT003", "slot_id": "P1-20261019-0900", "reason": "x"})], "Booked. Anything else?",
    ])
    r = run_scenario(s, prompt_text="P", trace_path=tmp_path / "t.jsonl", model_fn=model, judge_fn=None)
    events = [json.loads(l) for l in (tmp_path / "t.jsonl").read_text().splitlines()]
    assert [e["text"] for e in events if e["type"] == "auto_affirm"] == ["Yes, that's right."]
    assert "booking_count" not in r["failed_checks"] and "confirm_before_book" not in r["failed_checks"] and r["turns"] == 5
    # no affirmation when the agent's closing question has no time in it, or a booking already exists
    s2 = sc("identity-fail")
    r2 = run_scenario(s2, prompt_text="P", trace_path=tmp_path / "t2.jsonl", model_fn=fake_model(["no"] * 3 + ["Shall I connect you to staff?"]), judge_fn=None)
    assert r2["turns"] == 4 and not any(json.loads(l)["type"] == "auto_affirm" for l in (tmp_path / "t2.jsonl").read_text().splitlines())


def test_stand_in_reply_rules():
    assert stand_in_reply("Shall I book Dr. Patel on Thursday, October 15th at 1:00 PM?") == "Yes, that's right."
    assert stand_in_reply("Dr. Lee is open at 1:00 PM, or Dr. Patel at 1:30 PM. Would either of those work?") == "The first one, please."
    assert stand_in_reply("Which would you prefer: Dr. Lee at 1:00 PM or Dr. Patel at 1:30 PM?") == "The first one, please."
    assert stand_in_reply("Would you like me to connect you with our staff?") is None   # no time named
    assert stand_in_reply("You're all set for 1:00 PM with Dr. Lee.") is None            # not a question
