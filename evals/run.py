"""python -m evals.run --prompt prompts/v1.md --out reports/v1.json [--only id] [--no-judge]"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from agent.backend import Clinic
from agent.chat import load_env
from agent.loop import MODEL as AGENT_MODEL, Agent, gemini_model
from evals import judge as J
from evals.checks import TIME_RE, run_checks
from evals.score import aggregate, score_scenario
from evals.simulate import DONE, gemini_patient
from evals import versions as V
from evals.gate import gate
from evals.improver import propose

SCENARIOS_DIR = Path("scenarios")
AUTO_AFFIRM = "Yes, that's right."
AUTO_CHOOSE = "The first one, please."
MAX_AUTO_AFFIRM = 3


def stand_in_reply(agent_text: str) -> str | None:
    """Minimal patient stand-in for a scripted scenario whose script has run out (D37, D39).
    Answers only when no booking exists yet and the agent asked a question that names a slot time:
    a choice between several times or providers gets 'the first one', a single read-back gets 'yes'."""
    t = agent_text.rstrip()
    if not t.endswith("?") or not TIME_RE.search(t):
        return None
    times = {m.lower() for m in TIME_RE.findall(t)}
    if len(times) > 1 or re.search(r"\bwhich\b|\beither\b|\bor\b", t, re.I):
        return AUTO_CHOOSE
    return AUTO_AFFIRM


def load_scenarios(only: str | None = None) -> list[dict]:
    scs = [json.loads(p.read_text()) for p in sorted(SCENARIOS_DIR.glob("*.json"))]
    scs = [s for s in scs if "id" in s and (only is None or s["id"] == only)]
    if not scs:
        sys.exit(f"no scenarios matched {only!r}")
    return scs


def make_fault_hooks(clinic: Clinic, faults: list[dict], agent_ref: list):
    """Returns (on_tool_call, on_tool_result).
    before_tool=book_appointment, which=requested: the slot the patient picked is gone at booking time, whatever the
    conversation shape (D38). Fires once.
    after_tool=get_available_slots, which=first_offered: takes the first slot of every fetch until slot_taken is hit (D26)."""
    disarmed = set()

    def before(name, args):
        for i, f in enumerate(faults):
            if i in disarmed or f.get("before_tool") != name or f["action"] != "take_slot":
                continue
            sid = args.get("slot_id") if f.get("which", "requested") == "requested" else f["which"]
            if sid and sid in clinic.slots and sid not in clinic.taken:
                clinic.taken[sid] = "FAULT"
                disarmed.add(i)
                agent_ref[0].log("fault", action="take_slot", slot_id=sid, when="before_book")

    def after(name, args, result):
        for i, f in enumerate(faults):
            if i in disarmed or "after_tool" not in f:
                continue
            if f["action"] == "take_slot" and name == "book_appointment" and result.get("error") == "slot_taken":
                disarmed.add(i)
            elif f["action"] == "take_slot" and name == f["after_tool"] and result.get("slots"):
                sid = result["slots"][0]["slot_id"] if f.get("which", "first_offered") == "first_offered" else f["which"]
                clinic.taken[sid] = "FAULT"
                agent_ref[0].log("fault", action="take_slot", slot_id=sid)
    return before, after


def run_scenario(sc: dict, *, prompt_text: str, trace_path: Path, model_fn, patient_fn=None, judge_fn=None) -> dict:
    """model_fn: Agent model seam. patient_fn: (brief) -> next_line callable, required for simulated. judge_fn: (sc, events, checks) -> dict | None."""
    clinic = Clinic.from_seed()
    for b in sc.get("setup", {}).get("book", []):
        clinic.verified.add(b["patient_id"])
        assert clinic.book_appointment(b["patient_id"], b["slot_id"], b.get("reason", "seeded"))["ok"], f"bad setup in {sc['id']}"
        clinic.verified.discard(b["patient_id"])
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace_path.unlink(missing_ok=True)
    ref: list = []
    before, after = make_fault_hooks(clinic, sc.get("faults", []), ref) if sc.get("faults") else (None, None)
    agent = Agent(clinic, model_fn, trace_path, system_prompt=prompt_text, on_tool_result=after, on_tool_call=before)
    ref.append(agent)
    max_turns = sc.get("max_turns", 12)
    if sc["mode"] == "scripted":
        reply = ""
        for t in sc["turns"][:max_turns]:
            reply = agent.turn(t)
        # Affirm on demand (D37): a fixed script cannot answer a confirmation question the agent asks after the
        # script's last line. If no booking exists yet and the agent just read back a slot and asked, say yes, at most twice.
        affirmations = 0
        while affirmations < MAX_AUTO_AFFIRM and agent.state.turn_count < max_turns and agent.state.booked_appointment_id is None:
            line = stand_in_reply(reply)
            if line is None:
                break
            affirmations += 1
            agent.log("auto_affirm", text=line)
            reply = agent.turn(line)
    else:
        patient = patient_fn(sc["brief"])
        line = patient(None)
        while line != DONE and agent.state.turn_count < max_turns:
            line = patient(agent.turn(line))
    agent.log("backend", appointments=clinic.appointments, taken=sorted(clinic.taken))
    events = [json.loads(l) for l in trace_path.read_text().splitlines()]
    checks = run_checks(events, sc)
    jd = judge_fn(sc, events, checks) if judge_fn else None
    return {"id": sc["id"], "category": sc.get("category"), "mode": sc["mode"], "trace": str(trace_path),
            "turns": agent.state.turn_count, "checks": checks, "judge": jd, **score_scenario(checks, jd)}


def write_markdown(report: dict, path: Path) -> None:
    o = report["overall"]
    L = [f"# Eval report {report['version']}", "",
         f"prompt `{report['prompt_path']}` sha256 `{report['prompt_sha256'][:12]}` | agent `{report['models']['agent']}` | judge `{report['models']['judge']}` | simulator `{report['models']['simulator']}` | {report['generated_at']}", "",
         f"**Mean score {o['mean_score']}** over {o['scenario_count']} scripted scenarios, **{o['pass_count']} pass** at >= 0.8.", "",
         "| scenario | mode | score | gates failed | failed checks | lowest judge criterion |", "|---|---|---|---|---|---|"]
    for r in report["scenarios"]:
        low = min(r["judge"].items(), key=lambda kv: kv[1]["score"]) if r["judge"] else None
        lowtxt = f"{low[0]} {low[1]['score']:.1f}: {low[1]['evidence']}" if low else "no judge"
        L.append(f"| {r['id']} | {r['mode']} | {r['score']:.2f} | {', '.join(r['gate_failed']) or ''} | {', '.join(r['failed_checks']) or ''} | {lowtxt} |")
    L += ["", "## Failures by check (scripted only)", ""]
    for name, ids in o["failures_by_check"].items():
        L.append(f"- **{name}** ({len(ids)}): {', '.join(ids)}")
    if o["informative"]:
        L += ["", "## Simulated scenarios (informative, not gates)", ""] + [f"- {i['id']}: {i['score']:.2f}, failed {i['failed_checks'] or 'nothing'}" for i in o["informative"]]
    L += ["", "## Evidence for failed checks", ""]
    for r in report["scenarios"]:
        for c in r["checks"]:
            if not c["passed"]:
                L.append(f"- `{r['id']}` **{c['name']}**: {c['evidence']}")
    path.write_text("\n".join(L) + "\n")



def evaluate(prompt_path: Path, out_path: Path, *, client, judge_fn, modes=("scripted", "simulated"), only=None, reuse=False) -> dict:
    """Runs every scenario in `modes` against the prompt and writes <out>.json and <out>.md. Returns the report."""
    prompt_text = prompt_path.read_text().strip()
    version = out_path.stem
    if reuse and out_path.exists():
        cached = json.loads(out_path.read_text())
        if cached["prompt_sha256"] == hashlib.sha256(prompt_text.encode()).hexdigest() and all(
                any(r["mode"] == m for r in cached["scenarios"]) or m == "simulated" for m in modes):
            print(f"reusing {out_path} (same prompt hash)")
            return cached
    results = []
    for sc in load_scenarios(only):
        if sc["mode"] not in modes:
            continue
        runs = 2 if sc["mode"] == "simulated" else 1
        for i in range(1, runs + 1):
            rid = sc["id"] if runs == 1 else f"{sc['id']}-run{i}"
            print(f"running {rid} ...", end=" ", flush=True)
            r = run_scenario(sc, prompt_text=prompt_text, trace_path=Path("runs/evals") / version / f"{rid}.jsonl",
                             model_fn=gemini_model(client), patient_fn=lambda brief: gemini_patient(client, brief, AGENT_MODEL), judge_fn=judge_fn)
            r["id"] = rid
            print(f"score {r['score']} failed={r['failed_checks']} gates={r['gate_failed']}")
            results.append(r)
    report = {"version": version, "prompt_path": str(prompt_path), "prompt_sha256": hashlib.sha256(prompt_text.encode()).hexdigest(),
              "models": {"agent": AGENT_MODEL, "judge": None if judge_fn is None else J.JUDGE_MODEL, "simulator": AGENT_MODEL},
              "generated_at": datetime.now().isoformat(timespec="seconds"), "scenarios": results, "overall": aggregate(results)}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=1) + "\n")
    write_markdown(report, out_path.with_suffix(".md"))
    print(f"\nmean {report['overall']['mean_score']}  pass {report['overall']['pass_count']}/{report['overall']['scenario_count']}  -> {out_path}, {out_path.with_suffix('.md')}")
    return report


def load_traces(report: dict) -> dict[str, list[dict]]:
    return {r["id"]: [json.loads(l) for l in Path(r["trace"]).read_text().splitlines()] for r in report["scenarios"]}


def _score_table(g: dict) -> list[str]:
    L = ["| scenario | before | after | delta |", "|---|---|---|---|"]
    L += [f"| {p['id']} | {p['before']:.3f} | {p['after']:.3f} | {p['delta']:+.3f} |" for p in g["per_scenario"]]
    return L


def run_loop(start: Path, max_iterations: int, *, evaluate_fn, propose_fn, auto_apply: bool, history_dir: Path = Path("history"),
             reports_dir: Path = Path("reports"), ask=input) -> dict:
    """evaluate -> propose one rule -> apply as v<n+1> -> re-evaluate scripted -> gate -> accept or stop.
    evaluate_fn(prompt_path, out_path, modes, reuse) -> report. propose_fn(prompt_text, report, out_json_path) -> rule."""
    history_dir.mkdir(exist_ok=True)
    md = [f"# Improvement loop, started {datetime.now().isoformat(timespec='seconds')}", "", f"Start prompt `{start}`, max {max_iterations} iterations.", ""]
    current = start
    report = evaluate_fn(current, reports_dir / f"{current.stem}.json", ("scripted",), True)
    summary = {"start": str(start), "iterations": [], "final": str(current)}
    for it in range(1, max_iterations + 1):
        fails = {k: v for k, v in report["overall"]["failures_by_check"].items() if not k.startswith("judge:")}
        md += [f"## Iteration {it}: {current.stem}", ""]
        if not fails:
            md += ["No failed layer-1 checks remain. Loop ends.", ""]
            break
        try:
            rule = propose_fn(current.read_text().strip(), report, reports_dir / f"{current.stem}.json")
        except ValueError as e:
            md += [f"Improver stopped: {e}", ""]
            break
        print(f"\n[iteration {it}] target {rule['target_check']} ({len(rule['source_scenarios'])} scenarios)\nproposed rule:\n  {rule['rule_text']}\nrationale: {rule['rationale']}\nrisk: {rule['risk']}")
        if not auto_apply and ask("apply this rule? [y/n] ").strip().lower() != "y":
            md += [f"Target `{rule['target_check']}`. Proposed rule declined by human. Loop ends.", "", f"> {rule['rule_text']}", ""]
            break
        new_path, rule = V.apply_rule(current, rule)
        new_report = evaluate_fn(new_path, reports_dir / f"{new_path.stem}.json", ("scripted",), False)
        g = gate(report, new_report, rule["target_check"])
        verdict = "accepted" if g["accepted"] else "rejected"
        print(f"[iteration {it}] gate: {verdict}" + ("" if g["accepted"] else "\n  " + "\n  ".join(g["reasons"])))
        entry = {"version": new_path.stem, "path": str(new_path), "sha256": V.sha(new_path.read_text()), "parent": current.stem,
                 "parent_sha256": V.sha(current.read_text()), "rule": rule, "verdict": verdict, "gate": g,
                 "scores": {"before_mean": g["mean_before"], "after_mean": g["mean_after"], "target_pass_before": g["target_pass_before"], "target_pass_after": g["target_pass_after"]}}
        V.record(entry, history_dir / "versions.json")
        summary["iterations"].append(entry)
        md += [f"**Target check:** `{rule['target_check']}` on {', '.join(rule['source_scenarios'])}", "",
               f"**Rule {rule['rule_id']}:** {rule['rule_text']}", "", f"*Rationale:* {rule['rationale']}  ", f"*Risk:* {rule['risk']}", "",
               f"**{current.stem} -> {new_path.stem}**: mean {g['mean_before']} -> {g['mean_after']}, {rule['target_check']} passing {g['target_pass_before']} -> {g['target_pass_after']}", ""]
        md += _score_table(g) + ["", f"**Gate: {verdict.upper()}**" + ("" if g["accepted"] else " because:"), ""]
        md += [f"- {r}" for r in g["reasons"]] + [""]
        if not g["accepted"]:
            (history_dir / f"reject-{new_path.stem}.json").write_text(json.dumps(entry, indent=1) + "\n")
            md += [f"Version {new_path.stem} discarded (kept on disk for the record, not used as a parent). Loop stops; retrying with a different rule is future work.", ""]
            break
        current, report = new_path, new_report
    summary["final"] = str(current)
    md += ["## Final accepted prompt", "", f"`{current}` sha256 `{V.sha(current.read_text())[:12]}`", "", "```", current.read_text().strip(), "```", ""]
    (history_dir / "loop.md").write_text("\n".join(md))
    summary["loop_md"] = str(history_dir / "loop.md")
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", type=Path)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--only")
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--start", type=Path, default=Path("prompts/v1.md"))
    ap.add_argument("--max-iterations", type=int, default=3)
    ap.add_argument("--auto-apply", action="store_true")
    args = ap.parse_args(argv)
    load_env()
    from google import genai
    if not os.environ.get("GEMINI_API_KEY") or (not args.no_judge and not os.environ.get("ANTHROPIC_API_KEY")):
        sys.exit("GEMINI_API_KEY and ANTHROPIC_API_KEY must be set in .env (ANTHROPIC optional with --no-judge)")
    client = genai.Client()
    judge_fn = None if args.no_judge else J.judge
    if args.loop:
        def evaluate_fn(prompt_path, out_path, modes, reuse):
            return evaluate(prompt_path, out_path, client=client, judge_fn=judge_fn, modes=modes, reuse=reuse)
        def propose_fn(prompt_text, report, _out):
            return propose(prompt_text, report, load_traces(report))
        summary = run_loop(args.start, args.max_iterations, evaluate_fn=evaluate_fn, propose_fn=propose_fn, auto_apply=args.auto_apply)
        final = Path(summary["final"])
        print(f"\nfinal accepted prompt: {final}. Running simulated scenarios on it (informative) ...")
        evaluate(final, Path("reports") / f"{final.stem}-simulated.json", client=client, judge_fn=judge_fn, modes=("simulated",))
        print(f"loop record: {summary['loop_md']}")
        return
    if not (args.prompt and args.out):
        sys.exit("--prompt and --out are required without --loop")
    evaluate(args.prompt, args.out, client=client, judge_fn=judge_fn, only=args.only)


if __name__ == "__main__":
    main()
