"""The Docker half of the secret channel and the ``stop`` verb (issue #13, #14).

WHY this module exists
----------------------
Two features, one boundary each, and both boundaries are about a value going
somewhere it must never reach.

``run(..., env=...)`` (issue #13) opens a channel for a caller to hand a job
process secrets — an API token, a credential — without paying the two costs
every existing channel already had: ``argv`` is world-readable off
``/proc/<pid>/cmdline`` and every surface that renders a command, and a label
is readable by anyone who can run ``docker inspect`` (this provider already
treats labels as public — see ``headspace/providers/docker.py``'s own module
docstring, "Why labels, and never handles"). So the whole of what this half of
the module proves is a *placement* claim: ``env`` lands in the job container's
``environment=`` creation kwarg, in no label, in no argv entry, and never on
the long-lived anchor container :meth:`~headspace.providers.docker.DockerProvider.create`
makes — a secret handed to one job must not silently become readable by every
job that workspace ever runs afterward.

``stop`` ends a job that :meth:`~headspace.providers.docker.DockerProvider.run`
is still blocking on, from a *second* CLI invocation — ``run`` holds the
workspace's flock for a job's whole duration, so nothing calling through
``run`` can ever be the one to end it early. That makes ``stop`` engine-side
only by construction: it may not take the store's lock or write the store's
state, because ``run`` is already the store's one writer for this workspace,
and a second writer racing it is exactly the bug the store's locking exists to
prevent. This module proves that boundary two ways: functionally (the label
lookup, the stop-then-kill escalation, the honest "nothing was running"
report) and structurally (``stop`` never constructs the state store at all,
proven by making the constructor raise and calling ``stop`` anyway).

No engine required
-------------------
Every test here runs against :class:`_StubEngine`, a hand-written stand-in for
``docker.DockerClient`` carrying just the surface
:class:`~headspace.providers.docker.DockerProvider` actually touches for
``create``, ``run`` and ``stop``. The real provider is injected with
``connect=``, so nothing about the code under test is stubbed — only the
daemon underneath it, following the same shape
``tests/test_docker_classification.py`` already established for this module.

``stop``'s tests build their job container directly against the stub's
registry rather than by calling ``provider.run()`` first. That is deliberate,
not a shortcut: ``run()`` is synchronous and would already have finished — and
reaped its own container — by the time a test could call ``stop()``, exactly
as it would in production if the two ran in the same process. The container
:func:`_in_flight_job` registers is what a *second*, concurrent ``run()`` call
would have left behind while still blocked inside its own wait, which is the
only case ``stop`` exists to reach.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from headspace.cli._errors import EXIT_USER_ERROR, CliError
from headspace.core import profiles
from headspace.core.policy import EffectivePolicy, Policy
from headspace.core.policy import resolve as resolve_policy
from headspace.core.store import HOME_ENV_VAR, Store
from headspace.providers.docker import (
    LABEL_CREATED_AT,
    LABEL_JOB_ID,
    LABEL_PROVIDER,
    LABEL_ROLE,
    LABEL_WORKSPACE_ID,
    PROVIDER_NAME,
    ROLE_JOB,
    ROLE_WORKSPACE,
    DockerProvider,
)

WORKSPACE = "ctrl-ws"

#: The environment the fixture workspace is created from — the default
#: profile's own digest-pinned reference.
ENVIRONMENT = profiles.resolve(profiles.DEFAULT_PROFILE)


# --- a hand-written stand-in for the docker SDK, scoped to this module's needs


def _label_filter(filters: Mapping[str, Any] | None) -> dict[str, str]:
    pairs = (filters or {}).get("label", [])
    return dict(str(pair).split("=", 1) for pair in pairs)


def _matches(container: "_StubContainer", wanted: Mapping[str, str]) -> bool:
    return all(container.labels.get(key) == value for key, value in wanted.items())


class _StubContainer:
    """One engine container: state plus every kwarg it was created with.

    ``kwargs`` is the whole of what
    :meth:`~headspace.providers.docker.DockerProvider` passed to
    ``client.containers.create`` beyond ``image`` — ``command``, ``labels``,
    ``environment`` and the closed-posture kwargs — kept verbatim rather than
    picked apart, so a test asserts what the provider actually sent the engine
    instead of a paraphrase of it.
    """

    def __init__(self, image: str, kwargs: dict[str, Any]) -> None:
        self.image = image
        self.kwargs = kwargs
        self.labels: dict[str, str] = dict(kwargs.get("labels") or {})
        self.command: Any = kwargs.get("command")
        self.id = f"stub-{id(self):x}"
        self.attrs: dict[str, Any] = {
            "Config": {"Image": image},
            "HostConfig": {"NetworkMode": str(kwargs.get("network_mode") or "none")},
            "State": {"Status": "created", "ExitCode": None, "OOMKilled": False},
        }
        self.removed = False
        #: Every ``timeout=`` a graceful stop was called with, in call order.
        self.stop_calls: list[Any] = []
        self.kill_calls = 0
        #: Armed by a test: whether a graceful ``stop()`` alone ends the job.
        #: ``False`` (the default) means it does — the ordinary case, a job
        #: that caught ``SIGTERM`` and exited inside its grace window.
        #: ``True`` models a job that ignores it, so ``stop`` must escalate.
        self.survives_stop = False

    def start(self) -> None:
        # Settles immediately: nothing in this module drives a real wall
        # clock, and the env/label/command placement this module proves does
        # not depend on how long the job ran.
        self.attrs["State"] = {"Status": "exited", "ExitCode": 0, "OOMKilled": False}

    def reload(self) -> None:
        """The stub's state is already current; nothing to re-fetch."""

    def remove(self, **_: Any) -> None:
        self.removed = True

    def attach(self, **_: Any) -> Iterator[bytes]:
        return iter(())

    def logs(self, **_: Any) -> Iterator[bytes]:
        return iter(())

    def stats(self, **_: Any) -> dict[str, Any]:
        return {}

    def stop(self, timeout: Any = None) -> None:
        self.stop_calls.append(timeout)
        if not self.survives_stop:
            self.attrs["State"] = {"Status": "exited", "ExitCode": 143, "OOMKilled": False}

    def kill(self) -> None:
        self.kill_calls += 1
        self.attrs["State"] = {"Status": "exited", "ExitCode": 137, "OOMKilled": False}


class _StubImages:
    def get(self, reference: str) -> Any:
        return reference


class _StubVolume:
    def __init__(self, name: str) -> None:
        self.name = name

    def remove(self, **_: Any) -> None:
        pass


class _StubVolumes:
    def __init__(self, engine: "_StubEngine") -> None:
        self._engine = engine

    def create(self, name: str, **_: Any) -> _StubVolume:
        volume = _StubVolume(name)
        self._engine.volume_registry.append(volume)
        return volume


class _StubContainers:
    def __init__(self, engine: "_StubEngine") -> None:
        self._engine = engine

    def create(self, image: str, **kwargs: Any) -> _StubContainer:
        container = _StubContainer(image, kwargs)
        self._engine.registry.append(container)
        return container

    def list(self, all: bool = False, filters: Mapping[str, Any] | None = None) -> list[Any]:
        del all  # the stub prunes nothing; every container it holds is listed
        wanted = _label_filter(filters)
        return [c for c in self._engine.registry if _matches(c, wanted) and not c.removed]


class _StubEngine:
    """A stand-in for ``docker.DockerClient``: state, no daemon, no network."""

    def __init__(self) -> None:
        self.containers = _StubContainers(self)
        self.volumes = _StubVolumes(self)
        self.images = _StubImages()
        self.registry: list[_StubContainer] = []
        self.volume_registry: list[_StubVolume] = []
        self.closed = 0

    def version(self) -> dict[str, Any]:
        return {"Version": "29.1.3", "ApiVersion": "1.52"}

    def info(self) -> dict[str, Any]:
        return {
            "ServerVersion": "29.1.3",
            "MemoryLimit": True,
            "CpuCfsQuota": True,
            "CpuCfsPeriod": True,
            "PidsLimit": True,
            "Plugins": {"Network": ["bridge", "null"], "Volume": ["local"]},
        }

    def df(self) -> dict[str, Any]:
        return {
            "Volumes": [{"Name": v.name, "UsageData": {"Size": 0}} for v in self.volume_registry]
        }

    def close(self) -> None:
        self.closed += 1


def _in_flight_job(engine: _StubEngine, workspace_id: str, job_id: str) -> _StubContainer:
    """Register a *running* job container without going through ``run()``.

    Models what a concurrent, still-blocked ``run()`` call in another process
    would have left in the engine — see the module docstring for why ``stop``
    cannot be exercised by calling ``run()`` first.
    """
    container = engine.containers.create(
        image=ENVIRONMENT,
        command=["sleep", "infinity"],
        name=f"headspace-{workspace_id}-{job_id}",
        labels={
            LABEL_WORKSPACE_ID: workspace_id,
            LABEL_PROVIDER: PROVIDER_NAME,
            LABEL_ROLE: ROLE_JOB,
            LABEL_JOB_ID: job_id,
            LABEL_CREATED_AT: "2026-07-29T00:00:00Z",
        },
    )
    container.attrs["State"] = {"Status": "running", "ExitCode": None, "OOMKilled": False}
    return container


# --- fixtures -----------------------------------------------------------------


@pytest.fixture
def engine() -> _StubEngine:
    return _StubEngine()


@pytest.fixture
def provider(engine: _StubEngine) -> DockerProvider:
    return DockerProvider(connect=lambda: engine)


@pytest.fixture
def policy(provider: DockerProvider) -> EffectivePolicy:
    return resolve_policy(Policy(), provider.capabilities())


@pytest.fixture
def workspace(provider: DockerProvider, policy: EffectivePolicy) -> str:
    provider.create(WORKSPACE, ENVIRONMENT, policy)
    return WORKSPACE


@pytest.fixture
def store_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A store root that does not exist yet — proof enough if it stays that way."""
    home = tmp_path / "would-be-headspace-home"
    monkeypatch.setenv(HOME_ENV_VAR, str(home))
    return home


def _jobs(engine: _StubEngine) -> list[_StubContainer]:
    return [c for c in engine.registry if c.labels.get(LABEL_ROLE) == ROLE_JOB]


def _anchors(engine: _StubEngine) -> list[_StubContainer]:
    return [c for c in engine.registry if c.labels.get(LABEL_ROLE) == ROLE_WORKSPACE]


# --- criterion 1: env lands only in the job container's environment kwarg ----


def test_env_reaches_the_job_containers_environment_kwarg(
    engine: _StubEngine, provider: DockerProvider, policy: EffectivePolicy, workspace: str
) -> None:
    secret = {"API_TOKEN": "s3cr3t-value"}
    provider.run(workspace, ("true",), policy, job_id="job-env", env=secret)

    jobs = _jobs(engine)
    assert len(jobs) == 1
    assert jobs[0].kwargs.get("environment") == secret


def test_env_never_appears_in_a_label(
    engine: _StubEngine, provider: DockerProvider, policy: EffectivePolicy, workspace: str
) -> None:
    secret = {"API_TOKEN": "s3cr3t-value"}
    provider.run(workspace, ("true",), policy, job_id="job-env-label", env=secret)

    job = _jobs(engine)[0]
    assert "API_TOKEN" not in job.labels
    assert all("s3cr3t-value" != value for value in job.labels.values())


def test_env_is_never_interpolated_into_the_command(
    engine: _StubEngine, provider: DockerProvider, policy: EffectivePolicy, workspace: str
) -> None:
    secret = {"API_TOKEN": "s3cr3t-value"}
    provider.run(workspace, ("echo", "hi"), policy, job_id="job-env-argv", env=secret)

    job = _jobs(engine)[0]
    assert job.command == ["echo", "hi"]
    assert all("s3cr3t-value" not in str(part) for part in job.command)


def test_the_anchor_container_never_receives_job_env(
    engine: _StubEngine, provider: DockerProvider, policy: EffectivePolicy, workspace: str
) -> None:
    """``create``'s call site is untouched by ``env`` — proved on the object it made."""
    provider.run(workspace, ("true",), policy, job_id="job-env-anchor", env={"SECRET": "x"})

    anchors = _anchors(engine)
    assert len(anchors) == 1
    assert "environment" not in anchors[0].kwargs


def test_the_closed_default_sends_no_environment_beyond_the_image(
    engine: _StubEngine, provider: DockerProvider, policy: EffectivePolicy, workspace: str
) -> None:
    """Omitting ``env`` and passing ``{}`` must be indistinguishable to the engine."""
    provider.run(workspace, ("true",), policy, job_id="job-no-env")

    job = _jobs(engine)[0]
    assert job.kwargs.get("environment") == {}


def test_omitting_env_and_passing_empty_env_reach_the_engine_identically(
    engine: _StubEngine, provider: DockerProvider, policy: EffectivePolicy, workspace: str
) -> None:
    provider.run(workspace, ("true",), policy, job_id="job-implicit")
    provider.run(workspace, ("true",), policy, job_id="job-explicit", env={})

    jobs = _jobs(engine)
    assert jobs[0].kwargs.get("environment") == jobs[1].kwargs.get("environment") == {}


# --- criterion 2: stop finds by label, stops then kills, touches no state ----


def test_stop_finds_the_job_by_role_label_and_signals_it(
    engine: _StubEngine, provider: DockerProvider, workspace: str
) -> None:
    job = _in_flight_job(engine, workspace, "job-1")

    result = provider.stop(workspace)

    assert result == {"workspace_id": workspace, "job_id": "job-1", "stopped": True}
    assert job.stop_calls, "container.stop() was never called"


def test_stop_is_graceful_first(
    engine: _StubEngine, provider: DockerProvider, workspace: str
) -> None:
    """A job that ends inside its grace window is never escalated to a kill."""
    job = _in_flight_job(engine, workspace, "job-2")
    job.survives_stop = False

    provider.stop(workspace)

    assert job.stop_calls
    assert job.kill_calls == 0


def test_stop_escalates_to_kill_only_if_the_job_survives_the_graceful_stop(
    engine: _StubEngine, provider: DockerProvider, workspace: str
) -> None:
    job = _in_flight_job(engine, workspace, "job-3")
    job.survives_stop = True

    result = provider.stop(workspace)

    assert job.stop_calls
    assert job.kill_calls == 1
    assert result == {"workspace_id": workspace, "job_id": "job-3", "stopped": True}


def test_stop_reports_nothing_running_as_a_fact_not_a_failure(
    provider: DockerProvider, workspace: str
) -> None:
    """No job container at all — the ordinary "already finished" race."""
    result = provider.stop(workspace)

    assert result == {"workspace_id": workspace, "job_id": None, "stopped": False}


def test_stop_treats_a_stray_exited_job_container_as_nothing_running(
    engine: _StubEngine, provider: DockerProvider, workspace: str
) -> None:
    """A job container that outlived its reap is not evidence of a live job."""
    job = _in_flight_job(engine, workspace, "job-stray")
    job.attrs["State"] = {"Status": "exited", "ExitCode": 0, "OOMKilled": False}

    result = provider.stop(workspace)

    assert result == {"workspace_id": workspace, "job_id": None, "stopped": False}
    assert not job.stop_calls
    assert job.kill_calls == 0


def test_stop_raises_for_a_workspace_this_engine_does_not_hold(
    provider: DockerProvider,
) -> None:
    with pytest.raises(CliError) as caught:
        provider.stop("no-such-workspace")
    assert caught.value.code == EXIT_USER_ERROR
    assert "unknown workspace" in caught.value.message


def test_stop_never_constructs_the_state_store(
    engine: _StubEngine, provider: DockerProvider, workspace: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Structural proof: patch the one constructor every store write goes through."""

    def _forbidden(self: Store, *_a: Any, **_kw: Any) -> None:
        raise AssertionError("DockerProvider.stop() must never construct the state store")

    monkeypatch.setattr(Store, "__init__", _forbidden)

    _in_flight_job(engine, workspace, "job-store-1")
    provider.stop(workspace)  # job running -> stop/kill path
    provider.stop(workspace)  # nothing running -> fact path


def test_stop_leaves_the_store_root_untouched_on_disk(
    engine: _StubEngine, provider: DockerProvider, workspace: str, store_home: Path
) -> None:
    _in_flight_job(engine, workspace, "job-store-2")

    provider.stop(workspace)

    assert not store_home.exists()


def test_stop_leaves_the_store_root_untouched_on_the_nothing_running_path(
    provider: DockerProvider, workspace: str, store_home: Path
) -> None:
    provider.stop(workspace)

    assert not store_home.exists()
