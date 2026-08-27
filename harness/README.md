# A harness, built in five stages

An **agentic harness** is the program that turns a language model into an
agent. The model itself is stateless and inert: you hand it a conversation,
it hands back one message. It cannot run a command, read a file, remember
yesterday, or decide it is finished. Everything that makes an agent *agentic*
is ordinary code sitting outside the model.

This directory builds that code up one concern at a time, so you can see
exactly what each concern costs.

```
stage1_loop.py         ~160 lines   the loop. one tool. nothing else.
stage2_tools.py        ~370 lines   + tool registry, parallelism, error results, truncation
stage3_permissions.py  ~290 lines   + sandbox, permission engine, audit log
stage4_context.py      ~330 lines   + token accounting, caching, eviction vs. compaction
stage5_subagents.py    ~315 lines   + subagents, streaming, hooks
_mock.py                            scaffolding: a fake model so it all runs with no API key
```

**The tool surface stops changing after stage 2.** Stages 3-5 import stage 2's
registry unchanged and add layers *around* it. That is not a shortcut — it is
the actual shape of the problem. Writing tools is the easy part; almost all
harness design lives in what wraps them.

## Run it

Every stage runs immediately with a canned fake model, so you can watch the
mechanics before spending anything:

```bash
python3 stage1_loop.py --prompt "what files are here?"
python3 stage3_permissions.py --prompt "read the README"
python3 stage4_context.py --prompt "look around" --cache --strategy evict --budget 100
```

With a real key, pass `--real`:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
pip install anthropic
python3 stage2_tools.py --real          # no --prompt gives you a REPL
```

The mock never reads your tool results and always answers the same thing. It
is there to exercise the *loop*, not to be a model.

## Read it as a sequence of diffs

The code is written so the diffs are the lesson:

```bash
diff -u stage1_loop.py stage2_tools.py | less
```

Each stage's docstring states what it adds and, more usefully, what that
addition costs. Six ideas are worth carrying out of it:

1. **The loop is trivial.** Stage 1 is ~40 real lines. If the loop is all you
   need, you are done in an afternoon. Everything after stage 1 exists because
   of what happens on turn 30, not turn 2.

2. **A tool description is a prompt.** The single biggest lever on tool-use
   quality is the `description` and schema text — not the implementation.
   `edit_file`'s description in stage 2 is longer than its body, deliberately.

3. **Failures are information, not exceptions.** A tool that crashes, a path
   that escapes the sandbox, a permission the user declines — all of them come
   back as `is_error: true` tool results. The model reads the error and
   reroutes. A harness that raises on a bad tool call throws away the model's
   ability to recover.

4. **Advertise for guidance, enforce at the gate.** Stage 5's subagent depth
   limit was originally enforced by not listing the `task` tool for children.
   That is a hint, and the mock model ignored it, and the loop ran away to
   44,863 turns and a $133 estimate on the first test run. The guard now sits
   in the dispatcher. Never make the model's cooperation load-bearing.

5. **Context is the scarce resource.** Naive appending makes cost grow
   quadratically in turns. Stage 4 gives you the three answers — measure,
   cache, reclaim — and lets you switch between them to see how differently
   they fail.

6. **Subagents are a context boundary, not a speed trick.** They are worth it
   when the *output* of a task is far smaller than its *input*. They cost you
   the ability to ask a follow-up question.

## What this deliberately does not do

Real harnesses (Claude Code among them) add: session persistence and resume,
MCP servers for third-party tools, todo/plan state, diff review UI, retries
with backoff on 429/5xx, multi-file atomic edits, cost caps, and a container
boundary rather than a path check. The path-argument sandbox in stage 3 is
explicitly **not** a security boundary — `bash` takes an opaque string and
there are infinite spellings of `cat ../../etc/passwd`. Argument inspection is
a UX affordance for the common case. If you need a real boundary, it is a
container, a seccomp profile, or a separate uid.

Next: `EXPERIMENTS.md`.
