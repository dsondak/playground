#!/usr/bin/env python3
"""Stage 1 — the whole idea of an agentic harness, in one loop.

A harness is not the model. The model only maps (conversation) -> (next
message). Everything that makes an "agent" feel agentic lives out here:

    1. Give the model a description of some tools.
    2. Send the conversation.
    3. If the model asked for a tool, RUN IT and append the result.
    4. Go to 2. Stop when the model stops asking.

That's it. Stage 1 is that loop with exactly one tool (bash) and no safety,
no context management, no concurrency. Read it top to bottom, then diff it
against stage2 to see what each later concern actually costs in code.

Run:
    python3 stage1_loop.py --prompt "how many python files are here?"
    python3 stage1_loop.py --real --prompt "..."      # needs ANTHROPIC_API_KEY
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

MODEL = "claude-opus-5"

SYSTEM = """You are a small coding agent running on a Linux machine.
You have one tool: bash. Use it to inspect and change files.
Work in the current directory. Be concise."""

# A tool definition is just JSON the model reads. The `description` and the
# schema ARE the prompt — this text is the single biggest lever you have on
# whether the model uses the tool well.
TOOLS = [
    {
        "name": "bash",
        "description": (
            "Run a bash command and return its combined stdout and stderr. "
            "Commands run in the current working directory and time out after "
            "30 seconds. There is no interactive input."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The bash command to run."}
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    }
]


def run_bash(command: str) -> str:
    """The entire 'tool execution' layer of stage 1. Note: no safety at all."""
    try:
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return "Error: command timed out after 30s"
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        out += f"\n(exit code {proc.returncode})"
    return out.strip() or "(no output)"


def make_client(real: bool):
    if not real:
        from _mock import MockAnthropic

        return MockAnthropic()
    import anthropic

    return anthropic.Anthropic()


def agent(client, user_prompt: str, max_turns: int = 20) -> str:
    """The agentic loop. Everything below is the harness."""
    messages = [{"role": "user", "content": user_prompt}]

    for turn in range(1, max_turns + 1):
        response = client.messages.create(
            model=MODEL,
            max_tokens=8000,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
        )

        # Show the model's visible reasoning/answer for this turn.
        for block in response.content:
            if block.type == "text" and block.text.strip():
                print(f"\n\033[1massistant\033[0m  {block.text.strip()}")

        if response.stop_reason != "tool_use":
            # No tool requested -> the model considers itself finished.
            return "\n".join(b.text for b in response.content if b.type == "text")

        # CRITICAL: append the assistant turn *whole*, including tool_use
        # blocks. The API rejects a tool_result whose tool_use_id it never saw.
        messages.append({"role": "assistant", "content": response.content})

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            print(f"\033[2m  → bash: {block.input.get('command')}\033[0m")
            output = run_bash(block.input["command"])
            print(f"\033[2m  ← {output[:300]}\033[0m")
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,  # must echo the id back
                    "content": output,
                }
            )

        # All results from one assistant turn go back in ONE user message.
        # Splitting them teaches the model to stop batching tool calls.
        messages.append({"role": "user", "content": results})

    return f"(stopped: hit the {max_turns}-turn limit)"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", help="one-shot prompt; omit for a REPL")
    ap.add_argument("--real", action="store_true", help="use the real API")
    ap.add_argument("--max-turns", type=int, default=20)
    args = ap.parse_args()

    if args.real and not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set.", file=sys.stderr)
        return 1

    client = make_client(args.real)

    if args.prompt:
        print(agent(client, args.prompt, args.max_turns))
        return 0

    print("stage1 harness — ctrl-d to exit")
    while True:
        try:
            line = input("\n\033[1m>\033[0m ").strip()
        except EOFError:
            return 0
        if line:
            agent(client, line, args.max_turns)


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    raise SystemExit(main())
