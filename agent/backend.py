"""In-memory clinic. All enforcement lives here, not in the prompt."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

SEED_PATH = Path(__file__).resolve().parent.parent / "data" / "seed.json"
MAX_SLOTS = 6


@dataclass
class Slot:
    slot_id: str
    provider_id: str
    start: datetime


@dataclass
class Clinic:
    today: date
    providers: dict[str, str]
    patients: list[dict]
    slots: dict[str, Slot]
    taken: dict[str, str] = field(default_factory=dict)        # slot_id -> appointment_id
    appointments: dict[str, dict] = field(default_factory=dict)  # appointment_id -> {patient_id, slot_id, reason}
    verified: set[str] = field(default_factory=set)
    escalated_high: bool = False  # set by escalate_to_human(urgency=high); booking is refused afterwards (D42)
    _seq: int = 0

    @classmethod
    def from_seed(cls, path: Path = SEED_PATH) -> "Clinic":
        seed = json.loads(Path(path).read_text())
        today = date.fromisoformat(seed["today"])
        providers = {p["id"]: p["name"] for p in seed["providers"]}
        slots: dict[str, Slot] = {}
        day, days_added = today, 0
        while days_added < seed["business_days"]:
            if day.weekday() < 5:
                for pid in providers:
                    for start_s, end_s in seed["hours"]:
                        t = datetime.combine(day, datetime.strptime(start_s, "%H:%M").time())
                        end = datetime.combine(day, datetime.strptime(end_s, "%H:%M").time())
                        while t < end:
                            sid = f"{pid}-{t:%Y%m%d-%H%M}"
                            slots[sid] = Slot(sid, pid, t)
                            t += timedelta(minutes=seed["slot_minutes"])
                days_added += 1
            day += timedelta(days=1)
        clinic = cls(today, providers, seed["patients"], slots)
        for sid in seed["pre_taken"]:
            clinic.taken[sid] = "SEED"
        return clinic

    def _next(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq:04d}"

    # tools --------------------------------------------------------------

    def verify_patient(self, full_name: str, dob: str) -> dict:
        name = " ".join(full_name.lower().split())
        for p in self.patients:
            if " ".join(p["full_name"].lower().split()) == name and p["dob"] == dob:
                self.verified.add(p["id"])
                return {"verified": True, "patient_id": p["id"]}
        return {"verified": False, "patient_id": None}

    def get_available_slots(self, provider_id: str | None, date_from: str, date_to: str, reason: str,
                            time_from: str | None = None, time_to: str | None = None) -> dict:
        lo, hi = date.fromisoformat(date_from), date.fromisoformat(date_to)
        t_lo = datetime.strptime(time_from, "%H:%M").time() if time_from else None
        t_hi = datetime.strptime(time_to, "%H:%M").time() if time_to else None
        found = sorted(
            (s for s in self.slots.values()
             if s.slot_id not in self.taken
             and (provider_id is None or s.provider_id == provider_id)
             and lo <= s.start.date() <= hi
             and (t_lo is None or s.start.time() >= t_lo)
             and (t_hi is None or s.start.time() < t_hi)),
            key=lambda s: (s.start, s.provider_id),
        )
        return {
            "slots": [{"slot_id": s.slot_id, "provider": self.providers[s.provider_id], "start": s.start.isoformat(timespec="minutes")}
                      for s in found[:MAX_SLOTS]],
            "truncated": len(found) > MAX_SLOTS,
        }

    def book_appointment(self, patient_id: str, slot_id: str, reason: str) -> dict:
        if self.escalated_high:
            return {"ok": False, "error": "escalated_session"}
        if patient_id not in self.verified:
            return {"ok": False, "error": "not_verified"}
        if slot_id not in self.slots:
            return {"ok": False, "error": "unknown_slot"}
        if slot_id in self.taken:
            appt = self.appointments.get(self.taken[slot_id])
            if appt and appt["patient_id"] == patient_id:
                return {"ok": True, "appointment_id": self.taken[slot_id]}  # idempotent
            return {"ok": False, "error": "slot_taken"}
        appt_id = self._next("A")
        self.taken[slot_id] = appt_id
        self.appointments[appt_id] = {"patient_id": patient_id, "slot_id": slot_id, "reason": reason}
        return {"ok": True, "appointment_id": appt_id}

    def list_appointments(self, patient_id: str) -> dict:
        if patient_id not in self.verified:
            return {"ok": False, "error": "not_verified"}
        own = [{"appointment_id": aid, "slot_id": a["slot_id"], "provider": self.providers[self.slots[a["slot_id"]].provider_id],
                "start": self.slots[a["slot_id"]].start.isoformat(timespec="minutes"), "reason": a["reason"]}
               for aid, a in self.appointments.items() if a["patient_id"] == patient_id]
        return {"ok": True, "appointments": sorted(own, key=lambda a: a["start"])}

    def cancel_appointment(self, patient_id: str, appointment_id: str) -> dict:
        if patient_id not in self.verified:
            return {"ok": False, "error": "not_verified"}
        appt = self.appointments.get(appointment_id)
        if not appt or appt["patient_id"] != patient_id:
            return {"ok": False, "error": "not_found"}
        del self.taken[appt["slot_id"]]
        del self.appointments[appointment_id]
        return {"ok": True}

    def escalate_to_human(self, reason: str, urgency: str) -> dict:
        if urgency == "high":
            self.escalated_high = True
        return {"ok": True, "ticket_id": self._next("T")}
