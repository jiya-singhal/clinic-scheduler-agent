"""LLM-as-judge bias audit after Zheng et al. 2023 ("Judging LLM-as-a-judge"): position bias, length bias and
self-preference, three fixes re-measured, and a blinded sheet for human agreement.

uv run --env-file .env python -m evals.judge_audit --out evals/judge_audit/

Every judge call is stored in <out>/results.json under a key and written after each call, so a rerun resumes
where it stopped and a fully cached run makes no API calls (it only rebuilds the summary, REPORT.md and the sheet)."""
from __future__ import annotations

import argparse
import json
import random
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

from agent.chat import load_env
from agent.loop import ANTHROPIC_AGENT_MODEL, MODEL as GEMINI_MODEL
from evals import judge as J
from evals.checks import run_checks
from evals.run import load_scenarios
from evals.score import score_scenario

TRACES = Path("history/accepted-run-traces")
PROMPT_V2 = Path("prompts/v2.md")
ORDERS = ("AB", "BA")  # AB: labels[0] is shown as Transcript A; BA: the same pair swapped
RUBRIC = J.SYSTEM.split("Criteria:\n", 1)[1]  # the six criteria of the single-transcript judge, verbatim
FIX_LINE = "Length and verbosity are not quality; do not reward restatement."
PAIR_SYSTEM = """You compare two conversations between a clinic's voice scheduling assistant and a patient who wanted the same thing in both.
Decide which conversation is better overall on the rubric below, or say tie if neither is clearly better.
Give a short reason that points at the transcripts.

Rubric:
{rubric}"""
PAIR_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["reason", "winner"],
               "properties": {"reason": {"type": "string"}, "winner": {"type": "string", "enum": ["A", "B", "tie"]}}}
FILLER = ('Just to summarise what we discussed so far: you said "{said}". I want to make sure I have understood your request '
          'correctly and that nothing has been missed, so please let me know if any of that is not right.')
SHEET_SEED = 20261109
ANSWER_RE = re.compile(r"^(H\d+):\s*(X|Y|tie)\s*$", re.I | re.M)


# ---- judges: (system, user, schema) -> (json text or None on refusal, usage) ----

def claude_json(system: str, user: str, schema: dict, model: str = J.JUDGE_MODEL):
    import anthropic
    resp = anthropic.Anthropic().messages.create(
        model=model, max_tokens=2048, system=system, messages=[{"role": "user", "content": user}],
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}})
    usage = {"model": model, "input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens}
    return (None if resp.stop_reason == "refusal" else next(b.text for b in resp.content if b.type == "text")), usage


_gemini_client = None


def gemini_json(system: str, user: str, schema: dict, model: str = GEMINI_MODEL):
    global _gemini_client
    from google import genai
    from google.genai import types
    _gemini_client = _gemini_client or genai.Client()  # kept alive: a temporary client is closed before the request
    resp = _gemini_client.models.generate_content(model=model, contents=user, config=types.GenerateContentConfig(
        system_instruction=system, temperature=0, response_mime_type="application/json", response_json_schema=schema))
    u = resp.usage_metadata
    return resp.text, {"model": model, "input_tokens": u.prompt_token_count,
                       "output_tokens": (u.candidates_token_count or 0) + (u.thoughts_token_count or 0)}


JUDGES = {"opus": claude_json, "gemini": gemini_json}


class Calls(dict):
    """Every judge call by key, persisted after each new call so a crash loses at most the call in flight."""

    def __init__(self, path: Path):
        super().__init__(json.loads(path.read_text()).get("calls", {}) if path.exists() else {})
        self.path = path

    def once(self, key: str, fn):
        if key not in self:
            self[key] = fn()
            self.path.write_text(json.dumps({"calls": self}, indent=1) + "\n")
        return self[key]


# ---- pairs and order bookkeeping ----

def load_events(path: Path) -> list[dict]:
    return [json.loads(l) for l in Path(path).read_text().splitlines()]


def make_pairs(set_name: str, sides: dict[str, dict[str, list[dict]]]) -> list[dict]:
    """sides: {label: {scenario_id: events}}, two labels. One pair per scenario present on both sides."""
    (la, a), (lb, b) = sides.items()
    return [{"pair_id": f"{set_name}:{sid}", "set": set_name, "scenario": sid, "labels": [la, lb],
             "texts": {la: J.transcript_text(a[sid]), lb: J.transcript_text(b[sid])}} for sid in sorted(a) if sid in b]


def shown(pair: dict, order: str) -> tuple[str, str]:
    """(label shown as Transcript A, label shown as Transcript B)."""
    a, b = pair["labels"]
    return (a, b) if order == "AB" else (b, a)


def verdict_label(pair: dict, order: str, winner: str) -> str:
    """Judge's A/B/tie in one order -> the label of the side it picked, or tie."""
    return "tie" if winner == "tie" else shown(pair, order)[0 if winner == "A" else 1]


def both_orders(v_ab: str, v_ba: str) -> str:
    """Fix (i): a win counts only when the same side wins in both orders; anything else is a tie."""
    return v_ab if v_ab == v_ba else "tie"


def pad(events: list[dict]) -> list[dict]:
    """Length-only perturbation: each assistant line gets information-free filler restating the patient's last line."""
    out, said = [], ""
    for e in events:
        if e["type"] == "user":
            said = e["text"]
        elif e["type"] == "assistant":
            e = {**e, "text": f"{e['text']} {FILLER.format(said=said)}"}
        out.append(e)
    return out


def brief(sc: dict) -> str:
    return f"Scenario: {sc['id']} ({sc.get('category')}).\nWhat the patient was trying to do: {json.dumps(sc.get('brief') or sc.get('turns'))}"


def pair_system(rubric: str) -> str:
    return PAIR_SYSTEM.format(rubric=RUBRIC + (f"\n\n{FIX_LINE}" if rubric == "fixed" else ""))


def pair_message(sc: dict, a_text: str, b_text: str) -> str:
    return f"{brief(sc)}\n\nTRANSCRIPT A:\n{a_text}\n\nTRANSCRIPT B:\n{b_text}\n\nWhich conversation is better overall? Return the JSON."


def run_pairwise(pairs: list[dict], scenarios: dict, judge: str, rubric: str, calls: Calls, call_fn) -> list[dict]:
    rows = []
    for p in pairs:
        raw = {}
        for order in ORDERS:
            a, b = shown(p, order)
            def go(a=a, b=b):
                text, usage = call_fn(pair_system(rubric), pair_message(scenarios[p["scenario"]], p["texts"][a], p["texts"][b]), PAIR_SCHEMA)
                data = json.loads(text) if text else {"winner": "tie", "reason": "judge refused"}
                return {"winner": data["winner"], "reason": data["reason"], "usage": usage}
            raw[order] = calls.once(f"pairwise|{judge}|{rubric}|{p['pair_id']}|{order}", go)
        v = {o: verdict_label(p, o, raw[o]["winner"]) for o in ORDERS}
        rows.append({"pair_id": p["pair_id"], "scenario": p["scenario"], "judge": judge, "rubric": rubric, "AB": v["AB"], "BA": v["BA"],
                     "shown_winner": {o: raw[o]["winner"] for o in ORDERS}, "reasons": {o: raw[o]["reason"] for o in ORDERS},
                     "flipped": v["AB"] != v["BA"], "both_orders": both_orders(v["AB"], v["BA"])})
    return rows


def run_single(sc: dict, events: list[dict], checks: list[dict], rubric: str, calls: Calls, key: str, call_fn) -> dict:
    """The existing evals/judge.py judge, unchanged; the fixed rubric only appends FIX_LINE to its system prompt."""
    def go():
        usage = {}
        def call(system, user):
            text, u = call_fn(system + (f"\n\n{FIX_LINE}" if rubric == "fixed" else ""), user, J.SCHEMA)
            usage.update(u)
            return text or json.dumps({c: {"score": 0, "evidence": "judge refused to grade this transcript"} for c in J.CRITERIA})
        grades = J.judge(sc, events, checks, call=call)
        return {"grades": grades, "mean": round(sum(g["score"] for g in grades.values()) / len(grades), 4), "usage": usage}
    return calls.once(key, go)


def claude_agent_traces(out: Path, scenarios: list[dict]) -> dict[str, list[dict]]:
    """Second answer family: the 12 scripted scenarios with the v2 prompt on Claude. Existing traces are reused."""
    import anthropic
    from agent.loop import anthropic_model
    from evals.run import run_scenario
    d, model_fn, prompt = out / "traces" / "claude-v2", None, PROMPT_V2.read_text().strip()
    for sc in scenarios:
        path = d / f"{sc['id']}.jsonl"
        if not path.exists():
            model_fn = model_fn or anthropic_model(anthropic.Anthropic())
            print(f"claude agent: {sc['id']} ...", flush=True)
            run_scenario(sc, prompt_text=prompt, trace_path=path.with_suffix(".partial"), model_fn=model_fn)
            path.with_suffix(".partial").rename(path)
    return {sc["id"]: load_events(d / f"{sc['id']}.jsonl") for sc in scenarios if (d / f"{sc['id']}.jsonl").exists()}


# ---- summary ----

def block_stats(rows: list[dict]) -> dict:
    flips = [r for r in rows if r["flipped"]]
    return {"pairs": len(rows), "flips": len(flips),
            "reversals": sum(1 for r in flips if "tie" not in (r["AB"], r["BA"])),
            "shown_first_won_both": sum(1 for r in flips if r["shown_winner"] == {"AB": "A", "BA": "A"}),
            "shown_second_won_both": sum(1 for r in flips if r["shown_winner"] == {"AB": "B", "BA": "B"}),
            "picks_AB": dict(Counter(r["AB"] for r in rows)), "picks_BA": dict(Counter(r["BA"] for r in rows)),
            "picks_single_order": dict(Counter(r[o] for r in rows for o in ORDERS)),
            "picks_both_orders": dict(Counter(r["both_orders"] for r in rows)),
            "decisive_single_order": sum(r[o] != "tie" for r in rows for o in ORDERS),
            "decisive_both_orders": sum(r["both_orders"] != "tie" for r in rows)}


def single_stats(single: dict) -> dict:
    """single: {scenario: {"original": result, "padded": result}} for one rubric."""
    d = {s: v["padded"]["mean"] - v["original"]["mean"] for s, v in single.items()}
    crit = {c: round(sum(v["padded"]["grades"][c]["score"] - v["original"]["grades"][c]["score"] for v in single.values()) / len(single), 3)
            for c in J.CRITERIA}
    return {"scenarios": len(d), "padded_higher": sum(x > 0 for x in d.values()), "equal": sum(x == 0 for x in d.values()),
            "padded_lower": sum(x < 0 for x in d.values()),
            "mean_original": round(sum(v["original"]["mean"] for v in single.values()) / len(single), 3),
            "mean_padded": round(sum(v["padded"]["mean"] for v in single.values()) / len(single), 3),
            "criterion_mean_delta": crit, "padded_higher_ids": sorted(s for s, x in d.items() if x > 0)}


def token_totals(calls: dict) -> dict:
    t: dict = {}
    for c in calls.values():
        u = c.get("usage") or {}
        m = t.setdefault(u.get("model", "unknown"), {"calls": 0, "input_tokens": 0, "output_tokens": 0})
        m["calls"] += 1
        m["input_tokens"] += u.get("input_tokens") or 0
        m["output_tokens"] += u.get("output_tokens") or 0
    return t


def layer1(traces: dict[str, list[dict]], scenarios: dict) -> dict:
    res = {sid: score_scenario(run_checks(ev, scenarios[sid]), None) for sid, ev in traces.items()}
    return {"scenarios": len(res), "all_checks_pass": sum(1 for r in res.values() if not r["failed_checks"]),
            "mean_pass_rate": round(sum(r["layer1_pass_rate"] for r in res.values()) / len(res), 3) if res else None,
            "failed": {sid: r["failed_checks"] for sid, r in res.items() if r["failed_checks"]}}


# ---- human sheet ----

def write_human_sheet(out: Path, pairs: list[dict], scenarios: dict) -> bool:
    """Blinded, shuffled X/Y sheet plus its key. Never overwrites a sheet that already has answers in it."""
    sheet, key_path = out / "human_sheet.md", out / "human_sheet_key.json"
    if sheet.exists() and ANSWER_RE.search(sheet.read_text()):
        return False
    rng = random.Random(SHEET_SEED)
    order = list(pairs)
    rng.shuffle(order)
    key, L = {}, ["# Judge audit: human sheet", "",
                  "For each pair, read both transcripts and decide which conversation is better overall on the rubric below, or tie.",
                  "Labels X and Y are random per pair. Do not open human_sheet_key.json until you are done.",
                  "Write X, Y or tie after each id in the verdict list at the bottom, then run:", "",
                  "```", "uv run python evals/judge_audit/score_human.py", "```", "", "## Rubric", "", RUBRIC, ""]
    for i, p in enumerate(order, 1):
        hid = f"H{i:02d}"
        x, y = rng.sample(p["labels"], 2)
        key[hid] = {"pair_id": p["pair_id"], "X": x, "Y": y}
        L += [f"## {hid}", "", brief(scenarios[p["scenario"]]), "", "Transcript X:", "", "```", p["texts"][x], "```", "",
              "Transcript Y:", "", "```", p["texts"][y], "```", ""]
    L += ["## Your verdicts (X, Y or tie)", ""] + [f"{h}: " for h in key] + [""]
    sheet.write_text("\n".join(L))
    key_path.write_text(json.dumps(key, indent=1) + "\n")
    return True


def judge_verdicts(rows: dict[str, list[dict]], sets: tuple[str, ...]) -> dict[str, dict[str, str]]:
    """{config: {pair_id: label|tie}} for score_human.py: each judge and rubric, as AB, BA and the both-orders rule."""
    out = {}
    for name, rs in rows.items():
        s, judge, rubric = name.split("|")
        if s in sets:
            for mode in ("AB", "BA", "both_orders"):
                out.setdefault(f"{judge}/{rubric}/{mode}", {}).update({r["pair_id"]: r[mode] for r in rs})
    return out


# ---- the audit ----

PLAN = [  # (pair set, judge, rubric); the fixed rubric is re-run where it targets a bias (length)
    ("v1v2", "opus", "baseline"), ("v1v2", "gemini", "baseline"),
    ("family", "opus", "baseline"), ("family", "gemini", "baseline"),
    ("length", "opus", "baseline"), ("length", "gemini", "baseline"),
    ("length", "opus", "fixed"), ("length", "gemini", "fixed"),
]


def audit(out: Path, calls: Calls, judges: dict, agent_traces) -> dict:
    """judges: {name: call_fn}; blocks for a judge not in it are skipped. agent_traces(out, scenarios) -> {id: events}."""
    scs = {s["id"]: s for s in load_scenarios() if s["mode"] == "scripted"}
    gem = {v: {sid: load_events(TRACES / v / f"{sid}.jsonl") for sid in scs} for v in ("v1", "v2")}
    claude = agent_traces(out, list(scs.values()))
    pairs = {"v1v2": make_pairs("v1v2", {"v1": gem["v1"], "v2": gem["v2"]}),
             "family": make_pairs("family", {"gemini": gem["v2"], "claude": claude}),
             "length": make_pairs("length", {"original": gem["v2"], "padded": {s: pad(e) for s, e in gem["v2"].items()}})}
    rows = {f"{s}|{j}|{r}": run_pairwise(pairs[s], scs, j, r, calls, judges[j]) for s, j, r in PLAN if j in judges and pairs[s]}
    single = {}
    if "opus" in judges:
        for rubric in ("baseline", "fixed"):
            for sid, ev in gem["v2"].items():
                checks = run_checks(ev, scs[sid])  # the same layer 1 results for original and padded
                single.setdefault(rubric, {})[sid] = {
                    v: run_single(scs[sid], e, checks, rubric, calls, f"single|opus|{rubric}|{sid}|{v}", judges["opus"])
                    for v, e in (("original", ev), ("padded", pad(ev)))}
    summary = {"blocks": {n: block_stats(rs) for n, rs in rows.items()},
               "length_single": {r: single_stats(s) for r, s in single.items()},
               "layer1": {"gemini-v1": layer1(gem["v1"], scs), "gemini-v2": layer1(gem["v2"], scs), "claude-v2": layer1(claude, scs)},
               "tokens": token_totals(calls)}
    results = {"meta": {"generated_at": datetime.now().isoformat(timespec="seconds"),
                        "models": {"opus_judge": J.JUDGE_MODEL, "gemini_judge": GEMINI_MODEL, "gemini_agent": GEMINI_MODEL,
                                   "claude_agent": ANTHROPIC_AGENT_MODEL},
                        "settings": {"opus": "effort low, no temperature parameter (rejected by the model)", "gemini": "temperature 0",
                                     "claude_agent": "temperature 0, prompts/v2.md, scripted scenarios"},
                        "fix_line": FIX_LINE, "filler": FILLER, "plan": [list(p) for p in PLAN], "judges_run": sorted(judges),
                        "pair_sets": {s: [p["pair_id"] for p in ps] for s, ps in pairs.items()}},
               "summary": summary, "rows": rows, "single": single,
               "judge_verdicts": judge_verdicts(rows, ("v1v2", "family")), "calls": dict(calls)}
    (out / "results.json").write_text(json.dumps(results, indent=1) + "\n")
    write_human_sheet(out, pairs["v1v2"] + pairs["family"], scs)
    write_report(results, out / "REPORT.md")
    return results


# ---- report ----

def _c(counter: dict, *labels: str) -> str:
    return " / ".join(str(counter.get(l, 0)) for l in labels)


def flipped_examples(rows: dict[str, list[dict]], n: int = 2) -> list[dict]:
    """Prefer full reversals, then the v1v2 set, then Opus."""
    cands = [r for name in sorted(rows, key=lambda k: (not k.startswith("v1v2"), "opus" not in k)) for r in rows[name] if r["flipped"]]
    cands.sort(key=lambda r: "tie" in (r["AB"], r["BA"]))
    return cands[:n]


def write_report(res: dict, path: Path) -> None:
    S, B, M = res["summary"], res["summary"]["blocks"], res["meta"]
    def blk(name):
        return B.get(name)
    L = ["# Judge audit", "",
         f"Generated {M['generated_at']} by `python -m evals.judge_audit`. Every number below is computed from `results.json` in this folder.", "",
         f"Judges: Opus `{M['models']['opus_judge']}` (cross-family, effort low) and Gemini `{M['models']['gemini_judge']}` "
         f"(same family as the agent, temperature 0). Second answer model for the self-preference test: `{M['models']['claude_agent']}` "
         "at temperature 0 with `prompts/v2.md`. Method after Zheng et al. 2023.", ""]
    L += ["| model | calls | input tokens | output tokens |", "|---|---|---|---|"]
    L += [f"| {m} | {t['calls']} | {t['input_tokens']} | {t['output_tokens']} |" for m, t in S["tokens"].items()] + [""]

    L += ["## Headline", "", "- Judge agreed with me on X/N: pending human sheet (`human_sheet.md`, scored by `score_human.py`)."]
    for j in ("opus", "gemini"):
        b = blk(f"v1v2|{j}|baseline")
        if b:
            L.append(f"- {j}: swapping answer order changed {b['flips']}/{b['pairs']} verdicts on v1 vs v2; judging in both orders and "
                     f"counting a win only when it holds both ways leaves {b['decisive_both_orders']}/{b['pairs']} decisive verdicts.")
    for r in ("baseline", "fixed"):
        s = S["length_single"].get(r)
        if s:
            L.append(f"- single-transcript judge, {r} rubric: the padded copy scored higher on {s['padded_higher']}/{s['scenarios']} transcripts "
                     f"(equal {s['equal']}, lower {s['padded_lower']}).")
    L.append("")

    L += ["## Position bias", "",
          "A verdict changed by order means the side the judge picked was different when the same two transcripts were swapped. "
          "A full reversal is one side winning in one order and the other side in the other. Shown-first won both means the judge picked "
          "whichever transcript was labelled A in both orders.", "",
          "| pair set | judge | rubric | changed by order | full reversals | shown-first won both | shown-second won both | decisive, single order | decisive, both-orders rule |",
          "|---|---|---|---|---|---|---|---|---|"]
    for name, b in B.items():
        s, j, r = name.split("|")
        L.append(f"| {s} | {j} | {r} | {b['flips']}/{b['pairs']} | {b['reversals']} | {b['shown_first_won_both']} | {b['shown_second_won_both']} | "
                 f"{b['decisive_single_order']}/{2 * b['pairs']} | {b['decisive_both_orders']}/{b['pairs']} |")
    L += ["", "Fix (i), the both-orders rule, cannot flip with order by construction, so after the fix the changed-by-order count is 0 "
          "for every row. Its cost is the last column: verdicts that were decisive in one order become ties.", ""]

    L += ["## Length bias", "", f"Padding appends this to every assistant line, with the patient's previous line filled in: \"{M['filler']}\". "
          "Facts are unchanged; only length grows.", "",
          "Single-transcript judge (`evals/judge.py`, Opus, same layer 1 results for both copies):", "",
          "| rubric | padded scored higher | equal | padded lower | mean original | mean padded | clarity delta | resolves_request delta |",
          "|---|---|---|---|---|---|---|---|"]
    for r, s in S["length_single"].items():
        L.append(f"| {r} | {s['padded_higher']}/{s['scenarios']} | {s['equal']} | {s['padded_lower']} | {s['mean_original']} | {s['mean_padded']} | "
                 f"{s['criterion_mean_delta']['clarity']:+} | {s['criterion_mean_delta']['resolves_request']:+} |")
    L += ["", "Pairwise judge, original vs padded:", "",
          "| judge | rubric | padded wins, original shown first | padded wins, padded shown first | both-orders rule: original / padded / tie |",
          "|---|---|---|---|---|"]
    for name, b in B.items():
        s, j, r = name.split("|")
        if s == "length":
            L.append(f"| {j} | {r} | {b['picks_AB'].get('padded', 0)}/{b['pairs']} | {b['picks_BA'].get('padded', 0)}/{b['pairs']} | "
                     f"{_c(b['picks_both_orders'], 'original', 'padded', 'tie')} |")
    L.append("")

    L += ["## Self-preference", "",
          "Both judges compare the Gemini agent and the Claude agent on the same 12 scripted scenarios with the same prompt, in both orders. "
          "Raw pick rates mix quality with bias, so the comparison that isolates self-preference is the gap between the two judges on the same pairs.", "",
          "| judge | single order: gemini / claude / tie (of 24) | both-orders rule: gemini / claude / tie (of 12) |", "|---|---|---|"]
    fam = {j: blk(f"family|{j}|baseline") for j in ("opus", "gemini")}
    for j, b in fam.items():
        L.append(f"| {j} | {_c(b['picks_single_order'], 'gemini', 'claude', 'tie')} | {_c(b['picks_both_orders'], 'gemini', 'claude', 'tie')} |"
                 if b else f"| {j} | not run | not run |")
    if all(fam.values()):
        g, o = fam["gemini"]["picks_single_order"], fam["opus"]["picks_single_order"]
        L += ["", f"Gemini judge picks the Gemini agent in {g.get('gemini', 0)}/24 single-order verdicts, the Opus judge in {o.get('gemini', 0)}/24. "
              f"Opus picks the Claude agent in {o.get('claude', 0)}/24, Gemini in {g.get('claude', 0)}/24."]
    L += ["", "Layer 1 checks on the same transcripts, for context (quality the judges cannot see):", "",
          "| agent run | scenarios | all checks pass | mean check pass rate |", "|---|---|---|---|"]
    L += [f"| {k} | {v['scenarios']} | {v['all_checks_pass']} | {v['mean_pass_rate']} |" for k, v in S["layer1"].items()]
    L.append("")

    L += ["## v1 vs v2 as a validity check", "", "v2 adds the read-back rule that layer 1 rewards. A judge that prefers v1 here is not measuring what the loop optimised.", "",
          "| judge | single order: v2 / v1 / tie (of 24) | both-orders rule: v2 / v1 / tie (of 12) |", "|---|---|---|"]
    for j in ("opus", "gemini"):
        b = blk(f"v1v2|{j}|baseline")
        if b:
            L.append(f"| {j} | {_c(b['picks_single_order'], 'v2', 'v1', 'tie')} | {_c(b['picks_both_orders'], 'v2', 'v1', 'tie')} |")
    L.append("")

    L += ["## Two verdicts that flipped with order", ""]
    ex = flipped_examples(res["rows"])
    if not ex:
        L += ["No verdict flipped with order in this run.", ""]
    for r in ex:
        L += [f"**{r['pair_id']}**, {r['judge']} judge, {r['rubric']} rubric: picked `{r['AB']}` in one order and `{r['BA']}` in the other.", "",
              f"- Order AB, picked {r['shown_winner']['AB']} = `{r['AB']}`: \"{r['reasons']['AB']}\"",
              f"- Order BA, picked {r['shown_winner']['BA']} = `{r['BA']}`: \"{r['reasons']['BA']}\"", ""]

    L += ["## Fixes, before and after", "", "| fix | targets | before | after |", "|---|---|---|---|"]
    for j in ("opus", "gemini"):
        b = blk(f"v1v2|{j}|baseline")
        if b:
            L.append(f"| (i) both-orders rule, {j} | position | {b['flips']}/{b['pairs']} verdicts changed by order | 0 by construction, "
                     f"{b['decisive_both_orders']}/{b['pairs']} still decisive |")
    sb, sf = S["length_single"].get("baseline"), S["length_single"].get("fixed")
    if sb and sf:
        L.append(f"| (ii) verbosity rubric line, single judge | length | padded higher {sb['padded_higher']}/{sb['scenarios']} | padded higher {sf['padded_higher']}/{sf['scenarios']} |")
    for j in ("opus", "gemini"):
        bb, bf = blk(f"length|{j}|baseline"), blk(f"length|{j}|fixed")
        if bb and bf:
            L.append(f"| (ii) verbosity rubric line, pairwise {j} | length | padded wins {bb['picks_single_order'].get('padded', 0)}/{2 * bb['pairs']} single-order, "
                     f"{bb['picks_both_orders'].get('padded', 0)}/{bb['pairs']} both orders | padded wins {bf['picks_single_order'].get('padded', 0)}/{2 * bf['pairs']}, "
                     f"{bf['picks_both_orders'].get('padded', 0)}/{bf['pairs']} |")
    if all(fam.values()):
        L.append(f"| (iii) cross-family judge | self-preference | same-family Gemini judge picks Gemini {fam['gemini']['picks_single_order'].get('gemini', 0)}/24 | "
                 f"cross-family Opus judge picks Gemini {fam['opus']['picks_single_order'].get('gemini', 0)}/24 |")
    L.append("")

    L += ["## Human agreement", "", "Pending. `human_sheet.md` holds the v1 vs v2 and Gemini vs Claude pairs in a shuffled, blinded order. "
          "Fill the verdict list and run `uv run python evals/judge_audit/score_human.py` for agreement and Cohen's kappa per judge configuration.", "",
          "## Limits", "",
          "- Twelve pairs per set. One flip is about 8 points; read the counts, not the percentages.",
          "- Each verdict is one call. Opus at effort low takes no temperature and Gemini at temperature 0 is not deterministic, so a rerun can move a count by one or two.",
          "- The pairwise judge sees transcripts only, not the layer 1 checks the production judge sees. That is deliberate for a bias test and makes it a harder task than production grading.",
          "- v2 transcripts are longer than v1 because of the read-back turn, so v1 vs v2 mixes a quality difference with a length difference.",
          "- Padding is synthetic and the filler is obviously redundant. A real verbose agent is subtler, so this bounds length bias from below.",
          "- Self-preference is confounded with quality: the two agents are different models. The judge gap on the same pairs is the comparison; it still cannot separate bias from a real family-level taste for a style.",
          "- Blinding is imperfect for a reader who knows the project: the read-back turn identifies v2.", ""]
    path.write_text("\n".join(L))


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="LLM-as-judge bias audit")
    ap.add_argument("--out", type=Path, default=Path("evals/judge_audit"))
    ap.add_argument("--judges", default="opus,gemini", help="comma list of judges to run (opus, gemini); others are reported as not run")
    ap.add_argument("--no-claude-agent", action="store_true", help="do not generate Claude-agent traces; the family set uses any that exist")
    args = ap.parse_args(argv)
    load_env()
    args.out.mkdir(parents=True, exist_ok=True)
    judges = {j: JUDGES[j] for j in args.judges.split(",")}
    agent_fn = (lambda out, scs: {s["id"]: load_events(p) for s in scs if (p := out / "traces" / "claude-v2" / f"{s['id']}.jsonl").exists()}) \
        if args.no_claude_agent else claude_agent_traces
    res = audit(args.out, Calls(args.out / "results.json"), judges, agent_fn)
    print(json.dumps(res["summary"]["blocks"], indent=1))
    print(f"-> {args.out / 'results.json'}, {args.out / 'REPORT.md'}, {args.out / 'human_sheet.md'}")


if __name__ == "__main__":
    main()
