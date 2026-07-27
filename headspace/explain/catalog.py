"""Markdown catalog for ``headspace-cli explain <path>``.

Each entry is verbatim markdown. Keys are command-path tuples. The empty tuple
and ``("headspace-cli",)`` both resolve to the root entry.

Keep bodies self-contained: an agent reading one entry should get enough
context without chaining reads.
"""

from __future__ import annotations

_ROOT = """\
# headspace-cli

A clonable template for AgentCulture mesh agents. It carries an agent-first CLI
(cited from the teken `python-cli` reference), a mesh identity (`culture.yaml` +
`CLAUDE.md`), the canonical guildmaster skill kit under `.claude/skills/`, and a
buildable/deployable package baseline. Clone it, rename the package, edit
`culture.yaml`, and you have a new agent.

## Verbs

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
- `3+` reserved

## See also

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
}
