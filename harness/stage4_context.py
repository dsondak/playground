#!/usr/bin/env python3
"""Stage 4 — context is the scarce resource.

Stages 1-3 grow `messages` forever. That is the single most common reason a
homemade harness falls over on a real task: every tool result is appended and
re-sent on every turn, so cost and latency grow quadratically in turns while
the useful signal stays flat. A 40-turn session where each turn reads a file
sends those files 40, 39, 38 ... times.

Stage 4 adds the three things a harness does about that, each switchable so
you can measure them against each other:

  1. ACCOUNTING. Per-turn input/output/cache tokens and a running dollar
     estimate. You cannot tune what you cannot see; run this first.

  2. CACHING (--cache). A cache_control breakpoint after the tools+system
     prefix, and a rolling breakpoint on the newest tool results. Cache reads
     are ~10x cheaper than fresh input. Caching does not shrink the context;
     it makes re-sending it cheap. Watch cache_read_input_tokens.

  3. RECLAMATION, when the window itself gets tight — two different answers:
       --strategy evict    Blank out old tool_result CONTENT, keeping the
                           blocks and their ids. Lossy, instant, free.
       --strategy compact  Ask the model to summarize the old turns, then
                           replace them with the summary. Lossy differently,
                           costs one extra call, keeps intent.
       --strategy none     Do nothing and watch it grow.

     Evict throws away detail and keeps structure; compact throws away
     structure and keeps narrative. Neither is correct — they fail in
     different directions, which is why the flag exists.

Run:
    python3 stage4_context.py --prompt "..." --cache --strategy evict
    python3 stage4_context.py --real --prompt "..." --cache --budget 30000
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from stage2_tools import MODEL, REGISTRY, SYSTEM, _brief, execute  # noqa: E402
from stage3_permissions import AuditLog, Permissions  # noqa: E402

# Claude Opus 5 list price, $/1M tokens. Cache reads are 0.1x input;
# cache writes are 1.25x input.
PRICE_IN, PRICE_OUT = 5.00, 25.00


class Ledger:
    def __init__(self):
        self.turns = 0
        self.input = self.output = self.cache_read = self.cache_write = 0

    def add(self, usage):
        self.turns += 1
        self.input += getattr(usage, "input_tokens", 0) or 0
        self.output += getattr(usage, "output_tokens", 0) or 0
        self.cache_read += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.cache_write += getattr(usage, "cache_creation_input_tokens", 0) or 0

    @property
    def dollars(self) -> float:
        return (
            self.input * PRICE_IN
            + self.cache_read * PRICE_IN * 0.1
            + self.cache_write * PRICE_IN * 1.25
            + self.output * PRICE_OUT
        ) / 1_000_000

    def line(self) -> str:
        return (
            f"turn {self.turns:>2}  in={self.input:<7} out={self.output:<6} "
            f"cache_r={self.cache_read:<7} cache_w={self.cache_write:<7} "
            f"~${self.dollars:.4f}"
        )


def rough_tokens(messages) -> int:
    """Cheap local estimate so we can decide *before* paying for a call.

    ~4 chars per token. For anything that must be exact, use
    client.messages.count_tokens — but that is itself a network round trip,
    so a harness typically estimates to decide and measures to report.
    """
    return len(json.dumps(messages, default=str)) // 4


# ------------------------------------------------------------- caching ----

CACHE_CTL = {"type": "ephemeral"}


def system_blocks(cache: bool):
    """tools -> system -> messages is the render order, so one breakpoint at
    the end of `system` caches the tool schemas too. Those bytes are
    identical on every single turn — this is the highest-value breakpoint in
    the whole request and it costs one line."""
    block = {"type": "text", "text": SYSTEM}
    if cache:
        block["cache_control"] = CACHE_CTL
    return [block]


def roll_cache_breakpoint(messages, cache: bool):
    """Move the conversation breakpoint to the newest tool-result block.

    A prefix match means any byte change before the breakpoint invalidates
    it, so the breakpoint has to sit at the END of the stable prefix — i.e.
    on the most recent thing that will never change again. Max 4 breakpoints
    per request, so we clear the old one as we set the new.
    """
    if not cache:
        return
    for msg in messages:
        if isinstance(msg.get("content"), list):
            for block in msg["content"]:
                if isinstance(block, dict):
                    block.pop("cache_control", None)
    for msg in reversed(messages):
        content = msg.get("content")
        if isinstance(content, list) and content and isinstance(content[-1], dict):
            content[-1]["cache_control"] = CACHE_CTL
            return


# --------------------------------------------------------- reclamation ----

ELIDED = "[tool result elided by the harness to reclaim context]"


def evict(messages, keep_last: int = 6) -> int:
    """Blank out old tool_result CONTENT in place.

    The blocks and their tool_use_ids stay — the API rejects a conversation
    where an assistant tool_use has no matching result, so you cannot simply
    delete them. Returns the number of results elided.
    """
    boundary = max(0, len(messages) - keep_last)
    n = 0
    for msg in messages[:boundary]:
        if not isinstance(msg.get("content"), list):
            continue
        for block in msg["content"]:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                if block.get("content") != ELIDED:
                    block["content"] = ELIDED
                    n += 1
    return n


def compact(client, messages, keep_last: int = 6):
    """Summarize the old turns and replace them with the summary.

    Note what this costs beyond the extra call: it destroys the cached
    prefix (every byte after the rewrite is new), and it discards the
    tool_use/tool_result pairing, so the summary has to be a plain user
    message. That is why we hand it back as a fresh two-message opening.
    """
    boundary = max(0, len(messages) - keep_last)
    if boundary <= 1:
        return messages, 0
    old, recent = messages[:boundary], messages[boundary:]

    transcript = json.dumps(old, default=str)[:120_000]
    summary_resp = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        system="You compress agent transcripts. No preamble.",
        messages=[{
            "role": "user",
            "content": (
                "Summarize this agent transcript so another agent can continue "
                "the work without re-reading it. Keep: the user's goal, files "
                "touched and their key contents, decisions made, what failed "
                "and why, and what is left to do. Drop everything else.\n\n"
                + transcript
            ),
        }],
    )
    text = "\n".join(b.text for b in summary_resp.content if b.type == "text")

    # `recent` must not begin with a dangling tool_result whose tool_use is
    # now in the summary — drop from the front until it starts cleanly.
    while recent and isinstance(recent[0].get("content"), list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in recent[0]["content"]
    ):
        recent = recent[1:]

    rebuilt = [
        {"role": "user", "content": f"<summary_of_earlier_work>\n{text}\n</summary_of_earlier_work>"},
        {"role": "assistant", "content": "Understood. Continuing from that summary."},
        *recent,
    ]
    return rebuilt, len(old)


# --------------------------------------------------------------- agent ----


def make_client(real: bool):
    if not real:
        from _mock import MockAnthropic

        return MockAnthropic()
    import anthropic

    return anthropic.Anthropic()


def agent(client, perms, audit, prompt, *, cache=False, strategy="none",
          budget=60_000, max_turns=30):
    messages = [{"role": "user", "content": [{"type": "text", "text": prompt}]}]
    specs = [t.spec() for t in REGISTRY.values()]
    ledger = Ledger()

    for _ in range(max_turns):
        # Decide on reclamation BEFORE the call — the estimate is free, the
        # call is not.
        est = rough_tokens(messages)
        if est > budget:
            if strategy == "evict":
                n = evict(messages)
                print(f"\033[35m  ~ context {est} tok > budget: elided {n} old results\033[0m")
            elif strategy == "compact":
                messages, n = compact(client, messages)
                print(f"\033[35m  ~ context {est} tok > budget: compacted {n} messages\033[0m")
            else:
                print(f"\033[35m  ~ context {est} tok > budget (strategy=none)\033[0m")

        roll_cache_breakpoint(messages, cache)

        response = client.messages.create(
            model=MODEL,
            max_tokens=8000,
            system=system_blocks(cache),
            tools=specs,
            messages=messages,
        )
        ledger.add(response.usage)
        print(f"\033[2m[{ledger.line()}]\033[0m")

        for block in response.content:
            if block.type == "text" and block.text.strip():
                print(f"\n\033[1massistant\033[0m  {block.text.strip()}")

        if response.stop_reason != "tool_use":
            print(f"\n\033[2m── {ledger.line()}\033[0m")
            return "\n".join(b.text for b in response.content if b.type == "text")

        messages.append({"role": "assistant", "content": response.content})
        calls = [b for b in response.content if b.type == "tool_use"]

        decisions = [perms.decide(c.name, c.input) for c in calls]
        outcomes: list[tuple[str, bool]] = [("", False)] * len(calls)
        runnable = [i for i, (ok, _) in enumerate(decisions) if ok]
        for i, (ok, reason) in enumerate(decisions):
            if not ok:
                outcomes[i] = (f"Permission denied: {reason}", True)
            audit.write(event="decision", tool=calls[i].name, allowed=ok, reason=reason)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = {i: pool.submit(execute, calls[i].name, calls[i].input) for i in runnable}
            for i, fut in futures.items():
                outcomes[i] = fut.result()

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

    return f"(stopped: hit the {max_turns}-turn limit)  {ledger.line()}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt")
    ap.add_argument("--real", action="store_true")
    ap.add_argument("--root", default=".")
    ap.add_argument("--yolo", action="store_true")
    ap.add_argument("--cache", action="store_true", help="add cache_control breakpoints")
    ap.add_argument("--strategy", choices=["none", "evict", "compact"], default="none")
    ap.add_argument("--budget", type=int, default=60_000,
                    help="estimated input tokens before reclamation kicks in")
    ap.add_argument("--audit", default="audit.jsonl")
    ap.add_argument("--max-turns", type=int, default=30)
    args = ap.parse_args()

    if args.real and not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set.", file=sys.stderr)
        return 1

    perms = Permissions(args.root, yolo=args.yolo, interactive=sys.stdin.isatty())
    audit = AuditLog(args.audit)
    client = make_client(args.real)
    print(f"stage4 — cache={args.cache} strategy={args.strategy} budget={args.budget}")

    if args.prompt:
        print(agent(client, perms, audit, args.prompt, cache=args.cache,
                    strategy=args.strategy, budget=args.budget, max_turns=args.max_turns))
        return 0
    while True:
        try:
            line = input("\n\033[1m>\033[0m ").strip()
        except EOFError:
            return 0
        if line:
            agent(client, perms, audit, line, cache=args.cache,
                  strategy=args.strategy, budget=args.budget, max_turns=args.max_turns)


if __name__ == "__main__":
    raise SystemExit(main())
