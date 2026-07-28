"""``headspace export`` — publish a declared artifact out of a workspace, verifiably.

WHY this verb names a path but never reads one
----------------------------------------------
The bytes live inside a workspace volume no process outside the engine can
open, which is why the orchestrator grew a provider ``read`` verb (deviation
d6): the CLI knows an artifact's *name*, not its contents. So this module hands
down two strings — ``--path``, where the bytes sit inside the workspace, and
``--to``, where they should land on the host — and the orchestrator streams
between them. Nothing here opens a file, computes a digest, or decides what
"durable" means; that boundary belongs to
:func:`headspace.core.artifacts.export_artifact`, which copies to a temporary
name, digests during the copy, and publishes by atomic rename. A CLI that read
the bytes itself would be a second, weaker opinion about durability.

``--path`` defaults to the artifact's name, because a job usually writes its
output under the name it declared. The flag is for the case where it does not:
a declaration called ``report`` written to ``build/report.json``.

WHY ``--to`` is required
------------------------
An export that lands nowhere durable is not an export — it is a read with extra
steps. The orchestrator refuses a missing destination too, but making the flag
required means the refusal arrives from argparse, with the usage line and the
``hint:`` an agent reads, before a workspace is even opened.

WHY ``--expect-sha256`` is here and not in the workspace
--------------------------------------------------------
Integrity is a claim about bytes that *left*, so it is checked at the boundary
they cross. A caller who already knows what the artifact should hash to can
pass it and have the export refuse rather than publish; a caller who does not
gets the digest reported back in the result package's artifacts section, which
is the same fact in the other direction.
"""

from __future__ import annotations

import argparse

from headspace.cli._commands.create import add_common_flags, emit_package, orchestrator_for


def cmd_export(args: argparse.Namespace) -> int:
    package = orchestrator_for(args).export(
        args.workspace_id,
        args.name,
        destination=args.to,
        expected_sha256=args.expect_sha256,
        path=args.path,
    )
    return emit_package(package, args)


def register(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser(
        "export",
        help="Publish a declared artifact to a durable path, with its digest verified.",
    )
    parser.add_argument("workspace_id", metavar="WORKSPACE", help="Workspace to export from.")
    parser.add_argument(
        "name",
        metavar="NAME",
        help="The declared artifact's name. Only declared outputs can be exported.",
    )
    parser.add_argument(
        "--to",
        required=True,
        metavar="PATH",
        help="Host path to publish to. Written by atomic rename, so it either holds the "
        "complete artifact or nothing.",
    )
    parser.add_argument(
        "--path",
        default=None,
        metavar="PATH",
        help="Path inside the workspace to read from (default: the artifact's name).",
    )
    parser.add_argument(
        "--expect-sha256",
        default=None,
        metavar="HEX",
        help="Refuse to publish unless the bytes hash to this digest.",
    )
    add_common_flags(parser)
    parser.set_defaults(func=cmd_export)
