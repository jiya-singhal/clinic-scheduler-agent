"""Agreement between Jiya's filled human_sheet.md and every judge configuration in results.json, with Cohen's kappa.

uv run python evals/judge_audit/score_human.py [--sheet evals/judge_audit/human_sheet.md]

Reads the `H01: X|Y|tie` lines at the bottom of the sheet, maps X/Y back to sides through human_sheet_key.json,
and compares with results.json "judge_verdicts" (each judge and rubric, in order AB, order BA, and the both-orders rule)."""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
ANSWER_RE = re.compile(r"^(H\d+):[ \t]*(X|Y|tie)\b", re.I | re.M)  # tolerates a note after the verdict, e.g. "H03: X (close)"


def kappa(a: list[str], b: list[str]) -> float | None:
    """Cohen's kappa for two raters over the same items; None when chance agreement is 1 (both used one label throughout)."""
    n = len(a)
    po = sum(x == y for x, y in zip(a, b)) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum(ca[k] * cb[k] for k in ca) / (n * n)
    return None if pe == 1 else (po - pe) / (1 - pe)


def human_verdicts(sheet_text: str, key: dict) -> dict[str, str]:
    """{pair_id: side label or tie} from the filled sheet."""
    return {key[h]["pair_id"]: "tie" if v.lower() == "tie" else key[h][v.upper()] for h, v in ANSWER_RE.findall(sheet_text) if h in key}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet", type=Path, default=HERE / "human_sheet.md")
    ap.add_argument("--key", type=Path, default=HERE / "human_sheet_key.json")
    ap.add_argument("--results", type=Path, default=HERE / "results.json")
    args = ap.parse_args(argv)
    key = json.loads(args.key.read_text())
    human = human_verdicts(args.sheet.read_text(), key)
    print(f"{len(human)} of {len(key)} pairs answered")
    if not human:
        return
    configs = json.loads(args.results.read_text()).get("judge_verdicts", {}) if args.results.exists() else {}
    if not configs:
        print(f"no judge verdicts in {args.results} yet; run python -m evals.judge_audit, then score again")
        return
    print("| judge configuration | pair set | agreement | Cohen's kappa |\n|---|---|---|---|")
    for config, verdicts in configs.items():
        for pair_set in sorted({p.split(":")[0] for p in human} | {"all"}):
            ids = [p for p in human if p in verdicts and pair_set in ("all", p.split(":")[0])]
            if ids:
                h, j = [human[p] for p in ids], [verdicts[p] for p in ids]
                k = kappa(h, j)
                print(f"| {config} | {pair_set} | {sum(x == y for x, y in zip(h, j))}/{len(ids)} | {'n/a' if k is None else f'{k:.2f}'} |")


if __name__ == "__main__":
    main()
