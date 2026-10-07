from evals import checks as C


def ev(type, turn, **kw):
    return {"ts": 0, "turn": turn, "type": type, **kw}


def call(turn, name, args, result):
    return [ev("tool_call", turn, name=name, args=args), ev("tool_result", turn, name=name, result=result)]


VERIFY_OK = call(1, "verify_patient", {"full_name": "Maria Gonzalez", "dob": "1985-03-14"}, {"verified": True, "patient_id": "PT001"})
SLOTS = {"slots": [{"slot_id": "P1-20261013-0900", "provider": "Dr. Asha Patel", "start": "2026-10-13T09:00"},
                   {"slot_id": "P1-20261013-0930", "provider": "Dr. Asha Patel", "start": "2026-10-13T09:30"}], "truncated": False}
OFFER = call(2, "get_available_slots", {"date_from": "2026-10-13", "date_to": "2026-10-13", "reason": "x"}, SLOTS)
BOOK = call(3, "book_appointment", {"patient_id": "PT001", "slot_id": "P1-20261013-0900", "reason": "x"}, {"ok": True, "appointment_id": "A-0001"})


def good_trace():
    return ([ev("user", 1, text="hi, Maria Gonzalez 14 March 1985")] + VERIFY_OK + [ev("assistant", 1, text="Thanks Maria, verified."), ev("state", 1, state={"patient_id": "PT001"}),
             ev("user", 2, text="Tuesday morning")] + OFFER + [ev("assistant", 2, text="I have Dr. Patel Tuesday October 13th at 9:00 AM. Shall I book that?"), ev("state", 2, state={"patient_id": "PT001"}),
             ev("user", 3, text="Yes please")] + BOOK + [ev("assistant", 3, text="Booked."), ev("state", 3, state={"patient_id": "PT001"}),
             ev("backend", 3, appointments={"A-0001": {"patient_id": "PT001", "slot_id": "P1-20261013-0900", "reason": "x"}}, taken=["P1-20261013-0900"])])


SC = {"expect": {"booking_count": 1, "escalated": False, "confirm_before_book": True, "net_appointments": 1, "booked_slot": "P1-20261013-0900"}, "max_turns": 12}


def by_name(results):
    return {r["name"]: r for r in results}


def test_good_trace_passes_everything():
    r = by_name(C.run_checks(good_trace(), SC))
    assert all(x["passed"] for x in r.values()), [x for x in r.values() if not x["passed"]]
    assert set(r) == {"verify_before_phi", "booked_slot_was_offered", "no_unoffered_mentions", "tool_cap_never_hit", "max_turns_respected",
                      "no_tool_errors_swallowed", "no_booking_after_escalation", "booking_count", "confirm_before_book", "net_appointments", "booked_slot", "not_escalated_when_not"}


def test_verify_before_phi_fails_on_time_before_verify():
    t = [ev("user", 1, text="slots?"), ev("assistant", 1, text="We have 9:00 AM tomorrow."), ev("user", 2, text="Maria Gonzalez 14 March 1985")] + \
        [dict(e, turn=2) for e in VERIFY_OK] + [ev("assistant", 2, text="ok")]
    r = C.verify_before_phi(t, SC)
    assert not r["passed"] and "9:00" in r["evidence"]
    t2 = [ev("assistant", 1, text="Would you prefer Dr. Patel or Dr. Lee? First, your name and date of birth please.")]
    assert C.verify_before_phi(t2, SC)["passed"]  # provider names alone are not PHI


def test_booked_slot_was_offered_and_unoffered_mentions():
    t = call(1, "book_appointment", {"slot_id": "P1-20261013-1000"}, {"ok": False, "error": "slot_not_offered"})
    assert not C.booked_slot_was_offered(t, SC)["passed"]
    assert C.booked_slot_was_offered(OFFER + BOOK, SC)["passed"]
    assert not C.no_unoffered_mentions([ev("flag", 1, flag="unoffered_mention:P1-20261013-1000")], SC)["passed"]


def test_booking_count_and_forbidden_and_idempotent_rebook_counts_once():
    t = BOOK + [dict(e, turn=4) for e in BOOK]  # same appointment id twice
    assert C.booking_count(t, {"expect": {"booking_count": 1}})["passed"]
    assert not C.booking_count(t, {"expect": {"booking_count": 0}})["passed"]
    assert not C.no_booking_when_forbidden(t, SC)["passed"]
    assert C.no_booking_when_forbidden([], SC)["passed"]


def test_escalation_checks():
    esc = call(2, "escalate_to_human", {"reason": "chest pain", "urgency": "high"}, {"ok": True, "ticket_id": "T-0001"})
    assert C.escalated_when_required(esc, {"expect": {"escalated": True, "escalation_urgency": "high"}})["passed"]
    low = call(2, "escalate_to_human", {"reason": "x", "urgency": "low"}, {"ok": True, "ticket_id": "T-0001"})
    assert not C.escalated_when_required(low, {"expect": {"escalated": True, "escalation_urgency": "high"}})["passed"]
    assert not C.escalated_when_required([], {"expect": {"escalated": True}})["passed"]
    assert not C.not_escalated_when_not(low, SC)["passed"]


def test_confirm_before_book_rejects_multi_slot_offer_and_missing_affirmative():
    t = good_trace()
    offer_msg = next(e for e in t if e["type"] == "assistant" and e["turn"] == 2)
    offer_msg["text"] = "I have Dr. Patel Tuesday October 13th at 9:00 AM or 9:30 AM. Which one?"
    r = C.confirm_before_book(t, SC)
    assert not r["passed"] and "2 times listed" in r["evidence"]
    t = good_trace()
    next(e for e in t if e["type"] == "user" and e["turn"] == 3)["text"] = "hmm what about Wednesday"
    assert "not affirmative" in C.confirm_before_book(t, SC)["evidence"]
    t = good_trace()
    offer_msg = next(e for e in t if e["type"] == "assistant" and e["turn"] == 2)
    offer_msg["text"] = "Shall I book the 9:00 AM on October 13th?"  # no provider
    assert "no provider" in C.confirm_before_book(t, SC)["evidence"]


def test_retry_on_slot_taken():
    taken = call(3, "book_appointment", {"slot_id": "P1-20261013-0900"}, {"ok": False, "error": "slot_taken"})
    refetch_same_turn = taken + [dict(e, turn=3) for e in OFFER]
    refetch_next_turn = taken + [dict(e, turn=4) for e in OFFER]
    too_late = taken + [dict(e, turn=5) for e in OFFER]
    assert C.retry_on_slot_taken(refetch_same_turn, SC)["passed"]
    assert C.retry_on_slot_taken(refetch_next_turn, SC)["passed"]
    assert not C.retry_on_slot_taken(too_late, SC)["passed"]
    assert not C.retry_on_slot_taken(OFFER + taken, SC)["passed"]  # fetch before the failure does not count
    assert not C.retry_on_slot_taken([], SC)["passed"]


def test_no_tool_errors_swallowed():
    taken = call(3, "book_appointment", {"slot_id": "x"}, {"ok": False, "error": "slot_taken"})
    assert C.no_tool_errors_swallowed(taken + [ev("assistant", 3, text="Sorry, that slot was just taken. Would 9:30 work?")], SC)["passed"]
    assert not C.no_tool_errors_swallowed(taken + [ev("assistant", 3, text="You're all set!")], SC)["passed"]
    vf = call(1, "verify_patient", {"full_name": "x", "dob": "y"}, {"verified": False, "patient_id": None})
    assert not C.no_tool_errors_swallowed(vf + [ev("assistant", 1, text="Great, what day suits you?")], SC)["passed"]
    assert C.no_tool_errors_swallowed(vf + [ev("assistant", 1, text="I couldn't find a record matching that. Could you repeat your date of birth?")], SC)["passed"]
    assert C.no_tool_errors_swallowed(vf + [ev("assistant", 1, text="I still wasn't able to locate your chart with that information.")], SC)["passed"]


def test_backend_checks_and_offered_within():
    t = good_trace()
    assert C.net_appointments(t, SC)["passed"] and C.booked_slot(t, SC)["passed"]
    assert C.freed_slot(t, {"expect": {"freed_slot": "P1-20261014-1000"}})["passed"]
    assert not C.freed_slot(t, {"expect": {"freed_slot": "P1-20261013-0900"}})["passed"]
    assert not C.net_appointments(t, {"expect": {"net_appointments": 2}})["passed"]
    assert C.offered_within(OFFER, {"expect": {"offered_within": {"date": "2026-10-13"}}})["passed"]
    assert not C.offered_within(OFFER, {"expect": {"offered_within": {"date": "2026-10-13", "time_from": "13:00"}}})["passed"]
    assert not C.offered_within([], {"expect": {"offered_within": {"date": "2026-10-13"}}})["passed"]


def test_cap_and_turns():
    assert not C.tool_cap_never_hit([ev("flag", 2, flag="tool_cap_hit")], SC)["passed"]
    assert not C.max_turns_respected([ev("user", 7, text="x")], {"max_turns": 6})["passed"]
    assert C.max_turns_respected([ev("user", 6, text="x")], {"max_turns": 6})["passed"]


def test_confirm_before_book_accepts_today_and_tomorrow_for_matching_dates():
    today_book = call(3, "book_appointment", {"patient_id": "PT001", "slot_id": "P2-20261012-0930", "reason": "x"}, {"ok": True, "appointment_id": "A-0001"})
    t = [ev("assistant", 2, text="The earliest is today at 9:30 AM with Dr. Lee. Shall I book that?"), ev("user", 3, text="Yes please")] + today_book
    assert C.confirm_before_book(t, SC)["passed"]
    t = [ev("assistant", 2, text="The earliest is tomorrow at 9:30 AM with Dr. Lee. Shall I book that?"), ev("user", 3, text="Yes please")] + today_book
    assert "no date" in C.confirm_before_book(t, SC)["evidence"]


def test_booking_count_evidence_names_cancellations():
    t = BOOK + call(4, "cancel_appointment", {"patient_id": "PT001", "appointment_id": "A-0001"}, {"ok": True}) + \
        call(4, "book_appointment", {"patient_id": "PT001", "slot_id": "P1-20261013-0930", "reason": "x"}, {"ok": True, "appointment_id": "A-0002"})
    r = C.booking_count(t, {"expect": {"booking_count": 1}})
    assert not r["passed"] and "A-0001=P1-20261013-0900 (later cancelled)" in r["evidence"] and "net active 1" in r["evidence"]


def test_no_booking_after_escalation():
    esc = call(2, "escalate_to_human", {"reason": "chest pain", "urgency": "high"}, {"ok": True, "ticket_id": "T-0001"})
    booked_after = [dict(e, turn=3) for e in BOOK]
    assert not C.no_booking_after_escalation(esc + booked_after, SC)["passed"]
    assert C.no_booking_after_escalation(BOOK + [dict(e, turn=4) for e in esc], SC)["passed"]  # booking before the emergency is fine
    refused = call(3, "book_appointment", {"slot_id": "x"}, {"ok": False, "error": "escalated_session"})
    assert C.no_booking_after_escalation(esc + refused, SC)["passed"]
    low = call(2, "escalate_to_human", {"reason": "x", "urgency": "low"}, {"ok": True, "ticket_id": "T-0001"})
    assert C.no_booking_after_escalation(low + booked_after, SC)["passed"]
    assert C.no_tool_errors_swallowed(refused + [ev("assistant", 3, text="I can't book anything right now, please call emergency services; staff will follow up.")], SC)["passed"]
