#!/usr/bin/env python3
"""Stage 3 — permissions, sandboxing, and the audit trail.

The tool surface stops changing here: stage 3 imports stage 2's REGISTRY
unchanged. What changes is what *wraps* it. That is the real lesson — in a
production harness the tools are the easy part; the gate in front of them is
where the design lives.

Three things get added:

  1. A workspace root. Every path argument is resolved and must land inside
     it. The model is not asked to respect a boundary; it is prevented from
     crossing one.

  2. A permission engine. Rules are (tool, matcher) -> allow | deny | ask,
     evaluated most-specific-first, with session-scoped "always allow"
     answers. A denied call is NOT an exception: it comes back as an
     is_error tool_result, so the model learns and reroutes.

  3. An append-only JSONL audit log of every call and every decision.

The honest caveat, stated in code below: argument-level checks cannot
sandbox `bash`. `bash` takes an opaque string; `cat ../../etc/passwd` is one
of infinite spellings. Argument inspection is a UX affordance for the common
case, not a security boundary. A real boundary is a container, a seccomp
profile, or a separate uid — see notes/ for where that line sits.

Run:
    python3 stage3_permissions.py --prompt "read the README"
    python3 stage3_permissions.py --yolo --prompt "..."     # approve everything
    python3 stage3_permissions.py --real --prompt "..."
"""

from __future__ import annotations

import argparse
import concurrent.futures
import fnmatch
import json
import os
import shlex
import sys
import time
from dataclasses import dataclass
from typing import Callable, Literal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from stage2_tools import MODEL, REGISTRY, SYSTEM, _brief, execute  # noqa: E402

Decision = Literal["allow", "deny", "ask"]

# Path-bearing arguments, per tool. A harness has to know which strings are
# paths; there is no way to infer it from a JSON schema alone.
PATH_ARGS = {
    "read_file": ["path"],
    "write_file": ["path"],
    "edit_file": ["path"],
    "list_files": ["path"],
    "search": ["path"],
}

# bash commands whose *first word* is read-only enough to auto-approve.
SAFE_COMMANDS = {
    "ls", "cat", "head", "tail", "wc", "pwd", "echo", "grep", "find",
    "file", "stat", "diff", "which", "date", "env", "tree", "sort", "uniq",
}


@dataclass
class Rule:
    tool: str                  # tool name, or "*"
    decision: Decision
    match: Callable[[dict], bool] | None = None
    reason: str = ""

    def applies(self, name: str, args: dict) -> bool:
        if self.tool not in ("*", name):
            return False
        return self.match is None or self.match(args)


def _bash_head(args: dict) -> str:
    try:
        parts = shlex.split(args.get("command", ""))
    except ValueError:
        return ""
    return parts[0] if parts else ""


def _bash_is_plain(args: dict) -> bool:
    """True only for a single command with no shell metacharacters.

    Anything with a pipe, redirect, &&, ;, or backtick falls through to
    `ask` — not because those are inherently dangerous, but because this
    function stops being able to reason about them.
    """
    cmd = args.get("command", "")
    if any(ch in cmd for ch in "|;&><`$\n"):
        return False
    return _bash_head(args) in SAFE_COMMANDS


# Evaluated in order; first match wins. Put denies first.
DEFAULT_RULES = [
    Rule("bash", "deny",
         lambda a: any(bad in a.get("command", "") for bad in ("rm -rf /", ":(){", "mkfs", "dd if=")),
         "destructive command pattern"),
    Rule("read_file", "allow"),
    Rule("list_files", "allow"),
    Rule("search", "allow"),
    Rule("bash", "allow", _bash_is_plain, "read-only shell command"),
    Rule("*", "ask"),
]


class Permissions:
    def __init__(self, root: str, rules=None, yolo: bool = False, interactive: bool = True):
        self.root = os.path.realpath(root)
        self.rules = list(rules or DEFAULT_RULES)
        self.yolo = yolo
        self.interactive = interactive
        self.session_allows: set[str] = set()   # tool names blanket-approved this run

    # -- sandbox ------------------------------------------------------------

    def check_paths(self, name: str, args: dict) -> str | None:
        """Return a refusal string if any path argument escapes the root."""
        for key in PATH_ARGS.get(name, []):
            raw = args.get(key)
            if not raw:
                continue
            resolved = os.path.realpath(os.path.join(self.root, str(raw)))
            if resolved != self.root and not resolved.startswith(self.root + os.sep):
                return (
                    f"Path {raw!r} resolves outside the workspace root "
                    f"({self.root}). Stay inside the project directory."
                )
        return None

    # -- decision -----------------------------------------------------------

    def decide(self, name: str, args: dict) -> tuple[bool, str]:
        """Returns (allowed, reason)."""
        escape = self.check_paths(name, args)
        if escape:
            return False, escape            # sandbox outranks every rule

        if self.yolo:
            return True, "yolo"
        if name in self.session_allows:
            return True, "approved earlier this session"

        for rule in self.rules:
            if not rule.applies(name, args):
                continue
            if rule.decision == "allow":
                return True, rule.reason or "allowed by rule"
            if rule.decision == "deny":
                return False, rule.reason or "denied by rule"
            return self._ask(name, args)
        return self._ask(name, args)

    def _ask(self, name: str, args: dict) -> tuple[bool, str]:
        if not self.interactive:
            return False, "requires approval, but this run is non-interactive"
        print(f"\n\033[33m  permission\033[0m {name}({_brief(args)})")
        try:
            answer = input("  [y]es / [n]o / [a]lways for this tool: ").strip().lower()
        except EOFError:
            answer = "n"
        if answer.startswith("a"):
            self.session_allows.add(name)
            return True, "user approved (always)"
        if answer.startswith("y"):
            return True, "user approved"
        return False, "user declined this call"


class AuditLog:
    """Append-only record. If you can't reconstruct what an agent did, you
    don't have an agent, you have a rumor."""

    def __init__(self, path: str):
        self.path = path

    def write(self, **fields):
        fields["ts"] = time.time()
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(fields, default=str) + "\n")


def make_client(real: bool):
    if not real:
        from _mock import MockAnthropic

        return MockAnthropic()
    import anthropic

    return anthropic.Anthropic()


def agent(client, perms: Permissions, audit: AuditLog, user_prompt: str, max_turns: int = 30) -> str:
    messages = [{"role": "user", "content": user_prompt}]
    specs = [t.spec() for t in REGISTRY.values()]
    audit.write(event="prompt", text=user_prompt)

    for _ in range(max_turns):
        response = client.messages.create(
            model=MODEL, max_tokens=8000, system=SYSTEM, tools=specs, messages=messages
        )
        for block in response.content:
            if block.type == "text" and block.text.strip():
                print(f"\n\033[1massistant\033[0m  {block.text.strip()}")

        if response.stop_reason != "tool_use":
            return "\n".join(b.text for b in response.content if b.type == "text")

        messages.append({"role": "assistant", "content": response.content})
        calls = [b for b in response.content if b.type == "tool_use"]

        # Prompts must be serial (one human, one terminal); the work after
        # approval can still fan out. So: decide sequentially, execute in
        # parallel. Concurrency and consent don't mix well.
        decisions = [perms.decide(c.name, c.input) for c in calls]

        outcomes: list[tuple[str, bool]] = [("", False)] * len(calls)
        runnable = [i for i, (ok, _) in enumerate(decisions) if ok]
        for i, (ok, reason) in enumerate(decisions):
            if not ok:
                audit.write(event="denied", tool=calls[i].name, args=calls[i].input, reason=reason)
                outcomes[i] = (f"Permission denied: {reason}", True)
            else:
                audit.write(event="allowed", tool=calls[i].name, args=calls[i].input, reason=reason)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = {i: pool.submit(execute, calls[i].name, calls[i].input) for i in runnable}
            for i, fut in futures.items():
                outcomes[i] = fut.result()
                audit.write(event="result", tool=calls[i].name, is_error=outcomes[i][1],
                            bytes=len(outcomes[i][0]))

        results = []
        for call, (content, is_error) in zip(calls, outcomes):
            mark = "\033[31m✗\033[0m" if is_error else "\033[32m✓\033[0m"
            print(f"\033[2m  {mark} {call.name}({_brief(call.input)})\033[0m")
            results.append({
                "type": "tool_result",
                "tool_use_id": call.id,
                "content": content,
                "is_error": is_error,
            })
        messages.append({"role": "user", "content": results})

    return f"(stopped: hit the {max_turns}-turn limit)"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt")
    ap.add_argument("--real", action="store_true")
    ap.add_argument("--root", default=".", help="workspace root; paths cannot escape it")
    ap.add_argument("--yolo", action="store_true", help="approve every call (still sandboxed)")
    ap.add_argument("--audit", default="audit.jsonl")
    ap.add_argument("--max-turns", type=int, default=30)
    args = ap.parse_args()

    if args.real and not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set.", file=sys.stderr)
        return 1

    perms = Permissions(args.root, yolo=args.yolo, interactive=sys.stdin.isatty())
    audit = AuditLog(args.audit)
    client = make_client(args.real)

    print(f"stage3 harness — root={perms.root} yolo={args.yolo} audit={args.audit}")
    if args.prompt:
        print(agent(client, perms, audit, args.prompt, args.max_turns))
        return 0
    while True:
        try:
            line = input("\n\033[1m>\033[0m ").strip()
        except EOFError:
            return 0
        if line:
            agent(client, perms, audit, line, args.max_turns)


if __name__ == "__main__":
    raise SystemExit(main())
