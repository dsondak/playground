"""Scaffolding, not lesson content.

A fake Messages API client so every stage runs with no ANTHROPIC_API_KEY.
It returns canned, deterministic responses that exercise the harness loop:
one tool call, then a text answer. Swap it out for the real client with
--real once you have a key.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field


@dataclass
class TextBlock:
    text: str
    type: str = "text"


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict
    type: str = "tool_use"


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class MockMessage:
    content: list
    stop_reason: str = "end_turn"
    usage: Usage = field(default_factory=Usage)


class _Messages:
    def __init__(self, outer):
        self._outer = outer

    def create(self, **kwargs):
        return self._outer._respond(kwargs)


class MockAnthropic:
    """Duck-types the bits of anthropic.Anthropic the stages touch."""

    def __init__(self, script=None, seed: int = 0):
        # script: list of (tool_name, tool_input) to call before answering.
        self.script = script
        self.messages = _Messages(self)
        self._rng = random.Random(seed)
        self._calls = 0

    def _default_script(self, tool_names: list[str]):
        # Prefer a read-only probe if the harness exposes one.
        for name, args in (
            ("list_files", {"path": "."}),
            ("bash", {"command": "ls -la"}),
            ("read_file", {"path": "README.md"}),
        ):
            if name in tool_names:
                return [(name, args)]
        return []

    def _respond(self, kwargs):
        self._calls += 1
        messages = kwargs.get("messages", [])
        tool_names = [t["name"] for t in kwargs.get("tools", []) if "name" in t]
        script = self.script if self.script is not None else self._default_script(tool_names)
        # Only call tools that were actually offered — a real model mostly
        # stays in-bounds, and a mock that never does makes every harness
        # look broken. (Mostly. See stage5's depth guard for why "mostly"
        # is not good enough to build on.)
        script = [step for step in script if step[0] in tool_names] or self._default_script(tool_names)

        # How many tool results has the conversation already seen?
        done = sum(
            1
            for m in messages
            if m["role"] == "user"
            and isinstance(m.get("content"), list)
            and any(
                (b.get("type") if isinstance(b, dict) else getattr(b, "type", None)) == "tool_result"
                for b in m["content"]
            )
        )

        usage = Usage(
            input_tokens=sum(len(json.dumps(m, default=str)) for m in messages) // 4,
            output_tokens=self._rng.randint(40, 160),
        )

        if done < len(script):
            name, args = script[done]
            return MockMessage(
                content=[
                    TextBlock(f"[mock] I'll call `{name}` to find out."),
                    ToolUseBlock(id=f"toolu_mock_{self._calls}", name=name, input=args),
                ],
                stop_reason="tool_use",
                usage=usage,
            )

        return MockMessage(
            content=[
                TextBlock(
                    "[mock] Done. This answer is canned — the mock model never "
                    "reads your tool results. Run with --real to see a real agent."
                )
            ],
            stop_reason="end_turn",
            usage=usage,
        )
