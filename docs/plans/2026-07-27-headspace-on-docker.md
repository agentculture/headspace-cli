# Build Plan — headspace on docker

slug: `headspace-on-docker` · status: `exported` · from frame: `headspace-on-docker`

> headspace-cli ships ephemeral computational workspaces for agents: an agent creates a Docker-backed headspace under explicit resource and access policies, runs jobs that share workspace state, exports named artifacts, and receives a compact evidence-bearing result package while the raw execution transcript stays out of model context

## Tasks

### t1 — Lifecycle state machine — states enum and transition table in headspace/core/states.py

- instruction: write tests first from the transition table in the spec (c2 instruction); keep the enum and table in one module with no engine imports so the fake provider and CLI both consume it
- covers: c2, h1
- acceptance:
  - tests parametrize the full transition matrix, legal and illegal; every one of the nine states is reachable
  - an illegal transition raises CliError with a remediation hint and leaves stored state unchanged

### t2 — Result package schema and dual renderers (markdown default, JSON) with render-time byte bound in headspace/core/result.py

- instruction: build the result dataclass first, then two pure renderers over it (c19 instruction: data-first, never format-then-parse); enforce the byte bound in the renderer with truncation markers naming the inspect verb
- covers: c3, h2, c19, h13
- acceptance:
  - one result structure renders to markdown and JSON carrying identical fields, asserted by a rendering test
  - a multi-megabyte captured output yields a package within the configured byte bound, with truncation markers referencing the inspect path

### t3 — Policy and budget model with enforced-vs-measured effective-policy view in headspace/core/policy.py

- instruction: model limits as declared-vs-effective pairs; the effective view consumes the provider capability snapshot persisted by t9 but this module must not import any provider — keep it pure data
- covers: c21
- acceptance:
  - policy objects declare network, filesystem, and resource budgets; the effective-policy view labels every limit enforced or measured
  - requesting a policy the capability snapshot cannot satisfy raises a policy error before any job runs

### t4 — State store under ~/.headspace with schema_version fail-closed and per-workspace advisory lock in headspace/core/store.py

- instruction: one store module owns all filesystem access under ~/.headspace (c27 instruction: centralize the schema check); os.replace for atomicity, fcntl advisory lock per workspace id; honor a HEADSPACE_HOME env override so tests never touch the real store
- covers: c27, h21, c24
- acceptance:
  - a state file with a future schema_version makes every load fail with the version error and hint — no traceback, no mutation
  - two processes contending for one workspace lock serialize; store writes are atomic via temp-and-rename

### t5 — Runtime profiles resolving to digest-pinned images in headspace/core/profiles.py

- instruction: profiles are data: name maps to an image ref pinned by sha256 digest in one module (c20 instruction); resolution returns the digest without pulling — the provider pulls; ship a python 3.12 default profile
- covers: c20, h14
- acceptance:
  - each profile maps to a digest-pinned image reference; two resolutions of one profile return the same digest until the profile is explicitly updated
  - an unresolvable image fails create explicitly with a policy-grade error, never a silent substitute

### t6 — Artifact inventory and atomic digest-verified export in headspace/core/artifacts.py

- instruction: hash with sha256 while streaming to a .partial path, fsync, verify, os.replace (c23 instruction); inventory entries are plain structures feeding the result package artifact section
- covers: c23, h17
- acceptance:
  - export streams to a partial path, verifies the digest, then renames; interrupting mid-copy leaves no file at the final path
  - the inventory records name, type, size, digest, and retention status for every declared artifact

### t7 — Failure-taxonomy exit codes in the reserved 3+ band plus learn output update in headspace/cli/_errors.py

- instruction: extend the 3+ band in _errors.py with policy-denied, timeout, and infrastructure constants and document them in learn output (c6 instruction); never renumber 0 1 2
- covers: c6, h5
- acceptance:
  - policy denial, timeout, cancellation, computational failure, and infrastructure failure map to distinct documented exit codes; user error stays 1, environment error stays 2
  - each category is distinguishable in text and --json error output and listed in learn

### t8 — Provider interface plus in-memory fake and conformance suite skeleton in headspace/providers/

- instruction: define the provider Protocol from the verbs the workspace layer needs — create, run job, inspect, remove; the conformance suite is a pytest class parametrized by a provider fixture (c4 instruction) so the Docker provider reuses it unchanged
- depends on: t1, t2, t3
- covers: c4, h3
- acceptance:
  - the conformance suite runs green against the fake provider and is parametrized to accept any provider implementation
  - no provider-specific field appears in any structure the interface returns

### t9 — Docker provider — SDK client, ownership labels, closed-by-default creation, capability probe, capture-time log caps in headspace/providers/docker.py

- instruction: docker SDK imports live only in this module; every engine object gets a headspace workspace-id label (c22 instruction); create with network none, memory cpu pids from the budget, log-driver size caps (c25 instruction); probe engine capabilities once and persist the snapshot (c21 instruction)
- depends on: t8, t5
- covers: c4, h3, c11, c21, c22, c25, c26
- acceptance:
  - the conformance suite passes against the Docker provider on a live engine
  - created containers show NetworkMode none, budget-derived memory cpu pids limits, workspace-volume-only binds, no devices, no engine socket — asserted from HostConfig
  - the create-time capability probe persists an enforced-vs-measured snapshot and job containers carry log-driver size caps

### t10 — Workspace orchestration — lifecycle flows, intent journal, orphan reconciliation, destroy guard in headspace/core/workspace.py

- instruction: journal intent to the store before each engine mutation and reconcile labeled orphans at entry (c22 instruction); destroy diffs declared artifacts against the export log and gates on force (c28 instruction); depend only on the provider Protocol, never on docker directly
- depends on: t4, t6, t8
- covers: c22, h16, c24, c28, h22
- acceptance:
  - create, run, inspect, export, and destroy flows validate transitions and pass unit tests against the fake provider
  - a simulated crash between engine call and state write leaves a journaled orphan the next invocation reconciles and reports in attention items
  - destroy with unexported declared artifacts refuses without force and removes nothing; destroy during an active job takes the documented path

### t11 — CLI verbs create, run, inspect, export, destroy via the register(sub) extension point

- instruction: one module per verb under _commands following the register(sub) pattern (c5 instruction); render through the t2 result renderers; exit codes from t7; run the strict rubric gate before finishing
- depends on: t10, t7
- covers: c5, h4, c7, h8
- acceptance:
  - five _commands modules register without editing _dispatch, _output, or the parser plumbing; every verb honors --json
  - uv run teken cli doctor . --strict passes with the new verbs registered

### t12 — Docker integration suite — isolation, limits, crash recovery, flooding, concurrency against a live engine

- instruction: mark these tests integration and skip cleanly when no engine socket is present (c12: unit coverage never needs a daemon); each of h6 h15 h16 h18 h19 h20 gets a named test
- depends on: t9, t10
- covers: h6, h15, h16, h18, h19, h20
- acceptance:
  - egress from a default workspace fails; declared budgets appear in docker inspect; unsupported policy fails before any job runs
  - the kill-based orphan test reconciles and reports; an interrupted export leaves no final-path file; a gigabyte-writer job leaves only bounded capture with a truncation warning
  - parallel verbs against one workspace never corrupt state or double-start; destroy-during-run follows the documented path

### t13 — End-to-end MVP success test plus section 11 traceability table

- instruction: script the doc section 11 MVP success test literally (c1 instruction); the traceability table lists criterion, test id or documented manual check, and status (c17 instruction) — no criterion waved through
- depends on: t11, t12
- covers: c1, h7, c16, h11, c17, h12
- acceptance:
  - the scripted delegation creates a workspace, runs a noisy artifact-producing job, asserts a bounded evidence-bearing package, recovers the artifact by digest, destroys, and checks the disposition report
  - docs/traceability.md maps every section 11 acceptance criterion to a test id or documented manual check and CI runs the automated ones

### t14 — Packaging and docs — docker dependency pin, README rewrite, explain catalog for new verbs, version bump

- instruction: pin the docker SDK with a compatible-release specifier probed against engine 29.1.3 (c30); README keeps the agent-first quickstart shape; use the version-bump skill so pyproject and CHANGELOG move together (CI blocks otherwise)
- depends on: t11
- covers: c15, h10, c7
- acceptance:
  - pyproject pins the docker SDK; README replaces the zero-dependency claim, documents the new verbs, and quotes the doc section 2 problem statement as the before-state
  - explain covers every new verb path and markdownlint plus the rubric gate stay green

## Risks

- [unknown_nonblocking] rootless or non-Linux Docker hosts may not enforce cgroup limits; the integration suite targets rootful Linux only (frame v7) — enforced-vs-measured reporting is the mitigation
- [unknown_nonblocking] docker SDK release cadence versus engine API drift — SDK pinned (7.x probed against engine 29.1.3); the conformance suite is the canary
- [unknown_nonblocking] Docker-in-CI flakiness and latency for the integration suite — may need a dedicated job, retries, or a nightly split; unit coverage stays daemon-free by design
- [follow_up] file the shell-cli issue describing the execution seam headspace reserves (frame decision c18) once the provider interface stabilizes in t8
