"""``headspace stop`` — end a workspace's in-flight job, previewing by default.

WHY the default does nothing
----------------------------
``destroy`` acts unless its guard refuses; ``stop`` refuses to act until
``--apply`` says so. The inversion is deliberate and it is not a style choice:
destroy's guard can *see* what would be lost — declared artifacts nobody
exported, named back at the caller one by one — while nothing on this path can
see how far a running computation had got. A caller who nearly discarded an
artifact can export it and try again; a caller who kills the wrong job cannot
un-kill it. So the safe default here is the one that produces a report and no
effect at all: without ``--apply``, no signal is sent, no engine is contacted,
and nothing under ``~/.headspace`` is opened for writing.

WHY ``--apply`` and not ``--force``
-----------------------------------
``destroy --force`` means "proceed past a refusal that was correct" — there is
a guard, it fired, and the flag overrules it. Nothing refuses here. The flag
answers a different question: this verb ran in preview and the caller now wants
the preview carried out. ``--apply`` is the word for that, and reusing
``--force`` would suggest a guard that does not exist. It is also, for the same
reason as ``--force``, not ``--yes``: headspace is daemonless and
non-interactive, there is no prompt to skip, and a verb that blocked on a
terminal would hang every agent that called it.

WHY this exits 5 when it works
------------------------------
A ``stop`` that ended a job reports the result status ``cancelled``, and
:func:`~headspace.core.workspace.exit_code_for_status` maps that onto the
failure taxonomy's long-standing ``cancelled`` slot — exit 5, "the caller asked
for it to stop". Non-zero here does not mean the CLI failed; it means a
computation was ended, which is the fact a script checking ``$?`` needs. A
preview, and a stop that found nothing running, exit 0: nothing was cancelled,
and an exit code claiming otherwise would report an act that never happened.

WHY the CLI holds no logic of its own
-------------------------------------
One flag and one positional, handed to
:meth:`headspace.core.workspace.Orchestrator.stop`. The orchestrator decides
what a preview may read, that no workspace lock is taken and no state is
written — the property that keeps this verb from deadlocking against the very
``run`` it interrupts — and how a workspace with nothing running is reported.
A CLI that re-decided any of that would be a second opinion about the same
boundary, and the two could disagree.
"""

from __future__ import annotations

import argparse

from headspace.cli._commands.create import add_common_flags, emit_package, orchestrator_for


def cmd_stop(args: argparse.Namespace) -> int:
    package = orchestrator_for(args).stop(args.workspace_id, apply=args.apply)
    return emit_package(package, args)


def register(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser(
        "stop",
        help="End the job a workspace is running. Previews by default and changes nothing "
        "until --apply is passed.",
    )
    parser.add_argument("workspace_id", metavar="WORKSPACE", help="Workspace whose job to end.")
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Actually end the job. Without this flag the job is only described — no signal "
        "is sent and nothing is written. A job ended this way reports 'cancelled' and this "
        "verb exits 5.",
    )
    add_common_flags(parser)
    parser.set_defaults(func=cmd_stop)
