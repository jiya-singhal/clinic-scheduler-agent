import importlib.util
import json
import re
from pathlib import Path

from evals import judge as J
from evals import judge_audit as A
from evals.checks import TIME_RE

V2 = A.TRACES / "v2"


def ev(sid, v="v2"):
    return A.load_events(A.TRACES / v / f"{sid}.jsonl")


def texts(user):
    a, b = re.search(r"TRANSCRIPT A:\n(.*)\n\nTRANSCRIPT B:\n(.*)\n\nWhich", user, re.S).groups()
    return a, b


def longer_wins(system, user, schema):
    """Fake pairwise judge with pure length bias, fake single judge that rewards length."""
    if schema is A.PAIR_SCHEMA:
        a, b = texts(user)
        return json.dumps({"winner": "A" if len(a) >= len(b) else "B", "reason": f"len {len(a)} vs {len(b)}"}), {"model": "fake", "input_tokens": 1, "output_tokens": 1}
    s = min(1.0, len(user) / 20000)
    return json.dumps({c: {"score": s, "evidence": "x"} for c in J.CRITERIA}), {"model": "fake", "input_tokens": 2, "output_tokens": 2}


def always_a(system, user, schema):
    return json.dumps({"winner": "A", "reason": "the first one"}), {"model": "fake"}


def test_pairs_one_per_shared_scenario_with_labelled_transcripts():
    a = {"happy-path-book": ev("happy-path-book", "v1"), "identity-fail": ev("identity-fail", "v1")}
    b = {"happy-path-book": ev("happy-path-book")}
    (p,) = A.make_pairs("v1v2", {"v1": a, "v2": b})
    assert p["pair_id"] == "v1v2:happy-path-book" and p["labels"] == ["v1", "v2"]
    assert p["texts"]["v2"] == J.transcript_text(b["happy-path-book"]) and p["texts"]["v1"] != p["texts"]["v2"]


def test_order_swap_maps_shown_letters_back_to_sides():
    p = {"labels": ["v1", "v2"]}
    assert A.shown(p, "AB") == ("v1", "v2") and A.shown(p, "BA") == ("v2", "v1")
    assert A.verdict_label(p, "AB", "A") == "v1" and A.verdict_label(p, "BA", "A") == "v2"
    assert A.verdict_label(p, "AB", "B") == "v2" and A.verdict_label(p, "BA", "B") == "v1"
    assert A.verdict_label(p, "BA", "tie") == "tie"


def test_both_orders_rule():
    assert A.both_orders("v2", "v2") == "v2"
    assert A.both_orders("v1", "v2") == "tie"
    assert A.both_orders("v2", "tie") == "tie"
    assert A.both_orders("tie", "tie") == "tie"


def test_position_biased_judge_flips_every_pair_and_rule_turns_them_into_ties(tmp_path):
    scs = {"happy-path-book": json.loads(Path("scenarios/happy-path-book.json").read_text())}
    pairs = A.make_pairs("v1v2", {"v1": {"happy-path-book": ev("happy-path-book", "v1")}, "v2": {"happy-path-book": ev("happy-path-book")}})
    (r,) = A.run_pairwise(pairs, scs, "fake", "baseline", A.Calls(tmp_path / "r.json"), always_a)
    assert (r["AB"], r["BA"], r["flipped"], r["both_orders"]) == ("v1", "v2", True, "tie")
    s = A.block_stats([r])
    assert s["flips"] == 1 and s["reversals"] == 1 and s["shown_first_won_both"] == 1 and s["decisive_both_orders"] == 0


def test_judge_sees_the_swapped_transcripts_and_calls_resume_from_disk(tmp_path):
    scs = {"happy-path-book": json.loads(Path("scenarios/happy-path-book.json").read_text())}
    pairs = A.make_pairs("length", {"original": {"happy-path-book": ev("happy-path-book")},
                                    "padded": {"happy-path-book": A.pad(ev("happy-path-book"))}})
    seen = []
    def spy(system, user, schema):
        seen.append(texts(user))
        return longer_wins(system, user, schema)
    (r,) = A.run_pairwise(pairs, scs, "fake", "fixed", A.Calls(tmp_path / "r.json"), spy)
    assert seen[0] == seen[1][::-1]  # same two transcripts, swapped
    assert (r["AB"], r["BA"], r["flipped"]) == ("padded", "padded", False)
    assert A.FIX_LINE in A.pair_system("fixed") and A.FIX_LINE not in A.pair_system("baseline")
    again = A.run_pairwise(pairs, scs, "fake", "fixed", A.Calls(tmp_path / "r.json"), lambda *a: (_ for _ in ()).throw(AssertionError("called")))
    assert again[0]["both_orders"] == "padded"


def test_padding_keeps_facts_and_adds_length():
    for path in sorted(V2.glob("*.jsonl")):
        orig = A.load_events(path)
        padded = A.pad(orig)
        assert len(padded) == len(orig)
        for o, p in zip(orig, padded):
            if o["type"] == "assistant":
                assert p["text"].startswith(o["text"]) and len(p["text"]) > len(o["text"])
            else:
                assert p == o
        t0, t1 = J.transcript_text(orig), J.transcript_text(padded)
        assert len(t1) > len(t0)
        assert set(TIME_RE.findall(t1)) == set(TIME_RE.findall(t0))  # no new times
        assert set(re.findall(r"\d+", t1)) == set(re.findall(r"\d+", t0))  # no new numbers, dates or ids
        assert set(re.findall(r"Dr\. \w+", t1)) == set(re.findall(r"Dr\. \w+", t0))  # no new providers


def test_kappa_and_human_sheet_parsing():
    spec = importlib.util.spec_from_file_location("score_human", "evals/judge_audit/score_human.py")
    H = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(H)
    assert H.kappa(["x", "y", "x", "y"], ["x", "y", "x", "y"]) == 1.0
    assert H.kappa(["x", "x", "y", "y"], ["x", "y", "x", "y"]) == 0.0
    assert H.kappa(["x", "x"], ["x", "x"]) is None
    key = {"H01": {"pair_id": "v1v2:a", "X": "v2", "Y": "v1"}, "H02": {"pair_id": "v1v2:b", "X": "v1", "Y": "v2"}, "H03": {"pair_id": "v1v2:c", "X": "v1", "Y": "v2"}}
    assert H.human_verdicts("H01: X\nH02: y\nH03: \n", key) == {"v1v2:a": "v2", "v1v2:b": "v2"}


def test_audit_end_to_end_with_fake_judges(tmp_path):
    claude_stand_in = lambda out, scs: {s["id"]: ev(s["id"], "v1") for s in scs}  # plumbing only
    res = A.audit(tmp_path, A.Calls(tmp_path / "results.json"), {"opus": longer_wins, "gemini": always_a}, claude_stand_in)
    b = res["summary"]["blocks"]
    assert len(b) == len(A.PLAN) and all(x["pairs"] == 12 for x in b.values())
    assert b["v1v2|gemini|baseline"]["flips"] == 12 and b["v1v2|gemini|baseline"]["decisive_both_orders"] == 0
    assert b["length|opus|baseline"]["picks_both_orders"] == {"padded": 12}
    assert res["summary"]["length_single"]["baseline"]["padded_higher"] == 12
    assert len(res["judge_verdicts"]["opus/baseline/both_orders"]) == 24
    report = (tmp_path / "REPORT.md").read_text()
    assert "pending human sheet" in report and "## Two verdicts that flipped with order" in report
    sheet, key = (tmp_path / "human_sheet.md").read_text(), json.loads((tmp_path / "human_sheet_key.json").read_text())
    assert len(key) == 24 and "H24: " in sheet
    assert not re.search(r"\b(v1|v2|gemini|claude)\b", sheet.split("## Rubric")[1])  # blinded
    (tmp_path / "human_sheet.md").write_text(sheet.replace("H01: ", "H01: X"))
    A.audit(tmp_path, A.Calls(tmp_path / "results.json"), {"opus": longer_wins, "gemini": always_a}, claude_stand_in)
    assert "H01: X" in (tmp_path / "human_sheet.md").read_text()  # an answered sheet is never overwritten
