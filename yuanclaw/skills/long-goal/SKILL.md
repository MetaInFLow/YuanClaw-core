---
name: long-goal
description: Sustained objectives via long_task / complete_goal. Use for multi-turn work on one clear objective.
---

# Long-running objectives

Use `long_task` when the user asks for one sustained objective that may require multiple turns or long execution. Use `complete_goal` when the objective is finished, cancelled, replaced, or redirected.

## Start fast

Call `long_task` once the objective is clear. The goal text must be:

- Idempotent: safe to re-read after retries or compaction.
- Self-contained: include paths, scope, constraints, and done-ness that matter.
- Bounded: one objective, not a grab bag of unrelated tasks.
- Verifiable: describe what proves completion.

Do not delay `long_task` to write a full plan. Planning, research, implementation, and verification happen after the goal is recorded.

## Complete honestly

Call `complete_goal` only when the active objective should stop being tracked.

- If the objective succeeded, recap delivered outcomes and verification.
- If the user cancelled or changed direction, say that honestly in the recap.
- If replacing the objective, complete the old one first, then start a new `long_task`.
