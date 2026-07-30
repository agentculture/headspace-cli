"""``headspace.api`` — the declared import surface into headspace-cli, as a library.

WHY this module exists (issue #18)
-----------------------------------
headspace-cli ships a CLI, and underneath it, a plain Python package. Nothing
ever stopped a caller from writing ``from headspace.core.workspace import
Orchestrator`` directly: ``headspace/__init__.py`` declared only
``__version__`` in its own ``__all__``, and ``headspace/core/__init__.py``
declared no ``__all__`` at all. That silence is not neutral. A caller reading
it from the outside cannot distinguish "CLI-first, the package is reachable
but unsupported" from "library, just undocumented" — and agentculture's
embodiment project asked exactly that question (issue #18) because the gap
was real, not a misunderstanding on their part: every name a caller would
want — ``core.workspace``, ``core.policy``, ``core.result`` — imported
cleanly and answered nothing about whether it was safe to depend on.

This module is the answer, settled as a narrow declared facade rather than a
blanket "the package is the API": **``headspace.api`` is the one supported
import surface, versioned under this project's own semver promise.**
``headspace.core`` stays exactly as private as its own module docstring
already insists — "nothing in core may import a provider or the docker SDK" —
and now the same discipline runs the other way too: nothing outside this
module should import ``headspace.core`` directly, because doing so opts out
of the one surface this project promises to keep stable across a release.

Five operations, and deliberately not a sixth
----------------------------------------------
``create``, ``run``, ``put``, ``export``, ``destroy`` — chosen because they
are the five lifecycle verbs the CLI itself exposes with a stable
``ResultPackage`` contract (``headspace create|run|put|export|destroy``).
:class:`~headspace.core.workspace.Orchestrator` carries a sixth method,
``stop``, which the CLI renders as ``headspace stop``. It is left out of
``__all__`` here on purpose, and the reason is narrower than "it doesn't
matter": the plan that commissioned this module named exactly five
operations, ``stop`` is a preview-by-default verb with a materially different
contract from the other five (it takes no lock, writes no state, and acts on
the engine only when ``apply=True`` — see ``Orchestrator.stop``'s own
docstring), and guessing that it belongs on this surface anyway would be
precisely the kind of unilateral call this task was told to report rather
than make. A consumer that needs it programmatically is a real, open question
for the next revision of this surface — not a name quietly added here because
it was easy to wrap.

WHY wrapping, not refactoring
------------------------------
Every function below is a thin composition of the same steps the CLI's own
``headspace/cli/_commands/*.py`` modules perform before they ever call into
:class:`~headspace.core.workspace.Orchestrator`: resolve which backend serves
the call, build a fresh orchestrator over it and the default on-disk
:class:`~headspace.core.store.Store`, and delegate. The logic is *mirrored*
here rather than imported from ``headspace.cli._commands.create`` on purpose:
that package is the CLI's own private wiring (its leading underscore says so),
and this facade owes it no dependency in either direction — the CLI's argument
parsing is free to change without touching this module, and this module's
supported surface is free to stay put without pinning the CLI's internals.

Nothing about ``Orchestrator.create``, ``.run``, ``.put``, ``.export`` or
``.destroy`` changed to make this possible. Each was already a
``ResultPackage``-returning entry point with a stable contract, verified
present at the lines this task was handed (``create`` ~758, ``run`` ~866,
``export`` ~1097, ``put`` ~1219, ``destroy`` ~1595 in ``core/workspace.py``).
Refactoring the orchestration layer was explicitly out of scope for this
module, and nothing here needed it — every parameter below is copied from the
real method it delegates to, not invented.

WHY the docker import stays lazy
-----------------------------------
``python -c 'import headspace.api'`` has to succeed on a host with no Docker
daemon reachable at all: a CI lint step, an integrator's test-collection pass,
a machine that has never run ``docker`` once. That is only true if
constructing a backend — and therefore importing the ``docker`` SDK, which
``headspace/providers/docker.py`` does at its own module scope — never
happens at *this* module's scope. So :func:`_provider_for` imports
``headspace.providers.fake`` or ``headspace.providers.docker`` **inside its
own branches**, exactly the pattern ``headspace/cli/_commands/create.py``'s
``provider_for`` already uses for the same reason, and this module's own test
suite proves the claim out-of-process — an in-process assertion cannot rule
out some *other* test in the same run having imported the SDK first.

WHY a ``provider`` keyword, and why it is memoised per name
----------------------------------------------------------------
Every function below builds its own orchestrator, and building one needs a
backend. The CLI answers that with ``--provider`` (default ``"docker"``);
this facade answers it with a ``provider`` keyword carrying the same default
and the same two choices: ``"docker"``, the product, and ``"fake"``, the
in-memory backend that makes this whole module callable with no engine at all
(see ``headspace/providers/fake.py``). Instances are memoised per name for the
life of the process, exactly as the CLI's own ``provider_for`` memoises them,
and for the same reason: the fake backend's workspaces live inside the Python
object itself, so a caller driving ``create`` and then ``run`` against
``provider="fake"`` in one process needs the second call to find the first
call's workspace, not a second, empty ``FakeProvider``. :func:`_reset_providers`
exists so a test suite can clear that memo between cases; its leading
underscore says it is not part of the declared surface, the same way
``headspace.cli._commands.create.reset_providers`` is not part of the CLI's.

The on-disk :class:`~headspace.core.store.Store`, by contrast, is never
memoised here: it is stateless between calls beyond a lock file descriptor
held only for the duration of one orchestrator method, so each function opens
a fresh one rooted at ``$HEADSPACE_HOME`` (or ``~/.headspace``) — the same
root every CLI invocation already reads and writes. That is what lets a real
``provider="docker"`` caller create a workspace in one process and run it from
a completely different one, the same way ``headspace create`` and a later,
separate ``headspace run`` already do.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence

from headspace.cli._errors import EXIT_USER_ERROR, CliError
from headspace.core.artifacts import ByteSource
from headspace.core.policy import Policy
from headspace.core.profiles import DEFAULT_PROFILE
from headspace.core.result import ResultPackage
from headspace.core.workspace import (
    ArtifactDeclaration,
    InputRequest,
    JobEnvironment,
    Orchestrator,
)
from headspace.providers.base import Provider

__all__ = ["create", "run", "put", "export", "destroy"]

#: The backends :func:`_provider_for` accepts — identical to the CLI's own
#: ``--provider`` choices (``headspace/cli/_commands/create.py``), and kept as
#: a separate literal set rather than imported from there for the same reason
#: the construction logic itself is mirrored, not imported: this module owns
#: no dependency on the CLI's private command wiring. Underscore-prefixed like
#: every other name here that is not one of the five declared operations —
#: this module is the fix for "reachable but undeclared" (issue #18), so it
#: must not reintroduce the same ambiguity about its own internals.
_PROVIDER_DOCKER = "docker"
_PROVIDER_FAKE = "fake"
_PROVIDER_CHOICES: tuple[str, ...] = (_PROVIDER_DOCKER, _PROVIDER_FAKE)

#: One backend instance per name, for the life of the process. See the module
#: docstring's "WHY a ``provider`` keyword" section for why this exists.
_PROVIDERS: dict[str, Provider] = {}


def _provider_for(name: str) -> Provider:
    """The backend named ``name``, constructed once per process.

    Neither backend touches an engine when constructed — the Docker provider
    connects on its first verb — so this is cheap and safe to call on a host
    with no daemon, provided ``name`` is ``"fake"``. The import is inside the
    branch on purpose: importing the docker SDK is the expensive, engine-
    coupled part, and the fake path — and therefore a bare ``import
    headspace.api`` — must never pay it.
    """
    if name not in _PROVIDERS:
        _PROVIDERS[name] = _build_provider(name)
    return _PROVIDERS[name]


def _reset_providers() -> None:
    """Forget every memoised backend. For test isolation, not for callers."""
    _PROVIDERS.clear()


def _build_provider(name: str) -> Provider:
    if name == _PROVIDER_FAKE:
        from headspace.providers.fake import FakeProvider

        return FakeProvider()
    if name == _PROVIDER_DOCKER:
        from headspace.providers.docker import DockerProvider

        return DockerProvider()
    raise CliError(
        code=EXIT_USER_ERROR,
        message=f"unknown provider {name!r}",
        remediation="choose one of: " + ", ".join(_PROVIDER_CHOICES),
    )


def _orchestrator_for(provider: str) -> Orchestrator:
    """One orchestrator per call, over the backend named, and a fresh on-disk store.

    Mirrors ``headspace.cli._commands.create.orchestrator_for``: a fresh
    :class:`~headspace.core.workspace.Orchestrator` every call, over a
    memoised provider and a default-rooted
    :class:`~headspace.core.store.Store` that reads and writes exactly the
    same ``$HEADSPACE_HOME`` tree the CLI does.
    """
    return Orchestrator(_provider_for(provider))


# --- the five declared operations -------------------------------------------


def create(
    *,
    profile: str = DEFAULT_PROFILE,
    policy: Policy | None = None,
    workspace_id: str | None = None,
    provider: str = _PROVIDER_DOCKER,
) -> ResultPackage:
    """Provision a fresh workspace. Delegates to :meth:`Orchestrator.create`.

    ``policy`` left as ``None`` gets the same closed-by-default posture a
    caller who passes no flags to ``headspace create`` gets: no network, no
    host paths, the small budgets — see :class:`~headspace.core.policy.Policy`.
    """
    return _orchestrator_for(provider).create(
        profile=profile, policy=policy, workspace_id=workspace_id
    )


def run(
    workspace_id: str,
    command: Sequence[str],
    *,
    declares: Iterable[ArtifactDeclaration] = (),
    inputs: Sequence[InputRequest] = (),
    environment: JobEnvironment | None = None,
    job_id: str | None = None,
    provider: str = _PROVIDER_DOCKER,
) -> ResultPackage:
    """Execute one command in an existing workspace. Delegates to :meth:`Orchestrator.run`.

    ``declares`` registers outputs the job promises to produce — only a
    declared name can later be exported, and only a declared name is guarded
    by ``destroy``. ``inputs`` copies host bytes in before the command starts.
    ``environment`` is the secret channel: its values reach the job and never
    any recording surface; only its variable names do (see
    :class:`~headspace.core.workspace.JobEnvironment`).
    """
    return _orchestrator_for(provider).run(
        workspace_id,
        command,
        declares=declares,
        inputs=inputs,
        environment=environment,
        job_id=job_id,
    )


def put(
    workspace_id: str,
    host_path: str | os.PathLike[str],
    destination: str,
    *,
    overwrite: bool = False,
    provider: str = _PROVIDER_DOCKER,
) -> ResultPackage:
    """Copy host bytes into a workspace. Delegates to :meth:`Orchestrator.put`.

    Refuses an existing ``destination`` unless ``overwrite`` is set — a
    copy-in must not silently clobber a previous job's output.
    """
    return _orchestrator_for(provider).put(
        workspace_id, host_path, destination, overwrite=overwrite
    )


def export(
    workspace_id: str,
    name: str,
    source: ByteSource | None = None,
    destination: str | os.PathLike[str] | None = None,
    *,
    expected_sha256: str | None = None,
    path: str | None = None,
    provider: str = _PROVIDER_DOCKER,
) -> ResultPackage:
    """Publish a declared artifact durably. Delegates to :meth:`Orchestrator.export`.

    ``destination`` is required in substance — the orchestrator itself refuses
    ``None`` — but stays optional in this signature because that is the real
    method's own shape; the refusal is not re-implemented here, only reached.
    Omit ``source`` and the bytes are pulled from ``path`` inside the
    workspace (defaulting to ``name``) through the provider's ``read`` verb —
    the only path the CLI can take, since the bytes live inside a workspace
    volume no process outside the engine can open. A library caller that
    already holds the bytes may pass ``source`` directly, skipping that read
    entirely — a capability the Orchestrator has always had that the CLI has
    no flag for, because the CLI never holds bytes of its own to offer.
    """
    return _orchestrator_for(provider).export(
        workspace_id,
        name,
        source,
        destination,
        expected_sha256=expected_sha256,
        path=path,
    )


def destroy(
    workspace_id: str,
    *,
    force: bool = False,
    provider: str = _PROVIDER_DOCKER,
) -> ResultPackage:
    """Tear a workspace down, or refuse. Delegates to :meth:`Orchestrator.destroy`.

    Refuses, and removes nothing, when declared artifacts were never
    exported — unless ``force`` is set, in which case they are discarded and
    each one is still named in the returned result package.
    """
    return _orchestrator_for(provider).destroy(workspace_id, force=force)
