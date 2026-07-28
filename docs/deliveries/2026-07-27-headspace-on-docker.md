# Delivery Summary — headspace on docker

plan: `headspace-on-docker` · run: `complete` · date: `2026-07-27`
baseline: `devague summary skeleton`

## Intent

Build headspace-cli from a scaffold into a working product: an ephemeral
computational workspace an agent can delegate execution into, receiving back a
compact evidence-bearing result instead of a raw execution transcript. The run
executed the converged plan's fourteen tasks across five dependency waves,
fanned out to parallel agents in isolated git worktrees, each merge gated on
tests passing before and after.

## Planned Work

Quoted verbatim from the `devague summary` skeleton:

- `t1` — Lifecycle state machine — states enum and transition table in headspace/core/states.py
- `t2` — Result package schema and dual renderers (markdown default, JSON) with render-time byte bound in headspace/core/result.py
- `t3` — Policy and budget model with enforced-vs-measured effective-policy view in headspace/core/policy.py
- `t4` — State store under ~/.headspace with schema_version fail-closed and per-workspace advisory lock in headspace/core/store.py
- `t5` — Runtime profiles resolving to digest-pinned images in headspace/core/profiles.py
- `t6` — Artifact inventory and atomic digest-verified export in headspace/core/artifacts.py
- `t7` — Failure-taxonomy exit codes in the reserved 3+ band plus learn output update in headspace/cli/_errors.py
- `t8` — Provider interface plus in-memory fake and conformance suite skeleton in headspace/providers/
- `t9` — Docker provider — SDK client, ownership labels, closed-by-default creation, capability probe, capture-time log caps in headspace/providers/docker.py
- `t10` — Workspace orchestration — lifecycle flows, intent journal, orphan reconciliation, destroy guard in headspace/core/workspace.py
- `t11` — CLI verbs create, run, inspect, export, destroy via the register(sub) extension point
- `t12` — Docker integration suite — isolation, limits, crash recovery, flooding, concurrency against a live engine
- `t13` — End-to-end MVP success test plus section 11 traceability table
- `t14` — Packaging and docs — docker dependency pin, README rewrite, explain catalog for new verbs, version bump

One task was added mid-run and is not in the plan: **`t9b`** — artifact
read-back across the provider seam (deviation `d6`, issue #3).

## Actual Delivery

| Plan task | Status | What actually landed |
|-----------|--------|----------------------|
| `t1` | delivered | `headspace/core/states.py` — nine-state enum + explicit transition table; merge `b063962`. Amended in wave 2 by `d4` (added the `running -> ready` edge). |
| `t2` | delivered | `headspace/core/result.py` — nine-section `ResultPackage`, markdown + JSON renderers from one structure, 8 KiB default bound; merge `37ec7b6` |
| `t3` | delivered | `headspace/core/policy.py` — closed-by-default `Policy`, `CapabilitySnapshot`, enforced-vs-measured `EffectivePolicy`; merge `9571c93`. Error type amended by `d1`. |
| `t4` | delivered | `headspace/core/store.py` — `HEADSPACE_HOME`-rooted store, fail-closed `schema_version`, re-entrant per-workspace `flock`, atomic temp-and-rename; merge `202de87` |
| `t5` | delivered | `headspace/core/profiles.py` — digest-pinned `python3.12` profile, construction-time pin validation; merge `3c86c40` |
| `t6` | delivered | `headspace/core/artifacts.py` — `ArtifactRecord`, `ArtifactInventory`, streaming digest-verified atomic export; merge `a1ec077` |
| `t7` | delivered | `headspace/cli/_errors.py` — exit codes 3–7 with `EXIT_CATEGORIES`; merge `ba71e53`. Rendering location amended by `d2`. |
| `t8` | delivered | `headspace/providers/base.py` + `fake.py` + `tests/conformance.py` — runtime-checkable `Provider` Protocol, in-memory fake, backend-neutrality scanner; merge `541d712`. Extended to six verbs by `t9b`. |
| `t9` | delivered | `headspace/providers/docker.py` — SDK client, ownership labels, closed-by-default creation, real capability probe, capture-time log caps; merge `17b4c1c` |
| `t10` | delivered | `headspace/core/workspace.py` — `Orchestrator`, intent journal, crash reconciliation, destroy guard, `d3`'s artifact mapping, `d4`'s `running` edge; merge `9e6990a` |
| `t11` | delivered | five `_commands` modules + registration lines only; merge `05fd8df`. Added `inspect --logs`, a flag `result.py` referenced but nothing implemented. |
| `t12` | delivered | `tests/test_integration_docker.py` — 10 tests, one group per honesty condition h6/h15/h16/h18/h19/h20, orchestrated path against a live engine; merge `1fbaf27` |
| `t13` | delivered | `tests/test_mvp_e2e.py` + `docs/traceability.md`; merge `3b24b89`. Found two product defects (both fixed, see Drift). |
| `t14` | delivered | README rewrite, CHANGELOG, 0.8.0, explain catalog; merge `26a00fa`. Reported a gap it could not fix (`d7`). |
| `t9b` | delivered | *(added mid-run)* sixth Protocol verb `read()`, fake + Docker implementations, conformance coverage, `export` wiring; merge `f12936b` |

## Mid-work Decisions

All seven are approved `devague deviate` records; each is also filed as a
GitHub issue.

- `d1` — `PolicyError` subclasses `CliError` and carries `EXIT_POLICY_DENIED` (3) instead of being a structural look-alike with exit code 1 — t3's brief told it to keep core free of CLI imports while t1 and t5 were told to import `CliError`, so wave 0 shipped two error contracts; `main()` catches `CliError` by name and wraps anything else as "unexpected: file a bug", so a policy refusal would have surfaced as an internal defect. Exit code 1 also contradicted honesty condition h5. (issue #1)
- `d2` — the failure-category tag renders in `emit_error` instead of being prefixed onto `CliError.message` — t7's acceptance criterion required the category to be visible in text output, but its brief scoped `_output.py` out of its file list, leaving message mutation as the only reachable workaround. (issue #1)
- `d3` — the `ArtifactRecord` → `result.Artifact` mapping assigned explicitly to t10 — t2 and t6 independently defined artifact vocabularies that do not line up, and no task owned the translation. (issue #1)
- `d4` — workspaces held at `ready` and never entered `running`; t10 added a `running -> ready` edge so the spec's state became real — t1 built the table as a strict DAG, so `ready -> running -> completed` would exhaust a workspace's lifecycle on its first job, yet FR-07 requires multiple jobs per session. (issue #2)
- `d5` — the docker SDK dependency landed in wave 2 instead of wave 4 — t9 cannot import a package pyproject does not declare, but the plan assigned pinning to t14. (no separate issue; recorded in the ledger)
- `d6` — an artifact read-back verb added to the provider seam — the Protocol shipped with five verbs and no way to read bytes *out* of a workspace, making `export` unimplementable from the CLI. (issue #3)
- `d7` — the agent-facing self-description in `learn` and `overview` rewritten — both still advertised a clonable scaffold and listed none of the five lifecycle verbs. (issue #4)

Two further decisions no deviation record covers:

- Pre-created `headspace/core/__init__.py` before the wave 0 fan-out. Six of seven tasks would each have created it in isolated worktrees and collided six ways at merge — a same-file conflict the dependency graph cannot express, since the tasks are genuinely independent in content. Commit `bf4f9ec`.
- Registered the `integration` pytest marker, clearing an unknown-mark warning and allowing `-m 'not integration'` to deselect the slow live-engine tests.

## Drift From Plan

| Plan item | Reason for divergence | Classification |
|-----------|-----------------------|----------------|
| `t3` (`d1`) | two error contracts shipped in one wave; exit code contradicted h5 | acceptable |
| `t7` (`d2`) | acceptance criterion implied editing a file the brief forbade | acceptable |
| `t10` (`d3`) | no task owned the artifact-vocabulary mapping | needs-follow-up |
| `t8` (`d4`) | strict-DAG table conflicted with FR-07's multi-job requirement | needs-follow-up |
| `t14` (`d5`) | dependency needed two waves earlier than the plan placed it | acceptable |
| `t10` (`d6`) | the five-verb seam was specified without a read-back path; required an unplanned task (`t9b`) | risky |
| `t14` (`d7`) | plan named two of the five places the product describes itself | acceptable |
| `t13` | uncovered two product defects not attributable to any task's contract: the truncation marker rendered an unparseable command (`inspect_path` applied twice), and `destroy` built its result without `artifacts=`, so the report never named surviving artifacts. Both fixed in commit `d3445f6`; the strict-xfail tripwires that caught them were removed once they passed. | acceptable |

**The pattern.** Five of the seven deviations (`d1`, `d3`, `d4`, `d6`, `d7`)
share one cause: **the plan decomposed the work correctly but under-specified a
contract that spans tasks.** Where a shared vocabulary was stated verbatim in
both briefs — the status names given to `t2` and `t7` — the seam composed with
zero rework. Where it was not, it did not compose. This is the run's principal
finding and is recorded in issue #1.

## Evidence

- tests: `uv run pytest -q` — **586 passed**, 0 failed, 0 xfail
- coverage: **95.82 %** line coverage on `headspace/` (gate: 60 %)
- no-daemon lane: `DOCKER_HOST=unix:///nonexistent/docker.sock uv run pytest -q` — 515 passed, 71 skipped, no failures, no hangs
- lint: `black --check`, `isort --check-only`, `flake8`, `bandit -c pyproject.toml -r headspace` — all clean
- rubric: `uv run teken cli doctor . --strict` — healthy, 26/26, 0 errors, 0 warnings
- markdown: `markdownlint-cli2 "**/*.md"` — 0 errors
- engine hygiene: `docker ps -a --filter label=headspace.workspace_id` and `docker volume ls --filter label=headspace.workspace_id` both empty after a complete run
- commits: `b7644a9..d3445f6` (15 merge commits, one per task plus `t9b`)
- issues: #1, #2, #3, #4
- live manual run: `create → run → export → destroy` against Docker 29.1.3 with a fresh `HEADSPACE_HOME`

## Delivery Claims

| Claim | Confidence | Evidence |
|-------|------------|----------|
| An agent can delegate a computation and receive a compact evidence-bearing result | high | live run measured **376×** compression (3,080,009 raw bytes → 8,192 returned); test `tests/test_mvp_e2e.py` measured 210× on a 1.72 MB emission |
| The delegated answer is trustworthy, not merely plausible | high | `tests/test_mvp_e2e.py` recomputes the Collatz/LCG result with a second independent host implementation and compares; live run's `sum_of_squares` matched an independent host computation |
| Artifacts survive the workspace with verified integrity | high | live export digest `d20b9a8b…` equals `sha256sum` of the published file; four-way digest agreement in `tests/test_mvp_e2e.py` |
| Raw logs stay out of the default result but remain retrievable | high | live truncation marker rendered `headspace inspect job-… --logs` and that command ran; `tests/test_result.py::test_default_package_never_carries_raw_logs` |
| Workspaces are network-isolated by default | high | live job's egress attempt returned `OSError`; `tests/test_integration_docker.py` h6 group; `HostConfig.NetworkMode == 'none'` asserted in `tests/test_provider_docker.py` |
| Declared budgets land as real engine limits | high | `tests/test_provider_docker.py` asserts `Memory`, `MemorySwap`, `NanoCpus`, `PidsLimit` from `docker inspect` |
| Unenforceable policy fails closed before any job runs | high | live `create --allow-host-path /etc` → exit 3 with a naming hint; `tests/test_integration_docker.py` h15 group |
| The failure taxonomy is distinguishable | high | live: failing job → exit 6, policy denial → exit 3; `tests/test_exit_codes.py` (26 tests) |
| Unexported artifacts cannot be silently destroyed | high | live `destroy` refused with exit 1 and removed nothing; `--force` discarded with a warning; `tests/test_workspace.py` destroy-guard tests |
| A crash between engine call and state write is recoverable | high | `tests/test_workspace.py` injects a `BaseException` inside `Store.write_state`, then reconciles with a fresh `Orchestrator`; `tests/test_integration_docker.py` h16 does the same against a live engine |
| Result semantics are backend-neutral | high | `tests/conformance.py` runs identically against the fake and the Docker provider; a mechanical scanner rejects backend-specific field names and values |
| The state store cannot be corrupted by concurrent CLI invocations | high | `tests/test_store.py` proves cross-process serialization with real `multiprocessing` children and non-overlapping intervals |
| Cancellation is reachable by a caller | unverified | no `cancel` verb and no signal handling exist — `docs/traceability.md` criterion 10 records this gap; **not claimed done** |
| Retention modes (disposable / time-limited / resumable / durable) work | unverified | only destruction is caller-reachable; `expired` is never set by any code — `docs/traceability.md` criterion 12; **not claimed done** |
| Inputs can be staged into a workspace | unverified | the provider seam has no `write`; `docs/traceability.md` criterion 6; **not claimed done** |
| Workspaces carry purpose, owner, and retention metadata | unverified | none of the three exist in the CLI, `Orchestrator.create`, or the stored record — `docs/traceability.md` criterion 1; **not claimed done** |

## Remaining Work / Follow-up

Section 11 coverage is **9 full, 6 partial, 0 uncovered** — the gaps are named
in `docs/traceability.md` rather than smoothed over.

- **Workspace metadata** (criterion 1) — purpose, owner and retention mode do not exist anywhere in the stack. Needed for FR-01/FR-03 ownership and trace correlation.
- **Input staging** (criterion 6) — the provider seam has no `write` counterpart to `read`. This is the mirror image of `d6`; the same omission on the other direction of data flow.
- **Cancellation** (criterion 10) — the exit code and status vocabulary exist, but nothing produces them: no `cancel` verb, no signal handling.
- **Retention and expiry** (criterion 12) — `expired` is a state no code sets. "Retained for a bounded period" has no implementation.
- **Undeclared intermediate files** (criterion 8) — retrievable through no path at all; `export` refuses an undeclared name and `inspect --logs` returns only captured output.
- **`prog` vs. binary name** — `argparse` self-identifies as `headspace-cli` while the installed console script is `headspace`, so `--help` prints a name that cannot be typed. Deliberately left for a considered decision (issue #4).
- **Parked from the spec, untouched by this run** — package installation flow, secrets injection and redaction, resumable-session snapshot semantics, the rootless/non-Linux enforcement matrix, and the shell-cli integration mode (frame parks `v1`–`v4`, `v7`).
- **shell-cli execution seam** — plan risk `r4`: file the issue describing the seam headspace reserves, now that the provider interface has stabilized.
