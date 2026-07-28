"""A command the image cannot execute is the caller's error, not a broken engine.

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

import json
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from docker.errors import APIError

import headspace.cli._commands.create as provider_registry
from headspace.cli import main
from headspace.cli._commands.inspect import collect_logs
from headspace.cli._errors import EXIT_COMPUTATION_FAILED, EXIT_INFRASTRUCTURE_FAILURE
from headspace.core import profiles
from headspace.core.policy import EffectivePolicy, Policy
from headspace.core.policy import resolve as resolve_policy
from headspace.core.result import STATUS_FAILURE, STATUS_SUCCESS, render_json, render_markdown
from headspace.core.store import HOME_ENV_VAR, Store
from headspace.core.workspace import Orchestrator, exit_code_for_status
from headspace.providers.base import ProviderError
from headspace.providers.docker import (
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

    @property
    def status(self) -> str:
        return str(self.attrs["State"]["Status"])

    def start(self) -> None:
        if self._start_error is not None:
            raise self._start_error
        self.attrs["State"] = {"Status": self._settles_to, "ExitCode": self._exit_code}

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
        self.attrs["State"] = {"Status": "exited", "ExitCode": 137}


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
            settles_to="exited" if job else "running",
            exit_code=self._engine.job_exit_code if job else 0,
            frames=self._engine.job_frames if job else (),
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
