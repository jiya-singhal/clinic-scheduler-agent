"""python -m agent.chat            interactive
   python -m agent.chat --script scenarios/smoke.json   replay scripted patient turns"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from agent.backend import Clinic
from agent.loop import Agent, gemini_model


def load_env(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"'))


def build_agent(trace_path: Path | None = None, prompt: Path | None = None) -> Agent:
    load_env()
    from google import genai
    if not os.environ.get("GEMINI_API_KEY"):
        sys.exit("GEMINI_API_KEY missing. Copy .env.example to .env and fill it in.")
    return Agent(Clinic.from_seed(), gemini_model(genai.Client()), trace_path,
                 system_prompt=prompt.read_text().strip() if prompt else None)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Clinic scheduling agent")
    ap.add_argument("--script", type=Path, help="JSON file with {\"turns\": [...]} patient utterances")
    ap.add_argument("--trace", type=Path, help="trace path (default runs/<timestamp>.jsonl)")
    ap.add_argument("--prompt", type=Path, help="system prompt file (default prompts/v1.md)")
    args = ap.parse_args(argv)
    agent = build_agent(args.trace, args.prompt)
    print(f"[trace -> {agent.trace_path}]")
    if args.script:
        turns = json.loads(args.script.read_text())["turns"]
        for t in turns:
            print(f"\npatient> {t}")
            print(f"agent> {agent.turn(t)}")
        print(f"\n[flags: {agent.state.flags}]")
        return
    print("Type your message. Ctrl-D or 'quit' to exit.")
    while True:
        try:
            user = input("\npatient> ").strip()
        except EOFError:
            break
        if user in {"quit", "exit"}:
            break
        if user:
            print(f"agent> {agent.turn(user)}")


if __name__ == "__main__":
    main()
