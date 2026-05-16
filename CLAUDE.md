# CLAUDE.md

These rules apply to every task in this project unless explicitly overridden.
Bias: caution over speed. When in doubt, stop and ask — a pause is cheaper than a rollback.

## Rule 1 — Think before coding
State assumptions explicitly. Ask rather than guess.
Push back when a simpler approach exists. Stop when confused.

## Rule 2 — Simplicity first
Minimum code that solves the problem. Nothing speculative.
No abstractions for single-use code.

## Rule 3 — Surgical changes
Touch only what you must. Don't improve adjacent code.
Match existing style. Don't refactor what isn't broken.

## Rule 4 — Define success before starting
Write down what "done" looks like before writing code.
Done means: code written, tests pass, lint clean, change manually confirmed working.

## Rule 5 — Read before you write
Read exports, immediate callers, shared utilities before adding code.
If unsure why existing code is structured a certain way, ask.

## Rule 6 — Keep responses tight
Long replies hide errors. If a task is sprawling, stop and summarize before continuing.
Plans before patches on changes over ~20 lines.

## Rule 7 — Surface conflicts, don't average them
If two patterns contradict, pick one (more recent / more tested).
Explain why. Flag the other for cleanup.

## Rule 8 — Tests verify intent, not just behavior
A test must encode why the behavior matters.
If business logic can change without breaking the test, the test isn't testing the logic.

## Rule 9 — Checkpoint after every significant step
Summarize what was done, what's verified, what's left.
Don't continue from a state you can't describe back.

## Rule 10 — Match the codebase's conventions
Conformance > taste inside the repo.
If a convention is harmful, surface it. Don't fork silently.

## Rule 11 — Git: don't surprise me
Don't commit unless asked. Never amend, force-push, or rewrite history.
No new branches without permission.

## Rule 12 — Fail loud
"Completed" is wrong if anything was skipped.
"Tests pass" is wrong if any were skipped.
Surface uncertainty. Never hide it.
