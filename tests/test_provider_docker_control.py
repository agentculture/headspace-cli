"""The Docker half of the secret channel and the ``stop`` verb (issue #13, #14, #16).

WHY this module exists
----------------------
Three features, one boundary each, and every one of them is about a value going
somewhere it must — or must not — reach.

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

The cancellation sentinel (issue #16) is the third, and it is the first thing
``stop`` writes anywhere. It exists because ``run`` — the *other* process, the
one that will record the job's outcome — cannot tell an operator's stop from a
job that chose 137 for itself, so ``stop`` has to leave it a positive signal
naming the job it signalled. The boundary is that the sentinel goes into the
workspace *volume*, through the engine, and still nowhere near ``~/.headspace``:
the two structural proofs above are restated against the write specifically,
because "``stop`` writes something now" is exactly the change that could have
reached for the store. Two further properties are proved rather than asserted —
the write is refused outright when a job has planted a symlink at the reserved
name, and a job never learns the id it would have to forge to plant a sentinel
of its own.

No engine required
-------------------
Every test here runs against :class:`_StubEngine`, a hand-written stand-in for
``docker.DockerClient`` carrying just the surface
:class:`~headspace.providers.docker.DockerProvider` actually touches for
``create``, ``run`` and ``stop``. The real provider is injected with
``connect=``, so nothing about the code under test is stubbed — only the
daemon underneath it, following the same shape
``tests/test_docker_classification.py`` already established for this module.

What is *not* stubbed is the workspace volume. :class:`_StubContainer` mounts a
real directory, runs the provider's own script text through the host's
``/bin/sh``, and tars real paths out of it with their real types — so the
sentinel round trip below is a genuine round trip through two processes' only
shared surface, and "a planted symlink is refused" is a statement about ``mv``
and ``[ -L ]`` rather than about a stub's ``if``. The tests that need that
shell say so with :data:`requires_a_host_shell`; the placement assertions above
need none and are not skipped with them.

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

import io
import shutil
import subprocess  # nosec B404 - the fixture's own shell, never a caller's input
import tarfile
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from docker.errors import NotFound

from headspace.cli._errors import EXIT_USER_ERROR, CliError
from headspace.core import profiles
from headspace.core.policy import EffectivePolicy, Policy
from headspace.core.policy import resolve as resolve_policy
from headspace.core.result import STATUS_CANCELLED, STATUS_FAILURE
from headspace.core.store import HOME_ENV_VAR, Store
from headspace.providers import docker as docker_backend
from headspace.providers.docker import (
    CANCELLATION_INTENT_MARKER_NAME,
    CANCELLATION_MARKER_NAMES,
    CANCELLATION_SIGNALLED_MARKER_NAME,
    CLEAR_CANCELLATION_SCRIPT,
    LABEL_CREATED_AT,
    LABEL_JOB_ID,
    LABEL_PROVIDER,
    LABEL_ROLE,
    LABEL_WORKSPACE_ID,
    PROVIDER_NAME,
    ROLE_JOB,
    ROLE_WORKSPACE,
    WORKSPACE_MOUNT_PATH,
    DockerProvider,
)

WORKSPACE = "ctrl-ws"

#: What the volume-backed anchor below needs of the host to stand in for a
#: container's own filesystem. ``sh`` runs the provider's script text, ``mv``
#: is the script's one external tool, and ``rm`` is the clearing script's.
_HOST_TOOLS = ("sh", "mv", "rm")


def _absent_host_tools() -> list[str]:
    return [tool for tool in _HOST_TOOLS if shutil.which(tool) is None]


#: Applied per test rather than as a module ``pytestmark``: the env-placement
#: assertions above need no shell at all, and a module-wide skip would take
#: them with it.
requires_a_host_shell = pytest.mark.skipif(
    bool(_absent_host_tools()),
    reason=f"the host shell lacks {', '.join(_absent_host_tools())}",
)

#: The environment the fixture workspace is created from — the default
#: profile's own digest-pinned reference.
ENVIRONMENT = profiles.resolve(profiles.DEFAULT_PROFILE)


# --- a hand-written stand-in for the docker SDK, scoped to this module's needs


def _label_filter(filters: Mapping[str, Any] | None) -> dict[str, str]:
    pairs = (filters or {}).get("label", [])
    return dict(str(pair).split("=", 1) for pair in pairs)


def _matches(container: "_StubContainer", wanted: Mapping[str, str]) -> bool:
    return all(container.labels.get(key) == value for key, value in wanted.items())


def _exec_label(cmd: Sequence[str]) -> str:
    """Name one exec by what it does and which marker it does it to.

    The engine timeline is how ordering claims are proved here, and with two
    marker names a bare ``"exec"`` would no longer distinguish the two claims
    that matter: intent goes in *before* the signal, the countersignal only
    *after* it. So the label carries both halves, read off the argv the
    provider actually built rather than off a counter the stub keeps.
    """
    _shell, _dash_c, script, _argv0, path, *_rest = cmd
    verb = "clear" if script == CLEAR_CANCELLATION_SCRIPT else "write"
    marker = str(path).rpartition("/")[2]
    phase = {
        CANCELLATION_INTENT_MARKER_NAME: "intent",
        CANCELLATION_SIGNALLED_MARKER_NAME: "countersignal",
    }.get(marker, marker)
    return f"{verb}-{phase}"


class _StubContainer:
    """One engine container: state plus every kwarg it was created with.

    ``kwargs`` is the whole of what
    :meth:`~headspace.providers.docker.DockerProvider` passed to
    ``client.containers.create`` beyond ``image`` — ``command``, ``labels``,
    ``environment`` and the closed-posture kwargs — kept verbatim rather than
    picked apart, so a test asserts what the provider actually sent the engine
    instead of a paraphrase of it.

    Two surfaces are honoured for real rather than stubbed, and both are the
    workspace volume seen from a different side. ``exec_run`` runs the
    provider's own script text through the host's ``/bin/sh`` against a real
    directory, and ``get_archive`` tars a real path out of it with that path's
    real type — so "a planted symlink is refused" becomes a statement about
    ``mv``, ``[ -L ]`` and :func:`tarfile.TarFile.gettarinfo`, not about a
    stub's ``if``. That is what makes the cross-process sentinel round trip
    below a genuine round trip: the bytes ``stop`` writes are the bytes ``run``
    reads, through the two engine mechanisms the provider actually uses.
    """

    def __init__(
        self, image: str, kwargs: dict[str, Any], engine: "_StubEngine", *, is_job: bool
    ) -> None:
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
        self._engine = engine
        self._is_job = is_job
        #: Every command this container was asked to exec, in call order.
        self.exec_calls: list[list[str]] = []

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        # The anchor holds a posture for the workspace's whole life, so it
        # stays up; a job settles immediately, because nothing in this module
        # drives a real wall clock and none of what it proves depends on how
        # long the job ran.
        if not self._is_job:
            self.attrs["State"] = {"Status": "running", "ExitCode": None, "OOMKilled": False}
            return
        self.attrs["State"] = {
            "Status": "exited",
            "ExitCode": self._engine.job_exit_code,
            "OOMKilled": False,
        }

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
        self._engine.timeline.append("signal")
        self.stop_calls.append(timeout)
        if not self.survives_stop:
            self.attrs["State"] = {"Status": "exited", "ExitCode": 143, "OOMKilled": False}

    def kill(self) -> None:
        self._engine.timeline.append("kill")
        self.kill_calls += 1
        self.attrs["State"] = {"Status": "exited", "ExitCode": 137, "OOMKilled": False}

    # -- the workspace volume, from the two sides the provider reaches it -----

    def _to_host(self, value: str) -> str:
        root = self._engine.volume_root
        if value == WORKSPACE_MOUNT_PATH:
            return str(root)
        if value.startswith(f"{WORKSPACE_MOUNT_PATH}/"):
            return f"{root}{value[len(WORKSPACE_MOUNT_PATH):]}"
        return value

    def exec_run(self, cmd: Sequence[str], **_: Any) -> SimpleNamespace:
        """Run the provider's own script text, for real, against a real directory."""
        self._engine.timeline.append(_exec_label(cmd))
        self.exec_calls.append(list(cmd))
        _shell, _dash_c, script, argv0, *rest = cmd
        completed = subprocess.run(  # nosec B603 - the fixture's own shell, by design
            [
                shutil.which("sh") or "/bin/sh",
                "-c",
                script,
                argv0,
                *(self._to_host(argument) for argument in rest),
            ],
            capture_output=True,
            check=False,
        )
        return SimpleNamespace(
            exit_code=completed.returncode, output=completed.stdout + completed.stderr
        )

    def get_archive(self, path: str, chunk_size: int | None = None) -> tuple[Any, dict[str, Any]]:
        """One path out of the volume, carrying the type it really has.

        ``gettarinfo`` stats without dereferencing, so a symlink leaves here as
        a symlink member and a directory as a directory member — which is the
        only reason the provider's read-side refusal can be tested at all.
        A path holding nothing is the engine's 404, the ordinary answer for a
        sentinel nobody wrote.
        """
        del chunk_size  # the marker is far smaller than any chunk worth cutting
        host = Path(self._to_host(path))
        if not host.exists() and not host.is_symlink():
            raise NotFound(f'404 Client Error: Not Found ("{path}")')
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            entry = archive.gettarinfo(str(host), arcname=host.name)
            if entry.isfile():
                with host.open("rb") as handle:
                    archive.addfile(entry, handle)
            else:
                archive.addfile(entry)
        # A generator, not a plain iterator: the SDK hands back one, and the
        # provider registers its ``close`` with the stream's release — a stub
        # answering with something that has no ``close`` would quietly excuse
        # the provider from releasing what it opened.
        return (chunk for chunk in [buffer.getvalue()]), {"name": host.name}


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
        labels = dict(kwargs.get("labels") or {})
        container = _StubContainer(
            image, kwargs, self._engine, is_job=labels.get(LABEL_ROLE) != ROLE_WORKSPACE
        )
        self._engine.registry.append(container)
        return container

    def list(self, all: bool = False, filters: Mapping[str, Any] | None = None) -> list[Any]:
        del all  # the stub prunes nothing; every container it holds is listed
        wanted = _label_filter(filters)
        return [c for c in self._engine.registry if _matches(c, wanted) and not c.removed]


class _StubEngine:
    """A stand-in for ``docker.DockerClient``: state, no daemon, no network.

    ``volume_root`` is a real directory standing in for the one workspace
    volume every container here mounts — the shared, writable state that
    outlives a job, and therefore the only place a sentinel written by one
    process can be found by another.
    """

    def __init__(self, volume_root: Path) -> None:
        self.containers = _StubContainers(self)
        self.volumes = _StubVolumes(self)
        self.images = _StubImages()
        self.registry: list[_StubContainer] = []
        self.volume_registry: list[_StubVolume] = []
        self.volume_root = volume_root
        volume_root.mkdir(parents=True, exist_ok=True)
        #: What the next job container exits with. Independent of everything
        #: else, exactly as the engine's own fields are.
        self.job_exit_code = 0
        #: Every engine action that can be ordered against another, in the
        #: order it happened — which is how "the sentinel was written *before*
        #: the signal" is proved rather than asserted.
        self.timeline: list[str] = []
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
def engine(tmp_path: Path) -> _StubEngine:
    return _StubEngine(tmp_path / "workspace-volume")


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

    assert result.to_dict() == {"workspace_id": workspace, "job_id": "job-1", "stopped": True}
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
    assert result.to_dict() == {"workspace_id": workspace, "job_id": "job-3", "stopped": True}


def test_stop_reports_nothing_running_as_a_fact_not_a_failure(
    provider: DockerProvider, workspace: str
) -> None:
    """No job container at all — the ordinary "already finished" race."""
    result = provider.stop(workspace)

    assert result.to_dict() == {"workspace_id": workspace, "job_id": None, "stopped": False}


def test_stop_treats_a_stray_exited_job_container_as_nothing_running(
    engine: _StubEngine, provider: DockerProvider, workspace: str
) -> None:
    """A job container that outlived its reap is not evidence of a live job."""
    job = _in_flight_job(engine, workspace, "job-stray")
    job.attrs["State"] = {"Status": "exited", "ExitCode": 0, "OOMKilled": False}

    result = provider.stop(workspace)

    assert result.to_dict() == {"workspace_id": workspace, "job_id": None, "stopped": False}
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


# --- criterion 3: the two markers stop leaves for run to find (issue #16) ----
#
# ``stop`` cannot tell ``run`` anything through headspace's own state: ``run``
# holds the workspace's flock for the job's whole duration and is the single
# writer of that workspace's records, so a ``stop`` that wrote there would
# deadlock against the very call it exists to interrupt. The engine is the
# only channel the two processes share, and inside it the workspace volume is
# the only writable thing that outlives a job — so the markers go there, and
# everything below holds ``stop`` to the boundary while it does.
#
# Two of them, in two phases, because one was not honest enough. A marker
# written before the signal records that an operator *asked*; only a marker
# written after the signal has landed records that the ask took effect. The
# first alone would let a ``stop`` that died mid-way convict a job that then
# failed for its own reasons — so ``stop`` writes both, in that order, and the
# tests below pin the order rather than the mere presence.


def _marker(engine: _StubEngine, name: str) -> Path:
    return engine.volume_root / name


def _intent(engine: _StubEngine) -> Path:
    return _marker(engine, CANCELLATION_INTENT_MARKER_NAME)


def _countersignal(engine: _StubEngine) -> Path:
    return _marker(engine, CANCELLATION_SIGNALLED_MARKER_NAME)


def _volume_entries(engine: _StubEngine) -> list[str]:
    """Everything in the volume, links never followed and never resolved."""
    return sorted(entry.name for entry in engine.volume_root.iterdir())


@pytest.fixture
def brief_settle(monkeypatch: pytest.MonkeyPatch) -> float:
    """Shrink ``run``'s wait for a countersignal to something a test can spend.

    ``run`` holds the door open for a bounded moment when an intent marker
    names the job that just settled, because in production the countersignal is
    being written by another process at that very instant. Nothing here is a
    second process, so the tests that leave intent unanswered would sit out the
    whole real budget for a countersignal that was never coming. The mechanism
    is the provider's throughout; only the number is this test's.
    """
    budget = 4 * docker_backend.POLL_INTERVAL_SECONDS
    monkeypatch.setattr(docker_backend, "CANCELLATION_SETTLE_SECONDS", budget)
    return budget


@requires_a_host_shell
def test_stop_writes_two_markers_naming_the_job_it_signalled(
    engine: _StubEngine, provider: DockerProvider, workspace: str
) -> None:
    """The payload is the whole point: which job, not merely that one was ended.

    ``run`` classifies from these files, and the job it classifies may not be
    the job that was stopped — a marker that only said "somebody stopped
    something here" would convict whichever later job happened to fail.
    """
    _in_flight_job(engine, workspace, "job-marked")

    provider.stop(workspace)

    assert _intent(engine).read_text() == "job-marked"
    assert _countersignal(engine).read_text() == "job-marked"


@requires_a_host_shell
def test_intent_is_recorded_before_the_signal_and_countersigned_only_after_it(
    engine: _StubEngine, provider: DockerProvider, workspace: str
) -> None:
    """Ordering is the evidence; presence alone would prove nothing.

    Both halves matter, in opposite directions.

    Intent goes in *first* because the other process is already watching:
    ``run`` is blocked in ``_await_exit``, re-reading the very container this
    call is about to signal, and it classifies the instant that container
    settles. Intent written after the signal would race that poll and lose
    whenever the job dies quickly — which is exactly when a forceful stop is
    involved, and exactly when ``run`` most needs to know to wait.

    The countersignal goes in *last* because of what it claims. It says the
    signal landed, so it cannot be written before that is true; writing it up
    front is precisely the fabrication this second phase exists to prevent —
    a ``stop`` that died in the gap would have left behind a completed-looking
    record of an ending that never happened.
    """
    job = _in_flight_job(engine, workspace, "job-ordered")
    job.survives_stop = True  # so the timeline carries the escalation too

    provider.stop(workspace)

    assert engine.timeline == ["write-intent", "signal", "kill", "write-countersignal"]


@requires_a_host_shell
def test_stop_writes_nothing_when_there_was_no_job_to_name(
    engine: _StubEngine, provider: DockerProvider, workspace: str
) -> None:
    """The ordinary race leaves the volume exactly as it found it.

    A caller racing a job that finished on its own signalled nothing, so there
    is no job id to write — and a marker naming nothing is residue the next job
    would have to be protected from for no gain at all.
    """
    result = provider.stop(workspace)

    assert result.stopped is False
    assert _volume_entries(engine) == []


@requires_a_host_shell
def test_the_marker_writes_leave_no_staging_residue_in_the_volume(
    engine: _StubEngine, provider: DockerProvider, workspace: str
) -> None:
    """The volume a caller reads back holds the two markers and nothing beside them.

    Each write lands on a nonce path first and is renamed onto its marker's
    name, because a rename replaces a link rather than writing through one.
    The nonces must not survive that: a workspace is the caller's, and
    headspace littering it with scratch files is a cost the caller never
    agreed to.
    """
    _in_flight_job(engine, workspace, "job-tidy")

    provider.stop(workspace)

    assert _volume_entries(engine) == sorted(CANCELLATION_MARKER_NAMES)


@requires_a_host_shell
@pytest.mark.parametrize("name", CANCELLATION_MARKER_NAMES)
def test_a_pre_planted_symlink_at_a_marker_path_is_never_written_through(
    engine: _StubEngine, provider: DockerProvider, workspace: str, tmp_path: Path, name: str
) -> None:
    """The write-side half of the discipline the copy-in path already keeps.

    A job shares the workspace volume and can plant a link at any name it
    likes, including these two. A shell redirection follows a link and would
    write headspace's own bytes wherever the job pointed it — outside the
    volume, over a file the job could not otherwise touch. Nothing legitimate
    puts a link here, so one standing there is refused outright.
    """
    outside = tmp_path / "outside-the-volume"
    outside.write_text("untouched")
    _marker(engine, name).symlink_to(outside)
    _in_flight_job(engine, workspace, "job-planted")

    provider.stop(workspace)

    assert outside.read_text() == "untouched"
    # Refused, not merely survived. A ``mv`` onto the link would also have left
    # the target untouched — it replaces the link rather than following it —
    # so the assertion that separates "refused" from "quietly tidied away" is
    # that the job's own link is still exactly what stands there, and that the
    # refusal left no nonce behind on its way out.
    assert _marker(engine, name).is_symlink()
    # The other phase still wrote, so the volume holds both names: the job's
    # own link where it planted it, and headspace's marker where it did not.
    assert _volume_entries(engine) == sorted(CANCELLATION_MARKER_NAMES)


@requires_a_host_shell
@pytest.mark.parametrize("name", CANCELLATION_MARKER_NAMES)
def test_a_pre_planted_directory_at_a_marker_path_is_refused_not_moved_into(
    engine: _StubEngine, provider: DockerProvider, workspace: str, name: str
) -> None:
    """``mv -f file dir`` succeeds — and puts the file *inside* the directory.

    The symlink refusal above does not cover this one, and the difference is
    the nastiest kind: a rename onto a directory does not fail, it relocates.
    Without an explicit refusal the write would exit 0, the provider would
    believe the marker was there, and what actually stands at the reserved name
    is a directory holding a nonce nobody will ever read. A job that plants one
    could therefore suppress the naming of its own cancellation and re-open the
    #16 symptom against itself — pushing towards ``failure``, never towards a
    fabricated cancellation, but pushing all the same for free.

    So anything at a reserved name that is not a regular file is refused in one
    check, which covers a directory, a FIFO and a device node alike: nothing
    legitimate is ever any of those, and only a regular file can be renamed
    over safely.
    """
    _marker(engine, name).mkdir()
    _in_flight_job(engine, workspace, "job-planted-dir")

    provider.stop(workspace)

    assert _marker(engine, name).is_dir()
    assert list(_marker(engine, name).iterdir()) == []  # nothing was moved inside


@requires_a_host_shell
@pytest.mark.parametrize("name", CANCELLATION_MARKER_NAMES)
def test_a_planted_directory_still_leaves_the_channel_recoverable(
    engine: _StubEngine,
    provider: DockerProvider,
    policy: EffectivePolicy,
    workspace: str,
    name: str,
    brief_settle: float,
) -> None:
    """Refusing the write must not be how a job wedges the channel for good.

    The clearing step removes whatever stands at a reserved name — a directory
    included, which is why it recurses — so the job that planted one costs its
    own cancellation its name and nothing more. The next ``stop`` in that
    workspace meets an empty name again and both phases land.
    """
    del brief_settle  # the refused write can leave intent unanswered; see above
    _marker(engine, name).mkdir()
    wedged = _in_flight_job(engine, workspace, "job-wedger")
    provider.stop(workspace)

    engine.job_exit_code = 137
    provider.run(workspace, ("false",), policy, job_id="job-wedger")
    assert _volume_entries(engine) == []

    # In production the blocked ``run`` reaps its own job container the moment
    # the job settles; this stand-in was never ``run``'s to reap, so it is
    # retired by hand before the next one takes its place.
    wedged.removed = True
    _in_flight_job(engine, workspace, "job-after-the-wedge")
    provider.stop(workspace)

    assert _intent(engine).read_text() == "job-after-the-wedge"
    assert _countersignal(engine).read_text() == "job-after-the-wedge"


@requires_a_host_shell
@pytest.mark.parametrize("name", CANCELLATION_MARKER_NAMES)
def test_a_refused_marker_write_never_stops_the_job_from_being_stopped(
    engine: _StubEngine, provider: DockerProvider, workspace: str, tmp_path: Path, name: str
) -> None:
    """Ending the runaway job is the operator's need; naming it is the nicety.

    The markers only improve how the *other* process narrates the outcome. A
    refusal to write either one — a planted link, an image with no ``mv``, an
    anchor that has exited — must therefore degrade the narration to exactly
    today's answer and never withhold the signal, which is the whole reason the
    operator reached for this verb.
    """
    _marker(engine, name).symlink_to(tmp_path / "elsewhere")
    job = _in_flight_job(engine, workspace, "job-still-stopped")

    result = provider.stop(workspace)

    assert result.to_dict() == {
        "workspace_id": workspace,
        "job_id": "job-still-stopped",
        "stopped": True,
    }
    assert job.stop_calls


@requires_a_host_shell
def test_writing_the_markers_still_constructs_no_state_store(
    engine: _StubEngine, provider: DockerProvider, workspace: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The new writes are engine-side, like every other thing ``stop`` does.

    Restated against the markers specifically, because "``stop`` writes
    something now" is exactly the change that could have reached for the
    store — and the second phase is a second chance to make that mistake, on
    the far side of the signal where a caller might feel entitled to record an
    outcome. Both markers go into the *volume*, through the engine, and the
    store's constructor is still never called.
    """

    def _forbidden(self: Store, *_a: Any, **_kw: Any) -> None:
        raise AssertionError("DockerProvider.stop() must never construct the state store")

    monkeypatch.setattr(Store, "__init__", _forbidden)

    _in_flight_job(engine, workspace, "job-store-3")
    provider.stop(workspace)

    assert _intent(engine).read_text() == "job-store-3"
    assert _countersignal(engine).read_text() == "job-store-3"


@requires_a_host_shell
def test_the_markers_land_in_the_volume_and_not_under_the_store_root(
    engine: _StubEngine, provider: DockerProvider, workspace: str, store_home: Path
) -> None:
    _in_flight_job(engine, workspace, "job-store-4")

    provider.stop(workspace)

    assert _intent(engine).is_file()
    assert _countersignal(engine).is_file()
    assert not store_home.exists()


# --- and the round trip the two processes actually make ----------------------


@requires_a_host_shell
def test_a_stop_and_a_later_run_agree_that_the_job_was_cancelled(
    engine: _StubEngine, provider: DockerProvider, policy: EffectivePolicy, workspace: str
) -> None:
    """End to end through the volume: one process writes, the other reads.

    Two verbs, one shared workspace volume, and no channel between them but
    that. ``stop`` names the job it signalled; ``run`` — which in production is
    a different process, already blocked on that job — reads the name back out
    through the archive endpoint and records the outcome an operator would
    recognise.
    """
    _in_flight_job(engine, workspace, "job-round-trip")
    provider.stop(workspace)

    engine.job_exit_code = 137
    outcome = provider.run(workspace, ("sleep", "600"), policy, job_id="job-round-trip")

    assert outcome.status == STATUS_CANCELLED
    assert outcome.exit_status is None


@requires_a_host_shell
def test_a_stop_that_never_countersigned_reports_the_job_as_it_failed(
    engine: _StubEngine,
    provider: DockerProvider,
    policy: EffectivePolicy,
    workspace: str,
    brief_settle: float,
) -> None:
    """The fabrication case, through the volume the two processes really share.

    A ``stop`` that got its intent down and then died — killed, disconnected,
    its signal refused — leaves exactly what is planted here. The job it named
    then fails on its own account, which is the trap: with one marker, that job
    found a sentinel naming *itself* and was written down as cancelled, so
    headspace would have claimed a human ended work that failed by itself.

    The countersignal is the difference. It is absent, so nothing here says the
    ending ever happened, and the job is recorded exactly as the code before
    any of this existed recorded it — a failure, with its own exit status
    intact. Both leftovers still go, because residue is residue.
    """
    del brief_settle  # intent naming this job is precisely what makes it wait
    _intent(engine).write_text("job-half-stopped")

    engine.job_exit_code = 137
    outcome = provider.run(workspace, ("./train.py",), policy, job_id="job-half-stopped")

    assert outcome.status == STATUS_FAILURE
    assert outcome.exit_status == 137
    assert _volume_entries(engine) == []


@requires_a_host_shell
def test_the_run_that_consumed_the_markers_removes_both_from_the_volume(
    engine: _StubEngine, provider: DockerProvider, policy: EffectivePolicy, workspace: str
) -> None:
    """A consumed marker is residue, and residue in a caller's volume is a leak."""
    _in_flight_job(engine, workspace, "job-consumed")
    provider.stop(workspace)
    assert _volume_entries(engine) == sorted(CANCELLATION_MARKER_NAMES)

    engine.job_exit_code = 137
    provider.run(workspace, ("sleep", "600"), policy, job_id="job-consumed")

    assert _volume_entries(engine) == []


@requires_a_host_shell
def test_markers_from_an_earlier_job_do_not_reach_the_next_one(
    engine: _StubEngine, provider: DockerProvider, policy: EffectivePolicy, workspace: str
) -> None:
    """Through real files this time, not an armed stub attribute."""
    _in_flight_job(engine, workspace, "job-earlier")
    provider.stop(workspace)

    engine.job_exit_code = 137
    outcome = provider.run(workspace, ("false",), policy, job_id="job-later")

    assert outcome.status == STATUS_FAILURE
    assert outcome.exit_status == 137
    assert _volume_entries(engine) == []


# --- criterion 3: a job can never learn the name it would have to forge ------


def test_the_job_id_reaches_the_engine_and_never_the_container(
    engine: _StubEngine, provider: DockerProvider, policy: EffectivePolicy, workspace: str
) -> None:
    """The sentinel is unforgeable only because a job cannot name itself.

    A job shares the workspace volume, so it can write whatever it likes at
    the sentinel's path; the one string it cannot write there is its own job
    id, because nothing inside the box ever tells it what that is. The id
    travels as a container *name* and a ``headspace.job_id`` label, and both
    are readable from outside the container and from nowhere within it — a
    container's own hostname is its engine id, not its name, and a label is
    not visible to the process at all.

    So this pins the two channels a process really can read from inside its
    own container: its environment and its argv. Both are the caller's to
    fill, and neither is built from the job id or with any knowledge that one
    exists.
    """
    job_id = "job-unforgeable"
    provider.run(workspace, ("echo", "hi"), policy, job_id=job_id, env={"TOKEN": "t"})

    job = _jobs(engine)[0]
    assert job.labels[LABEL_JOB_ID] == job_id
    assert all(job_id not in str(value) for value in (job.kwargs.get("environment") or {}).values())
    assert all(job_id not in str(part) for part in (job.command or ()))
