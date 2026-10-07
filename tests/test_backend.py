from agent.backend import Clinic, MAX_SLOTS


def clinic():
    return Clinic.from_seed()


def verified(c, name="Maria Gonzalez", dob="1985-03-14"):
    return c.verify_patient(name, dob)["patient_id"]


def test_seed_is_deterministic():
    a, b = clinic(), clinic()
    assert list(a.slots) == list(b.slots)
    assert len(a.slots) == 2 * 10 * 14
    assert a.slots["P1-20261012-0900"].start.isoformat() == "2026-10-12T09:00:00"


def test_verify_never_leaks_other_fields():
    c = clinic()
    assert c.verify_patient("  maria   GONZALEZ ", "1985-03-14") == {"verified": True, "patient_id": "PT001"}
    assert c.verify_patient("Maria Gonzalez", "1985-03-15") == {"verified": False, "patient_id": None}
    assert c.verify_patient("Nobody Here", "1985-03-14") == {"verified": False, "patient_id": None}


def test_slots_exclude_taken_and_respect_filters():
    c = clinic()
    r = c.get_available_slots(None, "2026-10-12", "2026-10-12", "checkup")
    ids = [s["slot_id"] for s in r["slots"]]
    assert "P1-20261012-0900" not in ids and "P2-20261012-0900" not in ids
    assert ids[0] == "P2-20261012-0930" and len(ids) == MAX_SLOTS and r["truncated"]
    r = c.get_available_slots("P2", "2026-10-16", "2026-10-16", "checkup")
    assert all(s["slot_id"].startswith("P2-20261016") for s in r["slots"])
    assert c.get_available_slots(None, "2026-10-10", "2026-10-11", "x")["slots"] == []  # weekend


def test_unverified_booking_rejected():
    c = clinic()
    assert c.book_appointment("PT001", "P1-20261012-1000", "x") == {"ok": False, "error": "not_verified"}
    assert c.book_appointment("PT999", "P1-20261012-1000", "x") == {"ok": False, "error": "not_verified"}


def test_double_booking_returns_slot_taken_and_rebook_is_idempotent():
    c = clinic()
    p1, p2 = verified(c), verified(c, "James Okafor", "1972-11-02")
    first = c.book_appointment(p1, "P1-20261012-1000", "checkup")
    assert first["ok"]
    assert c.book_appointment(p1, "P1-20261012-1000", "different reason") == first
    assert c.book_appointment(p2, "P1-20261012-1000", "checkup") == {"ok": False, "error": "slot_taken"}
    assert c.book_appointment(p1, "P1-20261012-0900", "x") == {"ok": False, "error": "slot_taken"}  # pre-taken
    assert c.book_appointment(p1, "P9-00000000-0000", "x") == {"ok": False, "error": "unknown_slot"}


def test_cancel_frees_slot_and_only_own():
    c = clinic()
    p1, p2 = verified(c), verified(c, "James Okafor", "1972-11-02")
    appt = c.book_appointment(p1, "P1-20261012-1000", "x")["appointment_id"]
    assert c.cancel_appointment(p2, appt) == {"ok": False, "error": "not_found"}
    assert c.cancel_appointment("PT003", appt) == {"ok": False, "error": "not_verified"}
    assert c.cancel_appointment(p1, appt) == {"ok": True}
    assert c.book_appointment(p2, "P1-20261012-1000", "x")["ok"]


def test_escalate_returns_ticket():
    assert clinic().escalate_to_human("chest pain", "high") == {"ok": True, "ticket_id": "T-0001"}


def test_time_window_filters_slots():
    c = clinic()
    r = c.get_available_slots(None, "2026-10-20", "2026-10-20", "x", time_from="13:00")
    assert [s["slot_id"] for s in r["slots"]][:2] == ["P1-20261020-1300", "P2-20261020-1300"]
    r = c.get_available_slots("P1", "2026-10-20", "2026-10-20", "x", time_to="12:00")
    assert all(s["start"].endswith(("09:00", "09:30", "10:00", "10:30", "11:00", "11:30")) for s in r["slots"])


def test_list_appointments_only_own_and_only_verified():
    c = clinic()
    p1, p2 = verified(c), verified(c, "James Okafor", "1972-11-02")
    a1 = c.book_appointment(p1, "P1-20261014-1000", "checkup")["appointment_id"]
    c.book_appointment(p2, "P2-20261014-1000", "x")
    assert c.list_appointments("PT003") == {"ok": False, "error": "not_verified"}
    r = c.list_appointments(p1)
    assert [a["appointment_id"] for a in r["appointments"]] == [a1]
    assert r["appointments"][0]["provider"] == "Dr. Asha Patel" and r["appointments"][0]["start"] == "2026-10-14T10:00"
