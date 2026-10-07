import json

import pytest

from evals import improver as I


def report():
    chk = lambda n, p, e="": {"name": n, "passed": p, "evidence": e}
    return {"scenarios": [
        {"id": "ambiguous-date", "category": "d", "score": 0.8, "checks": [chk("confirm_before_book", False, "2 times listed"), chk("offered_within", False)]},
        {"id": "happy-path-book", "category": "b", "score": 0.99, "checks": [chk("confirm_before_book", True)]},
        {"id": "wrong-dob-then-correct", "category": "i", "score": 0.9, "checks": [chk("confirm_before_book", False, "3 times listed")]},
    ], "overall": {"failures_by_check": {"confirm_before_book": ["ambiguous-date", "wrong-dob-then-correct"], "offered_within": ["ambiguous-date"], "judge:clarity": ["a", "b", "c"]}}}


TRACES = {"ambiguous-date": [{"type": "user", "text": "hi"}, {"type": "assistant", "text": "yo"}], "wrong-dob-then-correct": [{"type": "user", "text": "x"}]}
GOOD = {"rule_id": "R-9", "target_check": "confirm_before_book", "source_scenarios": [],
        "rule_text": "Before booking, repeat the single chosen slot with provider, day and time, then wait for the patient to say yes.",
        "rationale": "r", "risk": "k"}


def test_pick_target_ignores_judge_and_takes_highest():
    assert I.pick_target(report()["overall"]["failures_by_check"]) == "confirm_before_book"
    assert I.pick_target({"judge:clarity": ["a"]}) is None
    assert I.pick_target({"a": ["x"], "b": ["y"]}) == "b"  # tie broken by name, deterministic


def test_user_message_contains_only_target_failures():
    msg = I.build_user_message("PROMPT", report(), "confirm_before_book", TRACES)
    assert "PROMPT" in msg and "--- ambiguous-date" in msg and "--- wrong-dob-then-correct" in msg and "--- happy-path-book" not in msg
    assert "PATIENT: hi" in msg and "2 times listed" in msg


def test_validate_catches_each_constraint():
    ids, names = ["ambiguous-date"], ["confirm_before_book"]
    assert I.validate(GOOD, "confirm_before_book", ids, names) == []
    assert any("forbidden word 'scenario'" in p for p in I.validate({**GOOD, "rule_text": "In this scenario say yes."}, "confirm_before_book", ids, names))
    assert any("mentions scenario" in p for p in I.validate({**GOOD, "rule_text": "Handle ambiguous date requests."}, "confirm_before_book", ids, names))
    assert any("mentions check" in p for p in I.validate({**GOOD, "rule_text": "Always confirm before book."}, "confirm_before_book", ids, names))
    assert any("max 400" in p for p in I.validate({**GOOD, "rule_text": "x" * 401}, "confirm_before_book", ids, names))
    assert any("target_check" in p for p in I.validate({**GOOD, "target_check": "other"}, "confirm_before_book", ids, names))


def test_propose_retries_once_then_fails():
    calls = []
    def flaky(system, user):
        calls.append(user)
        return json.dumps({**GOOD, "rule_text": "This test rule."} if len(calls) == 1 else GOOD)
    rule = I.propose("P", report(), TRACES, call=flaky)
    assert rule["rule_text"] == GOOD["rule_text"] and rule["source_scenarios"] == ["ambiguous-date", "wrong-dob-then-correct"]
    assert len(calls) == 2 and "violated these constraints" in calls[1]
    bad = lambda s, u: json.dumps({**GOOD, "rule_text": "Pass the check."})
    with pytest.raises(ValueError, match="twice"):
        I.propose("P", report(), TRACES, call=bad)
    with pytest.raises(ValueError, match="no failed"):
        I.propose("P", {"scenarios": [], "overall": {"failures_by_check": {}}}, {}, call=bad)
