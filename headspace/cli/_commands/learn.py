"""``headspace-cli learn`` — the learnability affordance.

Prints a structured self-teaching prompt. Must satisfy the agent-first rubric:
>=200 chars and mention purpose, command map, exit codes, --json, and explain.
"""

from __future__ import annotations

import argparse

from headspace import __version__
from headspace.cli._errors import (
    EXIT_CANCELLED,
    EXIT_COMPUTATION_FAILED,
    EXIT_INFRASTRUCTURE_FAILURE,
    EXIT_POLICY_DENIED,
    EXIT_RESOURCE_EXHAUSTED,
    EXIT_TIMEOUT,
    category_for_code,
)
from headspace.cli._output import emit_result

_TEXT = """\
headspace-cli — ephemeral computational workspaces for agents.

Purpose
-------
Offload execution into a bounded, isolated workspace and get back a compact,
evidence-bearing result instead of a raw execution transcript. Create a
workspace under an explicit policy, run one or more jobs that share its
temporary state, export the artifacts worth keeping, then destroy it. The noisy
working process stays outside your context: what returns is the outcome,
selected evidence, an artifact inventory with digests, resource usage,
provenance, and anything needing attention. Raw logs stay inspectable through
`inspect --logs` rather than arriving by default.

The console command is `headspace`.

Lifecycle commands
------------------
  headspace create [--profile NAME] [--workspace-id ID] [--network POSTURE]
                                   Create a workspace under a declared policy.
  headspace put <workspace> <host-path> <destination> [--overwrite]
                                   Copy a host file or directory in. Refuses an
                                   existing destination without --overwrite.
  headspace run <workspace> <cmd>...
                                   Run a job; flags go BEFORE the workspace id.
                                   --input NAME=HOST_PATH copies a payload in
                                   first; --env NAME and --env-file PATH hand
                                   the job values that never enter argv. Only
                                   names, paths and digests are recorded.
  headspace stop <workspace> [--apply]
                                   End an in-flight job. Previews by default —
                                   without --apply nothing is signalled.
  headspace inspect <handle> [--logs]
                                   Status, or the full captured output.
  headspace export <workspace> <name> --to PATH
                                   Publish an artifact, digest-verified.
  headspace destroy <workspace> [--force]
                                   Tear down; refuses if artifacts are
                                   declared but never exported.

Every lifecycle verb takes --provider {docker,fake} (default docker) and
--max-result-bytes N to bound what comes back.

Introspection commands
----------------------
  headspace whoami                 Identity from culture.yaml.
  headspace learn                  This self-teaching prompt.
  headspace explain <path>...      Markdown docs for any noun/verb path.
  headspace overview               Descriptive snapshot of the agent.
  headspace doctor                 Check the agent-identity invariants.
  headspace cli overview           Describe the CLI surface itself.

Machine-readable output
-----------------------
Every command supports --json. Errors in JSON mode emit
{"code", "message", "remediation", "category"} to stderr. Stdout and stderr
never mix.

Exit-code policy
----------------
  0 success
  1 user-input error (bad flag, bad path, missing arg)
  2 environment / setup error
  3 policy_denied — refused by policy before running
  4 timeout — wall-clock or budget limit hit
  5 cancelled — caller asked for it to stop
  6 computation_failed — ran correctly, produced a failing result. If the
    job's own exit_status is 126 or 127, the command itself is what failed
    (127 = not found, 126 = found but not executable) — fix the command,
    do not retry it unchanged and do not suspect the engine.
  7 infrastructure_failure — engine/environment broke, not a computation
    failure
  8 resource_exhausted — the job was killed for exceeding its declared
    memory ceiling. Raise the memory budget (or shrink the job's working
    set) before retrying; retrying unchanged will be killed again.

More detail
-----------
  headspace explain headspace-cli
"""


def _as_json_payload() -> dict[str, object]:
    return {
        "tool": "headspace-cli",
        "version": __version__,
        "purpose": (
            "Ephemeral computational workspaces for agents: offload execution into a "
            "bounded, isolated workspace and receive a compact, evidence-bearing result "
            "instead of a raw execution transcript."
        ),
        "command": "headspace",
        "commands": [
            {"path": ["create"], "summary": "Create a workspace under a declared policy."},
            {"path": ["run"], "summary": "Run a job inside a workspace."},
            {"path": ["inspect"], "summary": "Status, or full captured output with --logs."},
            {"path": ["export"], "summary": "Publish an artifact, digest-verified."},
            {"path": ["destroy"], "summary": "Tear down; refuses unexported artifacts."},
            {"path": ["whoami"], "summary": "Identity probe from culture.yaml."},
            {"path": ["learn"], "summary": "Self-teaching prompt."},
            {"path": ["explain"], "summary": "Markdown docs by path."},
            {"path": ["overview"], "summary": "Descriptive snapshot of the agent."},
            {"path": ["doctor"], "summary": "Check the agent-identity invariants."},
            {"path": ["cli", "overview"], "summary": "Describe the CLI surface."},
        ],
        "exit_codes": {
            "0": "success",
            "1": "user-input error",
            "2": "environment/setup error",
            "3": f"{category_for_code(EXIT_POLICY_DENIED)} — refused by policy before running",
            "4": f"{category_for_code(EXIT_TIMEOUT)} — wall-clock or budget limit hit",
            "5": f"{category_for_code(EXIT_CANCELLED)} — caller asked for it to stop",
            "6": (
                f"{category_for_code(EXIT_COMPUTATION_FAILED)} — ran correctly, "
                "produced a failing result; exit_status 127/126 means the command "
                "itself is not found/not executable — fix the command, not the engine"
            ),
            "7": (
                f"{category_for_code(EXIT_INFRASTRUCTURE_FAILURE)} — engine/environment "
                "broke, not a computation failure"
            ),
            "8": (
                f"{category_for_code(EXIT_RESOURCE_EXHAUSTED)} — killed for exceeding "
                "its declared memory ceiling; raise the memory budget, do not retry unchanged"
            ),
        },
        "json_support": True,
        "explain_pointer": "headspace explain <path>",
    }


def cmd_learn(args: argparse.Namespace) -> int:
    if getattr(args, "json", False):
        emit_result(_as_json_payload(), json_mode=True)
    else:
        emit_result(_TEXT, json_mode=False)
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "learn",
        help="Print a structured self-teaching prompt for agent consumers.",
    )
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_learn)
