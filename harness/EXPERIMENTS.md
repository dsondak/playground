# Experiments

Ordered roughly by cost. The first four need no API key. Each one has a
prediction step on purpose — the value is in being wrong about something
before you look.

---

## 1. Break the loop's invariants (free, mock)

Open `stage1_loop.py` and, one at a time, sabotage each of these. Predict the
failure first, then run it with `--real` (the mock is too forgiving here).

- Stop appending the assistant turn (`messages.append({"role": "assistant"...})`).
- Return a `tool_result` with a `tool_use_id` you made up.
- Split the tool results across two separate user messages instead of one.
- Drop the `is_error` flag and return the exception text as a normal result.

The third is the interesting one: it does not error. It quietly teaches the
model to stop batching tool calls, and you pay for it in latency for the rest
of the session.

## 2. Rewrite a tool description, change nothing else (needs a key, ~$0.05)

In `stage2_tools.py`, replace `edit_file`'s description with just
`"Edit a file."` Keep the implementation identical. Run the same editing task
five times against both versions and count the failed edits.

This is the cheapest, highest-leverage experiment here. The schema is the
prompt.

## 3. Watch the permission engine hold (free, mock)

```bash
python3 -c "
import sys; sys.path.insert(0,'.')
from stage3_permissions import Permissions
p = Permissions('.', interactive=False)
for c in ['ls -la', 'cat x | tee y', 'rm -rf / --no-preserve-root', 'git push']:
    print(f'{c:35} -> {p.decide(\"bash\", {\"command\": c})}')
"
```

Then find three commands that `_bash_is_plain` waves through but shouldn't.
(Start with: what does `find` do with `-exec`?) This is the point where the
argument-inspection-is-not-a-sandbox claim in the README stops being abstract.

## 4. Make the audit log answer a question (free)

Run a few sessions, then:

```bash
python3 -c "
import json,collections
c=collections.Counter(json.loads(l).get('tool') for l in open('audit.jsonl'))
print(c.most_common())
"
```

Which tool does the model reach for most? Is it the one you expected? If
`bash` dominates your dedicated tools, your dedicated tools' descriptions are
losing an argument with the model's priors.

## 5. Measure the quadratic (needs a key, ~$1-3)

The centrepiece. Give stage 4 a task that needs 15+ turns of file reading —
e.g. *"read every Python file here and write a one-paragraph summary of each
to NOTES.md"* — and run it four times:

```bash
python3 stage4_context.py --real --yolo --strategy none              --prompt "..."
python3 stage4_context.py --real --yolo --strategy none    --cache   --prompt "..."
python3 stage4_context.py --real --yolo --strategy evict   --cache --budget 15000 --prompt "..."
python3 stage4_context.py --real --yolo --strategy compact --cache --budget 15000 --prompt "..."
```

Plot per-turn `in=` from the ledger line. Predict the four curves first.
Things worth watching:

- Run 1's input tokens per turn should climb roughly linearly, so cumulative
  cost climbs quadratically.
- Run 2 costs about the same in *tokens* and much less in *dollars*. Caching
  does not shrink context; it makes re-sending it cheap.
- Watch `cache_r` go to zero the turn after a compaction. Compaction rewrites
  the prefix, so it destroys the cache it was competing with. Eviction has the
  same problem — it mutates old messages in place. Fixing that (evict only up
  to the last breakpoint, never past it) is a genuinely good exercise.
- Eviction and compaction fail differently. Evict loses detail and keeps
  structure; compact loses structure and keeps narrative. Find a task where
  each one produces a wrong answer the other gets right.

## 6. Find the subagent break-even (needs a key, ~$1)

Run the same search task in stage 5 two ways: once with `--prompt` phrased to
encourage delegation, once with `CHILD_TOOLS = []` so `task` is useless and
the parent must do it inline. Compare total tokens (the ledger includes
subagents) and answer quality.

Subagents win when the task's output is much smaller than its input. Find
roughly where the crossover is for your codebase. Then try a task that needs
a clarifying question mid-flight and watch a subagent guess instead of asking.

## 7. Add the thing that's missing

Pick one and build it into stage 5:

- **Retries.** The SDK retries 429/5xx for you, but not a tool that flaked.
  Where does that belong — the tool, the dispatcher, or the loop?
- **Session persistence.** Serialize `messages` to disk and add `--resume`.
  You will immediately hit the question of what to do about tool results that
  are no longer true.
- **A todo tool.** Give the model a scratchpad it can write plan state into.
  Then check whether it actually improves multi-step tasks or just adds turns.
  Measure it; the honest answer is task-dependent.
- **A cost cap.** Kill the loop at $N using the `Ledger`. Decide what the
  model should be told when you do — and whether telling it is safe.
