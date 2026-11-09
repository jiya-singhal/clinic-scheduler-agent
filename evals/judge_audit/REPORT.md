# Judge audit

Generated 2026-11-09T18:40:58 by `python -m evals.judge_audit`. Every number below is computed from `results.json` in this folder.

Judges: Opus `claude-opus-5-5` (production judge, cross-family, effort low). Second answer model for the self-preference test: `claude-haiku-4-5` at temperature 0 with `prompts/v2.md`. Method after Zheng et al. 2023.

The Gemini judge has no complete block in this run, so the same-family position numbers and the self-preference gap are pending.

| model | calls | input tokens | output tokens |
|---|---|---|---|
| claude-opus-5-5 | 144 | 294954 | 39149 |
| gemini-3.5-flash | 18 | 15708 | 7833 |

## Headline

- Judge agreed with me on X/N: pending human sheet (`human_sheet.md`, scored by `score_human.py`).
- opus: swapping answer order changed 3/12 verdicts on v1 vs v2; judging in both orders and counting a win only when it holds both ways leaves 5/12 decisive verdicts.
- single-transcript judge, baseline rubric: the padded copy scored higher on 0/12 transcripts (equal 0, lower 12).
- single-transcript judge, fixed rubric: the padded copy scored higher on 0/12 transcripts (equal 0, lower 12).

## Position bias

A verdict changed by order means the side the judge picked was different when the same two transcripts were swapped. A full reversal is one side winning in one order and the other side in the other. Shown-first won both means the judge picked whichever transcript was labelled A in both orders.

| pair set | judge | rubric | changed by order | full reversals | shown-first won both | shown-second won both | decisive, single order | decisive, both-orders rule |
|---|---|---|---|---|---|---|---|---|
| v1v2 | opus | baseline | 3/12 | 0 | 0 | 0 | 13/24 | 5/12 |
| family | opus | baseline | 4/12 | 1 | 0 | 1 | 21/24 | 8/12 |
| length | opus | baseline | 0/12 | 0 | 0 | 0 | 24/24 | 12/12 |
| length | opus | fixed | 0/12 | 0 | 0 | 0 | 24/24 | 12/12 |

Fix (i), the both-orders rule, cannot flip with order by construction, so after the fix the changed-by-order count is 0 for every row. Its cost is the last column: verdicts that were decisive in one order become ties.

## Length bias

Padding appends this to every assistant line, with the patient's previous line filled in: "Just to summarise what we discussed so far: you said "{said}". I want to make sure I have understood your request correctly and that nothing has been missed, so please let me know if any of that is not right.". Facts are unchanged; only length grows.

Single-transcript judge (`evals/judge.py` prompt, opus `claude-opus-5-5`, same layer 1 results for both copies):

| rubric | padded scored higher | equal | padded lower | mean original | mean padded | clarity delta | resolves_request delta |
|---|---|---|---|---|---|---|---|
| baseline | 0/12 | 0 | 12 | 0.958 | 0.794 | -0.575 | -0.029 |
| fixed | 0/12 | 0 | 12 | 0.953 | 0.79 | -0.596 | -0.046 |

Pairwise judge, original vs padded:

| judge | rubric | padded wins, original shown first | padded wins, padded shown first | both-orders rule: original / padded / tie |
|---|---|---|---|---|
| opus | baseline | 0/12 | 0/12 | 12 / 0 / 0 |
| opus | fixed | 0/12 | 0/12 | 12 / 0 / 0 |

## Self-preference

Both judges compare the Gemini agent and the Claude agent on the same 12 scripted scenarios with the same prompt, in both orders. Raw pick rates mix quality with bias, so the comparison that isolates self-preference is the gap between the two judges on the same pairs.

| judge | single order: gemini / claude / tie | both-orders rule: gemini / claude / tie | pairs |
|---|---|---|---|
| opus | 17 / 4 / 3 | 7 / 1 / 4 | 12 |

Layer 1 checks on the same transcripts, for context (quality the judges cannot see):

| agent run | scenarios | all checks pass | mean check pass rate |
|---|---|---|---|
| gemini-v1 | 12 | 7 | 0.942 |
| gemini-v2 | 12 | 12 | 1.0 |
| claude-v2 | 12 | 10 | 0.983 |

## v1 vs v2 as a validity check

v2 adds the read-back rule that layer 1 rewards. A judge that prefers v1 here is not measuring what the loop optimised.

| judge | single order: v2 / v1 / tie | both-orders rule: v2 / v1 / tie | pairs |
|---|---|---|---|
| opus | 10 / 3 / 11 | 5 / 0 / 7 | 12 |

## Two verdicts that flipped with order

**family:no-slots-in-range**, opus judge, baseline rubric: picked `claude` in one order and `gemini` in the other.

- Order AB, picked B = `claude`: "The two transcripts are nearly identical. Both verify identity, report honestly that the weekend is full, offer Monday, confirm the booking with Dr. Patel at 9:00 AM and complete it. Transcript A also offers Friday the 16th as an extra alternative. Transcript B states upfront that it is verifying identity, and it gives an appointment ID at the end, which is a concrete, useful confirmation. Overall the gap is small, and B is slightly better."
- Order BA, picked B = `gemini`: "Both handle verification, the empty weekend, the Monday 9:00 AM lookup and the confirmation before booking with Dr. Patel, with no medical advice or PHI issues. B is slightly better on the voice channel. It proactively offers concrete alternatives (Friday the 16th, Monday the 19th) instead of A's vaguer 'early next week'. It uses the patient's name warmly. A's one advantage is giving an appointment ID. Overall B edges ahead on clarity and helpfulness."

**v1v2:happy-path-book**, opus judge, baseline rubric: picked `tie` in one order and `v1` in the other.

- Order AB, picked tie = `tie`: "Both book the same slot with no medical advice and no PHI leak. A interprets 'the first one' as Dr. Patel and books right away, since the patient explicitly said 'Go ahead and book it.' B adds a read-back confirmation even though the patient already authorized the booking. On voice, a read-back can help catch mishearings. Here, though, it is redundant and adds a turn. The deciding point is shared: both offered 'Monday 9:00 AM with Dr. Patel or Dr. Lee' as if it were one slot. That makes 'the first one' mildly ambiguous in both, and both resolved it the same way. Neither transcript is clearly better overall."
- Order BA, picked B = `v1`: "Both book the same slot with Dr. Patel. Neither gives medical advice, leaks information, or hits an error. B asked "Which doctor would you prefer?", so the patient's "the first one please" clearly means Dr. Patel. B then books right away, as the patient explicitly said "go ahead and book it." A's question ("Would one of those work for you?") made "first one" slightly ambiguous, and A then added a confirmation turn the patient had already pre-empted. That extra step is a reasonable safeguard but makes A a little less efficient. B is slightly better: clearer question, fewer turns, and the same correct outcome."

## Fixes, before and after

| fix | targets | before | after |
|---|---|---|---|
| (i) both-orders rule, opus | position | 3/12 verdicts changed by order | 0 by construction, 5/12 still decisive |
| (ii) verbosity rubric line, single judge | length | padded higher 0/12 | padded higher 0/12 |
| (ii) verbosity rubric line, pairwise opus | length | padded wins 0/24 single-order, 0/12 both orders | padded wins 0/24, 0/12 |

## Human agreement

Pending. `human_sheet.md` holds H01 to H12 (v1 vs v2) and H13 onward (Gemini vs Claude), each batch shuffled, X and Y random per pair. Fill the verdict list and run `uv run python evals/judge_audit/score_human.py` for agreement and Cohen's kappa per judge configuration.

## Limits

- Twelve pairs per set. One flip is about 8 points; read the counts, not the percentages.
- Each verdict is one call. Opus at effort low takes no temperature and Gemini at temperature 0 is not deterministic, so a rerun can move a count by one or two.
- The pairwise judge sees transcripts only, not the layer 1 checks the production judge sees. That is deliberate for a bias test and makes it a harder task than production grading.
- v2 transcripts are longer than v1 because of the read-back turn, so v1 vs v2 mixes a quality difference with a length difference.
- Padding is synthetic and the filler is obviously redundant. A judge that penalises it is not fooled by blatant filler; that says nothing about subtler verbosity from a real agent.
- Self-preference is confounded with quality: the two agents are different models. The judge gap on the same pairs is the comparison; it still cannot separate bias from a real family-level taste for a style.
- Blinding is imperfect for a reader who knows the project: the read-back turn identifies v2.
