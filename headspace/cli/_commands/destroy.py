"""``headspace destroy`` — tear a workspace down, or refuse and remove nothing.

WHY this verb is nearly empty
-----------------------------
Destroying unexported work is the one mistake this product cannot walk back, so
none of the judgement lives here. The orchestrator diffs declared artifacts
against the export log *before* it journals anything and refuses without
``force``; it asks the lifecycle table — which deliberately has no
``running -> destroyed`` edge — whether a workspace with a job in flight may be
removed, so the refusal comes back in the table's own words; and it names every
artifact it discarded when ``force`` is given, with the digest field present and
empty, because an artifact that was never exported has no digest and inventing
one would be the lie the guard exists to prevent.

Putting any of that here would put it *after* the point of no return for one
caller and before it for another. A CLI that re-implemented the guard would
also be a CLI that could disagree with the library about what "unexported"
means. So this module turns two flags into one call and renders the answer.

WHY ``--force`` and not ``--yes``
---------------------------------
``--yes`` reads as "skip the prompt", and there is no prompt: headspace is
daemonless and non-interactive, and a verb that blocked on a terminal would
hang every agent that called it. ``--force`` reads as what it does — proceed
past a refusal that was correct — and the result package still names each
discarded artifact afterwards, so the receipt survives the decision.
"""

from __future__ import annotations

import argparse

from headspace.cli._commands.create import add_common_flags, emit_package, orchestrator_for


def cmd_destroy(args: argparse.Namespace) -> int:
    package = orchestrator_for(args).destroy(args.workspace_id, force=args.force)
    return emit_package(package, args)


def register(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser(
        "destroy",
        help="Remove a workspace. Refuses, and removes nothing, when declared artifacts "
        "were never exported.",
    )
    parser.add_argument("workspace_id", metavar="WORKSPACE", help="Workspace to remove.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Proceed even though declared artifacts were never exported, discarding them. "
        "Discarded artifacts cannot be recovered; each one is named in the result.",
    )
    add_common_flags(parser)
    parser.set_defaults(func=cmd_destroy)
