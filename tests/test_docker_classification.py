"""A command the image cannot execute is the caller's error, not a broken engine.

And, since issue #16, the other half of the same idea: a job an *operator*
ended is not a failed computation either. Both are cases where the honest
answer cannot be read off the engine's numbers — an exec-step 400 looks like
any other 400, and a stopped container looks exactly like one that chose 137
for itself — so both are settled by a signal the provider can actually trust:
the engine's own marker for the first, and a sentinel another process wrote
for the second. The classification tests for both live here.

The second one is written in two phases, and the tests below turn on the
difference. ``stop`` records the *intent* to end a job before it signals and
*countersigns* only once the signal has landed; ``run`` reads a cancellation
off the countersignal alone. What that buys is the one thing a single sentinel
could not: a ``stop`` that dies mid-way leaves intent behind, and intent alone
must never let headspace claim a human ended a job that failed on its own. So
the cases here are asymmetric on purpose — partial, missing or tampered
evidence classifies exactly as the pre-fix code did, and no arrangement of it
fabricates a cancellation.

WHY this module exists
----------------------
``container.start()`` sits inside :meth:`DockerProvider._engine`, and
:data:`~headspace.providers.docker._ENGINE_FAILURES` catches
:class:`docker.errors.DockerException` — the base class of
:class:`~docker.errors.APIError`. So before the fix every engine 400 became
exit 7, including the one 400 that is not the engine's fault at all: the caller
asked for a binary the image does not have. Two harms followed, and this module
pins both.

* **The retry loop.** Exit 7 carries "check the engine is running and
  reachable, then retry". An autonomous consumer that trusts that hint retries a
  deterministic failure forever. The whole point of the taxonomy is that exit 6
  means "your work failed" and exit 7 means "try again later"; a not-executable
  command is emphatically the former.
* **The leak.** ``str(APIError)`` is ``400 Client Error for
  http+docker://localhost/v1.52/containers/<64 hex>/start: ...``. That string
  landed in the caller's context — a container id and the engine's internal API
  URL, which is exactly the execution-transcript pollution headspace exists to
  prevent, and an information disclosure besides.

Fixtures recorded, not invented
-------------------------------
:data:`WORDINGS` are the four ``explanation`` strings a live engine actually
produced (Docker 29.1.3, API 1.52, SDK 7.2.0, probed 2026-07-28) for the four
shapes of "cannot execute": absent from ``$PATH``, an absolute path that is not
there, a directory, and a file without the execute bit.
:func:`test_the_recorded_wordings_reproduce_the_live_engine_string` re-derives
the full live error text from each fixture and compares it to the recorded
original, so a fixture cannot drift into fiction while the suite still passes.

No engine required
------------------
Every test here runs on a host with no daemon. :class:`StubEngine` is a
hand-written stand-in for the SDK's client that the *real*
:class:`~headspace.providers.docker.DockerProvider` drives through its real
``create`` and ``run`` — the provider is injected with ``connect=``, so nothing
about the code under test is stubbed, only the engine underneath it. The live
counterpart of these assertions belongs to the integration lane.
"""

from __future__ import annotations

import io
import json
import tarfile
import time
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from docker.errors import APIError, NotFound

import headspace.cli._commands.create as provider_registry
from headspace.cli import main
from headspace.cli._commands.inspect import collect_logs
from headspace.cli._errors import (
    EXIT_CANCELLED,
    EXIT_COMPUTATION_FAILED,
    EXIT_INFRASTRUCTURE_FAILURE,
    EXIT_RESOURCE_EXHAUSTED,
    EXIT_TIMEOUT,
)
from headspace.core import profiles
from headspace.core.policy import EffectivePolicy, Policy, ResourceBudget
from headspace.core.policy import resolve as resolve_policy
from headspace.core.result import (
    STATUS_CANCELLED,
    STATUS_FAILURE,
    STATUS_RESOURCE_EXHAUSTED,
    STATUS_SUCCESS,
    STATUS_TIMEOUT,
    render_json,
    render_markdown,
)
from headspace.core.store import HOME_ENV_VAR, Store
from headspace.core.workspace import Orchestrator, exit_code_for_status
from headspace.providers import docker as docker_backend
from headspace.providers.base import ProviderError
from headspace.providers.docker import (
    CANCELLATION_INTENT_MARKER_NAME,
    CANCELLATION_MARKER_NAMES,
    CANCELLATION_SIGNALLED_MARKER_NAME,
    CLEAR_CANCELLATION_SCRIPT,
    EXEC_INIT_MARKER,
    EXIT_COMMAND_NOT_EXECUTABLE,
    EXIT_COMMAND_NOT_FOUND,
    LABEL_CANCEL_TOKEN,
    LABEL_ROLE,
    MARKER_FIELD_SEPARATOR,
    POLL_INTERVAL_SECONDS,
    ROLE_WORKSPACE,
    WORKSPACE_MOUNT_PATH,
    DockerProvider,
    not_executable_exit_status,
    redact_engine_text,
)

# --- what the live engine actually said -------------------------------------

#: A container id of the shape the engine mints. Recorded from the same probe.
CONTAINER_ID = "7f2362c4b1d9a0e5c8f3117d4a6b2e90dd51c7a4839f0b6e2c15d7a380946fbe"

#: The engine's internal endpoint, which is where the container id leaked from.
ENDPOINT = f"http+docker://localhost/v1.52/containers/{CONTAINER_ID}/start"

#: The common prefix every OCI exec-step failure carries.
_INIT_PREFIX = (
    "failed to create task for container: failed to create shim task: "
    "OCI runtime create failed: runc create failed: unable to start container "
    "process: error during container init: exec: "
)

#: shape -> (argv, engine explanation, POSIX exit status the shape means).
#: Recorded verbatim from a live engine; see the module docstring.
WORDINGS: dict[str, tuple[str, str, int]] = {
    "absent from PATH": (
        "definitely-not-a-binary",
        _INIT_PREFIX + '"definitely-not-a-binary": executable file not found in $PATH',
        EXIT_COMMAND_NOT_FOUND,
    ),
    "absolute path absent": (
        "/usr/local/bin/nope",
        _INIT_PREFIX + '"/usr/local/bin/nope": stat /usr/local/bin/nope: no such file or directory',
        EXIT_COMMAND_NOT_FOUND,
    ),
    "a directory": (
        "/tmp",
        _INIT_PREFIX + '"/tmp": is a directory: permission denied',
        EXIT_COMMAND_NOT_EXECUTABLE,
    ),
    "not executable": (
        "/etc/hostname",
        _INIT_PREFIX + '"/etc/hostname": permission denied',
        EXIT_COMMAND_NOT_EXECUTABLE,
    ),
}

#: The full text the live engine put in front of a caller for the first shape,
#: recorded character for character. Everything this module claims about leakage
#: is claimed against this string.
LIVE_ERROR_TEXT = (
    f"400 Client Error for {ENDPOINT}: Bad Request " f"(\"{WORDINGS['absent from PATH'][1]}\")"
)

#: Engine 400s that are *not* the exec step failing. These must keep today's
#: behaviour exactly — exit 7 — because a caller told exit 6 concludes its work
#: is lost when the truth is that the engine needs another try.
NOT_EXEC_FAILURES: dict[str, str] = {
    "port conflict": (
        "driver failed programming external connectivity on endpoint: "
        "Bind for 0.0.0.0:8080 failed: port is already allocated"
    ),
    "name conflict": (
        'Conflict. The container name "/hs-ws" is already in use by container '
        f'"{CONTAINER_ID}". You have to remove (or rename) that container to be '
        "able to reuse that name."
    ),
    "device in use": "error while mounting volume: device or resource busy",
    # Close but deliberately outside the matcher: an exec mentioned anywhere is
    # not the same fact as the container's init step failing to exec.
    "exec named but not the init step": (
        'cannot exec in a stopped container: exec: "true": not running'
    ),
}

#: The three substrings the caller's context must never receive.
FORBIDDEN = (CONTAINER_ID, "http+docker", "400 Client Error")

WORKSPACE = "clsfy-ws"


def api_error(explanation: str, *, container_id: str = CONTAINER_ID) -> APIError:
    """An ``APIError`` shaped exactly as the SDK builds one from a 400 response."""
    response = SimpleNamespace(
        status_code=400,
        url=f"http+docker://localhost/v1.52/containers/{container_id}/start",
        reason="Bad Request",
    )
    return APIError("400 Client Error", response=response, explanation=explanation)


# --- an engine that is not there --------------------------------------------


def _label_filter(filters: Mapping[str, Any] | None) -> dict[str, str]:
    pairs = (filters or {}).get("label", [])
    return dict(str(pair).split("=", 1) for pair in pairs)


def _matches(container: StubContainer, wanted: Mapping[str, str]) -> bool:
    return all(container.labels.get(key) == value for key, value in wanted.items())


class StubContainer:
    """One engine container, with just the surface the provider actually touches."""

    def __init__(
        self,
        container_id: str,
        labels: Mapping[str, str],
        image: str,
        network_mode: str,
        *,
        start_error: BaseException | None = None,
        settles_to: str = "exited",
        exit_code: int = 0,
        frames: Sequence[bytes] = (),
        oom_killed: bool = False,
        kill_error: Exception | None = None,
    ) -> None:
        self.id = container_id
        self.labels = dict(labels)
        self.attrs: dict[str, Any] = {
            "Config": {"Image": image},
            "HostConfig": {"NetworkMode": network_mode},
            "State": {"Status": "created", "ExitCode": None},
        }
        self.removed = False
        self._start_error = start_error
        self._settles_to = settles_to
        self._exit_code = exit_code
        self._frames = list(frames)
        #: What the engine would report in ``State.OOMKilled``. Carried through
        #: both `start()`'s immediate settle and `kill()`'s — a wall-clock kill
        #: can land on a container the engine also marks OOMKilled, which is
        #: exactly the precedence case the OOM tests below pin.
        self._oom_killed = oom_killed
        #: Raised by ``kill()``. A daemon that refuses the signal while the
        #: container keeps running is an engine failure, not a job that
        #: happened to exit first — the two must not read alike.
        self._kill_error = kill_error
        #: What the workspace volume holds under each of headspace's two
        #: reserved cancellation names, as the *other* process would have left
        #: it: ``name -> (kind, payload)``, where ``kind`` is ``"file"``,
        #: ``"symlink"`` or ``"directory"`` and ``payload`` is the file's text
        #: or the link's target. Empty is the ordinary case — almost no job is
        #: ever stopped by an operator. Armed on the *anchor* container,
        #: because that is the object ``run`` reads the volume through.
        #:
        #: Only a regular file can name a job; the other two kinds are what a
        #: job sharing the volume can plant, and the archive endpoint reports
        #: each one honestly in the tar member's own type — which is the whole
        #: reason the provider can refuse them.
        self.markers: dict[str, tuple[str, str]] = {}
        #: Markers still on their way: ``name -> (probe, kind, payload)``, the
        #: marker appearing on the ``probe``-th read of its own name. The
        #: countersignal ``stop`` writes *after* the signal lands is written by
        #: a second process, and ``run`` can reach the volume before it — so
        #: "the wait collects a countersignal that had not arrived yet" is
        #: exercised here rather than asserted.
        self.pending: dict[str, tuple[int, str, str]] = {}
        #: How many times each name has been read, in total. The settle wait is
        #: bounded, and a count is how that is pinned without timing anything.
        self.probes: Counter[str] = Counter()
        #: Every command the provider ran as an exec, in call order.
        self.exec_calls: list[list[str]] = []

    # -- the workspace volume, as the archive endpoint and an exec see it -----

    def plant(self, name: str, job_id: str, kind: str = "file", *, on_probe: int = 1) -> None:
        """Leave one marker in the volume, as a real ``stop`` would have.

        Takes the **job id** and builds the payload the provider itself would
        write — this workspace's cancellation token from the anchor's own
        label, then the id. Deriving it here rather than at two dozen call
        sites is what keeps those tests about *classification*: none of them
        wants to be a test of the payload's format, and a suite that spelled
        the format out everywhere would have to be edited everywhere the format
        moved.

        A marker that is *not* authenticated is a different thing entirely —
        it is what a forging job writes — so it has its own verb,
        :meth:`plant_unauthenticated`, and reads as the attack it is.

        ``on_probe`` is which read of that name first finds it: ``1`` (the
        default) for a marker already standing there when ``run`` looks, and
        anything higher for one ``stop`` is still on its way to writing.
        """
        self.plant_unauthenticated(
            name,
            f"{self.labels[LABEL_CANCEL_TOKEN]}{MARKER_FIELD_SEPARATOR}{job_id}",
            kind,
            on_probe=on_probe,
        )

    def plant_unauthenticated(
        self, name: str, payload: str, kind: str = "file", *, on_probe: int = 1
    ) -> None:
        """Leave literal bytes at a marker name, carrying no workspace token.

        What a job with a shell in the volume can actually do: it writes
        whatever it likes, and the one thing it cannot put there is this
        workspace's secret. Kept separate from :meth:`plant` so a test that
        uses it is visibly staging an attack rather than a stop.
        """
        if on_probe <= 1:
            self.markers[name] = (kind, payload)
        else:
            self.pending[name] = (on_probe, kind, payload)

    def get_archive(self, path: str, chunk_size: int | None = None) -> tuple[Any, dict[str, Any]]:
        """The engine's transfer archive for one path in the volume.

        Answers a 404 for a path that holds nothing, exactly as the daemon
        does — which is the ordinary answer for both marker names, since almost
        no job is ever stopped by an operator. When something *is* there, the
        member carries that object's real tar type, so a planted symlink or
        directory reaches the provider as a symlink or directory rather than
        as bytes a stub decided to hand over.
        """
        del chunk_size  # a marker is far smaller than any chunk worth cutting
        name = path.rpartition("/")[2]
        if not path.startswith(f"{WORKSPACE_MOUNT_PATH}/") or name not in CANCELLATION_MARKER_NAMES:
            raise NotFound(f'404 Client Error for {ENDPOINT}: Not Found ("{path}")')
        self.probes[name] += 1
        arriving = self.pending.get(name)
        if arriving is not None and self.probes[name] >= arriving[0]:
            self.markers[name] = (arriving[1], arriving[2])
            del self.pending[name]
        if name not in self.markers:
            raise NotFound(f'404 Client Error for {ENDPOINT}: Not Found ("{path}")')
        kind, payload = self.markers[name]
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            entry = tarfile.TarInfo(name)
            if kind == "symlink":
                entry.type = tarfile.SYMTYPE
                entry.linkname = payload
                archive.addfile(entry)
            elif kind == "directory":
                entry.type = tarfile.DIRTYPE
                archive.addfile(entry)
            else:
                encoded = payload.encode("utf-8")
                entry.size = len(encoded)
                archive.addfile(entry, io.BytesIO(encoded))
        # A generator, not a plain iterator: the SDK hands back one, and the
        # provider registers its ``close`` with the stream's release — a stub
        # answering with something that has no ``close`` would quietly excuse
        # the provider from releasing what it opened.
        return (chunk for chunk in [buffer.getvalue()]), {"name": name}

    def exec_run(self, cmd: Sequence[str], **_: Any) -> SimpleNamespace:
        """Run one of the provider's scripts — recorded, and honoured for effect.

        Only the clearing script has an effect worth modelling here: it is the
        one whose *absence* would leave a stale marker behind for the next job,
        so the stub really removes what stands at that name rather than merely
        counting the call. It removes an unarrived marker too: a clear that
        raced the other process's write is still the clear this job made.
        """
        self.exec_calls.append(list(cmd))
        if len(cmd) > 4 and cmd[2] == CLEAR_CANCELLATION_SCRIPT:
            cleared = cmd[4].rpartition("/")[2]
            self.markers.pop(cleared, None)
            self.pending.pop(cleared, None)
        return SimpleNamespace(exit_code=0, output=b"")

    @property
    def status(self) -> str:
        return str(self.attrs["State"]["Status"])

    def start(self) -> None:
        if self._start_error is not None:
            raise self._start_error
        self.attrs["State"] = {
            "Status": self._settles_to,
            "ExitCode": self._exit_code,
            "OOMKilled": self._oom_killed,
        }

    def reload(self) -> None:
        """The engine's state is already in ``attrs``; nothing to re-fetch."""

    def remove(self, **_: Any) -> None:
        self.removed = True

    def attach(self, **_: Any) -> Iterator[bytes]:
        return iter(self._frames)

    def logs(self, **_: Any) -> Iterator[bytes]:
        return iter(self._frames)

    def stats(self, **_: Any) -> dict[str, Any]:
        return {}

    def kill(self) -> None:
        if self._kill_error is not None:
            raise self._kill_error
        # The wall-clock enforcer's own kill, which yields the same 137 a
        # kernel OOM kill does — and, on a host under real memory pressure,
        # can land on a container the engine also marks OOMKilled.
        self.attrs["State"] = {"Status": "exited", "ExitCode": 137, "OOMKilled": self._oom_killed}


class StubVolume:
    def __init__(self, name: str) -> None:
        self.name = name
        self.removed = False

    def remove(self, **_: Any) -> None:
        self.removed = True


class StubEngine:
    """A stand-in for ``docker.DockerClient``: state, no daemon, no network."""

    def __init__(self) -> None:
        self.containers = _StubContainers(self)
        self.volumes = _StubVolumes(self)
        self.images = _StubImages()
        self.registry: list[StubContainer] = []
        self.volume_registry: list[StubVolume] = []
        #: Armed by a test: what the NEXT job container's ``start`` raises.
        self.job_start_error: BaseException | None = None
        self.job_exit_code = 0
        self.job_frames: Sequence[bytes] = ()
        #: ``State.OOMKilled`` the next job container reports — never inferred
        #: by the stub from ``job_exit_code``, exactly as the real engine's two
        #: fields are independent of each other.
        self.job_oom_killed = False
        #: What ``start()`` settles the next job container to. ``"exited"``
        #: (the default) makes the job already-done by the first poll — the
        #: shape every other test here wants. ``"running"`` keeps it alive
        #: until something calls ``kill()``, which is how a wall-clock timeout
        #: is driven without a real clock.
        self.job_settles_to = "exited"
        #: Raised by the next job container's ``kill()``. Models a daemon
        #: that refuses the signal, which must not read as "the job exited
        #: on its own" — see the false-success test below.
        self.job_kill_error: BaseException | None = None
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
            "Volumes": [
                {"Name": volume.name, "UsageData": {"Size": 0}} for volume in self.volume_registry
            ]
        }

    def close(self) -> None:
        # Counted, not disabled: the provider opens a connection per verb and
        # closes it, and the stub must survive being closed and reused.
        self.closed += 1


class _StubImages:
    def get(self, reference: str) -> SimpleNamespace:
        return SimpleNamespace(id=reference)


class _StubVolumes:
    def __init__(self, engine: StubEngine) -> None:
        self._engine = engine

    def create(self, name: str, **_: Any) -> StubVolume:
        volume = StubVolume(name)
        self._engine.volume_registry.append(volume)
        return volume


class _StubContainers:
    def __init__(self, engine: StubEngine) -> None:
        self._engine = engine

    def create(self, image: str, **kwargs: Any) -> StubContainer:
        labels = dict(kwargs.get("labels") or {})
        job = labels.get(LABEL_ROLE) != ROLE_WORKSPACE
        container = StubContainer(
            container_id=CONTAINER_ID if job else "a" * 64,
            labels=labels,
            image=image,
            network_mode=str(kwargs.get("network_mode") or "none"),
            start_error=self._engine.job_start_error if job else None,
            settles_to=self._engine.job_settles_to if job else "running",
            exit_code=self._engine.job_exit_code if job else 0,
            frames=self._engine.job_frames if job else (),
            oom_killed=self._engine.job_oom_killed if job else False,
            kill_error=self._engine.job_kill_error if job else None,
        )
        self._engine.registry.append(container)
        return container

    def list(self, all: bool = False, filters: Mapping[str, Any] | None = None) -> list[Any]:
        del all  # every container the stub holds is listed; nothing is pruned
        wanted = _label_filter(filters)
        return [c for c in self._engine.registry if _matches(c, wanted) and not c.removed]


# --- fixtures ----------------------------------------------------------------


@pytest.fixture
def engine() -> StubEngine:
    return StubEngine()


@pytest.fixture
def provider(engine: StubEngine) -> DockerProvider:
    return DockerProvider(connect=lambda: engine)


@pytest.fixture
def policy(provider: DockerProvider) -> EffectivePolicy:
    return resolve_policy(Policy(), provider.capabilities())


@pytest.fixture
def workspace(provider: DockerProvider, policy: EffectivePolicy) -> str:
    provider.create(WORKSPACE, profiles.resolve(profiles.DEFAULT_PROFILE), policy)
    return WORKSPACE


@pytest.fixture
def store_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "headspace-home"
    monkeypatch.setenv(HOME_ENV_VAR, str(home))
    return home


# --- the fixtures are the engine's own words --------------------------------


def test_the_recorded_wordings_reproduce_the_live_engine_string() -> None:
    """The fixture is the live error, not a paraphrase of it."""
    assert str(api_error(WORDINGS["absent from PATH"][1])) == LIVE_ERROR_TEXT
    for shape, (_, explanation, _status) in WORDINGS.items():
        assert EXEC_INIT_MARKER in explanation, shape
        assert EXEC_INIT_MARKER in str(api_error(explanation)), shape


def test_todays_error_text_carries_everything_the_caller_must_not_receive() -> None:
    """Without this, the leak assertions below could pass vacuously."""
    for forbidden in FORBIDDEN:
        assert forbidden in LIVE_ERROR_TEXT


# --- the classifier, on its own ---------------------------------------------


@pytest.mark.parametrize("shape", sorted(WORDINGS))
def test_each_probed_wording_maps_to_its_posix_status(shape: str) -> None:
    """127 means "not there"; 126 means "there, but you cannot run it"."""
    _argv0, explanation, expected = WORDINGS[shape]
    assert not_executable_exit_status(str(api_error(explanation))) == expected


def test_the_two_posix_statuses_are_not_collapsed() -> None:
    """Reporting 127 for a command that was present would tell an agent to re-spell it."""
    produced = {
        not_executable_exit_status(str(api_error(explanation)))
        for _argv0, explanation, _status in WORDINGS.values()
    }
    assert produced == {EXIT_COMMAND_NOT_FOUND, EXIT_COMMAND_NOT_EXECUTABLE}


@pytest.mark.parametrize("shape", sorted(NOT_EXEC_FAILURES))
def test_an_engine_400_that_is_not_the_exec_step_is_not_classified(shape: str) -> None:
    """The fail-safe direction: no confident match means no reclassification."""
    assert not_executable_exit_status(str(api_error(NOT_EXEC_FAILURES[shape]))) is None


def test_the_reason_is_read_past_the_command_the_caller_typed() -> None:
    """A path that *contains* a not-found phrase is still 126 when it was found."""
    explanation = _INIT_PREFIX + '"/tmp/no such file or directory": permission denied'
    assert not_executable_exit_status(explanation) == EXIT_COMMAND_NOT_EXECUTABLE


def test_the_reason_is_read_past_a_command_name_that_contains_a_quote() -> None:
    """The split is up to the *first* separator, which a quote in argv0 must not break.

    The sibling test above passes even if the command is narrowed to "anything
    but a quote", because its path has none — so this is the one that holds the
    line. Narrowing it looks equivalent and is not: the detail then fails to
    split at all, falls back to scanning the whole string, and reads the
    not-found phrase out of the caller's own path, reporting a command the
    engine said was right there as missing.
    """
    explanation = _INIT_PREFIX + '"/tmp/we"ird/no such file or directory": permission denied'
    assert not_executable_exit_status(explanation) == EXIT_COMMAND_NOT_EXECUTABLE


def test_an_unrecognised_exec_wording_defaults_to_the_conservative_status() -> None:
    """126 says "it was there"; that never sends an agent off to re-spell a name."""
    explanation = _INIT_PREFIX + '"/bin/thing": some wording nobody has seen yet'
    assert not_executable_exit_status(explanation) == EXIT_COMMAND_NOT_EXECUTABLE


# --- redaction ---------------------------------------------------------------


def test_redaction_removes_the_envelope_the_transport_added() -> None:
    cleaned = redact_engine_text(LIVE_ERROR_TEXT, CONTAINER_ID)
    for forbidden in FORBIDDEN:
        assert forbidden not in cleaned
    assert "executable file not found in $PATH" in cleaned


def test_redaction_removes_a_handle_that_reached_the_explanation() -> None:
    """Belt and braces: the id is removed by knowing it, not by hoping."""
    text = f'error during container init: exec: "x": container {CONTAINER_ID} failed'
    cleaned = redact_engine_text(text, CONTAINER_ID)
    assert CONTAINER_ID not in cleaned
    assert CONTAINER_ID[:12] not in cleaned


# --- the provider seam -------------------------------------------------------


@pytest.mark.parametrize("shape", sorted(WORDINGS))
def test_run_reports_a_not_executable_command_as_the_callers_failure(
    shape: str,
    engine: StubEngine,
    provider: DockerProvider,
    policy: EffectivePolicy,
    workspace: str,
) -> None:
    argv0, explanation, expected = WORDINGS[shape]
    engine.job_start_error = api_error(explanation)

    outcome = provider.run(workspace, (argv0,), policy, job_id="job-nx")

    assert outcome.status == STATUS_FAILURE
    assert outcome.exit_status == expected


@pytest.mark.parametrize("shape", sorted(NOT_EXEC_FAILURES))
def test_run_still_calls_any_other_engine_400_an_infrastructure_failure(
    shape: str,
    engine: StubEngine,
    provider: DockerProvider,
    policy: EffectivePolicy,
    workspace: str,
) -> None:
    """Unmatched means unchanged: this path must keep behaving exactly as it did."""
    engine.job_start_error = api_error(NOT_EXEC_FAILURES[shape])

    with pytest.raises(ProviderError) as caught:
        provider.run(workspace, ("true",), policy, job_id="job-broken")

    assert caught.value.code == EXIT_INFRASTRUCTURE_FAILURE
    assert caught.value.category == "infrastructure_failure"


def test_the_job_container_is_still_reaped_when_the_command_was_refused(
    engine: StubEngine, provider: DockerProvider, policy: EffectivePolicy, workspace: str
) -> None:
    engine.job_start_error = api_error(WORDINGS["absent from PATH"][1])
    provider.run(workspace, ("definitely-not-a-binary",), policy, job_id="job-nx")
    jobs = [c for c in engine.registry if c.labels.get(LABEL_ROLE) != ROLE_WORKSPACE]
    assert jobs and all(c.removed for c in jobs)


def test_a_job_that_really_ran_is_untouched_by_the_new_branch(
    engine: StubEngine, provider: DockerProvider, policy: EffectivePolicy, workspace: str
) -> None:
    engine.job_frames = [b"hello\n"]
    outcome = provider.run(workspace, ("echo", "hello"), policy, job_id="job-ok")
    assert outcome.status == STATUS_SUCCESS
    assert outcome.exit_status == 0
    assert outcome.output == "hello\n"


def test_the_refusal_names_the_environment_and_the_command_the_caller_typed(
    engine: StubEngine, provider: DockerProvider, policy: EffectivePolicy, workspace: str
) -> None:
    engine.job_start_error = api_error(WORDINGS["absent from PATH"][1])
    outcome = provider.run(
        workspace, ("definitely-not-a-binary", "--flag"), policy, job_id="job-nx"
    )

    environment = profiles.resolve(profiles.DEFAULT_PROFILE)
    assert "definitely-not-a-binary" in outcome.output
    assert environment in outcome.output


def test_the_refusal_carries_no_engine_handle_and_no_engine_url(
    engine: StubEngine, provider: DockerProvider, policy: EffectivePolicy, workspace: str
) -> None:
    """``output`` is the only text channel the provider has, so this is the whole leak surface."""
    engine.job_start_error = api_error(WORDINGS["absent from PATH"][1])
    outcome = provider.run(workspace, ("definitely-not-a-binary",), policy, job_id="job-nx")

    for forbidden in FORBIDDEN:
        assert forbidden not in outcome.output


def test_the_engine_diagnosis_survives_on_the_captured_output_path(
    engine: StubEngine, provider: DockerProvider, policy: EffectivePolicy, workspace: str
) -> None:
    """A misclassification must stay diagnosable, or the fix trades one blindness for another."""
    engine.job_start_error = api_error(WORDINGS["absent from PATH"][1])
    outcome = provider.run(workspace, ("definitely-not-a-binary",), policy, job_id="job-nx")
    assert EXEC_INIT_MARKER in outcome.output


def test_the_reported_output_volume_matches_what_was_captured(
    engine: StubEngine, provider: DockerProvider, policy: EffectivePolicy, workspace: str
) -> None:
    engine.job_start_error = api_error(WORDINGS["a directory"][1])
    outcome = provider.run(workspace, ("/tmp",), policy, job_id="job-dir")
    assert outcome.usage.output_bytes == len(outcome.output.encode("utf-8"))
    assert outcome.truncated is False


# --- an OOM kill is resource_exhausted, never inferred from exit status 137 --
#
# Verified live on this host (Docker 29.1.3 / API 1.52, 2026-07-28): a genuine
# memory kill reports ``State.OOMKilled=true, ExitCode=137``, and
# ``python -c "raise SystemExit(137)"`` reports ``OOMKilled=false,
# ExitCode=137``. So every test below drives ``OOMKilled`` and ``ExitCode``
# independently through ``engine.job_oom_killed`` / ``engine.job_exit_code`` —
# the same way the live engine's two fields are independent of each other —
# rather than letting the stub infer one from the other.


def test_an_oom_kill_is_reported_as_resource_exhausted(
    engine: StubEngine, provider: DockerProvider, policy: EffectivePolicy, workspace: str
) -> None:
    """``State.OOMKilled=true`` is the whole signal; the number beside it is not."""
    engine.job_exit_code = 137
    engine.job_oom_killed = True

    outcome = provider.run(workspace, ("stress-me",), policy, job_id="job-oom")

    assert outcome.status == STATUS_RESOURCE_EXHAUSTED
    assert outcome.exit_status == 137
    assert exit_code_for_status(outcome.status) == EXIT_RESOURCE_EXHAUSTED


def test_an_honest_exit_137_without_oomkilled_is_an_ordinary_failure(
    engine: StubEngine, provider: DockerProvider, policy: EffectivePolicy, workspace: str
) -> None:
    """A program choosing 137 for itself is a failed computation, not a budget breach.

    This is the case that makes the bare exit status ambiguous: nothing about
    it distinguishes a kernel OOM kill from a command that picked the same
    number on its own account. Only ``OOMKilled`` can, so a container that
    exits 137 with ``OOMKilled=false`` must land on exit 6, not exit 8.
    """
    engine.job_exit_code = 137
    engine.job_oom_killed = False

    outcome = provider.run(
        workspace, ("python3", "-c", "raise SystemExit(137)"), policy, job_id="job-self-137"
    )

    assert outcome.status == STATUS_FAILURE
    assert outcome.exit_status == 137
    assert exit_code_for_status(outcome.status) == EXIT_COMPUTATION_FAILED


def test_a_wall_clock_kill_reports_timeout_even_when_oomkilled_is_true(
    engine: StubEngine, provider: DockerProvider, workspace: str
) -> None:
    """headspace stopping the job on purpose outranks the kernel's own budget.

    ``_await_exit``'s own ``kill()`` yields the same 137 an OOM kill does, so
    a container that is stopped by the wall-clock enforcer AND happens to
    carry ``OOMKilled=true`` must still be reported ``timeout`` — pinned here
    with both conditions true at once, rather than trusting branch order.
    """
    engine.job_settles_to = "running"  # stays alive until `kill()` is called
    engine.job_oom_killed = True
    tight_policy = resolve_policy(
        Policy(budget=ResourceBudget(wall_clock_seconds=0)), provider.capabilities()
    )

    outcome = provider.run(workspace, ("sleep", "infinity"), tight_policy, job_id="job-wallclock")

    assert outcome.status == STATUS_TIMEOUT
    assert outcome.exit_status is None
    assert exit_code_for_status(outcome.status) == EXIT_TIMEOUT


def test_a_kill_the_daemon_refuses_is_an_engine_failure_not_a_success(
    engine: StubEngine, provider: DockerProvider, workspace: str
) -> None:
    """A refused kill must never be read as "the job exited on its own".

    ``_await_exit`` catches ``APIError`` from its own ``kill()`` because the
    container may genuinely have exited between the poll and the signal. But a
    daemon that fails for any other reason raises the same exception, and the
    container keeps running — at which point the old code returned as though a
    real exit status were waiting, and ``run`` coerced the absent ``ExitCode``
    to 0 and reported **success** for a job that never finished. A false
    success is the one report this taxonomy must never produce.
    """
    engine.job_settles_to = "running"  # never exits, so the kill is not a race
    engine.job_kill_error = APIError("daemon refused the signal")
    tight_policy = resolve_policy(
        Policy(budget=ResourceBudget(wall_clock_seconds=0)), provider.capabilities()
    )

    with pytest.raises(ProviderError) as exc:
        provider.run(workspace, ("sleep", "infinity"), tight_policy, job_id="job-killfail")

    assert "still running" in str(exc.value)
    assert exc.value.code == EXIT_INFRASTRUCTURE_FAILURE


# --- an operator ended it: the one thing the exit status cannot say (issue #16)
#
# ``stop`` and ``run`` are different processes. ``run`` holds the workspace's
# flock for the job's whole duration, so the operator's ``stop`` cannot go
# through it — and what ``stop`` leaves behind at the engine is a container
# that exited 137 with ``OOMKilled=false``, which is byte-for-byte what
# ``python -c "raise SystemExit(137)"`` leaves behind. No heuristic can tell
# those apart, and the module docstring records the live probe that proves it.
#
# So the discriminator is a *positive signal* — and, since the fabrication
# window closed, a signal in two phases. ``stop`` writes an *intent* marker
# before it signals (an operator asked, about this job) and a *signalled*
# marker after the signal has landed (the ask took effect). ``run`` reads both
# back through the same archive endpoint the ``read`` verb uses and calls a job
# cancelled only on the second one. That asymmetry is the point of the whole
# shape: intent alone is a stop that may never have happened, and believing it
# would let headspace claim a human ended a job that failed on its own account.
#
# Every test below arms those markers on the anchor container, because the
# anchor is the object ``run`` reads the volume through — the job's own
# container is gone by then.

#: A job that fails on its own account, chosen to be indistinguishable at the
#: engine from a job an operator forced: 137 with ``OOMKilled`` false.
SELF_CHOSEN_137 = 137


def _anchor(engine: StubEngine) -> StubContainer:
    """The workspace container: the object the volume is reached through."""
    anchors = [c for c in engine.registry if c.labels.get(LABEL_ROLE) == ROLE_WORKSPACE]
    assert len(anchors) == 1, f"expected exactly one anchor, found {len(anchors)}"
    return anchors[0]


def _cleared(anchor: StubContainer, name: str) -> bool:
    """Whether the provider ran its clearing script against one marker name."""
    return any(
        len(cmd) > 4 and cmd[2] == CLEAR_CANCELLATION_SCRIPT and cmd[4].endswith(f"/{name}")
        for cmd in anchor.exec_calls
    )


@pytest.fixture
def brief_settle(monkeypatch: pytest.MonkeyPatch) -> float:
    """Shrink the countersignal wait to something a test can afford to spend.

    ``run`` holds the door open for a bounded moment when an intent marker
    names the job that just settled, because the countersignal is written by
    another process that has not finished writing it yet. Production spends
    seconds there and spends them rarely; a test that waits out the real budget
    on every stale intent marker would spend them on nearly every case below.

    What is patched is the *budget*, never the mechanism: the wait, the probes
    and the classification are the provider's own throughout. The two tests
    that are about the wait itself — that it collects a late countersignal, and
    that it ends — assert against this number rather than around it.
    """
    budget = 4 * POLL_INTERVAL_SECONDS
    monkeypatch.setattr(docker_backend, "CANCELLATION_SETTLE_SECONDS", budget)
    return budget


def test_a_job_an_operator_stopped_is_recorded_cancelled_not_failed(
    engine: StubEngine, provider: DockerProvider, policy: EffectivePolicy, workspace: str
) -> None:
    """The defect from live testing: the operator's own stop read as a failure.

    ``stop`` already reported ``cancelled`` and exited 5 for its *own*
    invocation. The job's recorded outcome came from here, and here had no
    branch that could produce it — so an operator who stopped a runaway job
    was told, in the job's own record, that their work had failed.

    Complete evidence, which is what a stop that ran to completion leaves: the
    operator asked about this job, and the ask took effect.
    """
    engine.job_exit_code = SELF_CHOSEN_137
    anchor = _anchor(engine)
    anchor.plant(CANCELLATION_INTENT_MARKER_NAME, "job-stopped")
    anchor.plant(CANCELLATION_SIGNALLED_MARKER_NAME, "job-stopped")

    outcome = provider.run(workspace, ("sleep", "infinity"), policy, job_id="job-stopped")

    assert outcome.status == STATUS_CANCELLED
    assert exit_code_for_status(outcome.status) == EXIT_CANCELLED


def test_a_cancelled_job_reports_no_exit_status_of_its_own(
    engine: StubEngine, provider: DockerProvider, policy: EffectivePolicy, workspace: str
) -> None:
    """The seam refuses a cancelled outcome carrying one, and it is right to.

    ``JobOutcome.__post_init__`` rejects an exit status on a ``cancelled``
    outcome, because a job somebody else ended never produced one of its own —
    the 137 is the signal's number, not the command's answer. Dropping it is
    the contract, not a workaround for the check.
    """
    engine.job_exit_code = SELF_CHOSEN_137
    anchor = _anchor(engine)
    anchor.plant(CANCELLATION_INTENT_MARKER_NAME, "job-no-status")
    anchor.plant(CANCELLATION_SIGNALLED_MARKER_NAME, "job-no-status")

    outcome = provider.run(workspace, ("sleep", "infinity"), policy, job_id="job-no-status")

    assert outcome.status == STATUS_CANCELLED
    assert outcome.exit_status is None


def test_an_intent_marker_alone_never_fabricates_a_cancellation(
    engine: StubEngine,
    provider: DockerProvider,
    policy: EffectivePolicy,
    workspace: str,
    brief_settle: float,
) -> None:
    """The fabrication window, closed — and the single most important case here.

    One marker was not enough. It was written *before* the signal, so a ``stop``
    that died in the gap — killed, disconnected, its signal refused by the
    engine — left behind a sentinel naming a job it never actually ended. A job
    that then failed on its own account found that sentinel naming *itself* and
    was recorded ``cancelled``: headspace claiming, in the job's own permanent
    record, that a human stopped work that in truth failed by itself. An agent
    reading that record stops investigating a real defect, and an operator
    reading it is told they did something they did not do.

    So intent is not evidence of an ending. The classification moves only on
    the countersignal ``stop`` writes *after* the signal has landed, and this
    case — an operator's intent, no countersignal, a job that failed for its
    own reasons — has to classify exactly as it did before any of this existed.
    Every other residual in this channel is allowed to cost a true cancellation
    its name; none of them may invent one.
    """
    del brief_settle  # the wait is real here; the test only declines to sit through it
    engine.job_exit_code = SELF_CHOSEN_137
    _anchor(engine).plant(CANCELLATION_INTENT_MARKER_NAME, "job-asked-about")

    outcome = provider.run(workspace, ("./train.py",), policy, job_id="job-asked-about")

    assert outcome.status == STATUS_FAILURE
    assert outcome.exit_status == SELF_CHOSEN_137
    assert exit_code_for_status(outcome.status) == EXIT_COMPUTATION_FAILED


def test_the_countersignal_alone_is_enough_to_read_a_cancellation(
    engine: StubEngine, provider: DockerProvider, policy: EffectivePolicy, workspace: str
) -> None:
    """The asymmetry is deliberate: one marker is evidence, the other is not.

    The countersignal is written only after the signal has actually landed, so
    it says everything the intent marker says and more. Requiring both would
    hand a job a way to suppress its own cancellation: it shares the volume, so
    it could plant something at the *intent* name that ``stop``'s write refuses,
    and a classification that insisted on seeing intent would then read a real
    ending as a failure. Nothing a job can do to the reserved names may buy it
    a better-looking record than the truth.
    """
    engine.job_exit_code = SELF_CHOSEN_137
    _anchor(engine).plant(CANCELLATION_SIGNALLED_MARKER_NAME, "job-countersigned")

    outcome = provider.run(workspace, ("sleep", "600"), policy, job_id="job-countersigned")

    assert outcome.status == STATUS_CANCELLED
    assert outcome.exit_status is None


def test_a_countersignal_naming_another_job_changes_nothing(
    engine: StubEngine, provider: DockerProvider, policy: EffectivePolicy, workspace: str
) -> None:
    """A stale marker must never flip a later job's classification.

    This is the case that makes each marker's payload load-bearing rather than
    decorative: a bare "somebody stopped something here" flag would convict the
    next job that happened to fail.
    """
    engine.job_exit_code = SELF_CHOSEN_137
    anchor = _anchor(engine)
    anchor.plant(CANCELLATION_INTENT_MARKER_NAME, "job-from-yesterday")
    anchor.plant(CANCELLATION_SIGNALLED_MARKER_NAME, "job-from-yesterday")

    outcome = provider.run(workspace, ("false",), policy, job_id="job-today")

    assert outcome.status == STATUS_FAILURE
    assert outcome.exit_status == SELF_CHOSEN_137


def test_a_self_chosen_137_stays_a_failure_when_no_marker_is_there(
    engine: StubEngine, provider: DockerProvider, policy: EffectivePolicy, workspace: str
) -> None:
    """The indistinguishable case, pinned from the other side.

    ``python -c "raise SystemExit(137)"`` produces exactly what a stopped job
    produces. With nothing naming it, it must keep reading as the honest
    computational failure it is — the classification only ever moves on the
    positive signal, never on the number.
    """
    engine.job_exit_code = SELF_CHOSEN_137
    assert _anchor(engine).markers == {}

    outcome = provider.run(
        workspace, ("python3", "-c", "raise SystemExit(137)"), policy, job_id="job-honest-137"
    )

    assert outcome.status == STATUS_FAILURE
    assert outcome.exit_status == SELF_CHOSEN_137
    assert exit_code_for_status(outcome.status) == EXIT_COMPUTATION_FAILED


def test_a_countersignal_beside_a_job_that_succeeded_is_not_a_cancellation(
    engine: StubEngine, provider: DockerProvider, policy: EffectivePolicy, workspace: str
) -> None:
    """A job that caught the signal and finished cleanly really did succeed.

    ``stop`` sends ``SIGTERM`` before it forces anything, so a job with a
    handler can complete its work and exit 0 inside its grace window. Its own
    answer is the truthful one; the markers say the operator asked and that the
    ask landed, never that the job was cut short.
    """
    engine.job_exit_code = 0
    anchor = _anchor(engine)
    anchor.plant(CANCELLATION_INTENT_MARKER_NAME, "job-graceful")
    anchor.plant(CANCELLATION_SIGNALLED_MARKER_NAME, "job-graceful")

    outcome = provider.run(workspace, ("tidy-up",), policy, job_id="job-graceful")

    assert outcome.status == STATUS_SUCCESS
    assert outcome.exit_status == 0


def test_cancelled_outranks_a_wall_clock_timeout(
    engine: StubEngine, provider: DockerProvider, workspace: str
) -> None:
    """The documented precedence, top of the order: cancelled > timeout.

    ``_status`` already put ``timeout`` above ``resource_exhausted`` because
    headspace's own enforcer *deliberately* stopped the job, outranking the
    kernel's mark. An operator's stop is more deliberate still, so it takes the
    top slot — pinned with both conditions true at once rather than trusting
    branch order.
    """
    engine.job_settles_to = "running"  # stays alive until the enforcer kills it
    tight_policy = resolve_policy(
        Policy(budget=ResourceBudget(wall_clock_seconds=0)), provider.capabilities()
    )
    _anchor(engine).plant(CANCELLATION_SIGNALLED_MARKER_NAME, "job-both")

    outcome = provider.run(workspace, ("sleep", "infinity"), tight_policy, job_id="job-both")

    assert outcome.status == STATUS_CANCELLED
    assert outcome.exit_status is None


def test_cancelled_outranks_an_oom_kill(
    engine: StubEngine, provider: DockerProvider, policy: EffectivePolicy, workspace: str
) -> None:
    """Bottom of the same order: a stopped job is not a budget breach.

    A host under real memory pressure can mark a container ``OOMKilled`` in
    the same instant an operator ends it. Reporting ``resource_exhausted``
    would send an autonomous caller off to raise a memory ceiling over a
    decision a human already made.
    """
    engine.job_exit_code = SELF_CHOSEN_137
    engine.job_oom_killed = True
    _anchor(engine).plant(CANCELLATION_SIGNALLED_MARKER_NAME, "job-oom-and-stopped")

    outcome = provider.run(workspace, ("stress-me",), policy, job_id="job-oom-and-stopped")

    assert outcome.status == STATUS_CANCELLED
    assert outcome.exit_status is None


@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_a_countersignal_that_is_not_a_regular_file_is_refused(
    engine: StubEngine,
    provider: DockerProvider,
    policy: EffectivePolicy,
    workspace: str,
    kind: str,
) -> None:
    """A job sharing the volume can plant one; neither may name a job.

    The read-side half of the symlink discipline the copy-in path already
    keeps. The archive endpoint resolves a link target within the container's
    filesystem quite happily, so a link at a reserved name would let a job
    nominate any file on that filesystem as the thing naming it. Only a regular
    file is read, and anything else leaves the classification exactly where it
    was — which is the direction every tampering case has to fail in.
    """
    engine.job_exit_code = SELF_CHOSEN_137
    _anchor(engine).plant(CANCELLATION_SIGNALLED_MARKER_NAME, "job-planted", kind)

    outcome = provider.run(workspace, ("false",), policy, job_id="job-planted")

    assert outcome.status == STATUS_FAILURE
    assert outcome.exit_status == SELF_CHOSEN_137


# --- the countersignal is written by a process that has not finished writing it


def test_an_intent_marker_naming_this_job_waits_for_the_countersignal(
    engine: StubEngine, provider: DockerProvider, policy: EffectivePolicy, workspace: str
) -> None:
    """Why the intent marker is written at all, now that it cannot convict.

    The two phases are not two pieces of evidence to add up. The countersignal
    is the evidence; the intent marker is the *warning* that one is coming, and
    it is the only thing that makes a countersignal written after the signal
    reachable at all. ``stop`` cannot write it any earlier — the signal has to
    have landed for it to be true — and by then this process is already awake,
    because ``stop`` blocked until the container it signalled had exited and
    ``run`` is polling that same container. Without a reason to wait, ``run``
    would read the volume in the gap and record a genuine, completed
    cancellation as a failure most of the time.

    So an intent marker naming the job that just settled holds the door open
    for a bounded moment. Nothing else does: a job with no intent marker pays
    nothing, and the wait can only ever turn a missed cancellation into a
    recorded one — never a failure into a cancellation, because what it waits
    for is still the countersignal and nothing else.
    """
    engine.job_exit_code = SELF_CHOSEN_137
    anchor = _anchor(engine)
    anchor.plant(CANCELLATION_INTENT_MARKER_NAME, "job-in-flight")
    # Not there when `run` first looks, exactly as a countersignal the other
    # process is still on its way to writing is not there.
    anchor.plant(CANCELLATION_SIGNALLED_MARKER_NAME, "job-in-flight", on_probe=3)

    outcome = provider.run(workspace, ("sleep", "600"), policy, job_id="job-in-flight")

    assert outcome.status == STATUS_CANCELLED
    assert anchor.probes[CANCELLATION_SIGNALLED_MARKER_NAME] == 3


def test_the_wait_for_a_countersignal_ends(
    engine: StubEngine,
    provider: DockerProvider,
    policy: EffectivePolicy,
    workspace: str,
    brief_settle: float,
) -> None:
    """Bounded, and it fails towards ``failure`` when the budget runs out.

    A ``stop`` that died before it could countersign leaves an intent marker
    that will never be answered. Waiting on it forever would hold a finished
    job's result hostage to a process that is not coming back — so the wait has
    a budget, and spending it decides the case the way the evidence actually
    reads: no countersignal, no cancellation.
    """
    engine.job_exit_code = SELF_CHOSEN_137
    anchor = _anchor(engine)
    anchor.plant(CANCELLATION_INTENT_MARKER_NAME, "job-abandoned")

    started = time.monotonic()
    outcome = provider.run(workspace, ("./train.py",), policy, job_id="job-abandoned")
    elapsed = time.monotonic() - started

    assert outcome.status == STATUS_FAILURE
    assert elapsed >= brief_settle  # it really did hold the door open
    # And it really did stop. The probes are what the budget bounds, so
    # counting them pins the ceiling without asserting on a clock the host owns
    # and the suite's other tests are competing for.
    ceiling = int(brief_settle / POLL_INTERVAL_SECONDS) + 2
    assert anchor.probes[CANCELLATION_SIGNALLED_MARKER_NAME] <= ceiling


def test_only_an_intent_marker_naming_this_job_is_worth_waiting_for(
    engine: StubEngine, provider: DockerProvider, policy: EffectivePolicy, workspace: str
) -> None:
    """Residue from somebody else's stop costs this job nothing.

    An intent marker naming another job says nothing about the job that just
    ran, so there is nothing to hold the door open for: this run reads once,
    clears the residue and reports what the job did. Left unqualified, the wait
    would become a tax every job in a workspace paid for one crashed ``stop``
    — with the real budget, not this test's.
    """
    engine.job_exit_code = SELF_CHOSEN_137
    anchor = _anchor(engine)
    anchor.plant(CANCELLATION_INTENT_MARKER_NAME, "somebody-elses-job")

    outcome = provider.run(workspace, ("false",), policy, job_id="job-today")

    assert outcome.status == STATUS_FAILURE
    assert anchor.probes[CANCELLATION_SIGNALLED_MARKER_NAME] == 1


# --- consumed, always, and under both names ----------------------------------


@pytest.mark.parametrize("exit_code", [0, SELF_CHOSEN_137])
def test_both_markers_are_consumed_whatever_the_job_did(
    engine: StubEngine,
    provider: DockerProvider,
    policy: EffectivePolicy,
    workspace: str,
    exit_code: int,
) -> None:
    """Including exit 0 — the path no non-zero branch would ever reach.

    A ``stop`` that races a job finishing naturally leaves markers nobody
    consumed. Clearing them only on the failing path would leave them to be
    found by whichever later job happened to fail, which is precisely the
    stale-marker hazard the payload check already guards — and a guard that is
    never exercised is one nobody notices breaking.

    Under *both* names, and that is the half a two-phase channel makes easy to
    get wrong: clearing only the name this classification read would leave the
    other one standing, and one marker left behind is the whole wedge.
    """
    engine.job_exit_code = exit_code
    anchor = _anchor(engine)
    for name in CANCELLATION_MARKER_NAMES:
        anchor.plant(name, "job-raced")

    provider.run(workspace, ("whatever",), policy, job_id="job-raced")

    assert anchor.markers == {}
    assert all(_cleared(anchor, name) for name in CANCELLATION_MARKER_NAMES)


def test_markers_naming_another_job_are_cleared_too(
    engine: StubEngine, provider: DockerProvider, policy: EffectivePolicy, workspace: str
) -> None:
    """Residue is residue, whoever it names — and clearing it ends the hazard."""
    engine.job_exit_code = SELF_CHOSEN_137
    anchor = _anchor(engine)
    for name in CANCELLATION_MARKER_NAMES:
        anchor.plant(name, "job-from-yesterday")

    provider.run(workspace, ("false",), policy, job_id="job-today")

    assert anchor.markers == {}


@pytest.mark.parametrize("name", CANCELLATION_MARKER_NAMES)
@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_a_planted_object_at_either_marker_path_is_cleared_as_well(
    engine: StubEngine,
    provider: DockerProvider,
    policy: EffectivePolicy,
    workspace: str,
    name: str,
    kind: str,
) -> None:
    """Refusing to *read* it is not enough; leaving it there wedges the channel.

    A link or a directory a job planted at either reserved name would sit in
    the volume forever, and every later ``stop`` would meet it as the planted
    object its own write refuses — so the job that planted it would suppress
    the naming of every cancellation that workspace ever saw afterwards.
    Clearing whatever stands there — the link itself, never its target — is
    what keeps the channel usable after an attack on it.
    """
    engine.job_exit_code = SELF_CHOSEN_137
    anchor = _anchor(engine)
    anchor.plant(name, "job-planted", kind)

    provider.run(workspace, ("false",), policy, job_id="job-planted")

    assert anchor.markers == {}


def test_no_marker_means_no_exec_against_the_workspace_container(
    engine: StubEngine, provider: DockerProvider, policy: EffectivePolicy, workspace: str
) -> None:
    """The ordinary job pays two archive probes and nothing else.

    Almost no job is ever stopped by an operator, so the clearing exec must be
    the exception rather than a tax every ``run`` pays — and ``run`` must not
    quietly acquire a requirement for an anchor that can execute a shell, which
    it has never had.

    Two probes rather than one is what the second phase costs, and it is not
    optional: the intent name has to be read even when nothing was ever
    cancelled, because a crashed ``stop`` leaves a marker there that no other
    code path would ever take away.
    """
    anchor = _anchor(engine)

    provider.run(workspace, ("true",), policy, job_id="job-ordinary")

    assert anchor.exec_calls == []
    assert dict(anchor.probes) == {name: 1 for name in CANCELLATION_MARKER_NAMES}


def test_a_stale_marker_cannot_reach_the_job_after_next(
    engine: StubEngine, provider: DockerProvider, policy: EffectivePolicy, workspace: str
) -> None:
    """The lifecycle end to end: consumed once, and never seen again."""
    engine.job_exit_code = SELF_CHOSEN_137
    _anchor(engine).plant(CANCELLATION_SIGNALLED_MARKER_NAME, "job-one")

    first = provider.run(workspace, ("false",), policy, job_id="job-one")
    second = provider.run(workspace, ("false",), policy, job_id="job-one")

    assert first.status == STATUS_CANCELLED
    assert second.status == STATUS_FAILURE
    assert second.exit_status == SELF_CHOSEN_137


def test_what_a_crashed_run_left_behind_is_cleaned_by_the_next_one(
    engine: StubEngine, provider: DockerProvider, policy: EffectivePolicy, workspace: str
) -> None:
    """The one thing that consumes a marker is the next ``run``.

    ``run`` clears what it reads, so a ``run`` that never got as far as
    classifying — killed, or its host lost — leaves both names standing in the
    volume with a job id nobody will ever ask about again. Nothing else comes
    along afterwards to tidy up: reconciliation reads the journal and reaches
    the engine, and neither of those knows these two names exist.

    What does know them is the next job's own classification, which reads and
    clears both unconditionally, whoever they name. So this is the guarantee
    stated the way it is actually kept — the leftovers cost the next job in
    that workspace nothing at all, and the job after that never sees them.
    """
    engine.job_exit_code = SELF_CHOSEN_137
    anchor = _anchor(engine)
    for name in CANCELLATION_MARKER_NAMES:
        anchor.plant(name, "job-nobody-classified")

    first = provider.run(workspace, ("false",), policy, job_id="job-after-the-crash")
    second = provider.run(workspace, ("false",), policy, job_id="job-after-that")

    assert first.status == STATUS_FAILURE
    assert second.status == STATUS_FAILURE
    assert anchor.markers == {}
    assert all(_cleared(anchor, name) for name in CANCELLATION_MARKER_NAMES)


# --- through the orchestrator, to the rendered package -----------------------


@pytest.fixture
def orchestrator(provider: DockerProvider, store_home: Path) -> Orchestrator:
    del store_home  # the env var is what Store() reads
    return Orchestrator(provider, Store())


def _run_a_refused_command(orchestrator: Orchestrator, engine: StubEngine) -> Any:
    orchestrator.create(workspace_id=WORKSPACE)
    engine.job_start_error = api_error(WORDINGS["absent from PATH"][1])
    return orchestrator.run(WORKSPACE, ["definitely-not-a-binary"], job_id="job-nx")


def test_the_package_reports_a_failed_computation_not_a_broken_engine(
    orchestrator: Orchestrator, engine: StubEngine
) -> None:
    package = _run_a_refused_command(orchestrator, engine)
    assert package.status == STATUS_FAILURE
    assert exit_code_for_status(package.status) == EXIT_COMPUTATION_FAILED


def test_neither_rendering_carries_a_container_id_or_an_engine_url(
    orchestrator: Orchestrator, engine: StubEngine
) -> None:
    package = _run_a_refused_command(orchestrator, engine)
    for rendered in (render_markdown(package), render_json(package)):
        for forbidden in FORBIDDEN:
            assert forbidden not in rendered


def test_the_rendered_package_names_the_profile_and_the_command(
    orchestrator: Orchestrator, engine: StubEngine
) -> None:
    package = _run_a_refused_command(orchestrator, engine)
    rendered = render_markdown(package)
    assert profiles.DEFAULT_PROFILE in rendered
    assert "definitely-not-a-binary" in rendered


def test_the_full_engine_diagnosis_is_retrievable_for_the_job_handle(
    orchestrator: Orchestrator, engine: StubEngine, store_home: Path
) -> None:
    """The bounded-evidence escape hatch: ``headspace inspect <job> --logs``."""
    del store_home
    _run_a_refused_command(orchestrator, engine)
    payload = collect_logs(Store(), "job-nx")
    assert EXEC_INIT_MARKER in payload["jobs"][0]["output"]


# --- and through the process boundary an agent actually reads ----------------


@pytest.fixture
def cli_provider(provider: DockerProvider) -> Iterator[DockerProvider]:
    """Wire ``--provider docker`` to the stub-backed provider, then put it back."""
    original = provider_registry._build_provider
    provider_registry.reset_providers()
    provider_registry._build_provider = lambda name: provider  # type: ignore[assignment]
    try:
        yield provider
    finally:
        provider_registry._build_provider = original  # type: ignore[assignment]
        provider_registry.reset_providers()


def test_the_cli_exits_six_and_leaks_nothing_on_a_command_the_image_cannot_run(
    cli_provider: DockerProvider,
    engine: StubEngine,
    store_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The reproduction from the live test, at the surface a subprocess consumer reads."""
    del store_home, cli_provider
    assert main(["create", "--workspace-id", WORKSPACE]) == 0
    capsys.readouterr()

    engine.job_start_error = api_error(WORDINGS["absent from PATH"][1])
    code = main(["run", "--json", "--job-id", "job-nx", WORKSPACE, "definitely-not-a-binary"])
    captured = capsys.readouterr()
    combined = captured.out + captured.err

    assert code == EXIT_COMPUTATION_FAILED
    for forbidden in FORBIDDEN:
        assert forbidden not in combined
    assert "retry" not in combined.lower()

    payload = json.loads(captured.out)
    assert payload["status"] == STATUS_FAILURE
    assert payload["provenance"]["profile"] == profiles.DEFAULT_PROFILE
    assert "definitely-not-a-binary" in json.dumps(payload)


def test_the_cli_exits_eight_on_a_job_the_engine_marks_oomkilled(
    cli_provider: DockerProvider,
    engine: StubEngine,
    store_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The end-to-end surface an autonomous consumer reads: exit 8, not exit 6.

    This is the defect from live testing: without this, a memory-killed job
    reported plain ``failure`` at exit 6 and an agent retried an identical job
    that would be killed identically, instead of raising its memory ceiling.
    """
    del store_home, cli_provider
    assert main(["create", "--workspace-id", WORKSPACE]) == 0
    capsys.readouterr()

    engine.job_exit_code = 137
    engine.job_oom_killed = True
    code = main(["run", "--json", "--job-id", "job-oom", WORKSPACE, "stress-me"])
    captured = capsys.readouterr()

    assert code == EXIT_RESOURCE_EXHAUSTED
    payload = json.loads(captured.out)
    assert payload["status"] == STATUS_RESOURCE_EXHAUSTED


def test_the_cli_exits_five_on_a_job_an_operator_stopped(
    cli_provider: DockerProvider,
    engine: StubEngine,
    store_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The whole point of issue #16, at the surface the operator's ``run`` prints.

    The ``stop`` invocation already exited 5. This is the *other* process — the
    one that was blocked on the job and writes its record — and until now it
    exited 6 and wrote ``failure``, telling an operator their job had failed
    when they were the one who ended it.
    """
    del store_home, cli_provider
    assert main(["create", "--workspace-id", WORKSPACE]) == 0
    capsys.readouterr()

    engine.job_exit_code = SELF_CHOSEN_137
    anchor = _anchor(engine)
    anchor.plant(CANCELLATION_INTENT_MARKER_NAME, "job-stopped-cli")
    anchor.plant(CANCELLATION_SIGNALLED_MARKER_NAME, "job-stopped-cli")
    code = main(["run", "--json", "--job-id", "job-stopped-cli", WORKSPACE, "sleep", "infinity"])
    captured = capsys.readouterr()

    assert code == EXIT_CANCELLED
    payload = json.loads(captured.out)
    assert payload["status"] == STATUS_CANCELLED


# --- criterion 3: the secret is what makes the id enough (issue #20) ---------


@pytest.mark.parametrize(
    "forged",
    [
        pytest.param("job-forger", id="the-bare-job-id"),
        pytest.param(f"{MARKER_FIELD_SEPARATOR}job-forger", id="an-empty-token"),
        pytest.param(f"guessed{MARKER_FIELD_SEPARATOR}job-forger", id="a-guessed-token"),
        pytest.param(f"job-forger{MARKER_FIELD_SEPARATOR}job-forger", id="the-id-as-the-token"),
    ],
)
def test_a_countersignal_a_job_wrote_for_itself_is_refused(
    engine: StubEngine,
    provider: DockerProvider,
    policy: EffectivePolicy,
    workspace: str,
    forged: str,
) -> None:
    """The hole a caller-chosen ``--job-id`` used to open, closed.

    A job shares the volume, so it can write anything it likes at either
    marker's path — the write was never what made a marker believable. Until
    the payload carried a secret, the *only* thing standing between a job and a
    fabricated ``cancelled`` was its not knowing its own id, and that id is the
    caller's to choose. A caller who picks predictable ids and runs code it
    does not trust had handed that code everything it needed.

    So the payload is the workspace's token *and* the job id, and this test is
    the guarantee: every shape a job could plant knowing its own id in full —
    the bare id, an empty token, a guessed one, the id reused as the token —
    reaches classification and is refused. What it cannot produce is 128 bits
    it has no way to observe, because the token lives on the anchor container's
    labels and a label is readable by no process inside any container, let
    alone by a job in a *different* container.

    Recorded as a `cancelled` before the token existed; a live probe forged one
    exactly this way. Compare
    :func:`test_a_job_an_operator_stopped_is_recorded_cancelled_not_failed`,
    which plants the same names *with* the token and is believed — the pair is
    what makes this an assertion about the secret rather than about the write.
    """
    engine.job_exit_code = SELF_CHOSEN_137
    anchor = _anchor(engine)
    anchor.plant_unauthenticated(CANCELLATION_INTENT_MARKER_NAME, forged)
    anchor.plant_unauthenticated(CANCELLATION_SIGNALLED_MARKER_NAME, forged)

    outcome = provider.run(workspace, ("./train.py",), policy, job_id="job-forger")

    assert outcome.status == STATUS_FAILURE
    assert outcome.exit_status == SELF_CHOSEN_137
    # And they are still cleared: refusing to believe a marker is not a reason
    # to leave it in the volume for the next job to be measured against.
    assert anchor.markers == {}


def test_a_workspace_with_no_token_classifies_no_cancellation_at_all(
    engine: StubEngine, provider: DockerProvider, policy: EffectivePolicy, workspace: str
) -> None:
    """An anchor from before the token: the honest answer is the old answer.

    A workspace created by an earlier headspace has an anchor carrying no
    :data:`LABEL_CANCEL_TOKEN`, so there is no secret for either process to put
    in a marker or to check one against. The tempting move — fall back to
    comparing the bare job id when no token is present — is precisely the hole
    this closes, and an attacker who could induce the fallback would have the
    whole of it back.

    So no token means no cancellation can be read here, whatever stands in the
    volume, and such a workspace records a stopped job exactly as headspace did
    before any of this channel existed. That is the same direction every other
    partial state of this channel fails in: it costs a true cancellation its
    name and never invents one.
    """
    engine.job_exit_code = SELF_CHOSEN_137
    anchor = _anchor(engine)
    # Both markers, complete and well-formed for the *previous* payload format.
    anchor.plant_unauthenticated(CANCELLATION_INTENT_MARKER_NAME, "job-legacy")
    anchor.plant_unauthenticated(CANCELLATION_SIGNALLED_MARKER_NAME, "job-legacy")
    del anchor.labels[LABEL_CANCEL_TOKEN]

    outcome = provider.run(workspace, ("./train.py",), policy, job_id="job-legacy")

    assert outcome.status == STATUS_FAILURE
    assert anchor.markers == {}


def test_two_workspaces_never_share_a_cancellation_token(
    provider: DockerProvider, policy: EffectivePolicy, engine: StubEngine, workspace: str
) -> None:
    """A token shared across workspaces would be one leak away from useless.

    Nothing in the design says a job in workspace A cannot end up learning
    A's token — a caller might print it, an image might be built from an
    inspect dump, a support bundle might carry it. What the design *does* say
    is that such a leak costs exactly one workspace, so the tokens have to be
    independent draws rather than anything derived from the workspace id or
    minted once per process.
    """
    provider.create("second-ws", profiles.resolve(profiles.DEFAULT_PROFILE), policy)

    tokens = {
        container.labels[LABEL_CANCEL_TOKEN]
        for container in engine.registry
        if container.labels.get(LABEL_ROLE) == ROLE_WORKSPACE
    }
    assert len(tokens) == 2, f"two workspaces produced {len(tokens)} distinct token(s)"
