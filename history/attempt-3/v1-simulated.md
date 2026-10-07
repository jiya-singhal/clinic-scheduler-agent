# Eval report v1-simulated

prompt `prompts/v1.md` sha256 `ade94fa896f6` | agent `gemini-3.8-flash` | judge `claude-opus-5-5` | simulator `gemini-3.8-flash` | 2026-10-07T15:11:50

**Mean score None** over 0 scripted scenarios, **0 pass** at >= 0.8.

| scenario | mode | score | gates failed | failed checks | lowest judge criterion |
|---|---|---|---|---|---|
| impatient-interrupter-run1 | simulated | 0.99 |  |  | tone_appropriate_for_patients 0.9: The assistant stayed warm and calm ("Thanks, Priya"), but its second request repeated the ask for the date of birth, which irritated an impatient patient ("Stop asking me twice"). |
| impatient-interrupter-run2 | simulated | 0.99 |  |  | tone_appropriate_for_patients 0.9: The assistant stays calm and polite when the patient complains ("Thank you for verifying, Priya"), though it does not briefly acknowledge the complaint about being asked twice. |
| rambling-patient-run1 | simulated | 0.90 |  | confirm_before_book | clarity 0.7: Replies were short, but "9:00 AM and 9:30 AM with either Dr. Patel or Dr. Lee" left the doctor-time pairing unclear, and the doctor was assigned without a read-back (confirm_before_book failed). |
| rambling-patient-run2 | simulated | 0.92 |  | confirm_before_book | tone_appropriate_for_patients 0.9: The assistant stayed warm and patient through the rambling ("Thanks, Daniel"), though it never acknowledged the small talk. |

## Failures by check (scripted only)


## Simulated scenarios (informative, not gates)

- impatient-interrupter-run1: 0.99, failed nothing
- impatient-interrupter-run2: 0.99, failed nothing
- rambling-patient-run1: 0.90, failed ['confirm_before_book']
- rambling-patient-run2: 0.92, failed ['confirm_before_book']

## Evidence for failed checks

- `rambling-patient-run1` **confirm_before_book**: turn 4 P1-20261019-0900: 2 times listed, not a single read-back; prev='Thank you, Daniel. On Monday, October 19th, we have openings at 9:00 AM and 9:30 AM with either Dr. '
- `rambling-patient-run2` **confirm_before_book**: turn 4 P1-20261019-0900: patient turn not affirmative; prev='Thank you, Daniel. I have openings on Monday, October 19th at 9:00 AM with Dr. Patel or Dr. Lee. Wou'
