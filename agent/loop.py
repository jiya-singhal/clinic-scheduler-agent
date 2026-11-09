"""One-turn agent loop: user -> model -> tools -> apply -> model ... -> assistant. Every step traced to JSONL."""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from google.genai import types

from agent.backend import Clinic
from agent.state import ConversationState, apply
from agent.tools import TOOL_SCHEMAS, dispatch

MAX_TOOL_CALLS = 4
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")  # 2.5-flash is retired for new keys, see D16
ANTHROPIC_AGENT_MODEL = "claude-haiku-4-5"  # judge audit only: second answer family; accepts temperature 0, no thinking blocks to replay
SLOT_ID_RE = re.compile(r"\bP\d-\d{8}-\d{4}\b")

DEFAULT_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "v1.md"

# Signature of the model seam: (history, system_instruction, tools_enabled) -> types.Content (the model turn).
ModelFn = Callable[[list[types.Content], str, bool], types.Content]


def gemini_model(client, model: str = MODEL) -> ModelFn:
    tools = [types.Tool(function_declarations=[types.FunctionDeclaration(**s) for s in TOOL_SCHEMAS])]

    def call(history, system, tools_enabled):
        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=0,
            tools=tools,
            tool_config=types.ToolConfig(function_calling_config=types.FunctionCallingConfig(
                mode="AUTO" if tools_enabled else "NONE")),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        resp = client.models.generate_content(model=model, contents=history, config=config)
        return resp.candidates[0].content

    return call


def _json_schema(s):
    """Gemini schema dialect -> JSON Schema: `nullable: true` becomes a null type."""
    if not isinstance(s, dict):
        return s
    out = {k: _json_schema(v) for k, v in s.items() if k != "nullable"}
    if s.get("nullable"):
        out["type"] = [s["type"], "null"]
    return out


ANTHROPIC_TOOLS = [{"name": s["name"], "description": s["description"], "input_schema": _json_schema(s["parameters"])} for s in TOOL_SCHEMAS]


def to_anthropic_messages(history: list[types.Content]) -> list[dict]:
    """Gemini history -> Anthropic messages. The loop answers a model turn's calls in order in the next user turn,
    so tool_use ids are positional (call_<turn>_<part>) and each function_response takes the next id."""
    msgs, prev_ids = [], []
    for i, c in enumerate(history):
        ids, pending, blocks = [], iter(prev_ids), []
        for j, p in enumerate(c.parts or []):
            if p.function_call:
                ids.append(f"call_{i}_{j}")
                blocks.append({"type": "tool_use", "id": ids[-1], "name": p.function_call.name, "input": dict(p.function_call.args or {})})
            elif p.function_response:
                blocks.append({"type": "tool_result", "tool_use_id": next(pending), "content": json.dumps(p.function_response.response)})
            elif p.text:
                blocks.append({"type": "text", "text": p.text})
        prev_ids = ids
        if blocks:  # an empty model turn is dropped; the API merges the consecutive user turns that leaves
            msgs.append({"role": "assistant" if c.role == "model" else "user", "content": blocks})
    return msgs


def from_anthropic_content(blocks) -> types.Content:
    parts = [types.Part(text=b.text) if b.type == "text" else types.Part.from_function_call(name=b.name, args=dict(b.input))
             for b in blocks if b.type in ("text", "tool_use")]
    return types.Content(role="model", parts=parts)


def anthropic_model(client, model: str = ANTHROPIC_AGENT_MODEL) -> ModelFn:
    """Same seam as gemini_model, on Claude: tool schemas mapped to Anthropic tools, history mapped both ways."""
    def call(history, system, tools_enabled):
        resp = client.messages.create(model=model, max_tokens=1024, system=system, tools=ANTHROPIC_TOOLS, extra_body={"temperature": 0},
                                      tool_choice={"type": "auto" if tools_enabled else "none"}, messages=to_anthropic_messages(history))
        return from_anthropic_content(resp.content)

    return call


class Agent:
    def __init__(self, clinic: Clinic, model: ModelFn, trace_path: Path | None = None,
                 system_prompt: str | None = None, on_tool_result: Callable[[str, dict, dict], None] | None = None,
                 on_tool_call: Callable[[str, dict], None] | None = None):
        self.clinic, self.model = clinic, model
        self.system_prompt = system_prompt if system_prompt is not None else DEFAULT_PROMPT_PATH.read_text().strip()
        self.on_tool_result = on_tool_result
        self.on_tool_call = on_tool_call  # runs before dispatch; eval fault injection only
        self.state = ConversationState()
        self.history: list[types.Content] = []
        if trace_path is None:
            trace_path = Path("runs") / f"{datetime.now():%Y%m%d-%H%M%S}.jsonl"
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        self.trace_path = trace_path
        self._trace = open(trace_path, "a")

    def log(self, type: str, **data) -> None:
        self._trace.write(json.dumps({"ts": time.time(), "turn": self.state.turn_count, "type": type, **data}) + "\n")
        self._trace.flush()

    def flag(self, name: str) -> None:
        self.state.flags.append(name)
        self.log("flag", flag=name)

    def system_instruction(self) -> str:
        state = json.loads(self.state.to_json())
        state["today"] = f"{self.clinic.today.isoformat()} ({self.clinic.today:%A})"
        return f"{self.system_prompt}\n\nCURRENT STATE:\n{json.dumps(state, indent=1)}"

    def turn(self, user_text: str) -> str:
        self.state.turn_count += 1
        self.log("user", text=user_text)
        self.history.append(types.Content(role="user", parts=[types.Part(text=user_text)]))
        calls_made = 0
        while True:
            content = self.model(self.history, self.system_instruction(), calls_made < MAX_TOOL_CALLS)
            self.history.append(content)
            calls = [p.function_call for p in content.parts or [] if p.function_call]
            if not calls:
                break
            parts = []
            for fc in calls:
                args = dict(fc.args or {})
                self.log("tool_call", name=fc.name, args=args)
                if calls_made >= MAX_TOOL_CALLS:
                    result = {"ok": False, "error": "tool_budget_exhausted"}
                    self.flag("tool_cap_hit")
                else:
                    if self.on_tool_call:
                        self.on_tool_call(fc.name, args)
                    result = dispatch(self.clinic, self.state, fc.name, args)
                    apply(self.state, fc.name, args, result)
                    if result.get("error") == "slot_not_offered":
                        self.flag(f"slot_not_offered:{args.get('slot_id')}")
                    if self.on_tool_result:
                        self.on_tool_result(fc.name, args, result)
                calls_made += 1
                self.log("tool_result", name=fc.name, result=result)
                parts.append(types.Part.from_function_response(name=fc.name, response=result))
            self.history.append(types.Content(role="user", parts=parts))
        text = "".join(p.text for p in content.parts or [] if p.text).strip()
        for sid in sorted(set(SLOT_ID_RE.findall(text)) - set(self.state.offered_slots)):
            self.flag(f"unoffered_mention:{sid}")
        self.log("assistant", text=text)
        self.log("state", state=json.loads(self.state.to_json()))
        return text
