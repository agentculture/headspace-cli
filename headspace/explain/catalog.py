"""Markdown catalog for ``headspace-cli explain <path>``.

Each entry is verbatim markdown. Keys are command-path tuples. The empty tuple
and ``("headspace-cli",)`` both resolve to the root entry.

Keep bodies self-contained: an agent reading one entry should get enough
context without chaining reads.
"""

from __future__ import annotations

_ROOT = """\
# headspace-cli

Headspace is an ephemeral computational workspace for agents: a bounded,
isolated workspace a model can offload execution, experimentation, and
calculation into, then get back a compact, evidence-bearing result instead of
a raw execution transcript. This agent also carries its own mesh identity
(`culture.yaml` + `AGENTS.colleague.md`) and the canonical guildmaster skill
kit under `.claude/skills/`.

## Lifecycle verbs

The seven verbs that make and use a workspace. Each returns the same nine-section
result package — markdown by default (for agents and humans), `--json` for
scripts and non-AI bots — and takes its exit code from the package's status.

- `headspace-cli create` — provision an ephemeral workspace.
- `headspace-cli put <workspace> <host-path> <destination>` — copy host bytes
  into it.
- `headspace-cli run <workspace> <command> ...` — run one command in it.
- `headspace-cli stop <workspace>` — end its in-flight job (previews by default).
- `headspace-cli inspect <handle>` — report its state, cost and captured output.
- `headspace-cli export <workspace> <name> --to <path>` — publish an artifact.
- `headspace-cli destroy <workspace>` — remove it, or refuse and remove nothing.

## Introspection verbs

- `headspace-cli whoami` — identity probe from `culture.yaml`.
- `headspace-cli learn` — structured self-teaching prompt.
- `headspace-cli explain <path>` — markdown docs for any noun/verb.
- `headspace-cli overview` — descriptive snapshot of the agent.
- `headspace-cli doctor` — check the agent-identity invariants.
- `headspace-cli cli overview` — describe the CLI surface.

## Exit-code policy

- `0` success
- `1` user-input error
- `2` environment / setup error
- `3` policy denied
- `4` timeout
- `5` cancelled — `headspace-cli stop <workspace> --apply` ended a job in flight
- `6` computation failed — the job ran correctly and produced a failing result
- `7` infrastructure failure — the engine or environment broke
- `8` resource exhausted — the job was killed for exceeding its declared
  memory ceiling

Codes 3–8 mirror the result package's status vocabulary one to one, so a caller
learns *why* a job failed from the exit code alone.

## Choosing a backend

Every lifecycle verb takes `--provider docker` (the default) or `--provider fake`
(in-memory, needs no engine). Reconciliation leaves workspaces journalled under
another backend's name alone, so the flag is how two backends stay out of each
other's way.

## See also

- `headspace-cli explain create`
- `headspace-cli explain put`
- `headspace-cli explain run`
- `headspace-cli explain stop`
- `headspace-cli explain whoami`
- `headspace-cli explain doctor`
"""

_WHOAMI = """\
# headspace-cli whoami

Reports the agent's identity from `culture.yaml`: nick (`suffix`), backend,
served model, and the package version. Read-only.

## Usage

    headspace-cli whoami
    headspace-cli whoami --json
"""

_LEARN = """\
# headspace-cli learn

Prints a structured self-teaching prompt covering purpose, command map,
exit-code policy, `--json` support, and the `explain` pointer.

## Usage

    headspace-cli learn
    headspace-cli learn --json
"""

_EXPLAIN = """\
# headspace-cli explain <path>

Prints markdown documentation for any noun/verb path. Unlike `--help` (terse,
positional), `explain` is global and addressable by path.

## Usage

    headspace-cli explain headspace-cli
    headspace-cli explain whoami
    headspace-cli explain --json <path>
"""

_OVERVIEW = """\
# headspace-cli overview

Read-only descriptive snapshot of the agent: identity (from `culture.yaml`), the
verb surface, and the sibling-pattern artifacts the template carries. Accepts an
ignored `target` so a stray path never hard-fails.

## Usage

    headspace-cli overview
    headspace-cli overview --json
"""

_DOCTOR = """\
# headspace-cli doctor

Checks the agent-identity invariants `steward doctor` verifies:
prompt-file-present and backend-consistency (`colleague` → `AGENTS.colleague.md`), plus a
skills-present check. Exits 1 when unhealthy.

## Usage

    headspace-cli doctor
    headspace-cli doctor --json
"""

_CLI = """\
# headspace-cli cli

Noun group for CLI-surface introspection. `cli overview` describes the CLI
itself (distinct from the global `overview`, which describes the agent).

## Usage

    headspace-cli cli overview
    headspace-cli cli overview --json
"""


_CREATE = """\
# headspace-cli create

Provisions an ephemeral workspace and reports it as a result package. This is
the only verb that mints a workspace id and the only one that takes policy
flags: a workspace runs for its whole life under the contract it was created
under, so a ceiling cannot be raised afterwards.

Every default is the closed one — no network, no host paths, small budgets.

## Usage

    headspace-cli create
    headspace-cli create --profile python3.12 --wall-clock-seconds 60
    headspace-cli create --network enabled --json

## Flags

- `--profile NAME` — runtime profile (default `python3.12`), pinned to an exact
  image digest.
- `--workspace-id ID` — use this id instead of a minted one; refused if taken.
- `--network disabled|enabled` — network posture (default `disabled`).
- `--allow-host-path PATH` — host path the workspace may see, repeatable. Fails
  closed on a host that cannot enforce the scope.
- `--memory-bytes`, `--cpu-limit`, `--pids-limit`, `--storage-bytes`,
  `--wall-clock-seconds`, `--output-bytes`, `--concurrency` — the resource
  ceilings, one flag per budget field.
- `--provider docker|fake`, `--json`, `--max-result-bytes N`.

## Notes

A limit the host cannot enforce is reported as *measured* in the result's
warnings rather than silently dropped. The new workspace id is in the result
package under `provenance.workspace_id`.

## See also

- `headspace-cli explain put`
- `headspace-cli explain run`
- `headspace-cli explain destroy`
"""

_RUN = """\
# headspace-cli run

Runs one command in an existing workspace and returns the result package for
that job: `ready -> running -> ready`. The captured output enters the package
only as a bounded excerpt; the rest is retrievable with
`headspace-cli inspect <job-id> --logs`.

**Flags go before the workspace id. Everything after it is the command**,
argparse claims none of it — so `run ws-1 echo hi --json` passes `--json` to
`echo`, not to headspace. A leading `--` is accepted and dropped.

## Usage

    headspace-cli run ws-1 python -c "print(6 * 7)"
    headspace-cli run --json ws-1 pytest -q
    headspace-cli run --declare report.json="the findings" ws-1 ./analyse.sh
    headspace-cli run --input harness=./harness.py --env API_KEY ws-1 ./harness.py

## Flags

- `--declare NAME=PURPOSE` — register an output this job promises, repeatable.
  The purpose is required: it is what a refused `destroy` quotes back when it
  names the work it is protecting.
- `--input NAME=HOST_PATH` — copy a host file or directory into the workspace
  before the job starts, repeatable (issue #14). `NAME` is the
  workspace-relative destination; `HOST_PATH` may be a file or a whole
  directory. Drives the same copy-in `put` does — headspace records each
  file's destination, size and sha256, never its contents.
- `--env NAME` — forward the variable `NAME` from the caller's own
  environment into the job, repeatable (issue #13). The value is read here,
  by name, and never enters argv, so headspace records the name and never
  the value; an unset name is refused rather than forwarded empty, and
  `--env NAME=VALUE` is refused outright. Edge worth knowing: a job that
  prints its own environment (`env`, `printenv`, a traceback) writes the
  value into its own captured output, and captured output is kept — that is
  the job's doing, not headspace's.
- `--env-file PATH` — read `NAME=VALUE` lines from a host file and forward
  all of them, repeatable. The value is taken verbatim to the end of the
  line; a malformed line is refused by file and line number, never quoted
  back. headspace records the file's path and the names it defined, never
  the values.
- `--job-id ID` — use this job id instead of a minted one. It is the handle
  truncated output is retrieved by.
- `--provider docker|fake`, `--json`, `--max-result-bytes N`.

## Exit codes

The exit code is the job's status, not merely zero or non-zero: `6` for a
command that ran correctly and failed, `4` for one the budget stopped, `7` for
an engine that broke, `8` for one killed over its memory ceiling. A failed job
leaves a perfectly good workspace behind. A job that another process ended
with `headspace-cli stop --apply` exits `5` (`cancelled`) and carries no
`exit_status` at all — on every backend, and never inferred from the number
the engine reports, which cannot tell an ended job from one that chose to
exit that way.

## See also

- `headspace-cli explain put`
- `headspace-cli explain stop`
- `headspace-cli explain inspect`
- `headspace-cli explain export`
"""

_PUT = """\
# headspace-cli put

Copies a host file or directory into a workspace (issue #14) and records
where it landed — the host path, the workspace destination, the size and the
sha256 — never the bytes. Refuses an existing destination unless
`--overwrite`: an inbound overwrite can destroy workspace-side work a job may
never have exported, and `put` writes into a volume other jobs may read, so
replacing a destination silently is a mistake this product will not make by
default.

This is the identical copy-in `run --input NAME=HOST_PATH` drives before a
job starts — there is one implementation of getting bytes into a workspace,
not two that agree until they don't. `put` is the standalone form, for
staging files ahead of a run or replacing them between runs; `--input` is the
inline form for the common case of "copy this in, then run."

## Usage

    headspace-cli put ws-1 ./payload.tar dataset.tar
    headspace-cli put ws-1 ./src ./workspace-src --overwrite

## Flags

- `--overwrite` — permit replacing an existing destination. Refused by
  default.
- `--provider docker|fake`, `--json`, `--max-result-bytes N`.

## See also

- `headspace-cli explain run`
- `headspace-cli explain destroy`
"""

_STOP = """\
# headspace-cli stop

Ends the job a workspace is running, and **previews by default**: without
`--apply` it reports what is running and changes nothing — no signal is sent,
no engine is contacted, nothing under `~/.headspace` is opened for writing.
The default is the inert one because nothing on this path can see how far a
running computation had got: a caller who nearly discards an artifact can
export it and try again, but a caller who kills the wrong job cannot un-kill
it.

`--apply` actually ends the job. The `stop` invocation itself then reports
the result status `cancelled`, which maps to exit `5` — "the caller asked
for it to stop" — on every backend. A preview, and a `stop` that finds
nothing running, exits `0`: nothing was cancelled, so nothing claims to have
been.

`stop` itself is provably inert without `--apply`, on every backend: it takes
no workspace lock and writes no state, which is what keeps it from
deadlocking against the very `run` it interrupts. The *other* half of the
story — the *job's own* recorded outcome, which the interrupted `run`
invocation writes separately — now agrees with it everywhere: that job is
recorded `cancelled` with no exit status, and its `run` exits `5`. On
`docker` that costs a signal `stop` leaves in the workspace volume for the
blocked `run` to find, because the two are separate processes and the engine's
own numbers cannot tell an ended job from one that exited that way itself.
The signal is written in two phases and only the second one classifies, so a
`stop` that dies partway through costs a real cancellation its name rather
than inventing one that never happened.

## Usage

    headspace-cli stop ws-1
    headspace-cli stop ws-1 --apply

## Flags

- `--apply` — actually end the job. Without it, nothing is signalled and
  nothing is written.
- `--provider docker|fake`, `--json`, `--max-result-bytes N`.

## See also

- `headspace-cli explain run`
"""

_INSPECT = """\
# headspace-cli inspect

Reports headspace's lifecycle view of a workspace and the engine's, side by
side, plus what the session has cost so far. When the engine no longer holds the
workspace, the facts are labelled as the last ones observed rather than current
ones.

`--logs` returns the captured output **in full and unbounded** — the path every
truncation marker names. The handle is whatever the marker printed: a job id
when a job produced the output, the workspace id otherwise. Both are accepted.
`--max-result-bytes` does not apply to `--logs`.

## Usage

    headspace-cli inspect ws-1
    headspace-cli inspect ws-1 --json
    headspace-cli inspect job-7 --logs

## Flags

- `--logs` — full captured output instead of a bounded result package.
- `--provider docker|fake`, `--json`, `--max-result-bytes N`.

## See also

- `headspace-cli explain run`
"""

_EXPORT = """\
# headspace-cli export

Publishes a declared artifact out of a workspace to a durable host path, and
records what left. The bytes are streamed out of the workspace by headspace —
they live inside a volume no other process can open — digested during the copy,
and published by atomic rename, so the destination holds either the complete
artifact or nothing.

Only *declared* outputs can be exported (see `run --declare`), and only exported
artifacts appear in a result package's artifacts section: a wire artifact
promises retrievability and integrity, and a declared one can keep neither.

## Usage

    headspace-cli export ws-1 report.json --to ./report.json
    headspace-cli export ws-1 report --path build/out.json --to ./out.json
    headspace-cli export ws-1 model.bin --to ./model.bin --expect-sha256 <hex>

## Flags

- `--to PATH` — **required.** Host path to publish to.
- `--path PATH` — path inside the workspace (default: the artifact's name).
- `--expect-sha256 HEX` — refuse to publish unless the bytes hash to this.
- `--provider docker|fake`, `--json`, `--max-result-bytes N`.

## See also

- `headspace-cli explain destroy`
"""

_DESTROY = """\
# headspace-cli destroy

Removes a workspace — or refuses, and when it refuses it removes nothing. The
guard runs before anything is journalled and before the engine is called, so a
refusal is structurally incapable of leaving a partial effect.

Two refusals:

- **unexported work** — declared artifacts nobody exported. Export them first,
  or pass `--force` to discard them deliberately.
- **a job in flight** — the lifecycle table has no `running -> destroyed` edge,
  and the refusal comes back in the table's own words.

With `--force`, every discarded artifact is named in the result, with an empty
digest: it was never exported, so nothing can verify or recover it.

## Usage

    headspace-cli destroy ws-1
    headspace-cli destroy ws-1 --force
    headspace-cli destroy ws-1 --json

## Flags

- `--force` — discard unexported declared artifacts and proceed.
- `--provider docker|fake`, `--json`, `--max-result-bytes N`.

## See also

- `headspace-cli explain export`
"""


ENTRIES: dict[tuple[str, ...], str] = {
    (): _ROOT,
    ("headspace-cli",): _ROOT,
    ("headspace",): _ROOT,
    ("whoami",): _WHOAMI,
    ("learn",): _LEARN,
    ("explain",): _EXPLAIN,
    ("overview",): _OVERVIEW,
    ("doctor",): _DOCTOR,
    ("cli",): _CLI,
    ("cli", "overview"): _CLI,
    ("create",): _CREATE,
    ("put",): _PUT,
    ("run",): _RUN,
    ("stop",): _STOP,
    ("inspect",): _INSPECT,
    ("export",): _EXPORT,
    ("destroy",): _DESTROY,
}
