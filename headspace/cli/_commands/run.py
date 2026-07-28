"""``headspace run`` — execute one command in an existing workspace.

WHY the command is captured with ``REMAINDER``
----------------------------------------------
The whole point of this verb is to hand an arbitrary argv to a job, and almost
every interesting argv contains something argparse would try to claim:
``python -c ...``, ``pytest -q``, ``sh -c 'cmd | cmd'``. So the command is
:data:`argparse.REMAINDER`: **everything after the workspace id belongs to the
job**, flags included, and every flag for ``run`` itself goes before it::

    headspace run --json --declare report.json=the findings ws-1 python -c "..."

That is the same bargain ``docker exec`` and ``kubectl exec`` strike, and the
help text states it, because the failure mode when a caller gets it backwards
is quiet: ``run ws-1 echo hi --json`` passes ``--json`` to ``echo``. A leading
``--`` is accepted and dropped, so the explicit separator form works too.

An empty command is refused here rather than passed down. The provider would
reject it as well, but this is a caller who typed the wrong thing, and telling
them so before an engine is touched is the difference between a hint and a
backend error.

WHY a declaration must carry a purpose
--------------------------------------
``--declare NAME=PURPOSE`` registers an output *before* the job writes it, and
the gap between declaring and exporting is exactly the set ``destroy`` refuses
to discard. That refusal is only legible if it can say what is being protected:
"refusing to destroy: report.json (the findings) was never exported" is a
sentence a caller can act on, and "refusing to destroy: report.json" is one they
have to go and investigate. So the purpose is required, not defaulted — a
declaration nobody can judge is a guard nobody will respect.

WHY there are no policy flags here
----------------------------------
A workspace runs under the contract it was created under: the orchestrator
re-derives the effective policy from the declaration and the capability
snapshot stored at create time. Offering ``--memory-bytes`` on ``run`` would
imply a ceiling could be raised after the fact, which is precisely the thing
that must not be true, so the flag is absent rather than accepted and ignored.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from headspace.cli._commands.create import add_common_flags, emit_package, orchestrator_for
from headspace.cli._errors import EXIT_USER_ERROR, CliError
from headspace.core.workspace import ArtifactDeclaration

#: The separator between a declaration's name and its purpose. A name cannot
#: contain one; a purpose is prose and may contain as many as it likes, which is
#: why the split is on the first occurrence only.
DECLARE_SEPARATOR = "="


def parse_declarations(values: Sequence[str] | None) -> list[ArtifactDeclaration]:
    """Turn ``NAME=PURPOSE`` strings into declarations, refusing the incomplete."""
    declarations: list[ArtifactDeclaration] = []
    for raw in values or ():
        name, separator, purpose = raw.partition(DECLARE_SEPARATOR)
        if not separator or not name.strip() or not purpose.strip():
            raise CliError(
                code=EXIT_USER_ERROR,
                message=f"malformed artifact declaration {raw!r}",
                remediation=(
                    "write --declare NAME=PURPOSE; the purpose is required because it is what "
                    "a refused destroy quotes back when it names the work it is protecting"
                ),
            )
        declarations.append(ArtifactDeclaration(name=name.strip(), purpose=purpose.strip()))
    return declarations


def parse_command(values: Sequence[str] | None) -> list[str]:
    """The job's argv: everything after the workspace id, minus a leading ``--``."""
    argv = list(values or ())
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        raise CliError(
            code=EXIT_USER_ERROR,
            message="no command was given to run",
            remediation=(
                "write the command after the workspace id, as in: "
                "headspace run <workspace> python -c 'print(1)'. Every flag for run itself "
                "goes before the workspace id — everything after it belongs to the job"
            ),
        )
    return argv


def cmd_run(args: argparse.Namespace) -> int:
    package = orchestrator_for(args).run(
        args.workspace_id,
        parse_command(args.command),
        declares=parse_declarations(args.declare),
        job_id=args.job_id,
    )
    return emit_package(package, args)


def register(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser(
        "run",
        help="Run one command in a workspace. Flags go BEFORE the workspace id; "
        "everything after it is the command.",
    )
    parser.add_argument(
        "--declare",
        action="append",
        default=None,
        metavar="NAME=PURPOSE",
        help="Register an output this job promises to produce, repeatable. The purpose is "
        "required: it is what a refused destroy quotes when it names protected work.",
    )
    parser.add_argument(
        "--job-id",
        default=None,
        metavar="ID",
        help="Use this job id instead of a minted one. It is the handle truncated output "
        "is retrieved by.",
    )
    add_common_flags(parser)
    parser.add_argument("workspace_id", metavar="WORKSPACE", help="Workspace to run in.")
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        metavar="COMMAND ...",
        help="The command and its arguments. Taken verbatim — argparse claims none of it.",
    )
    parser.set_defaults(func=cmd_run)
