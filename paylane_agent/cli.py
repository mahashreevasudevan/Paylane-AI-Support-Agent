from __future__ import annotations
import argparse
import os
from datetime import datetime, timezone
from .backend import MockPaylane, SessionState
from .agent import SupportAgent, DEFAULT_MODEL
from .evaluation import run_all


def main():
    parser = argparse.ArgumentParser(description="Paylane mock support agent")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("chat", help="Start an interactive chat")
    ev = sub.add_parser("eval", help="Run 12 scenarios three times (live LLM calls)")
    ev.add_argument("--repeats", type=int, default=3)
    ev.add_argument("--model", default=os.getenv("PAYLANE_MODEL", DEFAULT_MODEL))
    ev.add_argument("--out", default="transcripts")
    args = parser.parse_args()

    if args.command == "eval":
        provider = os.getenv("PAYLANE_PROVIDER", "groq").strip().lower()
        key_name = "GROQ_API_KEY" if provider == "groq" else "OPENAI_API_KEY"
        if not os.getenv(key_name):
            raise SystemExit(f"Set {key_name} before running live evaluations. No fake transcripts are generated.")
        run_all(args.repeats, args.model, args.out)
        return

    backend = MockPaylane()
    session = SessionState(session_id="interactive")
    agent = SupportAgent(backend, session)
    print("Paylane mock support agent. Type 'quit' to exit.")
    while True:
        user = input("You: ").strip()
        if user.lower() in ("quit", "exit"):
            break
        print("Agent:", agent.reply(user))


if __name__ == "__main__":
    main()
