import json
from datetime import date
from pathlib import Path

import pytest

from evals import versions as V

RULE = {"rule_id": "ignored", "target_check": "confirm_before_book", "source_scenarios": ["a", "b"],
        "rule_text": "Before booking, read back the single chosen slot and wait for a yes.", "rationale": "r", "risk": "k"}


def test_apply_rule_appends_section_with_provenance_and_never_edits_parent(tmp_path):
    v1 = tmp_path / "v1.md"
    v1.write_text("You are the assistant.\nRule 1.\n")
    v2, rule = V.apply_rule(v1, RULE, today=date(2026, 10, 7))
    assert v2.name == "v2.md" and rule["rule_id"] == "R-1"
    text = v2.read_text()
    assert text.startswith("You are the assistant.\nRule 1.\n\n## Learned rules\n\n<!-- R-1 | target: confirm_before_book | sources: a, b | 2026-10-07 | parent sha256 ")
    assert text.rstrip().endswith(RULE["rule_text"])
    assert v1.read_text() == "You are the assistant.\nRule 1.\n"
    v3, rule3 = V.apply_rule(v2, {**RULE, "target_check": "offered_within", "source_scenarios": ["c"]}, today=date(2026, 10, 7))
    assert rule3["rule_id"] == "R-2" and v3.read_text().count("## Learned rules") == 1 and "<!-- R-1" in v3.read_text()
    v4, rule4 = V.apply_rule(v1, RULE, today=date(2026, 10, 7))  # v2 and v3 exist: next free number, never overwrite
    assert v4.name == "v4.md" and rule4["rule_id"] == "R-1" and "<!-- R-2" not in v4.read_text()


def test_version_number_and_sha_and_history(tmp_path):
    assert V.version_number(Path("prompts/v12.md")) == 12
    with pytest.raises(ValueError):
        V.version_number(Path("prompts/final.md"))
    assert V.sha("a\n") == V.sha("a")
    h = tmp_path / "versions.json"
    V.record({"version": "v2", "verdict": "accepted"}, h)
    V.record({"version": "v2", "verdict": "rejected"}, h)  # same version re-recorded replaces
    V.record({"version": "v3", "verdict": "accepted"}, h)
    assert [(e["version"], e["verdict"]) for e in json.loads(h.read_text())] == [("v2", "rejected"), ("v3", "accepted")]
