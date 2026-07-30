"""Tests for ``headspace.api`` — the declared import surface (issue #18).

Written first, against the three acceptance criteria this task was given:

1. ``headspace.api`` exposes ``create``, ``run``, ``put``, ``export`` and
   ``destroy`` as typed functions delegating to the same
   :class:`~headspace.core.workspace.Orchestrator` entry points the CLI uses,
   with ``__all__`` declaring exactly those five names — no more, no fewer,
   and nothing invented that is not already a parameter of the real method;
2. importing the module succeeds with no reachable engine: no engine probe,
   no ``docker`` SDK import, at module scope;
3. every one of the five operations is reachable, end to end, by a caller who
   never writes ``import headspace.core`` — only ``headspace.api`` itself.

**No engine, ever.** Every functional test here drives the in-memory fake
through ``provider="fake"``; nothing in this file needs a Docker daemon, a
socket or a network. ``HEADSPACE_HOME`` is redirected at a ``tmp_path`` by an
autouse fixture, so no test can see or touch a developer's real store — the
same isolation ``tests/test_cli_verbs.py`` uses for the CLI's own verbs.

**The fake is memoised per process on purpose** (see
:func:`headspace.api._provider_for`), which is what lets a test drive
``create`` and then ``run`` and ``put`` and ``export`` through four separate
calls and have each one find the workspace an earlier call made. The autouse
fixture clears that memo between tests, so no workspace outlives the test
that made it.

**Criterion 2 is proven out of process.** An in-process assertion that
``'docker' not in sys.modules`` cannot rule out some *other* test in this same
session having imported the SDK first (``tests/test_provider_docker.py``
certainly does) — a fresh interpreter is the only place the claim can be
observed honestly, exactly the reasoning ``test_cli_verbs.py`` already uses
for the CLI's own lazy-import proof.

**Criterion 3 is proven by a lifecycle test that imports nothing from
``headspace.core`` at all** — only ``headspace.api``, plus
``headspace.providers.fake`` to script the in-memory engine (a provider is a
sibling of core, not core itself, and scripting it is test setup, not use of
the facade). A *separate* test below does reach into
``headspace.core.workspace.Orchestrator`` — deliberately, and only there — to
mechanically check that every parameter this facade exposes is copied from
the real method, not invented; that is a white-box proof about this module's
own fidelity, not a demonstration of what an external caller needs to do.
"""

from __future__ import annotations

import inspect
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from headspace import api

#: The five verbs this module declares, in the order the plan and the module
#: itself list them.
OPERATIONS: tuple[str, ...] = ("create", "run", "put", "export", "destroy")


# --- fixtures ----------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_store_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point every test at a throwaway store root and hand each a fresh fake.

    ``HEADSPACE_HOME`` is the same environment variable the CLI and
    ``headspace.core.store.Store`` already read; it is set by name here
    (rather than imported from ``headspace.core.store.HOME_ENV_VAR``) so that
    even this shared fixture does not have to reach into ``headspace.core``.
    """
    root = tmp_path / "store"
    monkeypatch.setenv("HEADSPACE_HOME", str(root))
    api._reset_providers()
    yield root
    api._reset_providers()


# --- criterion 1: the declared surface ---------------------------------------


def test_all_declares_exactly_the_five_supported_operations() -> None:
    """``__all__`` is exactly the five named operations — no more, no fewer."""
    assert tuple(api.__all__) == OPERATIONS
    assert len(set(api.__all__)) == len(api.__all__), "no name is declared twice"


def test_every_declared_name_is_a_callable_function() -> None:
    """Every name ``__all__`` promises actually resolves to a function on the module."""
    for name in api.__all__:
        target = getattr(api, name)
        assert callable(target)
        assert inspect.isfunction(target), f"{name} should be a plain function, not {target!r}"


def test_stop_is_not_part_of_the_declared_surface() -> None:
    """``Orchestrator.stop`` exists, but this facade does not promise it.

    Not a claim that ``stop`` is broken — only that naming it here was never
    this module's call to make unilaterally (see the module docstring's
    "Five operations, and deliberately not a sixth").
    """
    assert "stop" not in api.__all__


def test_signatures_are_copied_from_the_real_orchestrator_methods_not_invented() -> None:
    """Every parameter (besides this facade's own ``provider``) exists, identically
    defaulted, on the real ``Orchestrator`` method the function delegates to.

    A mechanical proof rather than an inspection, in the same spirit as this
    repository's other structural tests (e.g. ``test_cli_verbs.py``'s
    registration-contract checks): it is the actual objects being compared,
    not a human's reading of them.
    """
    # Imported locally and only here: this is a white-box check of this
    # module's own fidelity to `headspace.core.workspace.Orchestrator`, not a
    # demonstration of what a caller of the facade needs to import.
    from headspace.core.workspace import Orchestrator

    pairs = (
        (api.create, Orchestrator.create),
        (api.run, Orchestrator.run),
        (api.put, Orchestrator.put),
        (api.export, Orchestrator.export),
        (api.destroy, Orchestrator.destroy),
    )
    for facade_fn, orch_method in pairs:
        facade_params = inspect.signature(facade_fn).parameters
        orch_params = inspect.signature(orch_method).parameters
        assert "provider" in facade_params, f"{facade_fn.__name__} must accept provider="
        for param_name, param in facade_params.items():
            if param_name == "provider":
                # This facade's own construction-wiring addition. It has no
                # counterpart on Orchestrator, whose caller already holds a
                # constructed provider.
                continue
            assert param_name in orch_params, (
                f"{facade_fn.__name__}'s {param_name!r} has no counterpart on "
                f"Orchestrator.{orch_method.__name__}"
            )
            assert param.default == orch_params[param_name].default, (
                f"{facade_fn.__name__}'s {param_name!r} default "
                f"{param.default!r} does not match Orchestrator."
                f"{orch_method.__name__}'s {orch_params[param_name].default!r}"
            )


# --- criterion 2: import stays side-effect free -------------------------------


def test_bare_import_never_touches_the_docker_sdk(tmp_path: Path) -> None:
    """``import headspace.api`` alone must not import ``docker`` or probe an engine.

    Run out of process because this test file's own session may have already
    imported the SDK elsewhere (``tests/test_provider_docker.py``); a fresh
    interpreter is the only place the claim can be observed honestly. The
    fabricated ``DOCKER_HOST`` means any accidental engine probe would fail
    loudly rather than silently succeeding against a real daemon.
    """
    script = (
        "import sys; import headspace.api;"
        "assert 'docker' not in sys.modules, sorted(m for m in sys.modules if 'docker' in m)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "HEADSPACE_HOME": str(tmp_path / "subprocess-store"),
            "DOCKER_HOST": "unix:///nonexistent/docker.sock",
        },
        check=False,
        cwd=str(Path(api.__file__).resolve().parents[1]),
    )
    assert completed.returncode == 0, completed.stderr


def test_full_fake_lifecycle_never_touches_the_docker_sdk(tmp_path: Path) -> None:
    """Exercising all five operations against ``provider="fake"`` still imports no SDK.

    The stronger, "genuinely convincing" claim: not just a bare import, but a
    complete create → run → put → export → destroy lifecycle driven entirely
    through the fake backend, in a fresh interpreter with no Docker socket
    reachable, and ``docker`` is still absent from ``sys.modules`` afterwards.
    """
    workdir = tmp_path / "subprocess-store"
    source = tmp_path / "payload.bin"
    source.write_bytes(b"hello from the host")
    destination = tmp_path / "exported.bin"

    script = f"""
import sys
from headspace import api
from headspace.providers.fake import FakeProvider, JobPlan

created = api.create(provider="fake")
assert created.status == "success", created.status
workspace_id = created.provenance.workspace_id
assert workspace_id

fake = api._provider_for("fake")
assert isinstance(fake, FakeProvider)
fake.script_command(["write"], JobPlan.succeeding("", writes={{"out.bin": b"payload"}}))

ran = api.run(
    workspace_id,
    ["write"],
    declares=[api.ArtifactDeclaration(name="out.bin", purpose="round-trip proof")],
    provider="fake",
)
assert ran.status == "success", ran.status

put_result = api.put(workspace_id, {str(source)!r}, "in.bin", provider="fake")
assert put_result.status == "success", put_result.status

exported = api.export(
    workspace_id, "out.bin", destination={str(destination)!r}, provider="fake"
)
assert exported.status == "success", exported.status

destroyed = api.destroy(workspace_id, provider="fake")
assert destroyed.status == "success", destroyed.status

assert 'docker' not in sys.modules, sorted(m for m in sys.modules if 'docker' in m)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "HEADSPACE_HOME": str(workdir),
            "DOCKER_HOST": "unix:///nonexistent/docker.sock",
        },
        check=False,
        cwd=str(Path(api.__file__).resolve().parents[1]),
    )
    assert completed.returncode == 0, completed.stderr
    assert destination.read_bytes() == b"payload"


# --- criterion 3: every operation reachable without touching headspace.core --


def test_full_lifecycle_reachable_through_the_facade_alone(tmp_path: Path) -> None:
    """create → run → put → export → destroy, using only names from ``headspace.api``.

    This test's own imports prove the point structurally: nothing here comes
    from ``headspace.core``. ``ArtifactDeclaration`` is reached as
    ``api.ArtifactDeclaration`` — the same class ``headspace.core.workspace``
    defines, but never imported from there by this test.
    """
    from headspace.providers.fake import FakeProvider, JobPlan

    created = api.create(provider="fake")
    assert created.status == "success"
    workspace_id = created.provenance.workspace_id
    assert workspace_id

    fake = api._provider_for("fake")
    assert isinstance(fake, FakeProvider)
    fake.script_command(["write"], JobPlan.succeeding("", writes={"out.bin": b"payload"}))

    ran = api.run(
        workspace_id,
        ["write"],
        declares=[api.ArtifactDeclaration(name="out.bin", purpose="round-trip proof")],
        provider="fake",
    )
    assert ran.status == "success"
    assert ran.provenance.workspace_id == workspace_id

    source = tmp_path / "payload.bin"
    source.write_bytes(b"hello from the host")
    put_result = api.put(workspace_id, source, "in.bin", provider="fake")
    assert put_result.status == "success"

    destination = tmp_path / "exported.bin"
    exported = api.export(workspace_id, "out.bin", destination=destination, provider="fake")
    assert exported.status == "success"
    assert destination.read_bytes() == b"payload"

    destroyed = api.destroy(workspace_id, provider="fake")
    assert destroyed.status == "success"


def test_create_honours_an_explicit_workspace_id() -> None:
    result = api.create(workspace_id="ws-explicit", provider="fake")
    assert result.status == "success"
    assert result.provenance.workspace_id == "ws-explicit"


def test_run_reports_a_job_failure_as_a_failure_status_not_an_exception() -> None:
    """A job that fails is a normal, successfully-reported result — not a raised error."""
    from headspace.providers.fake import JobPlan

    created = api.create(provider="fake")
    workspace_id = created.provenance.workspace_id

    fake = api._provider_for("fake")
    fake.script_command(["boom"], JobPlan.failing(1, "it broke"))

    result = api.run(workspace_id, ["boom"], provider="fake")
    assert result.status == "failure"


def test_export_of_an_undeclared_artifact_raises_a_structured_error() -> None:
    """The orchestrator's own guard surfaces unchanged through the facade.

    Proves the facade does not swallow, translate, or otherwise refactor the
    real method's error behaviour — it only reaches it.
    """
    from headspace.cli._errors import EXIT_USER_ERROR, CliError

    created = api.create(provider="fake")
    workspace_id = created.provenance.workspace_id

    with pytest.raises(CliError) as excinfo:
        api.export(workspace_id, "ghost", destination="/tmp/ghost.bin", provider="fake")
    assert excinfo.value.code == EXIT_USER_ERROR


def test_destroy_refuses_unexported_artifacts_without_force() -> None:
    from headspace.cli._errors import CliError
    from headspace.providers.fake import JobPlan

    created = api.create(provider="fake")
    workspace_id = created.provenance.workspace_id

    fake = api._provider_for("fake")
    fake.script_command(["write"], JobPlan.succeeding("", writes={"out.bin": b"payload"}))
    api.run(
        workspace_id,
        ["write"],
        declares=[api.ArtifactDeclaration(name="out.bin", purpose="never exported")],
        provider="fake",
    )

    with pytest.raises(CliError):
        api.destroy(workspace_id, provider="fake")

    # Nothing was removed: a second destroy still sees the same refusal.
    with pytest.raises(CliError):
        api.destroy(workspace_id, provider="fake")

    # force discards it and succeeds.
    forced = api.destroy(workspace_id, force=True, provider="fake")
    assert forced.status == "success"


# --- provider construction wiring --------------------------------------------


def test_fake_provider_is_memoised_across_calls_in_one_process() -> None:
    first = api._provider_for("fake")
    second = api._provider_for("fake")
    assert first is second


def test_reset_providers_clears_the_memo() -> None:
    first = api._provider_for("fake")
    api._reset_providers()
    second = api._provider_for("fake")
    assert first is not second


def test_unknown_provider_is_a_structured_error() -> None:
    from headspace.cli._errors import EXIT_USER_ERROR, CliError

    with pytest.raises(CliError) as excinfo:
        api.create(provider="podman")
    assert excinfo.value.code == EXIT_USER_ERROR
