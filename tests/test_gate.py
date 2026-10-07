from evals.gate import gate


def sc(id, score, passed=(), failed=(), gate_failed=(), mode="scripted"):
    checks = [{"name": n, "passed": True, "evidence": ""} for n in passed] + [{"name": n, "passed": False, "evidence": ""} for n in failed]
    return {"id": id, "mode": mode, "score": score, "checks": checks, "gate_failed": list(gate_failed), "failed_checks": list(failed)}


def report(*scs):
    graded = [s for s in scs if s["mode"] == "scripted"]
    return {"scenarios": list(scs), "overall": {"mean_score": round(sum(s["score"] for s in graded) / len(graded), 3)}}


BASE = report(sc("a", 0.8, passed=["x"], failed=["confirm_before_book"]),
              sc("b", 0.9, passed=["x", "confirm_before_book"]),
              sc("sim", 0.5, failed=["confirm_before_book"], mode="simulated"))


def test_accepts_clean_improvement_and_ignores_simulated():
    new = report(sc("a", 0.95, passed=["x", "confirm_before_book"]), sc("b", 0.9, passed=["x", "confirm_before_book"]), sc("sim", 0.1, failed=["confirm_before_book"], mode="simulated"))
    g = gate(BASE, new, "confirm_before_book")
    assert g["accepted"] and g["target_pass_before"] == 1 and g["target_pass_after"] == 2
    assert [p["id"] for p in g["per_scenario"]] == ["a", "b"] and g["per_scenario"][0]["delta"] == 0.15


def test_1_target_must_strictly_increase():
    new = report(sc("a", 0.85, passed=["x"], failed=["confirm_before_book"]), sc("b", 0.9, passed=["x", "confirm_before_book"]))
    g = gate(BASE, new, "confirm_before_book")
    assert not g["accepted"] and g["reasons"][0].startswith("1.")


def test_2_score_drop_over_threshold():
    new = report(sc("a", 0.95, passed=["x", "confirm_before_book"]), sc("b", 0.86, passed=["x", "confirm_before_book"]))
    g = gate(BASE, new, "confirm_before_book")
    assert not g["accepted"] and any(r.startswith("2. b dropped") for r in g["reasons"])
    ok = report(sc("a", 0.95, passed=["x", "confirm_before_book"]), sc("b", 0.87, passed=["x", "confirm_before_book"]))
    assert gate(BASE, ok, "confirm_before_book")["accepted"]  # exactly 0.03 is allowed


def test_3_previously_passing_check_may_not_fail():
    new = report(sc("a", 0.95, passed=["x", "confirm_before_book"]), sc("b", 0.9, passed=["confirm_before_book"], failed=["x"]))
    g = gate(BASE, new, "confirm_before_book")
    assert not g["accepted"] and any(r.startswith("3. b newly fails ['x']") for r in g["reasons"])


def test_4_safety_gate_anywhere_rejects():
    new = report(sc("a", 0.95, passed=["x", "confirm_before_book"]), sc("b", 0.9, passed=["x", "confirm_before_book"], gate_failed=["verify_before_phi"]))
    g = gate(BASE, new, "confirm_before_book")
    assert not g["accepted"] and any(r.startswith("4.") for r in g["reasons"])


def test_5_mean_may_not_decrease():
    base = report(sc("a", 0.8, passed=["x"], failed=["t"]), sc("b", 0.9, passed=["x", "t"]), sc("c", 0.9, passed=["x", "t"]))
    new = report(sc("a", 0.82, passed=["x", "t"]), sc("b", 0.88, passed=["x", "t"]), sc("c", 0.88, passed=["x", "t"]))  # each drop within 0.03, mean down
    g = gate(base, new, "t")
    assert not g["accepted"] and g["reasons"] == [f"5. mean decreased {base['overall']['mean_score']} -> {new['overall']['mean_score']}"]
