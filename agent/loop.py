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
SLOT_ID_RE = re.compile(r"\bP\d-\d{8}-\d{4}\b")

SYSTEM_PROMPT = """You are the appointment scheduling assistant for a small primary-care clinic, speaking with a patient by voice.
Keep every reply short and natural, one or two sentences, no lists or markdown.

Rules:
1. Identify and verify the patient (full name and date of birth) with verify_patient before giving or changing any appointment information. If verification fails, ask them to repeat their details once; after a second failure, offer to escalate.
2. Never give medical advice of any kind.
3. If the patient describes an emergency (chest pain, trouble breathing, stroke signs, suicidal thoughts), stop scheduling, tell them to call emergency services right now, and call escalate_to_human with urgency high.
4. Stay in scope: booking, rescheduling, cancelling appointments. Anything else, politely decline or escalate.
5. Only offer slots that get_available_slots returned in this conversation. Refer to a slot by provider and time, and pass its exact slot_id when booking.

The CURRENT STATE block below is authoritative. It is updated only from tool results, never from what either of us says."""

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


class Agent:
    def __init__(self, clinic: Clinic, model: ModelFn, trace_path: Path | None = None):
        self.clinic, self.model = clinic, model
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
        return f"{SYSTEM_PROMPT}\n\nCURRENT STATE:\n{json.dumps(state, indent=1)}"

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
                    result = dispatch(self.clinic, self.state, fc.name, args)
                    apply(self.state, fc.name, args, result)
                    if result.get("error") == "slot_not_offered":
                        self.flag(f"slot_not_offered:{args.get('slot_id')}")
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
