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

WHY ``--env`` takes a NAME and never a value
--------------------------------------------
Before this flag, argv was the only way to hand a job a value — and argv is
recorded verbatim in four places: the result package's ``outcome_summary``, its
``provenance.inputs``, the workspace's ``journal.jsonl`` and its ``state.json``.
A caller who needed to give a job an API key therefore had no correct move: any
spelling of ``run ws-1 sh -c 'API_KEY=… ./job'`` writes the key into all four.

So ``--env NAME`` names the variable and headspace reads the value out of *this*
process's environment. The value never enters argv, so no recording surface can
pick it up, and what is written down is the name (see
:class:`~headspace.core.workspace.JobEnvironment`). Three consequences worth
stating outright, because each is a decision:

* **An unset name is refused, loudly.** Forwarding an empty string instead would
  hand the job a credential-shaped nothing and fail somewhere far from the cause.
* **A value typed into the flag is refused too.** ``--env NAME=VALUE`` is exactly
  the leak the flag exists to close, and the refusal does not quote the value
  back, because an error message is a recording surface like any other.
* **There is no redaction pass over argv.** headspace cannot know which token of
  a command line is a secret, and a redactor that guesses and misses once is
  worse than a documented absence: a caller who believes their transcript is
  clean stops reading it. The answer to a secret in argv is the channel that does
  not need argv.

These are ``run`` flags, not ``create`` flags. Nothing is baked into the
workspace or its anchor: an environment lives exactly as long as the one job it
was passed to, which is what makes "rotate the key and run it again" a rerun
rather than a rebuild.

WHY ``--input`` exists next to them
------------------------------------
The same problem in the other direction (issue #14): a network-disabled
workspace could not be given a *file*, so payloads were smuggled through argv,
with the same four-surface consequence. ``--input NAME=HOST_PATH`` copies host
bytes in before the job starts, recording each file's destination, size and
sha256 — never its contents. It drives the very same orchestrator copy-in the
``put`` verb does; there is one implementation of getting bytes into a
workspace, not two that agree until they do not.
"""

from __future__ import annotations

import argparse
import os
import re
from collections.abc import Sequence
from pathlib import Path

from headspace.cli._commands.create import add_common_flags, emit_package, orchestrator_for
from headspace.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from headspace.core.workspace import ArtifactDeclaration, InputRequest, JobEnvironment

#: The separator between a declaration's name and its purpose. A name cannot
#: contain one; a purpose is prose and may contain as many as it likes, which is
#: why the split is on the first occurrence only.
DECLARE_SEPARATOR = "="

#: The separator in ``--input NAME=HOST_PATH`` and in an env-file's
#: ``NAME=VALUE``. Split on the *first* occurrence in both cases: a workspace
#: destination and a variable name may not contain one, while a host path and a
#: secret very well may.
ASSIGNMENT_SEPARATOR = "="

#: The grammar a forwarded variable's name must satisfy: the POSIX portable
#: name, which is what every shell, every OCI runtime and ``execve`` agree on.
#: Enforced here rather than left to the engine because a name the engine
#: silently drops is a job that runs *without* the credential it asked for, and
#: fails later for a reason that names something else entirely.
#:
#: ``re.ASCII`` is load-bearing, not decoration. Python's ``\w`` is Unicode-aware
#: by default, so the concise spelling would quietly accept ``PASSWORDé`` — the
#: opposite of "the POSIX portable name", and a name the engine would then drop.
#: The flag pins ``\w`` to exactly ``[A-Za-z0-9_]``, which is what this pattern
#: has always meant.
ENV_NAME_PATTERN = re.compile(r"\A[A-Za-z_]\w*\Z", re.ASCII)

#: An env-file line whose first non-blank character is this is a comment. Blank
#: lines and comments are the only two things skipped; everything else must be
#: an assignment, because the alternative is guessing.
ENV_FILE_COMMENT = "#"


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
    # Slice rather than index: `argv[:1] == ["--"]` is empty-safe by
    # construction, where `argv and argv[0] == "--"` relies on short-circuit
    # evaluation that a static analyser has to prove. Both are correct; this
    # one is correct without an argument (SonarCloud S6466).
    if argv[:1] == ["--"]:
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


def parse_inputs(values: Sequence[str] | None) -> list[InputRequest]:
    """Turn ``NAME=HOST_PATH`` strings into copy-in requests, bounding every name.

    ``NAME`` is the workspace-relative destination and is normalised by
    :func:`~headspace.providers.base.require_workspace_path` — inside
    :class:`~headspace.core.workspace.InputRequest`'s constructor, so the bound
    is a property of the object rather than of this function remembering to
    apply it. Building the requests here, before
    :func:`~headspace.cli._commands.create.orchestrator_for` has been called,
    is what makes "refused before any engine contact" true by construction.

    The host path is taken verbatim after the first ``=``: a destination cannot
    contain one, and a host path can. It is not checked for existence here —
    :func:`~headspace.core.inputs.expand_input` answers that by looking, and a
    second opinion is a second way to be wrong.
    """
    requests: list[InputRequest] = []
    for raw in values or ():
        destination, separator, host_path = raw.partition(ASSIGNMENT_SEPARATOR)
        if not separator or not destination.strip() or not host_path.strip():
            raise CliError(
                code=EXIT_USER_ERROR,
                message=f"malformed input {raw!r}",
                remediation=(
                    "write --input NAME=HOST_PATH, where NAME is the workspace-relative path "
                    "the file lands at and HOST_PATH is the file or directory to copy in"
                ),
            )
        requests.append(InputRequest(host_path=host_path, destination=destination.strip()))
    return requests


def parse_environment(names: Sequence[str] | None, files: Sequence[str] | None) -> JobEnvironment:
    """Resolve ``--env`` and ``--env-file`` into the one environment a job runs with.

    Resolution happens *here*, at parse time, for the same reason the input
    destinations are bounded here: everything that can be refused before an
    engine is touched should be, and an unset variable or a malformed file is a
    caller's mistake, not a workspace's.

    Both flags contribute to one mapping and each name may be defined once. A
    name given twice is refused rather than resolved: the two definitions may
    hold different secrets, headspace has no basis for preferring either, and a
    silently-chosen winner is the failure mode where the *wrong* credential
    travels and nothing says so.
    """
    values: dict[str, str] = {}
    sources = tuple(files or ())
    for raw in names or ():
        name = _require_env_name(raw)
        _refuse_redefinition(values, name, source="--env")
        values[name] = _resolve_env(name)
    for path in sources:
        for number, name, value in _read_env_file(path):
            _refuse_redefinition(values, name, source=f"--env-file {path} line {number}")
            values[name] = value
    return JobEnvironment(values=values, sources=sources)


def _require_env_name(raw: str) -> str:
    """Refuse anything that is not a bare variable name — a value most of all."""
    name = raw.strip()
    if ASSIGNMENT_SEPARATOR in name:
        named = name.partition(ASSIGNMENT_SEPARATOR)[0].strip()
        raise CliError(
            code=EXIT_USER_ERROR,
            message=(
                f"--env takes the name of a variable, and {named or raw!r} was given a value "
                "on the command line"
            ),
            remediation=(
                "export the variable in the shell that runs headspace and write --env NAME. "
                "A value typed into argv is recorded verbatim in the result package, the "
                "journal and the state file — which is the leak this flag exists to close, "
                "and why the value is not repeated in this message"
            ),
        )
    if not ENV_NAME_PATTERN.match(name):
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"{raw!r} is not a usable environment variable name",
            remediation=(
                "a name starts with a letter or underscore and continues with letters, "
                "digits or underscores — the portable name every shell and runtime agrees on"
            ),
        )
    return name


def _resolve_env(name: str) -> str:
    """Read one variable out of the caller's environment, refusing an unset name."""
    try:
        return os.environ[name]
    except KeyError:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"environment variable '{name}' is not set in the shell running headspace",
            remediation=(
                f"export {name} before running, or pass it with --env-file. headspace refuses "
                "rather than forwarding an empty string: a job handed a credential-shaped "
                "nothing fails somewhere far away from the cause"
            ),
        ) from None


def _refuse_redefinition(values: dict[str, str], name: str, *, source: str) -> None:
    if name in values:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"environment variable '{name}' was defined more than once (again by {source})",
            remediation=(
                "define each variable once. headspace refuses rather than picking a winner: "
                "two definitions may hold different secrets, and the wrong one would travel "
                "with nothing on any surface to say which it was"
            ),
        )


def _read_env_file(path: str) -> list[tuple[int, str, str]]:
    """Parse ``NAME=VALUE`` lines out of a host file, refusing anything else.

    The grammar is deliberately not a shell's. Blank lines and ``#`` comments are
    skipped; every other line must be ``NAME=VALUE`` with the name matching
    :data:`ENV_NAME_PATTERN` and the value taken **verbatim** from the first
    ``=`` to the end of the line — no quote stripping, no escape processing, no
    ``export`` prefix, no whitespace trimming. Every one of those conveniences is
    a guess about where a secret begins and ends, and a guess that is wrong once
    has silently changed a credential; a caller whose value really does start
    with a quote character is better served by a parser that keeps it.

    A refusal names the file and the 1-based line number and never quotes the
    line, for the plainest possible reason: the malformed line is exactly the
    kind of line that holds a secret.
    """
    file = Path(path)
    try:
        text = file.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"env-file '{path}' does not exist",
            remediation="pass the path to a file of NAME=VALUE lines that exists and is readable",
        ) from None
    except IsADirectoryError:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"env-file '{path}' is a directory",
            remediation="pass a file of NAME=VALUE lines, not the directory holding it",
        ) from None
    except UnicodeDecodeError:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"env-file '{path}' is not valid UTF-8",
            remediation=(
                "an env-file is text: one NAME=VALUE line per variable. Its bytes are not "
                "quoted here, because a file that failed to decode may still hold a secret"
            ),
        ) from None
    except OSError as err:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"cannot read env-file '{path}': {err.strerror or err}",
            remediation="check the file is readable by the user running headspace",
        ) from err

    assignments: list[tuple[int, str, str]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith(ENV_FILE_COMMENT):
            continue
        name, separator, value = line.partition(ASSIGNMENT_SEPARATOR)
        if not separator or not ENV_NAME_PATTERN.match(name):
            raise CliError(
                code=EXIT_USER_ERROR,
                message=f"env-file '{path}' line {number} is not a NAME=VALUE assignment",
                remediation=(
                    "write one NAME=VALUE per line, with no spaces around the '=', no "
                    "surrounding quotes and no 'export' prefix; the value is taken verbatim "
                    "from the first '=' to the end of the line. The line is not quoted back "
                    "here because it is exactly the kind of line that holds a secret"
                ),
            )
        assignments.append((number, name, value))
    return assignments


def cmd_run(args: argparse.Namespace) -> int:
    # Parsed to completion before a backend is constructed: an unset variable, a
    # malformed env-file line and an escaping input destination are all refused
    # with nothing created, nothing journalled and no engine contacted.
    command = parse_command(args.command)
    declares = parse_declarations(args.declare)
    inputs = parse_inputs(args.input)
    environment = parse_environment(args.env, args.env_file)

    package = orchestrator_for(args).run(
        args.workspace_id,
        command,
        declares=declares,
        inputs=inputs,
        environment=environment,
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
        "--input",
        action="append",
        default=None,
        metavar="NAME=HOST_PATH",
        help="Copy a host file or directory into the workspace before the job starts, "
        "repeatable. NAME is the workspace-relative path it lands at; HOST_PATH may be a "
        "file or a whole directory (a harness and its dependencies are one --input). "
        "headspace records each file's destination, size and sha256 — never its contents.",
    )
    parser.add_argument(
        "--env",
        action="append",
        default=None,
        metavar="NAME",
        help="Forward the variable NAME from your own environment into this job, repeatable. "
        "The value is read here, by name, and never appears in argv, so headspace records "
        "the name and never the value. An unset name is refused rather than forwarded as an "
        "empty string, and --env NAME=VALUE is refused outright: a value typed on the command "
        "line is recorded verbatim, which is the leak this flag exists to close. Edge worth "
        "knowing: a job that prints its own environment (env, printenv, a traceback) writes "
        "the value into its captured output, and captured output is kept — that is the job's "
        "doing, and no recording rule on this side can unsee it. Second edge, in the engine: "
        "while the job container exists the value is readable from its own configuration "
        "(docker inspect shows it under Config.Env), because that is how a process "
        "environment is set — so this keeps a secret out of headspace's durable records, not "
        "out of reach of whoever can already talk to your Docker daemon. The environment "
        "lives for this job only; nothing is baked into the workspace.",
    )
    parser.add_argument(
        "--env-file",
        action="append",
        default=None,
        metavar="PATH",
        help="Read NAME=VALUE lines from a host file and forward all of them, repeatable. "
        "Blank lines and # comments are skipped; every other line must be an assignment, with "
        "no quotes, no 'export' prefix and no spaces around the '=' — the value is taken "
        "verbatim to the end of the line rather than guessed at. headspace records the file's "
        "path and the names it defined, never the values, and the same captured-output edge "
        "described under --env applies.",
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
