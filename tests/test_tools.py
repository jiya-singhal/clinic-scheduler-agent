from agent.backend import Clinic
from agent.state import ConversationState, apply
from agent.tools import TOOL_SCHEMAS, dispatch


def test_cannot_book_slot_that_was_never_offered():
    c, s = Clinic.from_seed(), ConversationState()
    r = dispatch(c, s, "verify_patient", {"full_name": "Maria Gonzalez", "dob": "1985-03-14"})
    apply(s, "verify_patient", {}, r)
    # real, free slot in the backend, but never returned to this conversation
    r = dispatch(c, s, "book_appointment", {"patient_id": s.patient_id, "slot_id": "P1-20261012-1000", "reason": "x"})
    assert r == {"ok": False, "error": "slot_not_offered"}
    assert "P1-20261012-1000" not in c.taken
    # fake id is rejected the same way
    assert dispatch(c, s, "book_appointment", {"patient_id": s.patient_id, "slot_id": "P9-99999999-9999", "reason": "x"})["error"] == "slot_not_offered"
    # after an offer it books
    r = dispatch(c, s, "get_available_slots", {"date_from": "2026-10-12", "date_to": "2026-10-12", "reason": "x"})
    apply(s, "get_available_slots", {}, r)
    r = dispatch(c, s, "book_appointment", {"patient_id": s.patient_id, "slot_id": s.offered_slots[0], "reason": "x"})
    assert r["ok"]


def test_bad_arguments_do_not_crash():
    c, s = Clinic.from_seed(), ConversationState()
    assert dispatch(c, s, "get_available_slots", {"date_from": "tomorrow", "date_to": "x", "reason": "x"})["error"] == "bad_arguments"
    assert dispatch(c, s, "verify_patient", {"full_name": "x"})["error"] == "bad_arguments"
    assert dispatch(c, s, "nope", {})["error"] == "unknown_tool"


def test_schemas_match_backend_signatures():
    import inspect
    for t in TOOL_SCHEMAS:
        params = set(inspect.signature(getattr(Clinic, t["name"])).parameters) - {"self"}
        assert set(t["parameters"]["properties"]) == params, t["name"]
