# Changelog

All notable changes to this project will be documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/). This project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.11.0] - 2026-07-30

### Added

- **`headspace.api` — the declared Python import surface.** Closes [#18](https://github.com/agentculture/headspace-cli/issues/18), filed by an agent that wanted to depend on headspace-cli as a library and could not tell whether it was allowed to. `headspace.api` exposes exactly five operations — `create`, `run`, `put`, `export`, `destroy` — declared in `__all__`, each a thin wrapper over the same `Orchestrator` entry point the CLI calls, with signatures taken mechanically from `headspace/core/workspace.py` rather than invented (a test walks `inspect.signature` on both sides and fails if they drift). It is covered by the same semver commitment as the CLI; **`headspace.core`, and everything else under `headspace.` that is not `headspace.api`, is private** and may change shape in any release. Importing it is side-effect free — no engine probe, no Docker SDK import at module import time, proved by subprocess tests, so `import headspace.api` works with no daemon anywhere. Every function takes the `provider=` keyword the CLI spells as a flag (`"docker"` by default, `"fake"` for an in-memory backend needing no engine), memoized per process because the fake's workspaces live in that process's memory. `stop` is deliberately absent: it must run in a genuinely separate process from the `run` it interrupts, which makes it CLI-shaped rather than library-shaped.
- **A `Python API` section in README**, which is the surface #18 actually asked for — the bar being that an external consumer can settle the question from README plus `__all__` alone. Every line of its example was executed verbatim against `provider="fake"` with `DOCKER_HOST` pointed at a nonexistent socket before it was committed.
- **A two-phase cancellation channel in the Docker provider**, the mechanism behind the fix below. `stop` writes an *intent* marker (`.headspace-cancel-requested`) into the workspace volume naming the job it is about to end, then — once the signalling has run its course — a *countersignal* (`.headspace-cancelled`) through the anchor container, which outlives the job. `run` classifies `cancelled` from the countersignal alone. The split is the whole point: a single marker had to be written before the signal (a still-blocked `run` classifies the instant its container settles), which made it a claim about the future — a `stop` killed in the gap left a marker naming a job it never ended, and that job failing on its own account would have been recorded as one a human deliberately stopped. Intent alone, neither marker, a marker a job tampered with, or a `stop` that died owing its countersignal all classify exactly as the code before this channel existed. The channel can lose a cancellation's name; it cannot invent one.
- **`RESERVED_ROOT_NAMES`, enforced.** `.headspace-staging` and the two marker names are refused as copy-in destinations, matched on the destination's first segment before a socket is opened. Found by this release's own adversarial test wave: `headspace put <ws> <src> .headspace-cancelled` previously landed a regular file at the countersignal's path and reported success, letting a caller pre-plant the record of an ending that never happened for a job it had not started. `reserved` had been a word three docstrings used and nothing enforced.
- **An adversarial live-test wave against the new channel** — seven probes against a real engine: a job whose SIGTERM handler deletes both markers, a job ended by the SIGKILL escalation, an intent marker with no countersignal (the only test that exercises the settle wait's timeout path), a job both operator-stopped and past its wall-clock budget at once, a job dumping seven surfaces to prove none names its `job_id`, a pre-planted symlink at a marker path plus the channel's recovery afterwards, and a job handed its own id forging a countersignal for itself. The last one records what the guarantee actually rests on rather than asserting something desirable; see [#20](https://github.com/agentculture/headspace-cli/issues/20).

### Changed

- **A job ended by `headspace stop <ws> --apply` on the `docker` provider now exits `5` (`cancelled`) and carries no `exit_status`, where it exited `6` (`computation_failed`) with `exit_status` 137.** As in 0.9.0, this is **not purely additive for a consumer with an explicit branch table on exit code**: that case now lands in a different arm than it used to. Nothing was renumbered and every other code keeps its exact prior meaning.
- **`headspace explain run` and `headspace explain stop` no longer describe #16 as an open gap.** They were updated to document it when it opened, so closing it had to reach the same surface; left alone, the shipped product's own self-teaching text would have contradicted this changelog.
- **`docs/traceability.md` criterion 10 moves from `Partial` to `Full`** — cancellation now separates from computational failure for a job's own record, not only for the verb that ended it. The superseded verdict is preserved in the row per that file's convention rather than deleted. Summary line: 10 full, 5 partial.
- **README's exit-code table, row `5`,** drops the caveat naming #16 and states the property that now holds on every provider: both the `stop` invocation's own result and the job's separately recorded outcome read `cancelled`, with no exit status attached to either. The other eight rows are byte-identical. The lifecycle-verb table's `stop` row was widened for the same reason — its careful silence about the job's own record was written for a gap that no longer exists.
- **The Docker provider's module docstring no longer calls the channel `Unforgeable`.** The property is conditional on the job id's entropy, not on the marker write: a job that is *told* its own id can write a countersignal naming itself and be believed. Headspace mints 48 bits of `uuid4` and nothing inside the container leaks it — tested across seven surfaces at once — but `--job-id` is caller-supplied. Stated at the point of use and filed as [#20](https://github.com/agentculture/headspace-cli/issues/20).

### Fixed

- **[#16](https://github.com/agentculture/headspace-cli/issues/16) — a job ended by `stop --apply` was recorded `failure`/exit 6, telling an operator in the job's own permanent record that their work had failed.** The second half of confirmed claim c43, shipped half-met in 0.10.0 and tracked as deviation d5 of that run, is now met on Docker as well as on the fake. What made it hard is worth keeping: a container the engine killed for `stop` and one that called `sys.exit(137)` leave the engine in byte-for-byte identical states (137, `OOMKilled: false`), so no reading of the engine's own numbers could ever have separated them — and `run` holds the workspace flock for the job's whole duration, so `stop` cannot use headspace's own state to say anything either. The fix is a positive cross-process signal through the only channel the two processes share. The red bar was the repo's own tripwire: the live test written while the bug was open carried the instruction *"If a future change fixes #16, this test will fail — that is deliberate"*, and it fired unedited against the fixed engine before being updated to state the new fact.

## [0.10.0] - 2026-07-29

### Added

- **`headspace put <WORKSPACE> <HOST_PATH> <DESTINATION> [--overwrite]`** — copies a host file or directory into a workspace, recording the destination, size and sha256 in the result package and the input ledger, never the bytes. Refuses an existing destination unless `--overwrite`: an inbound overwrite can destroy workspace-side work a job may never have exported, and `put` writes into a volume other jobs may read, so replacing a destination silently is a mistake this product will not make by default. Closes [#14](https://github.com/agentculture/headspace-cli/issues/14) — `--allow-host-path` correctly fails closed on the Docker provider (it supports no host-path mounts at all), which left a network-disabled workspace with no way to receive its own code; callers had been smuggling payloads through argv (bounded by `ARG_MAX`, and recorded verbatim in four durable places) or standing up a host HTTP server, which itself required `--network enabled`.
- **`run --input NAME=HOST_PATH`** (repeatable, file or directory) — drives the identical copy-in `put` does, before the job starts. `NAME` is the workspace-relative destination; `HOST_PATH` may be a whole directory, so a harness plus its dependencies is one `--input`. Deliberately has no overwrite affordance: replacing an existing destination is a decision that belongs to `put`, not something a job launch makes implicitly. Backed by a new host-side module, `headspace/core/inputs.py`, which expands a host path into a digested manifest and checks it against the workspace's remaining storage budget before any provider is even constructed.
- **`run --env NAME`** (repeatable) — forwards one variable from the caller's own environment into the job, by name. The value is read here and handed to the provider and to nothing else, so it never enters argv and none of the four recording surfaces (`outcome_summary`, `provenance.inputs`, `journal.jsonl`, `state.json`) can pick it up. An unset name is refused rather than forwarded as an empty string, and `--env NAME=VALUE` is refused outright — a value typed into the flag is exactly the leak this flag exists to close, and the refusal does not quote the value back. Closes [#13](https://github.com/agentculture/headspace-cli/issues/13) — before this, argv was the only channel for a secret, and argv is recorded verbatim in all four places above. Stated with both its edges, because a guarantee that overclaims is worse than one that names its limits: a job that prints its own environment writes the value into its captured output, which is kept; and while the job container exists the value is readable from the container's own configuration (`docker inspect` shows it under `Config.Env`), since that is how a process environment is set. Measured live, not assumed. So `--env` keeps a secret out of headspace's durable records — not out of reach of whoever can already talk to your Docker daemon. It is the right choice over argv, which is recorded forever in four places; it is not a secrecy mechanism against the host.
- **`run --env-file PATH`** (repeatable) — reads `NAME=VALUE` lines from a host file and forwards all of them. The grammar is deliberately not a shell's: no quote stripping, no escape processing, no `export` prefix, no whitespace trimming — every one of those conveniences is a guess about where a secret begins and ends, and a guess that is wrong once has silently changed a credential. A malformed line is refused by naming the file and the 1-based line number, and never by quoting the line back, because the malformed line is exactly the kind that holds a secret.
- **`headspace stop <WORKSPACE> [--apply]`** — ends a workspace's in-flight job, previewing by default: without `--apply` it reports what is running and changes nothing — no signal sent, no engine contacted, nothing under `~/.headspace` opened for writing. The inversion from `destroy`'s act-unless-refused default is deliberate — `destroy`'s guard can see what would be lost, but nothing on this path can see how far a running computation had got, so the safe default here produces a report and no effect at all. `--apply` takes no workspace lock and writes no state of its own: `run` holds that lock for a job's whole duration and stays its single writer, so `stop` reaches the engine directly and the blocked `run` invocation observes the ending through the wait it was already doing and journals the outcome itself — the single exception to this codebase's own rule that every verb reconciles at entry. A `stop --apply` that ended a live job reports `cancelled` and exits `5`; a preview, and a `stop` that finds nothing running, exits `0`.
- **Known gap, tracked as [#16](https://github.com/agentculture/headspace-cli/issues/16): a job ended by `stop --apply` is not yet recorded as `cancelled` on the `docker` provider.** `stop`'s own invocation is correct and provably inert without `--apply`, on every backend, and it exits `5` (`cancelled`) whenever it really ends a job. But the *job's own* recorded outcome is written separately, by the concurrent `run` invocation, from whatever the backend reports — and `DockerProvider._status` has no branch that can produce `cancelled`, so a live job ended this way is recorded as `failure` with exit status `137`, and that `run` invocation exits `6` (`computation_failed`). Verified live against Docker 29.1.3, not inferred. The fix needs a cross-process discriminator (something both the `stop` and `run` invocations can see through the engine, since `stop` must not touch `~/.headspace` to set one) and is deliberately not improvised here; see issue #16 for the two candidate shapes considered and why neither is adopted yet. `tests/test_stop_verb.py` pins the contract on the fake provider and records the gap in its own docstring; no test pretends Docker already meets it.

### Changed

- **The lifecycle-verb count, wherever it was stated as five, is now seven.** `headspace/explain/catalog.py`'s root entry and README's "What's in this repository" and "Lifecycle verbs" sections gain `put` and `stop`, in lifecycle order (create, put, run, stop, inspect, export, destroy).
- **`headspace/explain/catalog.py`'s exit-code policy** gains the `8` (`resource_exhausted`) row 0.9.0 added to every other surface but this one, and its `run` entry documents `--input`, `--env` and `--env-file` alongside the flags it already covered, plus the `docker`-provider gap above.
- **README's `headspace/providers/` description**, stale since `write` and `stop` joined the `Provider` protocol ahead of this task, now names all eight methods (`capabilities`, `create`, `run`, `inspect`, `read`, `write`, `stop`, `remove`) instead of the original six.
- **README's exit-code table, row `5`,** now scopes its claim precisely to what the `stop` invocation itself reports (always `cancelled` on every backend) rather than implying the *job's* separately recorded outcome follows suit — which, per the known gap above, it does not yet on `docker` — and names the tracking issue inline.

## [0.9.0] - 2026-07-29

### Added

- **A ninth failure category, `resource_exhausted`, and exit code `8`** — added across all four vocabulary surfaces that mirror each other (`STATUSES`, `JOB_STATUSES`, `_STATUS_EXIT_CODES`, `EXIT_CATEGORIES`/`TAXONOMY_CODES`), with a test that fails if any one of the four is missing. A job killed for exceeding a declared memory ceiling is now a distinguishable outcome instead of being folded into an ordinary failing computation.
- **OOM detection keyed on `State.OOMKilled`, never on exit status 137** — a program can exit 137 deliberately, so the engine's own kill flag is the only sound discriminator between "the kernel killed this for memory" and "this process chose that number." Wall-clock `timeout` keeps precedence over an OOM verdict when both are true.
- **The result package names the enforced memory ceiling and an actionable remedy** on a `resource_exhausted` outcome, and labels `max_memory_bytes` a **sampled floor**, not a peak, when the job was OOM-killed — sampling misses the allocation spike that triggered the kill, so the measured number understates the true peak. The ceiling is never substituted for the measured figure; it gets its own honest home in the key finding.
- **Both providers (`docker` and the in-memory `fake`) are held to the widened taxonomy by the shared conformance suite**, so `resource_exhausted` is a mechanically checked backend-independent property, not a Docker-only special case. The fake gained scripting for both a not-executable command and an OOM kill so the taxonomy is exercisable without a live engine.
- README, `headspace learn`, and `docs/traceability.md` updated to document the ninth status, exit code `8`, and the two reclassified cases below.

### Changed

- **A job killed for exceeding its memory ceiling now exits `8` (`resource_exhausted`), not `6` (`computation_failed`).** It used to be indistinguishable from an ordinary failing computation — same exit code, same bare "the command completed with exit status 137" sentence, warnings and attention both empty. An autonomous caller's only sane move was to rerun the identical job and watch it die identically.
- **A command the image cannot execute now exits `6` (`computation_failed`), not `7` (`infrastructure_failure`).** It used to be reported as `infrastructure_failure` with the hint "the execution engine failed, not the job — check the engine is running and reachable, then retry," which told an autonomous agent to retry a deterministic failure forever. The job's own `exit_status` is now set by shell convention — `127` when the engine says the name isn't there, `126` when it says the name was found but isn't executable. Matching is scoped to the engine's exec-init step only, so every other engine `400` still reports `infrastructure_failure`; this change can only move cases *out* of exit `7`, never into it.
- Exit codes `0`–`7` keep their exact prior meanings — nothing was renumbered, and a test pins the mapping — but this is not a purely additive release for a consumer with an explicit branch table on exit code: the two cases above now land in different arms than they used to. The new status and exit `8` are additive; where those two specific cases land is not.

### Fixed

- **Information disclosure: a raw Docker engine handle no longer reaches a caller.** The failure report for a command the image cannot execute used to carry the container id and the engine's internal `http+docker://` endpoint straight from `docker.errors.APIError` into stdout/stderr — the exact pollution headspace exists to prevent. The report is now built from what headspace already knows (the environment reference and the caller's own `argv[0]`), with the engine's diagnosis appended beneath it, redacted of the transport envelope (e.g. "400 Client Error") and of the container id by exact knowledge of the handle. The engine's original sentence stays retrievable through `headspace inspect <job> --logs` so a misclassification stays diagnosable.

## [0.8.1] - 2026-07-28

### Fixed

- **SonarCloud gate blocker** (`new_reliability_rating` 4 -> A) -- `parse_command` in `headspace/cli/_commands/run.py` now uses the empty-safe slice `argv[:1] == ["--"]` instead of `argv and argv[0] == "--"`. The original was correct (short-circuit evaluation means the index is only reached when the list is non-empty), but rule S6466 has to *prove* that; the slice form is correct without an argument.
- **markdownlint on CI** -- `.markdownlint-cli2.yaml` now ignores `.venv/**` and `**/site-packages/**`. Installed third-party packages ship their own `LICENSE.md` files, which are not ours to reformat. They only appear once dependencies are synced, which is why CI caught this and a local run made before `uv sync` did not.
- **A test that could not survive parallel execution.** `test_reading_creates_no_engine_object_of_its_own` counted engine objects filtered on the label *key*, which spans every headspace object on the daemon — so under `pytest -n auto` (what CI runs) a sibling xdist worker creating or reaping its own workspace moved the number and failed the assertion for reasons unrelated to reading. Now scoped to the workspace under test. Nothing is lost: the provider labels every object it creates with the workspace id, which is the ownership invariant crash reconciliation depends on, so an object created to serve the read would still be counted.
- **README introspection-verb count** -- the prose said five while the table listed six (`cli overview` was uncounted). Reported by review on PR #5.

## [0.8.0] - 2026-07-28

### Added

- **Five lifecycle verbs** — `create`, `run`, `inspect`, `export`, `destroy` — each accepting `--json` and `--provider {docker,fake}`, and each returning the same nine-section result package (outcome summary, status, key findings, evidence, artifacts, warnings, resource usage, provenance, suggested attention). `create` mints a workspace under a declared policy; `run` executes one command in it (`ready -> running -> ready`, so one workspace hosts many jobs); `inspect` reports headspace's and the engine's view side by side, with `--logs` returning captured output in full and unbounded; `export` publishes a *declared* artifact to a durable host path by atomic rename with its digest verified; `destroy` refuses — and removes nothing — when declared artifacts were never exported, unless `--force` names what it is discarding.
- **`headspace/core/`, the backend-neutral engine.** `states.py` defines the nine-state lifecycle (requested/provisioning/ready/running/completed/failed/cancelled/expired/destroyed) as an explicit transition table, with the one deliberate cycle (`ready <-> running`, deviation d4) that lets a single workspace host several jobs while still refusing to destroy one mid-run. `policy.py` resolves a caller's closed-by-default `Policy` (no network, no host paths, small explicit `ResourceBudget` ceilings) against a provider's `CapabilitySnapshot` into an `EffectivePolicy` where every limit is labelled `enforced` or `measured`, and fails closed (`PolicyError`, exit 3) rather than silently weakening a limit the host cannot really back. `store.py` is the CLI-owned `~/.headspace` state store: fail-closed schema versioning, atomic temp-file-then-`os.replace` writes, and a per-workspace `flock` so parallel invocations cannot corrupt state. `profiles.py` resolves a runtime profile name to a digest-pinned image reference (`python3.12` is the only registered profile so far). `artifacts.py` streams an export through a `.partial` sidecar, digesting during the copy and publishing by atomic rename, so a destination path holds either the complete artifact or nothing. `result.py` is the context-return contract itself: the compact nine-section package, bounded to 8 KiB by default, with exactly two renderings (markdown for agents and humans, `--json` for scripts and bots) walked from one shared structure so they can never disagree. `workspace.py` is the `Orchestrator`: journal-before-engine ordering so a crash always leaves a recoverable trace, reconciliation on every verb entry that reports orphans rather than fixing them silently, and the destroy guard that diffs declared artifacts against the export log before anything is journalled.
- **`headspace/providers/`, the six-verb `Provider` protocol** (`capabilities`, `create`, `run`, `inspect`, `read`, `remove`) and two conformance-tested implementations. `FakeProvider` is an in-memory, clock-free backend that walks the real lifecycle table and enforces the real budgets — not a stub — with programmable failure injection (`script_command`, `break_next`) for testing timeout/cancellation/infrastructure-failure paths a real engine cannot be asked to hit on cue. `DockerProvider` is the real backend: a long-lived workspace container holding the closed-by-default posture (network `none` unless enabled, memory/cpu/pids ceilings, `privileged: false`, `no-new-privileges`, no host bind mounts, no devices, never the engine socket) plus one bounded workspace volume, with jobs run in their own containers (not `exec`, so a log-driver `max-size` can cap output and a kill API can enforce wall-clock) and output captured from a live attach stream so truncation is measured against what a job actually produced, not what a rotated log happened to keep.
- **`docker>=7.1,<8`, the package's first runtime dependency** — imported only inside `headspace/providers/docker.py`, so `--help`, `--provider fake`, and the whole unit-test suite still pay none of its import cost on a host with no engine.
- **A failure taxonomy on top of the existing exit codes** — `3` policy_denied, `4` timeout, `5` cancelled, `6` computation_failed, `7` infrastructure_failure — mirroring the result package's `status` vocabulary one to one, so a caller learns *why* a job failed from the exit code alone. `0`/`1`/`2` are unchanged.
- **A shared conformance suite** (`tests/conformance.py`) exercised against both providers, so "backend independence" is a mechanically checked property and not just a claim — growing the test suite to 561 passing tests.

### Changed

- **`README.md` rewritten to describe the shipped product.** It previously described this repo as a bare scaffold ("a clonable template for AgentCulture mesh agents") with "no third-party dependencies" and five introspection verbs — both claims are now false: the CLI has five lifecycle verbs backed by a real engine, `docker` is a runtime dependency, and the `.claude/skills/` kit carries 18 skills, not the 11 last recorded. The rewrite quotes the problem headspace solves from `docs/headspace_cli_issue_requirements.docx` §2, documents the three consumer classes (agents/humans read markdown, scripts/bots read `--json`), states the security posture honestly (what Docker enforces versus merely measures, e.g. storage on the default local volume driver), lists the full 0–7 exit-code table, and includes a create → run → export → destroy walkthrough copied from a real, verified run rather than invented output.
- **`headspace/explain/catalog.py` root entry's opening description** corrected to describe headspace as the ephemeral-computational-workspace product instead of carrying forward the same stale "clonable template" framing the README had. The five lifecycle-verb and five introspection-verb entries underneath were already accurate (added ahead of this task); only the opening paragraph was stale.

## [0.7.0] - 2026-07-28

### Added

- **Converged product spec** at `docs/specs/2026-07-27-headspace-on-docker.md` — the buildable spec for headspace as an ephemeral computational workspace on Docker, worked backwards from `docs/headspace_cli_issue_requirements.docx` via the `/scope` → `/think` → `/challenge` legs. Every requirement carries a confirmed honesty condition (what must be true) and an operator instruction (how to build or verify it). Records the three founding decisions: the docker Python SDK as the first runtime dependency, CLI-owned local state under `~/.headspace` reconciled against engine labels (no daemon), and flat lifecycle verbs (`create`, `run`, `inspect`, `export`, `destroy`). Audience is three consumer classes with distinct renderings — agents read the markdown default, humans read markdown (interactive/HTML view parked), scripts read `--json`.
- **Buildable plan** at `docs/plans/2026-07-27-headspace-on-docker.md` — 14 confirmed tasks covering all 42 spec targets, resolving to five dependency waves whose same-wave tasks touch disjoint files so parallel fan-out merges cleanly. Each task carries TDD-phrased acceptance criteria (the merge contract) plus an operator instruction citing the spec instruction it implements. Four plan risks are first-class state: the rootless/non-Linux enforcement matrix, docker SDK versus engine API drift, Docker-in-CI flakiness, and the follow-up to file the shell-cli execution-seam issue.
- **Frame and plan state** under `.devague/` — the durable evidence trail behind both artifacts: 20 scope entries (12 from the scope survey, 8 from the challenge pass), 30 claims, 22 honesty conditions, 8 parked unknowns, and the resolved questions. Kept with the exported docs so every confirmed claim traces back to the surface it came from.
- **Source requirements document** at `docs/headspace_cli_issue_requirements.docx` — the authoritative product-concept doc the spec cites by section (lifecycle in §6, the context-return contract in §8, MVP acceptance in §11).

### Changed

- `.gitignore` excludes `.devague/questions/` and `.devague/reviews/` — devague working state that is regenerated per run, unlike the frame and plan state which are committed.

## [0.6.1] - 2026-07-20

### Added

- **Worktree location convention** in `CLAUDE.md` — every worktree you create
  by hand (workforce fan-out lanes, scratch checkouts) lives in
  `../.worktrees.headspace-cli/<name>/`, one
  repo-named directory beside the checkout, replacing a shared `../worktrees/`
  folder. This workspace holds many sibling projects, so a generic shared
  folder accumulates orphaned trees from several repos at once with nothing
  indicating ownership — a stale-tree sweep can't tell a live lane from junk.
  Matches the convention already documented in sibling repo `reachy-mini-cli`.
  Adds branch-prefix guidance (scope the prefix to the work; plain `agent/*`
  collides with leftovers from earlier fan-outs and fails `git worktree add
  -b`), and notes that the vendored `assign-to-workforce` skill uses both the
  shared path *and* `agent/<task-id>` branches in its fan-out example — it is
  cited verbatim and must not be edited, so both are overridden when following
  it. Teardown guidance names `git worktree remove <path>` as the verb that
  actually deletes a worktree; `git worktree prune` only clears metadata for
  directories that are already gone. Tool-managed throwaways are explicitly
  out of scope: `ask-colleague`'s read-only verbs create a detached worktree
  under `${TMPDIR:-/tmp}` and reap it on an EXIT trap, so they never persist
  to need an owner.

## [0.6.0] - 2026-07-18

### Added

- **Four devague-origin skills re-vendored into `.claude/skills/`**
  (cite-don't-import), synced to the fixed devague source
  (devague#74/#75/#76):
  - `challenge` — a risk-scaled blind-spot discovery pass that runs between
    `/think` and `/spec-to-plan`, routing findings back through the existing
    deterministic moves as human-adjudicated proposals.
  - `scope` — the idea→scope leg that surveys the surfaces an idea touches
    before framing, seeding the Announcement Frame with provenance-backed
    boundary/non-goal/assumption claims.
  - `deviate` — stops an in-flight `assign-to-workforce` run when execution
    must diverge from the confirmed plan and records the divergence as a
    first-class, append-only deviation record.
  - `summarize-delivery` — closes the loop after an `assign-to-workforce`
    run with a planned-vs-actual accountability artifact.

  These four originate in `devague` and are re-broadcast via guildmaster; see
  `docs/skill-sources.md` for provenance.

## [0.5.0] - 2026-06-24

### Added

- **Memory-discipline "Conventions and workflow" section in `CLAUDE.md`** — a
  per-task *recall-before / remember-after* convention (scope localized to this
  repo's nick) so the vendored `remember` / `recall` skills are actually used,
  not just present: `/recall` before non-trivial work to build on prior
  decisions instead of re-deriving them, and `/remember` when a non-obvious
  decision, constraint, fix-and-why, or hard-won gotcha surfaces. The section
  documents this repo's memory as **in-repo and public** — records resolve to
  `<repo-root>/.eidetic/memory` (committed, team- and mesh-shared). Inserted
  idempotently (skipped if already present), slotted under an existing
  "Conventions and workflow" heading when one exists, else appended.

### Changed

- **Refreshed the `remember` + `recall` wrappers from eidetic-cli 0.10.0**
  (cite-don't-import) — picks up eidetic's **project-local store default**: the
  files backend now resolves per record by visibility — PUBLIC records inside a
  git repo go to `<repo-root>/.eidetic/memory` (committed, team-shared), PRIVATE
  records (or any record outside a repo) go to `$HOME/.eidetic/memory` (never
  committed), an explicit `EIDETIC_DATA_DIR` still wins, and recall reads both
  stores and merges. Also carries the 0.9.3 hardening (interactive-stdin guard,
  `help` as a search term, SIGPIPE-safe suffix parsing). **Recipe policy
  override (the wrappers here are NOT byte-verbatim):** the injected default
  visibility is flipped from eidetic's `private` to **`public`**, so a plain
  `/remember` lands the note in `./.eidetic/memory` in this repo, kept as part
  of the repo — pass `--visibility private` to route a record to `$HOME`
  instead. `remember` drives `eidetic remember` (idempotent upsert of one JSON
  record or an NDJSON batch on stdin); `recall` drives `eidetic recall` with
  four search modes (exact / approximate / keyword / hybrid). Each `SKILL.md` is
  localized only in the illustrative `--scope <nick>` examples (Provenance keeps
  "First-party to eidetic-cli"). Runtime dep: the `eidetic` CLI on PATH (else a
  local eidetic-cli checkout with `uv`) — **`eidetic >= 0.10.0`** for the
  in-repo routing; on an older CLI the public records still work but are stored
  in `$HOME/.eidetic/memory` instead of in-repo. Propagated by rollout-cli's
  `eidetic-memory` recipe.

## [0.4.0] - 2026-06-23

### Added

- **Vendored the `remember` + `recall` memory skills from eidetic-cli**
  (cite-don't-import) — the write/read halves of eidetic's shared
  `$HOME/.eidetic/memory` surface, so this agent (Claude and its colleague
  backend) can persist facts across sessions and recall them later, sharing
  one store.
  `remember` drives `eidetic remember` (idempotent upsert of one JSON record or
  an NDJSON batch on stdin, dedup by id + content hash); `recall` drives
  `eidetic recall` with four search modes — exact / approximate / keyword /
  hybrid — each hit carrying text, full provenance metadata, a relevance score,
  and a freshness signal. The `.sh` wrappers are byte-verbatim from eidetic-cli
  (their first-party origin); each `SKILL.md` is localized only in the
  illustrative `--scope <nick>` examples (Provenance keeps "First-party to
  eidetic-cli"). Both default to this agent's PRIVATE scope, reading the suffix
  from `culture.yaml`. Runtime dep: the `eidetic` CLI on PATH (else a local
  eidetic-cli checkout with `uv`). Propagated by rollout-cli's `eidetic-memory`
  recipe.

## [0.3.4] - 2026-06-20

### Fixed

- Identity docs and self-description strings still claimed `backend: claude`
  (prompt file `CLAUDE.md`), but this template was promoted to a colleague
  resident in #14/#15: `culture.yaml` declares `backend: colleague` (Qwen) with
  `AGENTS.colleague.md` as the resident prompt. Corrected the stale claim in
  `CLAUDE.md` (Identity section), `README.md`, `docs/skill-sources.md`, and the
  two CLI description strings (`overview` artifacts and `explain doctor`). The
  `doctor` backend→prompt-file mapping and the tests were already on
  `colleague`; this aligns the prose and self-description with them.

## [0.3.3] - 2026-06-20

### Fixed

- pyproject.toml: correct the `license` field and PyPI classifier from MIT to
  Apache-2.0 to match the `LICENSE` file. The README License section was already
  corrected in 0.3.2, but the package metadata was missed; the built wheel now
  reports `License-Expression: Apache-2.0`.

## [0.3.2] - 2026-06-18

### Added

- ask-colleague skill: `monitor`/`guide`/`stop` pilot verbs plus a `--watch`
  flag to dispatch, watch the live feed of, send mid-flight guidance to, and
  cooperatively stop a running colleague flight (re-vendored from colleague).

### Changed

- README: correct the License section from MIT to Apache 2.0 to match the
  `LICENSE` file.

## [0.3.1] - 2026-06-13

### Changed

- CLAUDE.md: add a convention to reach for the `ask-colleague` skill reflexively
  for explore/review/write/grade — read-only `review`/`explore` are always safe;
  side-effecting `write` needs the user's go-ahead.

## [0.3.0] - 2026-06-13

### Added

- AGENTS.colleague.md resident prompt file (backend colleague <-> AGENTS.colleague.md)

### Changed

- Promote agent identity to a colleague resident: culture.yaml backend
  claude -> colleague with a pinned model. The `doctor` backend-consistency
  map gains `colleague` -> AGENTS.colleague.md.

## [0.2.1] - 2026-06-12

### Changed

- **Re-vendored the `ask-colleague` skill from colleague (now 1.7.0, up from the
  0.39.2 sync)** — the wrapper had drifted multiple releases behind origin. Picks
  up the `clean` verb (reap stale/corrupt `colleague/*` branches + orphaned
  `.colleague/` artifacts a crashed run left behind), the `--json` flag on every
  verb (result JSON on stdout, diagnostics/digest on stderr), the
  `_colleague_via_uv` local-dev resolution that honors `--repo`, and the
  tri-state (0/1/2) exit-code contract. `scripts/ask-colleague.sh` + `prompts/`
  are byte-identical to the origin; `SKILL.md` diverges only in the one
  consumer-identifying Provenance clause (`headspace-cli vendors from
  guildmaster`). `docs/skill-sources.md` sync row updated to
  `2026-06-12 (colleague 1.7.0, direct)`. Refs: colleague#183, #186.

## [0.2.0] - 2026-06-06

### Added

- **`ask-colleague` skill** (`.claude/skills/ask-colleague/`) — the first-party front door to the `colleague` CLI (the renamed `convertible`). On top of `explore` / `review` / `write` it adds a `feedback` verb (grade a finished work item — the ROI loop), and `write` now **previews by default** in a throwaway worktree (no side effects) unless `--apply` / `--pr` is given. Reach for it reflexively — `review` for a diverse second opinion on a committed diff before opening a PR, `explore` for a fresh read of an unfamiliar area.

### Changed

- **Replaced the `outsource` skill with `ask-colleague`.** `outsource` was renamed to `ask-colleague` upstream ([colleague#148](https://github.com/agentculture/colleague/pull/148)). Because guildmaster has not re-broadcast the rename yet (its kit still ships the old `outsource`), `ask-colleague` is vendored **directly from the sibling `colleague` checkout** rather than from guildmaster — a tracked local divergence recorded in `docs/skill-sources.md`, parallel to the `agex` → `devex` one. Vendored verbatim except one consumer-identifying clause in the Provenance paragraph.
- **Ledger + CLAUDE.md + `.gitignore`:** point `docs/skill-sources.md` and the CLAUDE.md Skills section at `colleague` / `ask-colleague`, swap the *optional* runtime prerequisite `convertible` → `colleague` (env prefix `CONVERTIBLE_*` → `COLLEAGUE_*`, with the legacy names kept as a deprecated fallback), and gitignore the `.colleague/` run-artifact dir the skill writes (plus the stale `.agex/`).

## [0.1.4] - 2026-05-31

### Added

- **Vendor the `outsource` skill** (`.claude/skills/outsource/`) from
  guildmaster's canonical copy (origin
  [`agentculture/convertible`](https://github.com/agentculture/convertible),
  re-broadcast via guildmaster — guildmaster
  [#51](https://github.com/agentculture/guildmaster/pull/51)). Every agent
  cloned from this template now inherits the ability to hand a scoped task to a
  *different* engine/mind: `explore` (read-only investigation), `review` (a
  diverse second opinion on the committed diff), and `write` (delegate a small
  implementation). `explore`/`review` run isolated in a throwaway `git worktree`;
  `write` refuses a dirty tree. Fulfils
  [#8](https://github.com/agentculture/headspace-cli/issues/8).
- **Ledger + CLAUDE.md:** record `outsource` in `docs/skill-sources.md`
  (origin = convertible, re-broadcast via guildmaster; vendored verbatim — it
  already carries `type: command`) and document its *optional* runtime
  dependency on the `convertible` CLI (the skill exits with an install hint if
  absent, so a clone that never uses it is unaffected).

### Changed

### Fixed

## [0.1.3] - 2026-05-31

### Changed

- Expanded the clone-and-rename instructions in `CLAUDE.md`: added `README.md` to
  the rename targets and a portable `git grep` discovery command so a cloner can
  find every occurrence of the template name (hard-coded in ~100 places across the
  package, including the CLI command files and `_ISSUES_URL` in
  `headspace/cli/__init__.py`) rather than renaming by hand.
- Synced `README.md`'s "Make it your own" checklist with `CLAUDE.md`: it now lists
  `README.md` itself as a rename target and points to `CLAUDE.md`'s discovery
  command as the authoritative procedure, so the two onboarding checklists no
  longer drift.

## [0.1.2] - 2026-05-30

### Changed

- Renamed the PR-lifecycle CLI references `agex` / `agex-cli` to `devex` (same
  tool, new name) across `CLAUDE.md`, `docs/skill-sources.md`, `.gitignore`, and
  the vendored `cicd`, `assign-to-workforce`, and `communicate` skills — the
  `cicd` scripts now invoke `devex pr`.
- Logged the vendored-skill in-place patch as a local divergence in
  `docs/skill-sources.md`; the matching canonical rename is tracked upstream for
  guildmaster in
  [agentculture/guildmaster#48](https://github.com/agentculture/guildmaster/issues/48)
  so a future re-sync reconciles cleanly.
- Aligned the documented `devex` version floor to `>=0.21` across the vendored
  `cicd` `SKILL.md` and `workflow.sh` install hint (were `>=0.1`), matching
  `docs/skill-sources.md` and the `await`-era feature set; flagged upstream on
  guildmaster#48.

### Fixed

- SonarCloud now reports code coverage — added `relative_files = true` to
  `[tool.coverage.run]` so `coverage.xml` emits repo-relative paths that map to
  `sonar.sources=headspace` (absolute / `.venv` paths were dropped
  as unmappable). Mirrors the sibling `convertible` setup.

## [0.1.1] - 2026-05-26

### Changed

- **CI gates on the SonarCloud quality gate**
  ([issue #3](https://github.com/agentculture/headspace-cli/issues/3)) —
  added `sonar.qualitygate.wait=true` to `sonar-project.properties` so a failing
  gate fails the `test` job when `SONAR_TOKEN` is set. Token-less repos and fork
  PRs remain green (the scan step is guarded by `if: env.SONAR_TOKEN != ''`).

## [0.1.0] - 2026-05-26

### Added

- **Onboarded into the AgentCulture mesh** ([issue #1](https://github.com/agentculture/headspace-cli/issues/1)).
- **Agent-first CLI** cited from teken's (`afi-cli`) `python-cli` reference
  (`teken cli cite`) — verbs `whoami`, `learn`, `explain`, `overview`, `doctor`,
  and the `cli` noun group. Runtime is self-contained (`dependencies = []`);
  `teken>=0.8` is a dev dependency only. Passes the seven-bundle agent-first
  rubric (`teken cli doctor . --strict`). `doctor` checks the agent-identity
  invariants (prompt-file-present, backend-consistency, skills-present).
- **Mesh identity**: `culture.yaml` (`suffix: headspace-cli`,
  `backend: claude`) and the matching `CLAUDE.md` prompt file.
- **Canonical guildmaster skill kit** (11 skills) vendored under
  `.claude/skills/` (cite-don't-import): `agent-config`, `assign-to-workforce`,
  `cicd`, `communicate`, `doc-test-alignment`, `pypi-maintainer`, `run-tests`,
  `sonarclaude`, `spec-to-plan`, `think`, `version-bump`. Every `SKILL.md`
  carries `type: command` (load-bearing for the culture/claude backend);
  `cicd` / `communicate` consumer-identifying prose adapted, all script bodies
  verbatim. Provenance in `docs/skill-sources.md`. Three skills (`think`,
  `spec-to-plan`, `assign-to-workforce`) originate in `devague`, re-broadcast
  via guildmaster.
- **Build + deploy baseline**: `pyproject.toml` (hatchling), `tests/` (pytest,
  xdist, coverage), `.github/workflows/{tests,publish}.yml` (CI rubric/lint gate,
  PyPI Trusted Publishing), `.flake8`, `.markdownlint-cli2.yaml`,
  `sonar-project.properties`, and `.claude/skills.local.yaml.example`.

### Changed

### Fixed
