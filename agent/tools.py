"""Tool schemas and dispatch. Dispatch adds the one control the backend cannot know: offered_slots."""
from __future__ import annotations

from agent.backend import Clinic
from agent.state import ConversationState

TOOL_SCHEMAS = [
    {
        "name": "verify_patient",
        "description": "Verify a patient's identity by full name and date of birth. Must succeed before any booking or cancellation.",
        "parameters": {"type": "object", "properties": {
            "full_name": {"type": "string"},
            "dob": {"type": "string", "description": "Date of birth, YYYY-MM-DD"}},
            "required": ["full_name", "dob"]},
    },
    {
        "name": "get_available_slots",
        "description": "List open 30-minute appointment slots. Returns the earliest matches only.",
        "parameters": {"type": "object", "properties": {
            "provider_id": {"type": "string", "nullable": True, "description": "P1 (Dr. Asha Patel) or P2 (Dr. Marcus Lee); null for any"},
            "date_from": {"type": "string", "description": "YYYY-MM-DD inclusive"},
            "date_to": {"type": "string", "description": "YYYY-MM-DD inclusive"},
            "reason": {"type": "string", "description": "Patient's reason for the visit"}},
            "required": ["date_from", "date_to", "reason"]},
    },
    {
        "name": "book_appointment",
        "description": "Book a slot for a verified patient. slot_id must be one returned by get_available_slots.",
        "parameters": {"type": "object", "properties": {
            "patient_id": {"type": "string"},
            "slot_id": {"type": "string"},
            "reason": {"type": "string"}},
            "required": ["patient_id", "slot_id", "reason"]},
    },
    {
        "name": "cancel_appointment",
        "description": "Cancel one of the verified patient's own appointments.",
        "parameters": {"type": "object", "properties": {
            "patient_id": {"type": "string"},
            "appointment_id": {"type": "string"}},
            "required": ["patient_id", "appointment_id"]},
    },
    {
        "name": "escalate_to_human",
        "description": "Hand off to clinic staff. Use urgency high for emergencies or anything out of scope that cannot wait.",
        "parameters": {"type": "object", "properties": {
            "reason": {"type": "string"},
            "urgency": {"type": "string", "enum": ["low", "medium", "high"]}},
            "required": ["reason", "urgency"]},
    },
]
TOOL_NAMES = {t["name"] for t in TOOL_SCHEMAS}


def dispatch(clinic: Clinic, state: ConversationState, name: str, args: dict) -> dict:
    if name not in TOOL_NAMES:
        return {"ok": False, "error": "unknown_tool"}
    if name == "book_appointment" and args.get("slot_id") not in state.offered_slots:
        return {"ok": False, "error": "slot_not_offered"}
    if name == "get_available_slots":
        args = {"provider_id": None, **args}
    try:
        return getattr(clinic, name)(**args)
    except (TypeError, ValueError) as e:  # bad or missing arguments from the model
        return {"ok": False, "error": "bad_arguments", "detail": str(e)}
