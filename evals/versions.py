"""Prompt versions: v<n+1> = v<n> + one learned rule with provenance (D32). history/versions.json tracks lineage."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from pathlib import Path

HISTORY = Path("history/versions.json")
RULES_HEADER = "## Learned rules"


def sha(text: str) -> str:
    return hashlib.sha256(text.strip().encode()).hexdigest()


def version_number(path: Path) -> int:
    m = re.fullmatch(r"v(\d+)", path.stem)
    if not m:
        raise ValueError(f"prompt file must be named v<n>.md, got {path.name}")
    return int(m.group(1))


def load_history(path: Path = HISTORY) -> list[dict]:
    return json.loads(path.read_text()) if path.exists() else []


def save_history(entries: list[dict], path: Path = HISTORY) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=1) + "\n")


def next_rule_id(parent_text: str) -> str:
    n = max([int(m) for m in re.findall(r"<!-- R-(\d+) \|", parent_text)] + [0]) + 1
    return f"R-{n}"


def apply_rule(parent_path: Path, rule: dict, today: date | None = None) -> tuple[Path, dict]:
    """Writes prompts/v<n+1>.md and returns (path, rule with rule_id assigned). Never edits existing text."""
    parent_text = parent_path.read_text().strip()
    rule = {**rule, "rule_id": next_rule_id(parent_text)}
    n = version_number(parent_path) + 1
    while parent_path.with_name(f"v{n}.md").exists():  # versions are never overwritten; a clone already has the recorded v2
        n += 1
    new_path = parent_path.with_name(f"v{n}.md")
    stamp = (f"<!-- {rule['rule_id']} | target: {rule['target_check']} | sources: {', '.join(rule['source_scenarios'])} | "
             f"{(today or date.today()).isoformat()} | parent sha256 {sha(parent_text)[:12]} -->")
    body = parent_text if RULES_HEADER in parent_text else f"{parent_text}\n\n{RULES_HEADER}"
    new_path.write_text(f"{body}\n\n{stamp}\n{rule['rule_text'].strip()}\n")
    return new_path, rule


def record(entry: dict, path: Path = HISTORY) -> None:
    h = load_history(path)
    h = [e for e in h if e["version"] != entry["version"]] + [entry]
    save_history(h, path)
