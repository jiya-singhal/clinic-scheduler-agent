from agent.state import ConversationState, apply


def test_verify_only_moves_on_success():
    s = ConversationState()
    apply(s, "verify_patient", {"full_name": "x", "dob": "y"}, {"verified": False, "patient_id": None})
    assert s.to_json() == ConversationState().to_json()
    apply(s, "verify_patient", {"full_name": "x", "dob": "y"}, {"verified": True, "patient_id": "PT001"})
    assert (s.verified, s.patient_id) == (True, "PT001")


def test_slots_populate_offers_and_preferences_from_args():
    s = ConversationState()
    args = {"provider_id": None, "date_from": "2026-10-12", "date_to": "2026-10-13", "reason": "checkup"}
    apply(s, "get_available_slots", args, {"slots": [{"slot_id": "P1-20261012-1000"}, {"slot_id": "P2-20261012-1000"}]})
    apply(s, "get_available_slots", args, {"slots": [{"slot_id": "P1-20261012-1000"}]})
    assert s.offered_slots == ["P1-20261012-1000", "P2-20261012-1000"]
    assert (s.intent, s.reason) == ("book", "checkup")
    assert s.preferences == {"provider_id": None, "date_from": "2026-10-12", "date_to": "2026-10-13"}


def test_book_cancel_escalate():
    s = ConversationState()
    apply(s, "book_appointment", {"slot_id": "P1-20261012-1000"}, {"ok": False, "error": "slot_taken"})
    assert s.booked_appointment_id is None
    apply(s, "book_appointment", {"slot_id": "P1-20261012-1000"}, {"ok": True, "appointment_id": "A-0001"})
    assert (s.selected_slot, s.booked_appointment_id) == ("P1-20261012-1000", "A-0001")
    apply(s, "cancel_appointment", {"appointment_id": "A-0001"}, {"ok": True})
    assert (s.booked_appointment_id, s.selected_slot, s.intent) == (None, None, "cancel")
    apply(s, "escalate_to_human", {"urgency": "high"}, {"ok": True, "ticket_id": "T-0001"})
    assert s.escalated and s.intent == "escalate"
