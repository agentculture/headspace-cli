"""``headspace create`` — provision a workspace, and the seam its four siblings share.

WHY this module carries more than one verb
------------------------------------------
``create`` is the root of the lifecycle: it is the only verb that mints a
workspace id, the only one that resolves a policy from flags, and therefore the
one place the *shape* of a lifecycle invocation is decided. The other four
verbs need exactly three things from that shape — a backend, a rendering, and
an exit code — and if each of them built its own, five verbs would offer five
subtly different ``--provider`` flags and five ways to turn a result status into
a process exit. So the shared pieces live here and the siblings import them.
That is the same pattern the introspection verbs already use (``doctor`` imports
its identity reader from ``whoami``; the ``cli`` noun imports its section
renderer from ``overview``), applied to the lifecycle group.

The three consumer classes (claim c19)
--------------------------------------
Every one of these verbs returns a
:class:`~headspace.core.result.ResultPackage`, and that package has exactly two
renderings for three audiences:

* **AI agents** and **humans** read the default markdown, from
  :func:`~headspace.core.result.render_markdown`;
* **scripts and non-AI bots** read ``--json``, from
  :func:`~headspace.core.result.bounded_dict`.

Both come from one structure, so the two can never disagree about content — the
CLI never renders text and parses it back. Results go to stdout and errors to
stderr, never mixed, which is what makes ``--json`` safe to pipe.

Provider selection, and why the docker import is lazy
-----------------------------------------------------
``--provider`` picks the backend. The docker SDK is imported *inside*
:func:`provider_for`, never at module scope, so ``headspace --help``, every
``--provider fake`` invocation and every unit test run on a host with no engine
and pay none of the SDK's import cost. The flag is not a testing affordance
alone: reconciliation refuses to reap a workspace journalled under another
backend's name, so naming the backend is how a caller stays out of the other
one's way.

Instances are memoised for the life of the process. For a real invocation that
changes nothing — one process runs one verb — but it is what the in-memory fake
*requires* to be usable at all: its workspaces live in the object, so a caller
driving several verbs through :func:`headspace.cli.main` in one process must be
handed the same instance or the second verb would not find the first's work.
:func:`reset_providers` exists so a test suite can put that back to nothing
between tests.

What this module deliberately does not decide
---------------------------------------------
Nothing here interprets a lifecycle rule. The orchestrator resolves the policy
against the host's capabilities, journals before it calls the engine, reconciles
orphans and reports them as attention items; this module turns flags into its
arguments and its answer into bytes on a stream. If a verb here starts making a
lifecycle decision of its own, that decision has escaped the one place all five
verbs share it.
"""

from __future__ import annotations

import argparse
from dataclasses import fields

from headspace.cli._errors import EXIT_USER_ERROR, CliError
from headspace.cli._output import emit_result
from headspace.core.policy import FilesystemScope, NetworkPosture, Policy, ResourceBudget
from headspace.core.profiles import DEFAULT_PROFILE, REGISTRY
from headspace.core.result import (
    DEFAULT_MAX_BYTES,
    ResultPackage,
    bounded_dict,
    render_markdown,
)
from headspace.core.workspace import Orchestrator, exit_code_for_status
from headspace.providers.base import Provider

#: The backends ``--provider`` accepts. ``docker`` is the product; ``fake`` is
#: the in-memory backend that passes the same conformance suite, and is what
#: makes the whole CLI exercisable with no engine installed.
PROVIDER_DOCKER = "docker"
PROVIDER_FAKE = "fake"
PROVIDER_CHOICES: tuple[str, ...] = (PROVIDER_DOCKER, PROVIDER_FAKE)
DEFAULT_PROVIDER = PROVIDER_DOCKER

#: Field type name -> argparse ``type``. Written as a map rather than inferred
#: so that adding a ceiling of some new kind to :class:`ResourceBudget` fails
#: loudly here instead of quietly shipping a flag that parses it wrong. A test
#: asserts every budget field is reachable from the flag surface.
_BUDGET_TYPES: dict[str, type] = {"int": int, "float": float}

#: One line per ceiling, for ``--help``. Keyed by field name; a field with no
#: entry still gets a flag, just a duller description.
_BUDGET_HELP: dict[str, str] = {
    "memory_bytes": "Memory ceiling, in bytes.",
    "cpu_limit": "CPU ceiling, in cores (fractional allowed).",
    "pids_limit": "Ceiling on concurrent processes inside the workspace.",
    "storage_bytes": "Storage ceiling, in bytes. Measured, not enforced, on most hosts.",
    "wall_clock_seconds": "Wall-clock ceiling for a single job, in seconds.",
    "output_bytes": "Captured-output ceiling for a single job, in bytes.",
    "concurrency": "Ceiling on jobs running in this workspace at once.",
}

_INSTANCES: dict[str, Provider] = {}


# --- the shared seam --------------------------------------------------------


def provider_for(name: str) -> Provider:
    """The backend named ``name``, constructed once per process.

    Neither backend touches an engine when constructed — the Docker provider
    connects on its first verb — so this is cheap and safe to call on a host
    with no daemon. The import is inside the branch on purpose: importing the
    docker SDK is the expensive part, and the fake path must never pay it.
    """
    if name not in _INSTANCES:
        _INSTANCES[name] = _build_provider(name)
    return _INSTANCES[name]


def reset_providers() -> None:
    """Forget every memoised backend. For test isolation, not for callers."""
    _INSTANCES.clear()


def _build_provider(name: str) -> Provider:
    if name == PROVIDER_FAKE:
        from headspace.providers.fake import FakeProvider

        return FakeProvider()
    if name == PROVIDER_DOCKER:
        from headspace.providers.docker import DockerProvider

        return DockerProvider()
    raise CliError(
        code=EXIT_USER_ERROR,
        message=f"unknown provider {name!r}",
        remediation="choose one of: " + ", ".join(PROVIDER_CHOICES),
    )


def add_common_flags(parser: argparse.ArgumentParser) -> None:
    """The three flags every lifecycle verb carries, added the same way once."""
    parser.add_argument(
        "--provider",
        choices=PROVIDER_CHOICES,
        default=DEFAULT_PROVIDER,
        help="Execution backend to drive (default: %(default)s). 'fake' is the in-memory "
        "backend and needs no engine.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the result package as JSON — the rendering for scripts and non-AI bots. "
        "The default markdown is the rendering for agents and humans.",
    )
    parser.add_argument(
        "--max-result-bytes",
        type=int,
        default=DEFAULT_MAX_BYTES,
        metavar="N",
        help="Byte bound for the rendered result package (default: %(default)s). Distinct "
        "from --output-bytes, which bounds what a job's own output may occupy.",
    )


def add_budget_flags(parser: argparse.ArgumentParser) -> None:
    """Generate one flag per :class:`ResourceBudget` field.

    Generated rather than hand-written so a ceiling cannot be added to the
    budget and left unreachable from the CLI. Defaults stay ``None`` here: the
    dataclass owns the closed defaults, and only a flag the caller actually
    passed overrides one.
    """
    for budget_field in fields(ResourceBudget):
        parser.add_argument(
            "--" + budget_field.name.replace("_", "-"),
            type=_BUDGET_TYPES[str(budget_field.type)],
            default=None,
            metavar="N",
            help=_BUDGET_HELP.get(budget_field.name, f"Ceiling for {budget_field.name}.")
            + f" Default: {getattr(ResourceBudget(), budget_field.name)}.",
        )


def orchestrator_for(args: argparse.Namespace) -> Orchestrator:
    """One orchestrator per invocation, over the backend the caller named."""
    return Orchestrator(provider_for(str(getattr(args, "provider", DEFAULT_PROVIDER))))


def emit_package(package: ResultPackage, args: argparse.Namespace) -> int:
    """Render one result package and return the process exit code it implies.

    The exit code is :func:`~headspace.core.workspace.exit_code_for_status`
    applied to the package's own status — so a job that ran correctly and failed
    exits 6, a job the budget stopped exits 4, and a workspace that was simply
    provisioned exits 0. Non-zero here never means "the CLI broke"; that is what
    the ``CliError`` path is for.
    """
    max_bytes = int(getattr(args, "max_result_bytes", DEFAULT_MAX_BYTES))
    if bool(getattr(args, "json", False)):
        emit_result(bounded_dict(package, max_bytes=max_bytes), json_mode=True)
    else:
        emit_result(render_markdown(package, max_bytes=max_bytes), json_mode=False)
    return exit_code_for_status(package.status)


# --- the verb ---------------------------------------------------------------


def declared_policy(args: argparse.Namespace) -> Policy:
    """Build the caller's declared policy from the flags they actually passed.

    Every default is the closed one :class:`Policy` already carries, so a caller
    who declares nothing gets a workspace with no network, no host paths and the
    small budgets — the posture stays closed by omission, never by this module
    remembering to close it.
    """
    overrides = {
        budget_field.name: value
        for budget_field in fields(ResourceBudget)
        if (value := getattr(args, budget_field.name, None)) is not None
    }
    return Policy(
        network=NetworkPosture(args.network),
        filesystem=FilesystemScope(host_paths=tuple(args.allow_host_path or ())),
        budget=ResourceBudget(**overrides),
    )


def cmd_create(args: argparse.Namespace) -> int:
    package = orchestrator_for(args).create(
        profile=args.profile,
        policy=declared_policy(args),
        workspace_id=args.workspace_id,
    )
    return emit_package(package, args)


def register(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser(
        "create",
        help="Provision an ephemeral workspace and report it as a result package.",
    )
    parser.add_argument(
        "--profile",
        default=DEFAULT_PROFILE,
        metavar="NAME",
        # Deliberately not an argparse ``choices``: the registry already refuses
        # an unknown profile with a hint that lists the valid names, and a second
        # copy of that list here is a second place for it to go stale.
        help="Runtime profile the workspace runs (default: %(default)s). Each is pinned to "
        "an exact image digest. Registered: " + ", ".join(sorted(REGISTRY)) + ".",
    )
    parser.add_argument(
        "--workspace-id",
        default=None,
        metavar="ID",
        help="Use this workspace id instead of a minted one. Refused if it already exists.",
    )
    parser.add_argument(
        "--network",
        choices=[posture.value for posture in NetworkPosture],
        default=NetworkPosture.DISABLED.value,
        help="Network posture for the workspace (default: %(default)s).",
    )
    parser.add_argument(
        "--allow-host-path",
        action="append",
        default=None,
        metavar="PATH",
        help="Host path the workspace may see, repeatable. Fails closed on a host that "
        "cannot enforce the scope — which is every host the MVP Docker provider runs on.",
    )
    add_budget_flags(parser)
    add_common_flags(parser)
    parser.set_defaults(func=cmd_create)
