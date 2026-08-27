#!/usr/bin/env python3
"""Stage 5 — subagents, streaming, and hooks.

Two more things a harness does that the model cannot do for itself.

SUBAGENTS. The `task` tool below spawns a whole nested harness: its own
message list, its own turn budget, a deliberately smaller tool surface. The
parent sees only the child's final answer. That is the entire point — it is
a CONTEXT boundary, not a parallelism trick. A search that takes 25 turns
and reads 40 files costs the parent one tool result. Compare that to stage 4,
where the same search would have put all 40 files in the parent's history
forever. The cost is real: the child cannot ask a clarifying question, and
everything it learned but did not write down is gone the moment it returns.
So subagents are for work whose *output* is much smaller than its *input*.

Two guards make this safe to run: a depth limit (a subagent that can spawn
subagents that can spawn subagents is a fork bomb with a credit card), and a
read-only tool surface for children (concurrent writers to one working
directory corrupt each other).

STREAMING. Non-streaming requests with a large max_tokens hit HTTP timeouts,
and the user stares at nothing for 90 seconds. Streaming is not a nicety at
agent scale; it is how you avoid both.

HOOKS. Deterministic code on the tool boundary: run the formatter after
every edit, block a call the permission rules missed, log to a metrics sink.
Anything you want to happen EVERY time belongs here, not in the prompt — a
prompt is a request, a hook is a guarantee.

Run:
    python3 stage5_subagents.py --prompt "..."
    python3 stage5_subagents.py --real --prompt "find every TODO and summarize"
"""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import subprocess
import sys
from typing import Callable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from stage2_tools import MODEL, REGISTRY, SYSTEM, Tool, _brief, execute  # noqa: E402
from stage3_permissions import AuditLog, Permissions  # noqa: E402
from stage4_context import Ledger, roll_cache_breakpoint, system_blocks  # noqa: E402

MAX_DEPTH = 1                       # parent = 0; children may not spawn
CHILD_TOOLS = ["read_file", "list_files", "search"]   # read-only surface

SUBAGENT_SYSTEM = """You are a subagent. You were given one specific question
by a parent agent and you have read-only tools.

You cannot ask for clarification and nobody will see your intermediate work.
Investigate, then answer in full: your final message is the ONLY thing that
reaches the parent, so it must stand alone. Include the file paths and line
numbers you relied on. Do not describe your process."""


# ----------------------------------------------------------------- hooks ----

PreHook = Callable[[str, dict], str | None]     # return a string to BLOCK
PostHook = Callable[[str, dict, str], None]

PRE_HOOKS: list[PreHook] = []
POST_HOOKS: list[PostHook] = []


def block_secrets(name: str, args: dict) -> str | None:
    """Example pre-hook: never read a credential file, whatever the rules say."""
    path = str(args.get("path", ""))
    if os.path.basename(path) in {".env", "id_rsa", "credentials", ".netrc"}:
        return f"Blocked by hook: {path} may contain credentials."
    return None


def format_after_edit(name: str, args: dict, result: str) -> None:
    """Example post-hook: a guarantee, not a request in the prompt."""
    if name not in ("edit_file", "write_file"):
        return
    path = str(args.get("path", ""))
    if path.endswith(".py") and os.path.isfile(path):
        subprocess.run(["python3", "-m", "black", "-q", path],
                       capture_output=True, check=False)


PRE_HOOKS.append(block_secrets)
POST_HOOKS.append(format_after_edit)


def hooked_execute(name: str, args: dict) -> tuple[str, bool]:
    for hook in PRE_HOOKS:
        blocked = hook(name, args)
        if blocked:
            return blocked, True
    content, is_error = execute(name, args)
    for hook in POST_HOOKS:
        hook(name, args, content)
    return content, is_error


# -------------------------------------------------------------- the model ----


def call_model(client, **kwargs):
    """Stream when we can, fall back when we can't (the mock has no .stream).

    `.get_final_message()` hands back exactly the same Message object the
    non-streaming path returns, so the rest of the loop is identical.
    """
    stream_fn = getattr(getattr(client, "messages", None), "stream", None)
    if stream_fn is None:
        return client.messages.create(**kwargs)
    with stream_fn(**kwargs) as stream:
        for event in stream.text_stream:
            print(event, end="", flush=True)
        print()
        return stream.get_final_message()


# --------------------------------------------------------------- the loop ----


def run_agent(client, perms, audit, prompt, *, depth=0, tool_names=None,
              system=None, max_turns=30, cache=True, quiet=False, ledger=None):
    """One agent. The parent and every subagent run this same function —
    the only differences are the tool surface, the system prompt, and depth."""
    names = tool_names or list(REGISTRY)
    specs = [REGISTRY[n].spec() for n in names if n in REGISTRY]
    if depth < MAX_DEPTH:
        specs.append(TASK_SPEC)

    messages = [{"role": "user", "content": [{"type": "text", "text": prompt}]}]
    ledger = ledger if ledger is not None else Ledger()
    pad = "  " * depth

    for _ in range(max_turns):
        roll_cache_breakpoint(messages, cache)
        response = (
            call_model if not quiet else _quiet_call
        )(client, model=MODEL, max_tokens=8000,
          system=system_blocks_for(system, cache), tools=specs, messages=messages)
        ledger.add(response.usage)

        if quiet:
            for block in response.content:
                if block.type == "text" and block.text.strip():
                    print(f"{pad}\033[2m{block.text.strip()[:160]}\033[0m")

        if response.stop_reason != "tool_use":
            return "\n".join(b.text for b in response.content if b.type == "text"), ledger

        messages.append({"role": "assistant", "content": response.content})
        calls = [b for b in response.content if b.type == "tool_use"]

        decisions = [
            (True, "subagent spawn") if c.name == "task" else perms.decide(c.name, c.input)
            for c in calls
        ]
        outcomes: list[tuple[str, bool]] = [("", False)] * len(calls)
        for i, (ok, reason) in enumerate(decisions):
            audit.write(event="decision", depth=depth, tool=calls[i].name,
                        allowed=ok, reason=reason)
            if not ok:
                outcomes[i] = (f"Permission denied: {reason}", True)

        def dispatch(i):
            call = calls[i]
            if call.name == "task":
                return run_task(client, perms, audit, call.input, depth, ledger)
            return hooked_execute(call.name, call.input)

        runnable = [i for i, (ok, _) in enumerate(decisions) if ok]
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = {i: pool.submit(dispatch, i) for i in runnable}
            for i, fut in futures.items():
                outcomes[i] = fut.result()

        results = []
        for call, (content, is_error) in zip(calls, outcomes):
            mark = "\033[31m✗\033[0m" if is_error else "\033[32m✓\033[0m"
            print(f"{pad}\033[2m  {mark} {call.name}({_brief(call.input)})\033[0m")
            results.append({"type": "tool_result", "tool_use_id": call.id,
                            "content": content, "is_error": is_error})
        messages.append({"role": "user", "content": results})

    return f"(stopped: {max_turns}-turn limit at depth {depth})", ledger


def _quiet_call(client, **kwargs):
    return client.messages.create(**kwargs)


def system_blocks_for(text, cache):
    if text is None:
        return system_blocks(cache)
    block = {"type": "text", "text": text}
    if cache:
        block["cache_control"] = {"type": "ephemeral"}
    return [block]


# -------------------------------------------------------------- the task ----

TASK_SPEC = {
    "name": "task",
    "description": (
        "Delegate a self-contained research question to a subagent with "
        "read-only tools. Use this when answering would take many file reads "
        "whose contents you don't need to keep — the subagent does the "
        "reading and returns only its conclusion.\n\n"
        "The subagent cannot ask you questions and shares none of your "
        "context, so `prompt` must be a complete, standalone brief: what to "
        "find, where to look, and what the answer should contain. Do not use "
        "this for edits or for work where you need the raw file contents."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "3-5 word label."},
            "prompt": {"type": "string", "description": "The full standalone brief."},
        },
        "required": ["description", "prompt"],
        "additionalProperties": False,
    },
}


def run_task(client, perms, audit, args, depth, ledger) -> tuple[str, bool]:
    # Enforce the depth limit HERE, not by omitting TASK_SPEC from the child's
    # tool list. Not advertising a tool is a hint; the model can still emit a
    # call for it, and this exact loop ran away in testing when the guard
    # lived only in the tool list. Advertise for guidance, enforce at the gate.
    if depth + 1 > MAX_DEPTH:
        return (
            f"Error: subagents may not spawn subagents (depth limit {MAX_DEPTH}). "
            "Do this work yourself with your own tools.",
            True,
        )

    label = args.get("description", "subagent")
    print(f"\033[36m{'  ' * depth}  ⤷ subagent[{label}]\033[0m")
    audit.write(event="subagent_start", depth=depth + 1, label=label)
    try:
        answer, _ = run_agent(
            client, perms, audit, args["prompt"],
            depth=depth + 1,
            tool_names=CHILD_TOOLS,
            system=SUBAGENT_SYSTEM,
            max_turns=15,
            quiet=True,          # children never stream to the terminal
            ledger=ledger,       # ...but their tokens are still your bill
        )
    except Exception as exc:  # noqa: BLE001
        return f"Subagent failed: {type(exc).__name__}: {exc}", True
    audit.write(event="subagent_end", depth=depth + 1, label=label, chars=len(answer))
    print(f"\033[36m{'  ' * depth}  ⤶ subagent[{label}] returned {len(answer)} chars\033[0m")
    return answer, False


# ------------------------------------------------------------------ main ----


def make_client(real: bool):
    if not real:
        from _mock import MockAnthropic

        return MockAnthropic(script=[("task", {"description": "count files",
                                               "prompt": "How many python files are here?"})])
    import anthropic

    return anthropic.Anthropic()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt")
    ap.add_argument("--real", action="store_true")
    ap.add_argument("--root", default=".")
    ap.add_argument("--yolo", action="store_true")
    ap.add_argument("--audit", default="audit.jsonl")
    ap.add_argument("--max-turns", type=int, default=30)
    args = ap.parse_args()

    if args.real and not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set.", file=sys.stderr)
        return 1

    perms = Permissions(args.root, yolo=args.yolo, interactive=sys.stdin.isatty())
    audit = AuditLog(args.audit)
    client = make_client(args.real)
    print(f"stage5 — depth<={MAX_DEPTH}, child tools: {', '.join(CHILD_TOOLS)}")

    def go(text):
        answer, ledger = run_agent(client, perms, audit, text,
                                   system=SYSTEM, max_turns=args.max_turns)
        print(f"\n\033[2m── {ledger.line()} (includes all subagents)\033[0m")
        return answer

    if args.prompt:
        print(go(args.prompt))
        return 0
    while True:
        try:
            line = input("\n\033[1m>\033[0m ").strip()
        except EOFError:
            return 0
        if line:
            go(line)


if __name__ == "__main__":
    raise SystemExit(main())
