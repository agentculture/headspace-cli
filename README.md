# headspace-cli

Headspace is an ephemeral computational workspace for agents. It lets a model
offload execution, experimentation, and calculation into a bounded, isolated
workspace, and gets back a compact, evidence-bearing result instead of a raw
execution transcript.

## The problem

Agent runtimes today externalize computation through a generic shell or an ad
hoc sandbox, which conflates *running a command* with *managing a workspace*
and gives the model little control over what re-enters its context. As
`docs/headspace_cli_issue_requirements.docx` (§2) puts it:

> Agent runtimes need a safe place to externalize computation without turning
> the model context into an execution log. Generic command runners can execute
> work, but they do not provide a task-level abstraction for temporary state,
> artifact retention, provenance, result compression, or selective return to
> the model.

That absence creates recurring, specific problems the same document names:
execution output competes with requirements and reasoning for limited context
space; temporary files and intermediate state have unclear ownership; the
model must interpret raw logs even when only a conclusion or artifact matters;
reproducibility and provenance vary by backend; and security, network access,
and resource budgets are often left implicit rather than declared. Headspace
is the abstraction that closes that gap: a workspace with an explicit
lifecycle, a declared policy, and a result package that carries only what
deserves to re-enter the model's context.

## What's in this repository

- **The CLI** (`headspace` on `PATH` once installed) — five lifecycle verbs
  that create and use a workspace, plus six introspection verbs that
  describe this agent and its CLI surface. Every verb supports `--json`; every lifecycle verb
  supports `--provider {docker,fake}`. See [CLI](#cli) below.
- **`headspace/core/`** — the backend-neutral engine: a nine-state lifecycle
  (`states.py`), closed-by-default policy with an enforced-vs-measured
  effective view (`policy.py`), CLI-owned state under `~/.headspace` with
  fail-closed schema versioning and per-workspace locking (`store.py`),
  digest-pinned runtime profiles (`profiles.py`), atomic digest-verified
  artifact export (`artifacts.py`), the compact context-return result package
  (`result.py`), and orchestration with an intent journal and crash
  reconciliation (`workspace.py`).
- **`headspace/providers/`** — a six-verb `Provider` protocol
  (`capabilities`, `create`, `run`, `inspect`, `read`, `remove`), and two
  implementations that both pass the same conformance suite: an in-memory
  `fake` (no engine required — what makes the whole test suite runnable with
  nothing installed) and a real `docker` provider. `docker>=7.1,<8` is the
  package's one runtime dependency.
- **A mesh identity** — `culture.yaml` (`suffix` + `backend`) and the
  matching resident prompt file (`AGENTS.colleague.md`, since this agent runs
  `backend: colleague`).
- **The canonical guildmaster skill kit** (18 skills) under `.claude/skills/`,
  vendored cite-don't-import. See [`docs/skill-sources.md`](docs/skill-sources.md).
- **A build + deploy baseline** — pytest, lint, the agent-first rubric gate,
  and PyPI Trusted Publishing wired into GitHub Actions.

## Quickstart

```bash
uv sync
uv run pytest -n auto                 # run the test suite; docker-dependent
                                       # tests skip cleanly with no engine
uv run headspace whoami               # identity from culture.yaml
uv run headspace learn                # self-teaching prompt (add --json)
uv run teken cli doctor . --strict    # the agent-first rubric gate CI runs
```

## CLI

### Lifecycle verbs

The five verbs that create and use a workspace. Each returns the same
nine-section result package and takes its process exit code from that
package's status (see [Exit codes](#exit-codes)).

| Verb | What it does |
|------|--------------|
| `create` | Provision an ephemeral workspace under a declared policy; the only verb that mints a workspace id. |
| `run <workspace> <command> ...` | Execute one command in an existing workspace. Flags for `run` go *before* the workspace id — everything after it belongs to the command. |
| `inspect <handle>` | Report a workspace's lifecycle state, the engine's view of it, and what the session has cost. `--logs` returns captured output in full and unbounded. |
| `export <workspace> <name> --to <path>` | Publish a *declared* artifact to a durable host path, digest-verified, by atomic rename. |
| `destroy <workspace>` | Remove a workspace — or refuse, and remove nothing, when declared artifacts were never exported (`--force` to discard them deliberately). |

### Introspection verbs

| Verb | What it does |
|------|--------------|
| `whoami` | Report this agent's nick, version, backend, and model from `culture.yaml`. |
| `learn` | Print a structured self-teaching prompt. |
| `explain <path>` | Markdown docs for any noun/verb path. |
| `overview` | Read-only descriptive snapshot of the agent. |
| `doctor` | Check the agent-identity invariants (prompt-file-present, backend-consistency). |
| `cli overview` | Describe the CLI surface itself. |

Every command supports `--json`. Results go to stdout, errors/diagnostics go
to stderr, never mixed — which is what makes `--json` safe to pipe.

### Worked example: create, run, export, destroy

This is a real, verified run against the `docker` provider (the default
backend; pass `--provider fake` for an in-memory backend that needs no
engine — see [Choosing a backend](#choosing-a-backend)).

```bash
$ uv run headspace create --workspace-id demo
# headspace result
## Outcome summary
workspace demo is ready on docker, running the python3.12 profile
## Status
success
## Key findings
- lifecycle state: ready
- network: disabled
- environment digest: sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de
## Warnings and limitations
- the storage limit is measured, not enforced: reported by the engine, not capped (default local volume driver)
...

$ uv run headspace run --declare report.json="pi to 10 digits, with a sanity check" \
    demo python -c "
import json, math
value = round(math.pi, 10)
check = abs(value - 3.1415926536) < 1e-9
with open('report.json', 'w') as f:
    json.dump({'pi': value, 'check_passed': check}, f)
print(f'pi={value} check_passed={check}')
"
# headspace result
## Outcome summary
job job-9324b5e5a3cd ran python -c ... in workspace demo and reported success
## Status
success
## Evidence
- label: captured output
  - source: headspace inspect job-9324b5e5a3cd --logs
  - excerpt:
    pi=3.1415926536 check_passed=True
## Suggested attention
- artifact 'report.json' is declared but not exported (pi to 10 digits, with
  a sanity check); export it, or a destroy will refuse until you discard it
  with force

$ uv run headspace export demo report.json --to ./report.json
# headspace result
## Outcome summary
artifact 'report.json' left workspace demo and is durable at ./report.json
## Key findings
- 42 bytes verified as sha256:e0c22d1940fa5e341a0e32cadee14260530645847d1b135c6df6087ad8a16aca
- the artifact now outlives its workspace

$ cat ./report.json
{"pi": 3.1415926536, "check_passed": true}

$ uv run headspace destroy demo
# headspace result
## Outcome summary
workspace demo was destroyed on docker
## Key findings
- removed: runtime, storage
- lifecycle path: cancelled -> destroyed
```

Every one of those four steps is a single, bounded, evidence-bearing result —
the container's stdout, exit code, and resource usage never had to enter the
caller's context directly; only the parts that mattered did. Output above is
trimmed for length; the full result package also carries `artifacts`,
`resource_usage`, `provenance`, and `attention` sections on every call.

### The three consumer classes

Every verb's result package has exactly two renderings for three audiences:

- **AI agents** and **humans** read the default markdown, walked from the
  same nine-section structure every time.
- **Scripts and non-AI bots** read `--json`, the identical structure
  serialized instead of rendered.

Both come from one in-memory structure, so the two can never disagree about
content — the CLI never renders text and parses it back. The default
rendering is bounded to 8 KiB (`--max-result-bytes` to change it); whenever it
drops bytes, it says so with a marker naming the exact retrieval command
(`headspace inspect <handle> --logs`) rather than silently hiding the volume
that didn't fit.

### Choosing a backend

Every lifecycle verb takes `--provider docker` (the default, a real
container per job) or `--provider fake` (an in-memory backend that passes the
same conformance suite as Docker, so it's a genuine second implementation,
not a stub). `fake` needs no engine, which is what makes the entire test
suite runnable with nothing installed — but its workspaces live only in the
CLI process's memory, so they do not survive between separate shell
invocations the way `docker`'s (state under `~/.headspace`, reconciled
against engine labels) do. Use `fake` from a single Python process (tests, a
script driving the CLI's `main()` directly); use `docker` for a multi-step
shell session like the one above.

### Security posture

Every workspace starts closed by default: no network access, and a small,
explicit resource budget — 512 MiB memory, 1 CPU core, 128 pids, 1 GiB
storage, a 300-second wall clock, 10 MiB of captured output, 1 concurrent job
— never "unlimited." On the `docker` provider that policy becomes a real
container `HostConfig`, shared by the workspace container and every job
container it starts: `network_mode` is `none` unless `--network enabled` was
passed; `mem_limit` and `memswap_limit` are pinned equal (a container allowed
to swap has not really been given a memory limit); `nano_cpus` and
`pids_limit` come straight from the budget; `privileged` is always `false`
with `security_opt: [no-new-privileges]`. The only mount is the workspace's
own bounded volume — **no host bind mounts, no devices, no `volumes_from`,
and never the Docker engine socket itself.** `wall_clock_seconds`,
`output_bytes`, and `concurrency` are enforced by headspace's own process,
independent of the engine.

Say plainly what that does and does not guarantee: every limit in a result
package's policy summary is labelled **enforced** (the host will really apply
it) or **measured** (the host only reports it). The one limit that is
measured, not enforced, today is **storage**, on Docker's default local
volume driver — the engine exposes a volume's size through its `/system/df`
accounting, and reports it there, but no driver on offer takes a size cap.
That gap is surfaced in every affected result's warnings, not hidden. If the host
cannot really back a requested limit that *does* guard the isolation boundary
(network, filesystem scope, memory, cpu, pids), `create` fails closed —
`exit 3`, before anything runs — rather than silently serving a weaker box.
The MVP Docker provider also supports no host-path mounts at all, so
`--allow-host-path` fails closed on it today, by design, not by omission.

### Exit codes

| Code | Category | Meaning |
|------|----------|---------|
| `0` | success | the verb completed. |
| `1` | user_error | bad flag, missing argument, or unknown path — caller error. |
| `2` | environment_error | a local setup/tooling problem. |
| `3` | policy_denied | the declared policy could not be satisfied — refused before anything ran. |
| `4` | timeout | a wall-clock or budget ceiling was hit. |
| `5` | cancelled | the caller asked for it to stop. |
| `6` | computation_failed | the job ran correctly and produced a failing result. |
| `7` | infrastructure_failure | the engine or environment broke — not a computational failure. |

`0`–`2` are the original CLI-error codes and are unchanged. `3`–`7` are the
failure taxonomy, mirroring the result package's `status` vocabulary one to
one — a caller learns *why* a job failed from the exit code alone, without
parsing text.

## Mesh identity

This repository is itself a Culture mesh agent, not only the product it
builds: `culture.yaml` declares its `suffix` and `backend`, and
`AGENTS.colleague.md` is the resident prompt file that backend loads (this
agent runs `backend: colleague`). `headspace doctor` checks that pairing
stays consistent. The `.claude/skills/` kit is vendored cite-don't-import from
guildmaster (and, for a few skills, directly from their true origin,
`devague`) — see [`docs/skill-sources.md`](docs/skill-sources.md) for
per-skill provenance.

See [`CLAUDE.md`](CLAUDE.md) for the full conventions (version-bump-every-PR,
the `cicd` PR lane, deploy setup).

## License

Apache 2.0 — see [`LICENSE`](LICENSE).
