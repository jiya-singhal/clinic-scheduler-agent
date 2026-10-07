# ASSUMPTIONS

Ambiguity calls. Each one is a proposal until the reviewer confirms.

- **A1 Calendar anchor.** DECIDED. Slots start Monday 2026-10-12 and cover 10 business
  days through Friday 2026-10-23. Fixed in the seed, not relative to today, so
  the same seed gives the same slots on any day the evals run.
- **A2 Clinic hours.** 09:00 to 12:00 and 13:00 to 17:00, Monday to Friday,
  30-minute slots. 14 slots per provider per day, 280 total. A handful are
  pre-taken in the seed so slot_taken can occur.
- **A3 Providers.** DECIDED. Two general practitioners, P1 Dr. Asha Patel and P2
  Dr. Marcus Lee. Both see any reason, so `reason` does not filter slots. It is
  still required so the trace shows what the patient asked for.
- **A4 Emergency.** DECIDED (Q1). Chest pain, trouble breathing, stroke symptoms, severe
  bleeding, loss of consciousness, suicidal thoughts, or the patient saying it
  is an emergency. The agent escalates with urgency high and tells the patient
  to call emergency services. It does not triage anything else.
- **A5 Cancellation.** DECIDED. Cancelling frees the slot immediately. Patients can only
  cancel their own appointments; anything else returns not_found.
- **A6 Identity.** Full name plus date of birth is sufficient verification for
  this mock. Name match is case-insensitive with whitespace collapsed; date of
  birth must match exactly as YYYY-MM-DD.
- **A7 One active appointment.** The state tracks one booked appointment per
  conversation. Booking a second slot in the same conversation is allowed by
  the backend but the state keeps only the latest id. Phase 2 may revisit.
- **A8 Patients.** Five fictional patients with names and dates of birth in
  `data/seed.json`. No real data.
