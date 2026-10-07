"""Gemini plays the patient for mode=simulated scenarios. Fixed prompt, temperature 0, facts limited to the brief."""
from __future__ import annotations

import json

from google.genai import types

SIM_PROMPT = """You are role-playing a PATIENT phoning a clinic's appointment scheduling assistant. You speak first.

Your brief:
{brief}

Rules:
- Say only what the patient would say, one utterance per turn, plain speech, no stage directions, no quotes.
- Never invent facts that are not in the brief. If asked for something the brief does not give you, say you don't know or don't have it.
- Follow the behaviours in the brief faithfully, even when they make the call harder.
- Do not help the assistant; do not correct it; do not break character.
- When the brief's goal is achieved, or you have decided to give up, reply with exactly: DONE"""

DONE = "DONE"


def gemini_patient(client, brief: dict, model: str):
    """Returns next_line(assistant_text | None) -> patient utterance. Keeps its own history."""
    history: list[types.Content] = []
    config = types.GenerateContentConfig(system_instruction=SIM_PROMPT.format(brief=json.dumps(brief, indent=1)), temperature=0,
                                         automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))

    def next_line(assistant_text: str | None) -> str:
        history.append(types.Content(role="user", parts=[types.Part(text=assistant_text or "(The call connects. The assistant is listening.)")]))
        resp = client.models.generate_content(model=model, contents=history, config=config)
        content = resp.candidates[0].content
        history.append(content)
        text = "".join(p.text for p in content.parts or [] if p.text).strip()
        return DONE if text.strip().strip(".").upper() == DONE else text

    return next_line
