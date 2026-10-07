"""python -m evals.run --prompt prompts/v1.md --out reports/v1.json [--only id] [--no-judge]"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from agent.backend import Clinic
from agent.chat import load_env
from agent.loop import MODEL as AGENT_MODEL, Agent, gemini_model
from evals import judge as J
from evals.checks import run_checks
from evals.score import aggregate, score_scenario
from evals.simulate import DONE, gemini_patient

SCENARIOS_DIR = Path("scenarios")


def load_scenarios(only: str | None = None) -> list[dict]:
    scs = [json.loads(p.read_text()) for p in sorted(SCENARIOS_DIR.glob("*.json"))]
    scs = [s for s in scs if "id" in s and (only is None or s["id"] == only)]
    if not scs:
        sys.exit(f"no scenarios matched {only!r}")
    return scs


def make_fault_hook(clinic: Clinic, faults: list[dict], agent_ref: list):
    fired = set()

    def hook(name, args, result):
        for i, f in enumerate(faults):
            if i in fired or f["after_tool"] != name:
                continue
            if f["action"] == "take_slot" and result.get("slots"):
                sid = result["slots"][0]["slot_id"] if f.get("which", "first_offered") == "first_offered" else f["which"]
                clinic.taken[sid] = "FAULT"
                fired.add(i)
                agent_ref[0].log("fault", action="take_slot", slot_id=sid)
    return hook


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
    hook = make_fault_hook(clinic, sc.get("faults", []), ref) if sc.get("faults") else None
    agent = Agent(clinic, model_fn, trace_path, system_prompt=prompt_text, on_tool_result=hook)
    ref.append(agent)
    max_turns = sc.get("max_turns", 12)
    if sc["mode"] == "scripted":
        for t in sc["turns"][:max_turns]:
            agent.turn(t)
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


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--only")
    ap.add_argument("--no-judge", action="store_true")
    args = ap.parse_args(argv)
    load_env()
    from google import genai
    if not os.environ.get("GEMINI_API_KEY") or (not args.no_judge and not os.environ.get("ANTHROPIC_API_KEY")):
        sys.exit("GEMINI_API_KEY and ANTHROPIC_API_KEY must be set in .env (ANTHROPIC optional with --no-judge)")
    client = genai.Client()
    prompt_text = args.prompt.read_text().strip()
    version = args.out.stem
    judge_fn = None if args.no_judge else J.judge
    results = []
    for sc in load_scenarios(args.only):
        runs = 2 if sc["mode"] == "simulated" else 1
        for i in range(1, runs + 1):
            rid = sc["id"] if runs == 1 else f"{sc['id']}-run{i}"
            print(f"running {rid} ...", end=" ", flush=True)
            r = run_scenario(sc, prompt_text=prompt_text, trace_path=Path("runs/evals") / version / f"{rid}.jsonl",
                             model_fn=gemini_model(client), patient_fn=lambda brief: gemini_patient(client, brief, AGENT_MODEL), judge_fn=judge_fn)
            r["id"] = rid
            print(f"score {r['score']} failed={r['failed_checks']} gates={r['gate_failed']}")
            results.append(r)
    report = {"version": version, "prompt_path": str(args.prompt), "prompt_sha256": hashlib.sha256(prompt_text.encode()).hexdigest(),
              "models": {"agent": AGENT_MODEL, "judge": None if args.no_judge else J.JUDGE_MODEL, "simulator": AGENT_MODEL},
              "generated_at": datetime.now().isoformat(timespec="seconds"), "scenarios": results, "overall": aggregate(results)}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=1) + "\n")
    write_markdown(report, args.out.with_suffix(".md"))
    print(f"\nmean {report['overall']['mean_score']}  pass {report['overall']['pass_count']}/{report['overall']['scenario_count']}  -> {args.out}, {args.out.with_suffix('.md')}")


if __name__ == "__main__":
    main()
