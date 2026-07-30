"""A command the image cannot execute is the caller's error, not a broken engine.

And, since issue #16, the other half of the same idea: a job an *operator*
ended is not a failed computation either. Both are cases where the honest
answer cannot be read off the engine's numbers — an exec-step 400 looks like
any other 400, and a stopped container looks exactly like one that chose 137
for itself — so both are settled by a signal the provider can actually trust:
the engine's own marker for the first, and a sentinel another process wrote
for the second. The classification tests for both live here.

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
from headspace.providers.base import ProviderError
from headspace.providers.docker import (
    CANCELLATION_MARKER_NAME,
    CANCELLATION_MARKER_PATH,
    CLEAR_CANCELLATION_SCRIPT,
    EXEC_INIT_MARKER,
    EXIT_COMMAND_NOT_EXECUTABLE,
    EXIT_COMMAND_NOT_FOUND,
    LABEL_ROLE,
    ROLE_WORKSPACE,
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
        #: What the workspace volume holds at the cancellation sentinel's path,
        #: as the *other* process would have left it: the text of the marker
        #: file, or ``None`` for the ordinary case where no operator stopped
        #: anything. Armed on the anchor container, because that is the object
        #: ``run`` reads the volume through — see the sentinel tests below.
        self.marker: str | None = None
        #: What kind of filesystem object stands at that path: ``"file"``,
        #: ``"symlink"`` or ``"directory"``. Only a regular file is a sentinel;
        #: the other two are what a job sharing the volume can plant there, and
        #: the archive endpoint reports each one honestly in the tar member's
        #: own type — which is the whole reason the provider can refuse them.
        self.marker_kind = "file"
        #: Every command the provider ran as an exec, in call order.
        self.exec_calls: list[list[str]] = []

    # -- the workspace volume, as the archive endpoint and an exec see it -----

    def get_archive(self, path: str, chunk_size: int | None = None) -> tuple[Any, dict[str, Any]]:
        """The engine's transfer archive for one path in the volume.

        Answers a 404 for a path that holds nothing, exactly as the daemon
        does — which is the ordinary answer for the sentinel, since almost no
        job is ever stopped by an operator. When something *is* there, the
        member carries that object's real tar type, so a planted symlink or
        directory reaches the provider as a symlink or directory rather than
        as bytes a stub decided to hand over.
        """
        del chunk_size  # the marker is far smaller than any chunk worth cutting
        if path != CANCELLATION_MARKER_PATH or self.marker is None:
            raise NotFound(f'404 Client Error for {ENDPOINT}: Not Found ("{path}")')
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            entry = tarfile.TarInfo(CANCELLATION_MARKER_NAME)
            if self.marker_kind == "symlink":
                entry.type = tarfile.SYMTYPE
                entry.linkname = self.marker
                archive.addfile(entry)
            elif self.marker_kind == "directory":
                entry.type = tarfile.DIRTYPE
                archive.addfile(entry)
            else:
                payload = self.marker.encode("utf-8")
                entry.size = len(payload)
                archive.addfile(entry, io.BytesIO(payload))
        # A generator, not a plain iterator: the SDK hands back one, and the
        # provider registers its ``close`` with the stream's release — a stub
        # answering with something that has no ``close`` would quietly excuse
        # the provider from releasing what it opened.
        return (chunk for chunk in [buffer.getvalue()]), {"name": CANCELLATION_MARKER_NAME}

    def exec_run(self, cmd: Sequence[str], **_: Any) -> SimpleNamespace:
        """Run one of the provider's scripts — recorded, and honoured for effect.

        Only the clearing script has an effect worth modelling here: it is the
        one whose *absence* would leave a stale sentinel behind for the next
        job, so the stub really removes the marker rather than merely counting
        the call.
        """
        self.exec_calls.append(list(cmd))
        if len(cmd) > 2 and cmd[2] == CLEAR_CANCELLATION_SCRIPT:
            self.marker = None
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
# So the discriminator is a *positive signal*: ``stop`` writes a sentinel into
# the workspace volume naming the job it signalled, and ``run`` reads it back
# through the same archive endpoint the ``read`` verb uses. Every test below
# arms that sentinel on the anchor container, because the anchor is the object
# ``run`` reads the volume through — the job's own container is gone by then.


def _anchor(engine: StubEngine) -> StubContainer:
    """The workspace container: the object the volume is reached through."""
    anchors = [c for c in engine.registry if c.labels.get(LABEL_ROLE) == ROLE_WORKSPACE]
    assert len(anchors) == 1, f"expected exactly one anchor, found {len(anchors)}"
    return anchors[0]


def _cleared(anchor: StubContainer) -> bool:
    """Whether the provider ran its sentinel-clearing script at all."""
    return any(len(cmd) > 2 and cmd[2] == CLEAR_CANCELLATION_SCRIPT for cmd in anchor.exec_calls)


def test_a_job_an_operator_stopped_is_recorded_cancelled_not_failed(
    engine: StubEngine, provider: DockerProvider, policy: EffectivePolicy, workspace: str
) -> None:
    """The defect from live testing: the operator's own stop read as a failure.

    ``stop`` already reported ``cancelled`` and exited 5 for its *own*
    invocation. The job's recorded outcome came from here, and here had no
    branch that could produce it — so an operator who stopped a runaway job
    was told, in the job's own record, that their work had failed.
    """
    engine.job_exit_code = 137
    _anchor(engine).marker = "job-stopped"

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
    engine.job_exit_code = 137
    _anchor(engine).marker = "job-no-status"

    outcome = provider.run(workspace, ("sleep", "infinity"), policy, job_id="job-no-status")

    assert outcome.status == STATUS_CANCELLED
    assert outcome.exit_status is None


def test_a_sentinel_naming_another_job_changes_nothing(
    engine: StubEngine, provider: DockerProvider, policy: EffectivePolicy, workspace: str
) -> None:
    """A stale sentinel must never flip a later job's classification.

    This is the case that makes the sentinel's payload load-bearing rather
    than decorative: a bare "somebody stopped something here" flag would
    convict the next job that happened to fail.
    """
    engine.job_exit_code = 137
    _anchor(engine).marker = "job-from-yesterday"

    outcome = provider.run(workspace, ("false",), policy, job_id="job-today")

    assert outcome.status == STATUS_FAILURE
    assert outcome.exit_status == 137


def test_a_self_chosen_137_stays_a_failure_when_no_sentinel_is_there(
    engine: StubEngine, provider: DockerProvider, policy: EffectivePolicy, workspace: str
) -> None:
    """The indistinguishable case, pinned from the other side.

    ``python -c "raise SystemExit(137)"`` produces exactly what a stopped job
    produces. With no sentinel naming it, it must keep reading as the honest
    computational failure it is — the classification only ever moves on the
    positive signal, never on the number.
    """
    engine.job_exit_code = 137
    assert _anchor(engine).marker is None

    outcome = provider.run(
        workspace, ("python3", "-c", "raise SystemExit(137)"), policy, job_id="job-honest-137"
    )

    assert outcome.status == STATUS_FAILURE
    assert outcome.exit_status == 137
    assert exit_code_for_status(outcome.status) == EXIT_COMPUTATION_FAILED


def test_a_sentinel_beside_a_job_that_succeeded_is_not_a_cancellation(
    engine: StubEngine, provider: DockerProvider, policy: EffectivePolicy, workspace: str
) -> None:
    """A job that caught the signal and finished cleanly really did succeed.

    ``stop`` sends ``SIGTERM`` before it forces anything, so a job with a
    handler can complete its work and exit 0 inside its grace window. Its own
    answer is the truthful one; the sentinel says the operator asked, not that
    the job was cut short.
    """
    engine.job_exit_code = 0
    _anchor(engine).marker = "job-graceful"

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
    _anchor(engine).marker = "job-both"

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
    engine.job_exit_code = 137
    engine.job_oom_killed = True
    _anchor(engine).marker = "job-oom-and-stopped"

    outcome = provider.run(workspace, ("stress-me",), policy, job_id="job-oom-and-stopped")

    assert outcome.status == STATUS_CANCELLED
    assert outcome.exit_status is None


@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_a_sentinel_that_is_not_a_regular_file_is_refused(
    engine: StubEngine,
    provider: DockerProvider,
    policy: EffectivePolicy,
    workspace: str,
    kind: str,
) -> None:
    """A job sharing the volume can plant one; neither may name a job.

    The read-side half of the symlink discipline the copy-in path already
    keeps. The archive endpoint resolves a link target within the container's
    filesystem quite happily, so a link at the sentinel's path would let a job
    nominate any file on that filesystem as the thing naming it. Only a
    regular file is read, and anything else leaves the classification exactly
    where it was.
    """
    engine.job_exit_code = 137
    anchor = _anchor(engine)
    anchor.marker = "job-planted"
    anchor.marker_kind = kind

    outcome = provider.run(workspace, ("false",), policy, job_id="job-planted")

    assert outcome.status == STATUS_FAILURE
    assert outcome.exit_status == 137


@pytest.mark.parametrize("exit_code", [0, 137])
def test_the_sentinel_is_consumed_whatever_the_job_did(
    engine: StubEngine,
    provider: DockerProvider,
    policy: EffectivePolicy,
    workspace: str,
    exit_code: int,
) -> None:
    """Including exit 0 — the path no non-zero branch would ever reach.

    A ``stop`` that races a job finishing naturally leaves a sentinel nobody
    consumed. Clearing it only on the failing path would leave that one to be
    found by whichever later job happened to fail, which is precisely the
    stale-sentinel hazard the payload check already guards — and a guard that
    is never exercised is one nobody notices breaking.
    """
    engine.job_exit_code = exit_code
    anchor = _anchor(engine)
    anchor.marker = "job-raced"

    provider.run(workspace, ("whatever",), policy, job_id="job-raced")

    assert anchor.marker is None
    assert _cleared(anchor)


def test_a_sentinel_naming_another_job_is_cleared_too(
    engine: StubEngine, provider: DockerProvider, policy: EffectivePolicy, workspace: str
) -> None:
    """Residue is residue, whoever it names — and clearing it ends the hazard."""
    engine.job_exit_code = 137
    anchor = _anchor(engine)
    anchor.marker = "job-from-yesterday"

    provider.run(workspace, ("false",), policy, job_id="job-today")

    assert anchor.marker is None


@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_a_planted_object_at_the_sentinel_path_is_cleared_as_well(
    engine: StubEngine,
    provider: DockerProvider,
    policy: EffectivePolicy,
    workspace: str,
    kind: str,
) -> None:
    """Refusing to *read* it is not enough; leaving it there wedges the channel.

    A link a job planted at the sentinel's path would sit in the volume
    forever, and every later ``stop`` would meet it as the pre-planted link
    its own write refuses. Clearing whatever stands there — the link itself,
    never its target — is what keeps the channel usable after an attack on it.
    """
    engine.job_exit_code = 137
    anchor = _anchor(engine)
    anchor.marker = "job-planted"
    anchor.marker_kind = kind

    provider.run(workspace, ("false",), policy, job_id="job-planted")

    assert anchor.marker is None


def test_no_sentinel_means_no_exec_against_the_workspace_container(
    engine: StubEngine, provider: DockerProvider, policy: EffectivePolicy, workspace: str
) -> None:
    """The ordinary job pays one archive probe and nothing else.

    Almost no job is ever stopped by an operator, so the clearing exec must be
    the exception rather than a tax every ``run`` pays — and ``run`` must not
    quietly acquire a requirement for an anchor that can execute a shell,
    which it has never had.
    """
    provider.run(workspace, ("true",), policy, job_id="job-ordinary")

    assert _anchor(engine).exec_calls == []


def test_a_stale_sentinel_cannot_reach_the_job_after_next(
    engine: StubEngine, provider: DockerProvider, policy: EffectivePolicy, workspace: str
) -> None:
    """The lifecycle end to end: consumed once, and never seen again."""
    engine.job_exit_code = 137
    _anchor(engine).marker = "job-one"

    first = provider.run(workspace, ("false",), policy, job_id="job-one")
    second = provider.run(workspace, ("false",), policy, job_id="job-one")

    assert first.status == STATUS_CANCELLED
    assert second.status == STATUS_FAILURE
    assert second.exit_status == 137


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

    engine.job_exit_code = 137
    _anchor(engine).marker = "job-stopped-cli"
    code = main(["run", "--json", "--job-id", "job-stopped-cli", WORKSPACE, "sleep", "infinity"])
    captured = capsys.readouterr()

    assert code == EXIT_CANCELLED
    payload = json.loads(captured.out)
    assert payload["status"] == STATUS_CANCELLED
