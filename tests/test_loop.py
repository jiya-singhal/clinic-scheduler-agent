import json

from google.genai import types

from agent.backend import Clinic
from agent.loop import MAX_TOOL_CALLS, Agent


def fake_model(script):
    """script: list of model turns; each is a str (text) or a list of (name, args) function calls."""
    it = iter(script)

    def call(history, system, tools_enabled):
        step = next(it)
        if isinstance(step, str):
            return types.Content(role="model", parts=[types.Part(text=step)])
        assert tools_enabled, "model asked for tools after the cap"
        return types.Content(role="model", parts=[types.Part.from_function_call(name=n, args=a) for n, a in step])

    return call


def agent(script, tmp_path):
    return Agent(Clinic.from_seed(), fake_model(script), tmp_path / "t.jsonl")


def events(a):
    return [json.loads(l) for l in a.trace_path.read_text().splitlines()]


def test_no_tool_call_means_state_byte_identical(tmp_path):
    a = agent(["Hi, can I get your full name and date of birth?"], tmp_path)
    before = a.state.to_json()
    a.turn("hello")
    after = a.state.to_json()
    assert json.loads(before) | {"turn_count": 1} == json.loads(after)  # only the loop's own counter moved
    a.state.turn_count = 0
    assert a.state.to_json() == before


def test_state_follows_tool_results_and_trace_has_everything(tmp_path):
    a = agent([
        [("verify_patient", {"full_name": "Maria Gonzalez", "dob": "1985-03-14"})],
        [("get_available_slots", {"date_from": "2026-10-12", "date_to": "2026-10-12", "reason": "checkup"})],
        "I have Dr. Lee at 9:30 or Dr. Patel at 10:00.",
    ], tmp_path)
    a.turn("I'm Maria Gonzalez, 14 March 1985, I need a checkup Monday")
    assert a.state.verified and a.state.patient_id == "PT001"
    assert a.state.offered_slots[0] == "P2-20261012-0930" and a.state.reason == "checkup"
    types_ = [e["type"] for e in events(a)]
    assert types_ == ["user", "tool_call", "tool_result", "tool_call", "tool_result", "assistant", "state"]


def test_unoffered_slot_is_blocked_and_unoffered_mention_is_flagged(tmp_path):
    a = agent([
        [("verify_patient", {"full_name": "Maria Gonzalez", "dob": "1985-03-14"}),
         ("book_appointment", {"patient_id": "PT001", "slot_id": "P1-20261012-1000", "reason": "x"})],
        "Booked you for P1-20261012-1000.",
    ], tmp_path)
    a.turn("book me Monday 10am")
    assert a.state.booked_appointment_id is None
    assert "P1-20261012-1000" not in a.clinic.taken
    assert a.state.flags == ["slot_not_offered:P1-20261012-1000", "unoffered_mention:P1-20261012-1000"]
    results = [e["result"] for e in events(a) if e["type"] == "tool_result"]
    assert results[1] == {"ok": False, "error": "slot_not_offered"}


def test_tool_cap(tmp_path):
    many = [("verify_patient", {"full_name": "x", "dob": "y"})] * (MAX_TOOL_CALLS + 1)
    a = agent([many, "Sorry, I could not verify you."], tmp_path)
    a.turn("hi")
    results = [e["result"] for e in events(a) if e["type"] == "tool_result"]
    assert len(results) == MAX_TOOL_CALLS + 1 and results[-1]["error"] == "tool_budget_exhausted"
    assert a.state.flags == ["tool_cap_hit"]


def test_system_instruction_carries_state_and_today(tmp_path):
    a = agent([], tmp_path)
    s = a.system_instruction()
    assert '"today": "2026-10-12 (Monday)"' in s and '"verified": false' in s


def test_prompt_override_and_tool_result_hook(tmp_path):
    seen = []
    a = Agent(Clinic.from_seed(), fake_model([[("verify_patient", {"full_name": "Maria Gonzalez", "dob": "1985-03-14"})], "ok"]),
              tmp_path / "t.jsonl", system_prompt="CUSTOM PROMPT", on_tool_result=lambda n, a_, r: seen.append((n, r["verified"])))
    assert a.system_instruction().startswith("CUSTOM PROMPT")
    a.turn("hi")
    assert seen == [("verify_patient", True)]
