# headspace on docker

> headspace-cli ships ephemeral computational workspaces for agents: an agent creates a Docker-backed headspace under explicit resource and access policies, runs jobs that share workspace state, exports named artifacts, and receives a compact evidence-bearing result package while the raw execution transcript stays out of model context
> instruction: verify by scripting the doc section 11 MVP success test as an end-to-end pytest: delegate a computation that produces noisy output plus an artifact, assert the returned package is bounded and evidence-bearing, then recover the artifact by digest

## Audience

- three consumer classes with distinct renderings: AI agents consume the default markdown text output, humans consume markdown now with an interactive or HTML webserver view as a possible later mode, and scripts or non-AI bots consume --json — user decision refining the interface model
  - instruction: render data-first: build each result as a structure once, then render it as markdown (default) or JSON (--json); never format-then-parse; keep the interactive/HTML mode parked as v5

## Before → After

- Before: agents run computation through generic shells or ad hoc sandboxes: execution output floods model context, temporary state has no owner, security and provenance are implicit, and sandbox lifecycle logic is reimplemented per runtime (doc section 2 problem statement)
  - instruction: quote the doc section 2 problem bullets in the spec introduction so implementers see exactly what the workspace abstraction must eliminate — context flooding, unowned temp state, implicit security, per-runtime sandbox logic
- After: an agent delegates a non-trivial computation to a Docker-backed headspace and receives a compact evidence-bearing result package plus named artifacts, with the noisy working process staying outside its context — the doc section 11 MVP success test, passed literally
  - instruction: implement the MVP success test as an end-to-end pytest: create a headspace, run a noisy computation that produces an artifact, assert the result package is bounded and evidence-bearing, recover the artifact by digest, destroy, assert the disposition report

## Requirements

- explicit lifecycle with normalized states (requested, provisioning, ready, running, completed, failed, cancelled, expired, destroyed) per docs/headspace_cli_issue_requirements.docx section 6; state transitions observable, invalid transitions fail clearly
  - instruction: model the lifecycle as an explicit state enum plus transition table in one module; every verb validates the transition before touching Docker; tests parametrize the full transition matrix, legal and illegal
  - honesty: every lifecycle state and legal transition is exercised in tests via a fake provider, and an invalid transition raises CliError with a remediation hint without mutating stored state
- default result is a compact context-return package (outcome summary, normalized status, key findings, evidence, artifact inventory, warnings, resource usage, provenance, attention items) per doc section 8; raw logs stay inspectable via a separate path and never enter the default result (FR-11, FR-16, NFR-03)
  - instruction: define the result package as a typed schema with one renderer per consumer class; enforce the byte bound at render time with truncation markers pointing at the inspect path
  - honesty: for a job emitting megabytes of stdout, the default result package stays within a configurable byte bound and carries references instead of the transcript; a separate inspect path retrieves full logs on request
- Docker is the first conformance backend (user decision; resolves doc open decision 1) behind a provider abstraction so lifecycle and result semantics stay backend-neutral (FR-25, FR-26); host verified with Docker 29.1.3 plus seccomp, apparmor, cgroupns
  - instruction: implement a provider interface (create, exec, inspect, remove) with two implementations — docker SDK and in-memory fake — and a shared conformance suite parametrized over both; the Docker one uses labels for ownership and reconciliation
  - honesty: no Docker-specific field or string appears in the primary result schema: swapping the Docker provider for the in-memory fake yields the identical result structure, verified by a conformance test both providers pass
- new workspace noun groups register via the existing register(sub) extension point in headspace/cli/__init__.py _build_parser and every verb honors --json (FR-27 machine interface lands on the existing contract)
  - instruction: one _commands module per workspace verb following the register(sub) pattern; cli/__init__.py changes only by the registration lines the template comment reserves
  - honesty: new workspace verbs register without editing _dispatch, _output, or _errors, and teken cli doctor . --strict stays green after registration
- all new verbs keep the stable contract in headspace/cli/_errors.py and _output.py: results to stdout, errors to stderr as CliError {code, message, remediation}, exit codes 0/1/2 with 3+ reserved — the reserved band is where FR-12 failure taxonomy (policy denial, timeout, infra failure) can land
  - instruction: extend _errors.py with distinct exit codes in the reserved 3+ band for policy denial, timeout, and infrastructure failure; mirror each category in the result package status field and document them in learn output
  - honesty: policy denial, timeout, cancellation, computational failure, and infrastructure failure are distinguishable in both text and --json output, each mapped to a documented exit code with user error staying exit 1 and environment error exit 2
- security by default per NFR-02: containers start network-disabled with explicit resource limits and a bounded workspace mount; access expands only through declared policy, and unsupported policy is rejected rather than silently degraded (NFR-09, FR-05, FR-06)
  - instruction: create containers with network disabled, memory/cpu/pids limits derived from the declared budget, a single bounded workspace volume, and no host mounts; every policy expansion is an explicit create flag recorded in provenance
  - honesty: an integration test attempts network egress from a default headspace and fails, and the declared budget lands as container resource limits observable via docker inspect; requesting an unsupported policy fails before any job runs
- runtime profiles resolve to Docker images pinned by digest; the digest actually used is recorded in job provenance and create fails explicitly when the image cannot be resolved — image pull happens host-side so job-level network stays closed (FR-19, FR-20, doc section 13 non-reproducible-environments risk)
  - instruction: map each profile to a digest-pinned image reference in one module; resolve and pull at create; stamp the resolved digest into workspace state and every result package
  - honesty: two creates from the same profile record the same image digest unless the profile is explicitly updated, and every completed job result carries the digest actually used
- create probes engine capabilities (API version, cgroup controllers for memory, cpu, pids) and the effective-policy report distinguishes enforced from measured-only limits; a budget the host cannot enforce fails closed or degrades explicitly per NFR-09 — storage on the default local volume driver is measured-only and must be reported as such (FR-30, doc section 13 silent-weakening risk)
  - instruction: probe once at create via the SDK version and info handshake; persist the capability snapshot in workspace state; render enforced or measured per limit in the policy view
  - honesty: on a host that cannot enforce a requested memory limit, create fails with a policy error rather than running unlimited; the effective-policy output labels every limit enforced or measured, with storage showing measured on the local driver
- every engine object carries a headspace ownership label and state mutations are crash-consistent: a CLI killed between an engine call and the state write leaves a reconcilable orphan, and lazy reconciliation on any invocation adopts or reaps labeled orphans and reports the disposition (NFR-07: infrastructure failure never masquerades as computational failure)
  - instruction: write an intent journal entry before engine mutations, label every container and volume with the workspace id, and run label-based reconciliation at CLI start against the state store
  - honesty: killing the CLI between container create and state write leaves no permanent leak: the next invocation detects the labeled orphan, reconciles it, and reports it in attention items
- artifact export is atomic and digest-verified end to end: content is staged to a temporary path, its digest computed and checked, then renamed into place — an interrupted export never leaves a partial file presented as a completed artifact (FR-14, FR-15, NFR-05)
  - instruction: stream from the workspace volume to a .partial path, hash while streaming, verify, then rename; record digest and size in the artifact inventory
  - honesty: interrupting an export mid-copy leaves no destination file at the final path; a completed export always matches its recorded digest
- concurrent CLI invocations against one workspace serialize through a per-workspace lock; destroy while a job runs either cancels it first or refuses with a clear error — interleaved verbs can never corrupt the state store or double-start a job
  - instruction: use an advisory file lock per workspace id in the state directory; take it in every mutating verb; document and test the destroy-during-run path
  - honesty: parallel runs against a single workspace never corrupt state or double-start; destroy during an active job takes the documented path (cancel-then-destroy or refuse) — exercised by a concurrency test
- job stdout and stderr are bounded at capture time via engine log options and streaming caps, not only at render time — a job emitting unbounded output cannot exhaust host disk or bloat the state store; overflow is recorded as a truncation event in warnings (FR-16, FR-05 output volume)
  - instruction: set log driver max-size and max-file on every job container and cap the SDK log stream read; record truncation as an attention item
  - honesty: a job writing a gigabyte to stdout leaves only the configured bounded capture on disk with a truncation marker, and the overflow surfaces in the result package warnings
- the state store carries a schema_version from day one; an unknown or future version fails closed with a migration hint instead of reinterpreting or overwriting state (doc section 13 reproducibility risk)
  - instruction: stamp schema_version in every state file; check it on load before any parse; centralize the check in the store module
  - honesty: pointing the CLI at a state file with a future schema_version makes every verb exit with the version error and remediation hint — no traceback, no mutation
- destroy on a workspace holding declared but never-exported artifacts refuses with the artifact inventory and requires an explicit force flag; the destruction report states what was removed, what was retained elsewhere, and what could not be verified (doc section 11 destruction criterion — data-loss guard)
  - instruction: diff declared artifacts against the export log at destroy time; gate on the diff; render the disposition report from the same data in markdown and json
  - honesty: destroying a workspace with an unexported declared artifact and no force flag exits non-zero and removes nothing; with force it proceeds and the report lists the discarded artifacts by name and digest

## Honesty conditions

- the full loop runs against real Docker with no backend detail entering the model context beyond what the result package deliberately carries: create, run, inspect, export, destroy, each emitting bounded structured output
- the branch adding workspace verbs runs uv run teken cli doctor . --strict in CI and it passes — the rubric gate is exercised, not assumed
- the spec claims no problem beyond what doc section 2 records — every pain point in the before-state cites the requirements doc, none is invented
- the after-state is executable, not aspirational: a scripted delegation returns a bounded result package and recoverable artifacts with zero backend knowledge required of the caller
- each section 11 acceptance criterion maps to at least one automated test or documented manual check in the delivered plan — none is waved through
- the same underlying data renders in every mode: default markdown output and --json carry identical fields, asserted by a rendering test; no verb requires an agent to parse human-only formatting
- the conformance suite inspects every MVP job container and asserts no socket mount, no host bind mounts, network none by default, and only the workspace volume writable

## Success signals

- the section 11 acceptance list passes end to end against real Docker: create, run multiple jobs sharing state, inspect, export named artifacts with digests, destroy with a disposition report — default result bounded and far smaller than the raw transcript, CI green (pytest, coverage 60+, afi rubric, version bump)
  - instruction: maintain a traceability table in the plan mapping each section 11 acceptance criterion to a test id or documented manual check; CI runs every automated one

## Scope / boundaries

- the CI agent-first rubric gate (uv run teken cli doctor . --strict in .github/workflows/tests.yml) constrains any new CLI surface: nouns with action verbs must expose overview, errors need hint lines; new noun groups must pass it
  - instruction: run uv run teken cli doctor . --strict locally before each PR; the CI lint job already enforces it
- jobs never receive the engine socket, host filesystem bind mounts, host network, or extra devices; the only writable surface inside a job is the bounded workspace volume — verified per job, not assumed (probe evidence on this host: network none blocked egress, memory and pids limits landed in HostConfig)
  - instruction: assert container HostConfig in the integration suite: binds limited to the workspace volume, NetworkMode none, no devices, no engine socket path in any mount

## Non-goals

- headspace does not replace shells, command runners, container engines, hypervisors, or schedulers, and is not a terminal multiplexer or long-term memory (doc section 4 out-of-scope plus boundary rule); Docker mechanics never leak into the primary result contract
- the mesh-identity scaffold stays intact: culture.yaml, AGENTS.colleague.md, the whoami/learn/explain/overview/doctor verbs and vendored skills are untouched — headspace adds noun groups alongside; re-initializing the seed CLAUDE.md is a separate task

## Assumptions

- MVP equals doc phase 1 (contract plus local MVP): lifecycle, policies, result package, artifact model, provenance, one Docker backend, selective context return, judged against the section 11 acceptance criteria; phases 2-5 (hardening, more backends, ecosystem, snapshots) are out of the first build
- CI can run real Docker integration tests on ubuntu-latest runners while unit tests use a fake provider so the pyproject coverage gate (fail_under 60) never depends on a live daemon; every PR bumps the version per the version-check job in .github/workflows/tests.yml
- the job-execution seam is designed so shell-cli (sibling repo, package shell-cli, command shell, import shell) can slot in later per FR-26, but the MVP does not functionally depend on it: shell-cli README states no operation, environment, policy, or runner has been built yet — all four workspace x runner rows are marked not built
- MVP jobs are synchronous batch commands: run blocks until completion or budget timeout under the CLI process; detached, interactive, and long-running jobs are explicitly out of the MVP (doc open decision on interactive jobs)
- the docker Python SDK is compatible with the installed engine — probed on this host: SDK 7.2.0 against engine 29.1.3 API 1.52, egress blocked under network none, memory and pids limits observable in HostConfig; the SDK version gets pinned and the conformance suite runs against the pin

## Scope exploration

- `s1` — `docs/headspace_cli_issue_requirements.docx section 6 (lifecycle)`: the doc mandates nine explicit lifecycle states with observable transitions and clear failure on invalid ones — the state machine is a first-class deliverable, not an implementation detail
  - seeds: `c2`
- `s2` — `docs/headspace_cli_issue_requirements.docx section 8 (context-return contract)`: the defining feature is the information boundary: a compact evidence-bearing result package by default, raw logs inspectable only on explicit request — FR-11/FR-16/NFR-03 all converge here
  - seeds: `c3`
- `s3` — `host docker engine (docker --version, docker info)`: Docker 29.1.3 present with seccomp, apparmor, and cgroupns security options — the user-chosen first backend is verified available on this host
  - seeds: `c4`
- `s4` — `headspace/cli/__init__.py (_build_parser)`: the scaffold has an explicit extension-point comment for registering new noun groups via register(sub); all existing verbs take --json — new workspace verbs slot in without touching the dispatch/error plumbing
  - seeds: `c5`
- `s5` — `headspace/cli/_errors.py + headspace/cli/_output.py`: stable contract: stdout for results, stderr for CliError {code, message, remediation} with hint lines, exit codes 0/1/2 and 3+ reserved — the reserved band can carry the FR-12 failure taxonomy (policy denial, timeout, infra failure)
  - seeds: `c6`
- `s6` — `.github/workflows/tests.yml (lint job, afi rubric gate)`: CI runs uv run teken cli doctor . --strict plus black/isort/flake8/bandit/markdownlint — any new noun group must satisfy the agent-first rubric (nouns with action verbs expose overview, errors carry hints)
  - seeds: `c7`
- `s7` — `docs/headspace_cli_issue_requirements.docx section 4 + section 15 (boundaries)`: the doc draws hard component boundaries: headspace owns workspace contract and information boundary; execution/isolation belongs to lower layers; agent intent belongs above — explicitly not a VM manager, terminal multiplexer, or memory system
  - seeds: `c8`
- `s8` — `culture.yaml + headspace/cli/_commands/doctor.py + AGENTS.colleague.md`: the repo is a live mesh agent (backend colleague) whose doctor verifies prompt-file and skills invariants — the identity scaffold is working infrastructure the build must leave intact
  - seeds: `c9`
- `s9` — `docs/headspace_cli_issue_requirements.docx sections 11-12 (MVP criteria, phases)`: the doc itself cuts delivery into five phases with phase 1 = contract plus one local isolated backend and selective context return, judged by the section 11 acceptance list — the MVP cut is given, not invented
  - seeds: `c10`
- `s10` — `docs/headspace_cli_issue_requirements.docx section 10 (NFR-02, NFR-09)`: security posture is closed-by-default with explicit policy expansion, and unsupported policy must reject rather than silently degrade — maps to docker run with no network, resource flags, and a scoped workspace mount
  - seeds: `c11`
- `s11` — `.github/workflows/tests.yml (test job) + pyproject.toml (coverage, version)`: CI runs pytest with coverage fail_under 60 on ubuntu-latest (Docker preinstalled on GitHub runners) and a version-check job forces a semver bump per PR — integration tests against real Docker are feasible in CI but unit coverage must not require a daemon
  - seeds: `c12`
- `s12` — `../shell-cli (pyproject.toml, README.md, docs/specs+plans)`: shell-cli is the AgentCulture guarded local operations plane — exactly the doc section 15 shell/command-execution adjacent component; its control profile anticipates invoking the container engine, its per-operation evidence records align with FR-13/FR-19, it is pure-stdlib and trivially preinstallable in a workspace image; but its runners and operation engine are unbuilt today, so headspace can only reserve the seam, not consume it
  - seeds: `c13`
- `s13` — `challenge pass / adjacent-systems lens: Docker image supply + engine capability surface`: the spec never said where runtime-profile images come from or whether the engine can enforce a given budget; probe ran scratch container — SDK 7.2.0, engine 29.1.3, API 1.52 handshake ok; seeded image-digest provenance, capability probing with enforced-vs-measured policy transparency, and the SDK-compat assumption
  - seeds: `c20`, `c21`, `c30`
- `s14` — `challenge pass / failure-mode lens: crash windows in create, run, and export`: state write and engine mutation are two steps with a kill window between them, and export is a multi-step copy — neither had a claim; seeded crash-consistent labeling with lazy reconciliation and atomic digest-verified export
  - seeds: `c22`, `c23`
- `s15` — `challenge pass / concurrency lens: ~/.headspace store + parallel CLI invocations`: the CLI-owned state decision (q2) implies multiple concurrent writers with no locking claim; destroy-during-run was undefined; seeded the per-workspace lock requirement
  - seeds: `c24`
- `s16` — `challenge pass / security lens: job mount surface, engine socket, log flooding`: h2 bounded output only at render time — capture-time flooding could exhaust host disk; the no-socket no-host-mount surface was implied but never claimed; probe verified network none blocks egress and limits land in HostConfig; seeded capture-time bounding and the job-isolation boundary
  - seeds: `c25`, `c26`
- `s17` — `challenge pass / migration lens: state store schema evolution`: ~/.headspace will outlive the first release and had no versioning claim; seeded schema_version fail-closed requirement
  - seeds: `c27`
- `s18` — `challenge pass / reversibility lens: destroy vs unexported artifacts`: destroy was specified as reporting disposition but nothing prevented silently discarding never-exported artifacts; seeded the force-gated data-loss guard
  - seeds: `c28`
- `s19` — `challenge pass / overlooked-actors lens: interactive jobs, long-running jobs, multi-caller workspaces`: the requirements doc lists interactive and long-running jobs as an open decision the frame never routed; multi-caller sharing was silently absent; seeded the synchronous-batch assumption and parked both (v6 follow_up, v8 out_of_scope)
  - seeds: `c29`
- `s20` — `challenge pass / observability and recovery lens: inspect path, effective-policy view`: clean pass — the inspect path (c3) plus attention items cover evidence retrieval, and c21-c22 as seeded now carry policy transparency and orphan reporting; residual: operator-level debugging of engine failures still leans on docker tooling itself, accepted for MVP
  - seeds: `c21`, `c22`

## Decisions

- shell-cli coordination happens through GitHub issues on the shell-cli repo rather than blocking the MVP: either file upfront describing the execution seam headspace needs, or implement first and file an issue sharing learnings — user decision, both paths open, communicate skill carries the post

## Open / follow-up

- interactive or HTML-webserver rendering mode for human operators — a later addition beyond the markdown default; not implied by the MVP
- detached, interactive, or long-running job support — how jobs outlive a CLI invocation without headspace becoming a terminal multiplexer (doc open decision)
- multi-caller shared workspaces — FR-03 ownership is single-caller in MVP; sharing one workspace across agents stays out until a real use case arrives
