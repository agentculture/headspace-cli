"""``headspace put`` — copy host bytes into a workspace, recording paths but never content.

WHY put refuses an existing destination by default
---------------------------------------------------
An inbound overwrite can destroy workspace-side work a job may never have
exported. ``export`` overwrites its host destination freely because the caller
owns that path — replacing it atomically is a local decision with no shared
state. ``put`` writes into a workspace volume that other jobs may read, so
replacing a destination silently is a mistake this product cannot walk back.
The default is therefore refusal; ``--overwrite`` is the explicit opt-in that
says "I accept the risk."

WHY the CLI holds no logic of its own
-------------------------------------
This module declares three positional arguments and one flag, then hands them
to :meth:`headspace.core.workspace.Orchestrator.put`. The orchestrator takes
the lock, validates the workspace state, expands the host path into an
:class:`~headspace.core.inputs.InputManifest`, measures digests, calls the
provider, and journals the intent — all inside a critical section. If the CLI
layer made any of those decisions, it would be a second opinion about the same
boundary, and the two could disagree. The CLI knows a path and a destination;
the orchestrator knows what "safe" means.

The three positional arguments mirror the method signature exactly:
``WORKSPACE`` (the workspace id), ``HOST_PATH`` (a file or directory on the
host), and ``DESTINATION`` (the workspace-relative path it lands at). Nothing
here opens a file or computes a digest; that belongs to
:func:`headspace.core.inputs.expand_input`, which reads the source once,
measures its digest, and hands both to the provider as
``expected_sha256`` so the engine can verify what actually landed.
"""

from __future__ import annotations

import argparse

from headspace.cli._commands.create import add_common_flags, emit_package, orchestrator_for


def cmd_put(args: argparse.Namespace) -> int:
    package = orchestrator_for(args).put(
        args.workspace_id,
        args.host_path,
        args.destination,
        overwrite=args.overwrite,
    )
    return emit_package(package, args)


def register(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser(
        "put",
        help="Copy host bytes into a workspace, recording paths but never content.",
    )
    parser.add_argument("workspace_id", metavar="WORKSPACE", help="Workspace to copy into.")
    parser.add_argument(
        "host_path",
        metavar="HOST_PATH",
        help="File or directory on the host to copy in.",
    )
    parser.add_argument(
        "destination",
        metavar="DESTINATION",
        help="Workspace-relative path the bytes land at.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        default=False,
        help="Permit replacing an existing destination. Refused by default to protect "
        "workspace-side work a job may never have exported.",
    )
    add_common_flags(parser)
    parser.set_defaults(func=cmd_put)
