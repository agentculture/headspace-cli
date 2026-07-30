# Build Plan — file-in-path-14 aftermath

slug: `file-in-path-14-aftermath` · status: `exported` · from frame: `file-in-path-14-aftermath`

> The aftermath of the file-in-path-14 workforce run is settled: the five recorded deviations are adjudicated by the operator, a Docker job ended by stop --apply is recorded cancelled end-to-end so c43 is fully met, and headspace's supported import surface is declared where an external consumer can read it.

## Tasks

### t1 — Docker sentinel discriminator: stop writes a namespaced sentinel bearing the signalled `job_id` into the workspace volume before SIGTERM; run classifies the ended job cancelled (single file: headspace/providers/docker.py)

- instruction: Single file plus its unit tests: headspace/providers/docker.py. Add a cancelled branch to `_status` per the documented precedence (cancelled > timeout > `resource_exhausted`, c19/h16) and the sentinel write inside stop's existing engine boundary. Reuse the proven mechanisms rather than inventing: the anchor container's exec/`put_archive` staging path (docker.py 2033-2219, the put verb) writes, and `get_archive` (1935-1951, the read verb) reads with the runtime dead. Harden the path against symlinks exactly as t14 did (d55fc29 guard-before-hash, 6a0e635 re-check-before-rename) — c18. Do NOT weaken the seam: JobOutcome refuses a cancelled outcome carrying an exit status, so drop the 137. t6 upgrades this to two-phase evidence immediately after; keep the marker read/write helpers separable so t6 extends rather than rewrites.
- covers: c4, c5, c12, h6, h8, c18
- acceptance:
  - stop, after finding a live job and before signalling, execs a namespaced sentinel carrying the `job_id` it signalled into the workspace volume; it still takes no lock and writes nothing under ~/.headspace — `test_stop_verb`'s three-surface inertness proofs pass unchanged
  - run records status cancelled with `exit_status` None exactly when its wait returns non-zero and the sentinel names the job it just ran; a missing, stale, or wrong-job sentinel leaves classification as today — SystemExit(137) stays failure/137, timeout and OOM precedence unchanged
  - run removes the sentinel after classification, and the `job_id` is not exposed inside the container (no env/argv leak), so a job cannot name itself in a forged sentinel

### t2 — Live Docker cancelled test + retire the test-side #16 caveat (files: tests/`test_integration_docker.py`, tests/`test_stop_verb.py` docstring)

- instruction: Live-engine test only — tests/`test_integration_docker.py` plus retiring the gap paragraph in tests/`test_stop_verb.py`'s module docstring. Prove the red bar first: run the new test against pre-t1 code, record the failure output in the PR body as evidence (h4), then confirm green post-t1. Drive the in-flight case honestly the way `test_stop_verb` already does rather than fabricating a running job. Skip cleanly when no Docker socket is reachable, and mark it 'integration'. Approved deviation d3 binds here: this suite is single-writer against one daemon (uses deterministic object names), so it must not run concurrently with another live suite.
- depends on: t1
- covers: c8, h3, h4
- acceptance:
  - a live integration test runs stop --apply against a genuinely running Docker job and asserts the recorded outcome: package status cancelled, `exit_status` null, and the concurrent run invocation exiting 5; it skips cleanly when no Docker socket is reachable
  - the test demonstrably red-bars against pre-t1 code (evidence recorded in the PR) and greens after t1 merges
  - the gap paragraph in tests/`test_stop_verb.py`'s module docstring is retired; grep finds no #16 reference left under tests/

### t3 — headspace.api facade: the declared import surface (new files: headspace/api.py, tests/`test_api.py`)

- instruction: Two new files only: headspace/api.py and tests/`test_api.py` — no edits to existing modules, which is what keeps this wave-0 task file-disjoint from t1. Delegate to the six Orchestrator methods already verified present in core/workspace.py (create 758, run 866, export 1097, put 1219, stop 1488, destroy 1595) — wrap construction wiring (store, provider, policy resolution) as the CLI's own command modules do; do NOT refactor orchestration (c22). Import must stay side-effect free: no engine probe, no docker SDK import at module import time (h5), so keep provider construction lazy inside the functions. Declare `__all__` over exactly the five supported operations.
- covers: c2, c11, h5
- acceptance:
  - headspace/api.py exposes create, run, put, export, destroy as typed functions delegating to the same orchestration entry points the CLI uses, with `__all__` declaring exactly those names; signatures are decided against core/workspace.py's real entry points, not invented
  - importing headspace.api succeeds with no reachable engine — module import performs no engine probe and does not import the docker SDK
  - tests assert `__all__` matches the documented set and that every supported operation is reachable without touching headspace.core

### t4 — README closure: Python API section + retire the exit-5 caveat (single file: README.md)

- instruction: Single file: README.md. Add a 'Python API' section declaring headspace.api the sole supported import surface under semver and headspace.core private, with a minimal example — the bar is that an external consumer settles the question from README plus `__all__` alone, which is the exact test embodiment failed in #18. Remove the #16 caveat sentence from the exit-code-5 row and leave every other row of the 0-8 table byte-identical (c3/h7). Answering issue #18 with a comment citing the section is part of this task.
- depends on: t1, t2, t3
- covers: c3, h2
- acceptance:
  - a Python API section documents headspace.api as the sole supported import surface under semver, states headspace.core is private, and shows a minimal example — an external consumer can determine the surface from README plus `__all__` alone, the exact test embodiment failed in #18
  - the exit-code-5 row caveat referencing #16 is removed; every other row of the 0-8 table and the documented CLI contract are byte-identical
  - issue #18 is answered citing the new section

### t5 — Delivery verification + release: success-signal checklist, version bump, changelog, PR closure of #16/#18 (files: pyproject.toml, CHANGELOG.md)

- instruction: Release and verification only: pyproject.toml + CHANGELOG.md via the version-bump skill, then the four success signals from c16/h13 run with their output quoted in the delivery summary — the live cancelled pytest node, a python -c import probe of headspace.api, devague deviate --list showing d1-d5 approved, and a grep proving no #16 caveat survives in README.md or tests/. Full suite green with the pre-existing CLI-contract tests unchanged (c3). Close #16 and #18 citing the delivering PR; #15 is already closed by adjudication (2026-07-30).
- depends on: t1, t2, t3, t4
- covers: c1, h1, c9, h9, c13, h10, c14, h11, c15, h12, c16, h13, h7
- acceptance:
  - all four success signals run green with evidence quoted in the delivery summary: the live cancelled pytest node, a python -c import probe of headspace.api, devague deviate --list showing d1-d5 approved, and a grep proving no #16 caveat remains in README.md or tests/
  - the full suite passes and the pre-existing CLI-contract tests (verbs, result package, exit taxonomy) pass unchanged
  - version bumped with a CHANGELOG entry (CI version-check green); issues #16 and #18 closed citing the delivering PR; the ledger still shows d1-d5 approved (#15 closed by adjudication on 2026-07-30)

### t6 — Two-phase evidence upgrade (dep t1, single file: headspace/providers/docker.py + its unit tests): intent marker exec'd pre-signal, signalled marker via the anchor post-signal, run honors only a signalled marker naming its job; full marker lifecycle

- instruction: Depends on t1, same file (docker.py) — this is why it is a separate wave, not a parallel task. Upgrade the single sentinel to two-phase evidence (c23): an intent marker exec'd before the signal, a signalled marker written via the anchor (which outlives the job) after the signal lands, and run honoring ONLY a signalled marker naming the job it just ran. The direction of the residual matters more than closing it: partial, missing or tampered evidence must classify exactly as pre-fix code, so the irreducible window misreports a stopped job as failure and never fabricates a cancellation. Implement the full marker lifecycle including removal on the exit-0 path (a stop racing a job that finished naturally leaves a marker no non-zero branch would consume) and staleness rejection by `job_id`. Document the residual window and its direction in stop's docstring.
- depends on: t1
- covers: c23, c17, h14
- acceptance:
  - stop writes the intent marker into the reserved namespace before signalling and the signalled marker via the anchor after the signal lands; run classifies cancelled only on a signalled marker naming the job it just ran — intent-only, missing, or tampered evidence classifies exactly as pre-fix code
  - markers are removed on every classification path including exit 0; a stale marker naming a previous job never flips a later job's classification; a crashed run's leftovers are cleaned by reconciliation
  - stop stays lock-free and store-free with both marker writes (inertness proofs pass unchanged), and its docstring documents the residual ms window and its fail-toward-failure direction

### t7 — Adversarial and edge live-test wave (dep t2, t6; files: tests/ live suite): tamper, SIGKILL, intent-only, coincident stop+timeout, `job_id` leak probe, symlink probe

- instruction: Live-engine tests only, depends on t2 and t6. Six probes: a SIGTERM handler that deletes the markers (must record failure/137, never cancelled); a job ended via the SIGKILL escalation (must record cancelled); an intent-only marker under a naturally failing job (stays failure); a job both operator-stopped and past its wall-clock budget in one window (cancelled, per c19); a job dumping env/hostname//proc surfaces (`job_id` appears in none — h17); a pre-planted symlink at the marker path (write refused, classification stays failure — t14's probe shape, h15). The posture being proven is asymmetric: tampering can push a record toward failure, never toward cancelled. Same d3 constraint as t2 — single-writer lane, skip cleanly without a socket.
- depends on: t2, t6
- covers: c24, h15, h18, h19
- acceptance:
  - a job whose SIGTERM handler deletes the markers is recorded failure/137, never cancelled; a job ended by the SIGKILL escalation is recorded cancelled — both live against a real engine
  - an intent-only marker under a naturally failing job stays failure; a job both operator-stopped and past its wall-clock budget in the same window is recorded cancelled per the documented precedence (c19, h16)
  - a job dumping env, hostname and /proc surfaces never sees its `job_id` (h17); a pre-planted symlink at the marker path refuses the write and classification stays failure — t14's probe shape
  - all new live tests skip cleanly without a Docker socket and run inside the single-writer integration lane

## Risks

- [follow_up] fake provider parity: the fake pins the cancelled contract only via authored JobPlan; making fake.stop structurally flip an in-flight job's outcome to cancelled would prove the semantics on both backends — deliberate follow-up, not in this delivery
- [unknown_nonblocking] the new live test inherits the d3 constraint: this repo's live-engine tests use deterministic object names and are single-writer against one daemon; the namespacing-vs-documenting decision is still parked on the frame (task t2)
