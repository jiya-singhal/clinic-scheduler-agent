"""Claude proposes ONE rule for the single highest-count failed check (D31). Validation lives here, not in the model."""
from __future__ import annotations

import json
import re
from typing import Callable

from evals.judge import transcript_text

IMPROVER_MODEL = "claude-opus-5-5"
MAX_RULE_CHARS = 400
FORBIDDEN_WORDS = ["scenario", "eval", "test", "check", "judge"]
SCHEMA = {"type": "object", "additionalProperties": False,
          "required": ["rule_id", "target_check", "source_scenarios", "rule_text", "rationale", "risk"],
          "properties": {"rule_id": {"type": "string"}, "target_check": {"type": "string"},
                         "source_scenarios": {"type": "array", "items": {"type": "string"}},
                         "rule_text": {"type": "string"}, "rationale": {"type": "string"}, "risk": {"type": "string"}}}

SYSTEM = """You improve the system prompt of a voice appointment-scheduling assistant for a clinic, one rule at a time.

You will see the current prompt, a summary of which programmatic checks failed on which scenarios, and, for ONE target check,
the transcripts of the failing conversations with the check's evidence. Propose exactly one new rule that fixes that failure.

Hard constraints:
- You may only ADD a rule. Never rewrite, soften or remove existing prompt text.
- rule_text: one to three imperative sentences in the assistant's own voice (instructions it follows), at most 400 characters.
- rule_text must generalise. Do not mention scenario names, check names, patients' names, specific dates, or the existence of an evaluation.
  Never use the words scenario, eval, test, check or judge in rule_text.
- Prefer the smallest rule that fixes the target failure without harming other behaviours. Say in `risk` what it could regress."""


def pick_target(failures_by_check: dict) -> str | None:
    cands = {k: v for k, v in failures_by_check.items() if not k.startswith("judge:") and v}
    return max(cands, key=lambda k: (len(cands[k]), k)) if cands else None


def build_user_message(prompt_text: str, report: dict, target: str, traces: dict[str, list[dict]]) -> str:
    fails = report["overall"]["failures_by_check"]
    parts = [f"CURRENT PROMPT:\n{prompt_text}\n", f"FAILURES BY CHECK (scripted scenarios):\n{json.dumps(fails, indent=1)}\n",
             f"TARGET CHECK: {target}\n"]
    for r in report["scenarios"]:
        if r["id"] in fails.get(target, []):
            ev = next((c["evidence"] for c in r["checks"] if c["name"] == target), "")
            parts.append(f"--- {r['id']} ({r['category']}) score {r['score']}\nCHECK EVIDENCE: {ev}\nTRANSCRIPT:\n{transcript_text(traces[r['id']])}\n")
    parts.append("Propose the one rule as JSON.")
    return "\n".join(parts)


def validate(rule: dict, target: str, scenario_ids: list[str], check_names: list[str]) -> list[str]:
    t = rule.get("rule_text", "")
    low = t.lower()
    problems = []
    if len(t) > MAX_RULE_CHARS:
        problems.append(f"rule_text is {len(t)} chars, max {MAX_RULE_CHARS}")
    for w in FORBIDDEN_WORDS:
        if re.search(rf"\b{w}s?\b", low):
            problems.append(f"rule_text uses forbidden word '{w}'")
    for sid in scenario_ids:
        if sid.lower() in low or sid.replace("-", " ").lower() in low:
            problems.append(f"rule_text mentions scenario '{sid}'")
    for cn in check_names:
        if cn.lower() in low or cn.replace("_", " ").lower() in low:
            problems.append(f"rule_text mentions check '{cn}'")
    if rule.get("target_check") != target:
        problems.append(f"target_check must be {target}")
    if not t.strip():
        problems.append("rule_text empty")
    return problems


def anthropic_call(system: str, user: str, model: str = IMPROVER_MODEL) -> str:
    import anthropic
    resp = anthropic.Anthropic().messages.create(
        model=model, max_tokens=4096, system=system, messages=[{"role": "user", "content": user}],
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}})
    if resp.stop_reason == "refusal":
        raise RuntimeError("improver refused")
    return next(b.text for b in resp.content if b.type == "text")


def propose(prompt_text: str, report: dict, traces: dict[str, list[dict]], call: Callable[[str, str], str] = anthropic_call) -> dict:
    """Returns the validated rule, or raises ValueError after one retry with the violations fed back."""
    target = pick_target(report["overall"]["failures_by_check"])
    if target is None:
        raise ValueError("no failed layer-1 check to target")
    ids = [r["id"] for r in report["scenarios"]]
    names = sorted({c["name"] for r in report["scenarios"] for c in r["checks"]})
    user = build_user_message(prompt_text, report, target, traces)
    rule = json.loads(call(SYSTEM, user))
    problems = validate(rule, target, ids, names)
    if problems:
        retry = user + "\n\nYour previous proposal violated these constraints, fix them:\n- " + "\n- ".join(problems) + f"\nPrevious: {json.dumps(rule)}"
        rule = json.loads(call(SYSTEM, retry))
        problems = validate(rule, target, ids, names)
        if problems:
            raise ValueError("improver violated constraints twice: " + "; ".join(problems))
    rule["source_scenarios"] = report["overall"]["failures_by_check"][target]
    rule["target_check"] = target
    return rule
