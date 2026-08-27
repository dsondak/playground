#!/usr/bin/env python3
"""Stage 2 — a real tool surface.

Diff this against stage1. The loop is unchanged. Everything added is about
TOOLS, and every addition is a lesson about what a harness owes the model:

  * A registry, so tools are data (name -> schema + handler), not an if-chain.
  * Dedicated tools alongside bash. `read_file` is not a convenience wrapper
    over `cat` — it gives the model line numbers, a byte budget, and a
    machine-readable error, none of which `cat` provides.
  * `edit_file` with exact-match replacement. Letting a model rewrite whole
    files costs output tokens proportional to file size and loses unrelated
    edits; an anchored replace fails loudly instead of silently clobbering.
  * Parallel execution of the tool calls in one assistant turn.
  * Structured failures: `is_error: True` results instead of raised
    exceptions. A crashed tool should teach the model, not kill the loop.
  * Output truncation. A tool that returns 2MB of logs ends the session.

Run:
    python3 stage2_tools.py --prompt "summarize this directory"
    python3 stage2_tools.py --real --prompt "..."
"""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable

MODEL = "claude-opus-5"
MAX_TOOL_OUTPUT = 20_000  # characters returned to the model per tool call

SYSTEM = """You are a coding agent working in a single project directory.

Prefer the dedicated tools (read_file, edit_file, list_files, search) over
bash for anything they cover — they give you line numbers and precise errors.
Use bash for everything else: running tests, git, package managers.

Read a file before you edit it. Be concise."""


class ToolError(Exception):
    """Raised by a handler to produce an is_error tool_result."""


@dataclass
class Tool:
    name: str
    description: str
    schema: dict
    handler: Callable[..., str]
    # Tools that only read are safe to run concurrently and, in stage 3,
    # safe to auto-approve. Tracking this per tool is what makes a
    # permission model possible later.
    read_only: bool = True

    def spec(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.schema,
        }


REGISTRY: dict[str, Tool] = {}


def tool(name: str, description: str, schema: dict, read_only: bool = True):
    def wrap(fn):
        REGISTRY[name] = Tool(name, description, schema, fn, read_only)
        return fn

    return wrap


def _obj(props: dict, required: list[str]) -> dict:
    return {
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


# ---------------------------------------------------------------- tools ----


@tool(
    "read_file",
    "Read a text file. Returns the contents with 1-indexed line numbers. "
    "Use `offset` and `limit` for files too large to read at once.",
    _obj(
        {
            "path": {"type": "string", "description": "Path to the file."},
            "offset": {"type": "integer", "description": "First line to read (1-indexed)."},
            "limit": {"type": "integer", "description": "How many lines to read."},
        },
        ["path"],
    ),
)
def read_file(path: str, offset: int = 1, limit: int = 2000) -> str:
    if not os.path.isfile(path):
        raise ToolError(f"No such file: {path}")
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError as exc:
        raise ToolError(str(exc)) from exc
    if not lines:
        return "(file is empty)"
    chunk = lines[offset - 1 : offset - 1 + limit]
    if not chunk:
        raise ToolError(f"offset {offset} is past end of file ({len(lines)} lines)")
    body = "".join(f"{offset + i:>6}\t{ln}" for i, ln in enumerate(chunk))
    if offset - 1 + limit < len(lines):
        body += f"\n... ({len(lines) - offset + 1 - len(chunk)} more lines)"
    return body


@tool(
    "write_file",
    "Write a file, creating or overwriting it. Prefer edit_file for changes "
    "to an existing file.",
    _obj(
        {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        ["path", "content"],
    ),
    read_only=False,
)
def write_file(path: str, content: str) -> str:
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return f"Wrote {len(content)} bytes to {path}"


@tool(
    "edit_file",
    "Replace an exact string in a file. `old` must appear EXACTLY ONCE in the "
    "file, including indentation — otherwise the edit fails and nothing is "
    "changed. Include surrounding context to make `old` unique.",
    _obj(
        {
            "path": {"type": "string"},
            "old": {"type": "string", "description": "Exact text to replace."},
            "new": {"type": "string", "description": "Replacement text."},
        },
        ["path", "old", "new"],
    ),
    read_only=False,
)
def edit_file(path: str, old: str, new: str) -> str:
    if not os.path.isfile(path):
        raise ToolError(f"No such file: {path}")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    count = src.count(old)
    if count == 0:
        raise ToolError(
            "`old` was not found in the file. Read the file again and copy the "
            "exact text, including whitespace."
        )
    if count > 1:
        raise ToolError(
            f"`old` appears {count} times; it must be unique. Add surrounding "
            "lines to disambiguate."
        )
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(src.replace(old, new))
    return f"Edited {path}"


@tool(
    "list_files",
    "List files under a directory, recursively, skipping hidden dirs, "
    "node_modules, and __pycache__.",
    _obj({"path": {"type": "string", "description": "Directory. Defaults to '.'"}}, []),
)
def list_files(path: str = ".") -> str:
    if not os.path.isdir(path):
        raise ToolError(f"Not a directory: {path}")
    skip = {"node_modules", "__pycache__", ".git", "venv", ".venv"}
    out = []
    for root, dirs, files in os.walk(path):
        dirs[:] = sorted(d for d in dirs if d not in skip and not d.startswith("."))
        for f in sorted(files):
            out.append(os.path.join(root, f))
        if len(out) > 500:
            out.append("... (truncated at 500 entries)")
            break
    return "\n".join(out) or "(no files)"


@tool(
    "search",
    "Search file contents with a regular expression (ripgrep-style). Returns "
    "matching lines prefixed with path:line.",
    _obj(
        {
            "pattern": {"type": "string", "description": "Regular expression."},
            "path": {"type": "string", "description": "Directory to search. Defaults to '.'"},
            "glob": {"type": "string", "description": "Optional filename filter, e.g. '*.py'."},
        },
        ["pattern"],
    ),
)
def search(pattern: str, path: str = ".", glob: str | None = None) -> str:
    cmd = ["grep", "-rnI", "-E", pattern, path]
    if glob:
        cmd[1:1] = [f"--include={glob}"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if proc.returncode == 1:
        return "(no matches)"
    if proc.returncode > 1:
        raise ToolError(proc.stderr.strip() or "search failed")
    return proc.stdout.strip()


@tool(
    "bash",
    "Run a bash command in the project directory. Returns combined stdout and "
    "stderr. 60s timeout, no interactive input.",
    _obj({"command": {"type": "string"}}, ["command"]),
    read_only=False,
)
def bash(command: str) -> str:
    try:
        proc = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=60
        )
    except subprocess.TimeoutExpired:
        raise ToolError("command timed out after 60s") from None
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode != 0:
        out += f"\n(exit code {proc.returncode})"
    return out or "(no output)"


# --------------------------------------------------------------- harness ----


def execute(name: str, args: dict) -> tuple[str, bool]:
    """Run one tool. Returns (content, is_error). Never raises."""
    entry = REGISTRY.get(name)
    if entry is None:
        return f"Unknown tool: {name}", True
    try:
        result = entry.handler(**args)
    except ToolError as exc:
        return f"Error: {exc}", True
    except TypeError as exc:
        # Bad arguments — usually a schema the model misread. Say so plainly;
        # the model can retry with the right shape.
        return f"Error: invalid arguments for {name}: {exc}", True
    except Exception as exc:  # noqa: BLE001 - a crashing tool must not kill the loop
        return f"Error: {type(exc).__name__}: {exc}", True
    if len(result) > MAX_TOOL_OUTPUT:
        head = result[: MAX_TOOL_OUTPUT // 2]
        tail = result[-MAX_TOOL_OUTPUT // 2 :]
        dropped = len(result) - len(head) - len(tail)
        result = f"{head}\n\n... [{dropped} characters truncated] ...\n\n{tail}"
    return result, False


def make_client(real: bool):
    if not real:
        from _mock import MockAnthropic

        return MockAnthropic()
    import anthropic

    return anthropic.Anthropic()


def agent(client, user_prompt: str, max_turns: int = 30) -> str:
    messages = [{"role": "user", "content": user_prompt}]
    specs = [t.spec() for t in REGISTRY.values()]

    for _ in range(max_turns):
        response = client.messages.create(
            model=MODEL,
            max_tokens=8000,
            system=SYSTEM,
            tools=specs,
            messages=messages,
        )

        for block in response.content:
            if block.type == "text" and block.text.strip():
                print(f"\n\033[1massistant\033[0m  {block.text.strip()}")

        if response.stop_reason != "tool_use":
            return "\n".join(b.text for b in response.content if b.type == "text")

        messages.append({"role": "assistant", "content": response.content})
        calls = [b for b in response.content if b.type == "tool_use"]

        # One assistant turn can request several tools. Running them
        # concurrently is most of the wall-clock win in a real harness.
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(execute, c.name, c.input) for c in calls]
            outcomes = [f.result() for f in futures]

        results = []
        for call, (content, is_error) in zip(calls, outcomes):
            mark = "\033[31m✗\033[0m" if is_error else "\033[32m✓\033[0m"
            print(f"\033[2m  {mark} {call.name}({_brief(call.input)})\033[0m")
            print(f"\033[2m    {content.splitlines()[0][:200] if content else ''}\033[0m")
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": content,
                    "is_error": is_error,
                }
            )

        messages.append({"role": "user", "content": results})

    return f"(stopped: hit the {max_turns}-turn limit)"


def _brief(args: dict) -> str:
    parts = []
    for k, v in args.items():
        s = str(v).replace("\n", "\\n")
        parts.append(f"{k}={s[:40]}{'…' if len(s) > 40 else ''}")
    return ", ".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt")
    ap.add_argument("--real", action="store_true")
    ap.add_argument("--max-turns", type=int, default=30)
    args = ap.parse_args()

    if args.real and not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set.", file=sys.stderr)
        return 1

    client = make_client(args.real)
    if args.prompt:
        print(agent(client, args.prompt, args.max_turns))
        return 0

    print(f"stage2 harness — {len(REGISTRY)} tools: {', '.join(REGISTRY)}")
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
