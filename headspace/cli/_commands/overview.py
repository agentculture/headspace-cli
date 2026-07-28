"""``headspace-cli overview`` — read-only descriptive snapshot of the agent.

Describes the agent to an agent reader: identity (from culture.yaml), the verb
surface — lifecycle and introspection, listed separately because they answer
different questions — and the sibling-pattern artifacts the repo carries. The
shared section/render helpers here are reused by the ``cli`` noun's
``overview`` (see :mod:`headspace.cli._commands.cli`).

Descriptive verbs never hard-fail on a missing target path — an optional
positional ``target`` is accepted and ignored (overview describes this agent,
not an external target), so ``overview <bogus-path>`` still exits 0.
"""

from __future__ import annotations

import argparse

from headspace.cli._commands.whoami import report
from headspace.cli._output import emit_result

_ARTIFACTS = [
    "culture.yaml + AGENTS.colleague.md — mesh identity (suffix + backend)",
    ".claude/skills/ — the canonical guildmaster skill kit (cite-don't-import)",
    "docs/skill-sources.md — skill provenance ledger",
    "pyproject.toml + .github/workflows/ — buildable, deployable package baseline",
]

_LIFECYCLE_VERBS = [
    "create — create a workspace under a declared policy",
    "run <workspace> <command> — run a job inside a workspace",
    "inspect <handle> [--logs] — status, or the full captured output",
    "export <workspace> <name> --to PATH — publish an artifact, digest-verified",
    "destroy <workspace> [--force] — tear down; refuses unexported artifacts",
]

_VERBS = [
    "whoami — identity probe (nick, version, backend, model)",
    "learn — structured self-teaching prompt",
    "explain <path> — markdown docs for a topic",
    "overview — this descriptive snapshot",
    "doctor — check the agent-identity invariants",
]


def agent_sections() -> list[dict[str, object]]:
    """Sections describing the agent (used by the global verb)."""
    ident = report()
    return [
        {
            "title": "Identity",
            "items": [
                f"nick: {ident['nick']}",
                f"version: {ident['version']}",
                f"backend: {ident['backend']}",
                f"model: {ident['model']}",
            ],
        },
        {"title": "Lifecycle verbs", "items": list(_LIFECYCLE_VERBS)},
        {"title": "Introspection verbs", "items": list(_VERBS)},
        {"title": "Sibling-pattern artifacts", "items": list(_ARTIFACTS)},
    ]


def cli_sections() -> list[dict[str, object]]:
    """Sections describing the CLI surface itself (used by `cli overview`)."""
    return [
        {
            "title": "Verbs",
            "items": list(_LIFECYCLE_VERBS)
            + list(_VERBS)
            + ["cli overview — describe the CLI surface (this command)"],
        },
        {
            "title": "Conventions",
            "items": [
                "every command supports --json",
                "results to stdout, errors/diagnostics to stderr (never mixed)",
                "lifecycle verbs take --provider {docker,fake} and --max-result-bytes",
                "exit codes: 0 success, 1 user error, 2 environment error, then the "
                "failure taxonomy — 3 policy_denied, 4 timeout, 5 cancelled, "
                "6 computation_failed, 7 infrastructure_failure",
            ],
        },
    ]


def render_text(subject: str, sections: list[dict[str, object]]) -> str:
    lines = [f"# {subject}", ""]
    for section in sections:
        lines.append(f"## {section['title']}")
        for item in section["items"]:
            lines.append(f"- {item}")
        lines.append("")
    return "\n".join(lines).rstrip()


def emit_overview(subject: str, sections: list[dict[str, object]], *, json_mode: bool) -> None:
    if json_mode:
        emit_result({"subject": subject, "sections": sections}, json_mode=True)
    else:
        emit_result(render_text(subject, sections), json_mode=False)


def cmd_overview(args: argparse.Namespace) -> int:
    # `target` is accepted for rubric compatibility (descriptive verbs must not
    # hard-fail on a missing path) but overview describes this agent itself.
    emit_overview(
        "headspace-cli",
        agent_sections(),
        json_mode=bool(getattr(args, "json", False)),
    )
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "overview",
        help="Read-only descriptive snapshot of the agent (identity, verbs, artifacts).",
    )
    p.add_argument(
        "target",
        nargs="?",
        help="Ignored — overview always describes this agent itself. Accepted so a "
        "stray path argument never hard-fails.",
    )
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_overview)
