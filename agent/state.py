"""Typed conversation state. Mutates only inside apply(), which runs only after a tool executes."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field


@dataclass
class ConversationState:
    verified: bool = False
    patient_id: str | None = None
    intent: str | None = None
    reason: str | None = None
    preferences: dict = field(default_factory=dict)
    offered_slots: list[str] = field(default_factory=list)
    selected_slot: str | None = None
    booked_appointment_id: str | None = None
    escalated: bool = False
    turn_count: int = 0
    flags: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


def apply(state: ConversationState, tool: str, args: dict, result: dict) -> None:
    """The only place state changes. May read the call's args and its result."""
    if tool == "verify_patient" and result.get("verified"):
        state.verified, state.patient_id = True, result["patient_id"]
    elif tool == "get_available_slots":
        state.intent = "book"
        state.reason = args.get("reason")
        state.preferences = {k: args.get(k) for k in ("provider_id", "date_from", "date_to")}
        for s in result.get("slots", []):
            if s["slot_id"] not in state.offered_slots:
                state.offered_slots.append(s["slot_id"])
    elif tool == "book_appointment" and result.get("ok"):
        state.intent = "book"
        state.selected_slot = args.get("slot_id")
        state.booked_appointment_id = result["appointment_id"]
    elif tool == "cancel_appointment" and result.get("ok"):
        state.intent = "cancel"
        state.booked_appointment_id = None
        state.selected_slot = None
    elif tool == "escalate_to_human" and result.get("ok"):
        state.intent = "escalate"
        state.escalated = True
