# Screen recording order

Target length 5 to 7 minutes. Terminal on the left, editor on the right.

1. **Live chat** (about 90 seconds). `uv run python -m agent.chat --prompt prompts/v2.md`. Say: "Hi, I'd like to book an appointment." Give "Priya Raman, 21 July 1990". Ask for "the earliest slot next week, any doctor". Pick one. Show the read-back, say yes, show the booking confirmation. Point at the trace path printed at the top.
2. **Baseline eval** (about 60 seconds, mostly pre-run). Open `reports/v1.md`. Point at the confirm_before_book failures and the evidence lines at the bottom. If running live, use `--only wrong-dob-then-correct` to keep it short.
3. **The loop** (about 2 minutes). `uv run python -m evals.run --loop --start prompts/v1.md --max-iterations 3 --auto-apply`. Run `uv run python -m evals.run --prompt prompts/v1.md --out reports/v1.json` once before recording so v1 traces exist and the loop reuses them. On camera the loop then prints the proposed rule, rationale and risk, writes `prompts/v3.md` (v2 is the committed recorded run), and re-evaluates for about 10 minutes. Narrate the gate while it runs. Cut the wait.
4. **Evidence** (about 60 seconds). Open `history/loop.md`: target check, rule, before/after table, gate verdict. Then `diff prompts/v1.md prompts/v2.md` to show the append-only rule with its provenance comment.
5. **Design note** (20 seconds). Open `DESIGN_NOTE.md`, scroll once, stop on section 4.

## If it goes wrong on camera

- Gemini 503 or the loop stalls: open `history/loop.md` and `reports/v2.md` from the committed accepted run and narrate from those.
- The gate rejects live (judge drift): open `history/attempt-3/loop.md` and explain that this is exactly what the gate is for, then show the accepted `history/loop.md`.
