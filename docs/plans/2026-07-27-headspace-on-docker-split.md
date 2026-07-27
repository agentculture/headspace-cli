# Implementation split plan — headspace on docker

Human gate 2. The plan, waves, and per-task agent/model proposal below are what
the fan-out will execute if approved. Edit any row before approving.

Branch/worktree conventions follow this repo's CHANGELOG 0.6.1 override, not the
vendored skill's example: worktrees live in `../.worktrees.headspace-cli/<name>/`
and branches use a work-scoped prefix (`hs/<task>-<slug>`), because a plain
`agent/*` prefix collides with leftovers from earlier fan-outs and fails
`git worktree add -b`.

## Waves

| Wave | Tasks | Runs in parallel |
|------|-------|------------------|
| 0 | t1 t2 t3 t4 t5 t6 t7 | 7 agents |
| 1 | t8 | 1 |
| 2 | t9 t10 | 2 |
| 3 | t11 t12 | 2 |
| 4 | t13 t14 | 2 |

## Wave 0 — proposed assignment

Every task is `general-purpose`, TDD-first, in its own worktree, merged only
when its tests pass before *and* after merge.

| Task | Writes | Model | Why this tier |
|------|--------|-------|---------------|
| t1 | `headspace/core/states.py` | sonnet | Enum plus transition table; the matrix test is mechanical once the table is written. |
| t2 | `headspace/core/result.py` | opus | The product's defining contract (context-return). Two renderers must stay field-identical while a byte bound truncates one of them — easy to get subtly wrong. |
| t3 | `headspace/core/policy.py` | sonnet | Declared-vs-effective pairs; pure data, no I/O. |
| t4 | `headspace/core/store.py` | opus | Crash-atomic writes, `fcntl` advisory locking, fail-closed schema versioning. Concurrency plus durability is where cheap models produce plausible-but-racy code. |
| t5 | `headspace/core/profiles.py` | sonnet | Name-to-digest mapping; smallest task in the wave. |
| t6 | `headspace/core/artifacts.py` | opus | Stream-hash to `.partial`, fsync, verify, `os.replace` — a well-known pattern, but the interruption test must actually kill mid-copy. |
| t7 | `headspace/cli/_errors.py`, `headspace/cli/_commands/learn.py` | sonnet | Additive constants in the reserved 3+ band; the risk is renumbering 0/1/2, which the acceptance criterion forbids explicitly. |

## File-disjointness check

The dependency graph guarantees logical independence within a wave; it does not
guarantee file disjointness. Verified by hand for wave 0:

- t1–t6 each create exactly one new module under `headspace/core/` plus one new
  test file. No overlap.
- **Resolved before fan-out:** all six needed `headspace/core/__init__.py`,
  which did not exist — six agents would each have created it and collided at
  merge. Committed as a scaffold in `bf4f9ec` so no task creates it.
- t7 is the only task touching existing files (`_errors.py`, `learn.py`). No
  other wave-0 task touches `headspace/cli/`.

## Later waves — provisional

Assignments below are indicative and get re-confirmed as each wave starts.

| Task | Model | Note |
|------|-------|------|
| t8 provider Protocol + fake + conformance suite | opus | Defines the seam every later task builds on; a weak abstraction here propagates. |
| t9 Docker provider | opus | Real isolation semantics, live-engine assertions. |
| t10 workspace orchestration | opus | Intent journal, orphan reconciliation, destroy guard. |
| t11 CLI verbs | sonnet | Follows the established `register(sub)` pattern. |
| t12 integration suite | sonnet | Named test per honesty condition, skip-clean without an engine. |
| t13 e2e + traceability | sonnet | Scripted; the table is bookkeeping. |
| t14 packaging + docs | sonnet | Pin, README, explain catalog, version bump. |

## Gate 2 outcome

**Approved 2026-07-28 with richer tiers**: opus on t2, t4, and t6 — the three
tasks carrying crash, concurrency, or interruption semantics — and sonnet on
t1, t3, t5, t7. This is one tier above the original proposal, which had t6 on
sonnet; the operator chose to spend it on interruption semantics rather than
risk a fix-up round there.

Wave 0 therefore runs 3 opus and 4 sonnet agents concurrently.
