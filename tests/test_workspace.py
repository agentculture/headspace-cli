"""Tests for workspace orchestration: lifecycle flows, journal, reconciliation, destroy guard.

Written first, against the three acceptance criteria this task was given:

1. create, run, inspect, export and destroy validate transitions and pass
   against the fake provider;
2. a *simulated crash* between the engine call and the state write leaves a
   journalled orphan the next invocation reconciles and reports in attention
   items;
3. destroy with unexported declared artifacts refuses without ``force`` and
   removes nothing; destroy during an active job takes the documented path.

Two of those need care, so read this before reading the tests:

**The crash is real, not mocked.** :class:`CrashingStore` raises
``KeyboardInterrupt`` — a BaseException, the same class of interruption a
``SIGINT``-killed CLI raises — from inside :meth:`Store.write_state`, at the
exact write the criterion names. Nothing catches it: the orchestrator's error
handling catches ``CliError`` only, so no "abandoned" journal entry is written
and the flow unwinds precisely as a killed process would leave it. Each crash
test then asserts the *situation* on disk (state file stale, intent journalled
and unsettled, engine object still present) before a **fresh**
:class:`Orchestrator` over a **fresh** :class:`Store` — sharing nothing with the
crashed one but the store root on disk and the still-live provider, which is
the in-memory analogue of a new process against a running engine — reconciles
it. No test asserts that a function was called.

**"Removes nothing" is asserted positively.** After a refused destroy the tests
check that the workspace record, the engine object, every artifact record and
every exported file are all still there, and that no removal intent was ever
journalled — not merely that an exception was raised.

The provider is always the in-memory fake: this layer depends on the
:class:`~headspace.providers.base.Provider` Protocol and nothing else, and a
test at the bottom of this file enforces that at the source level.
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import io
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

import headspace.core.workspace as workspace_module
from headspace.cli._errors import (
    EXIT_COMPUTATION_FAILED,
    EXIT_ENV_ERROR,
    EXIT_INFRASTRUCTURE_FAILURE,
    EXIT_TIMEOUT,
    EXIT_USER_ERROR,
    CliError,
)
from headspace.core.artifacts import (
    RETENTION_DECLARED,
    RETENTION_EXPORTED,
    ArtifactRecord,
)
from headspace.core.policy import (
    CapabilitySnapshot,
    NetworkPosture,
    Policy,
    PolicyError,
    ResourceBudget,
)
from headspace.core.profiles import DEFAULT_PROFILE
from headspace.core.result import (
    STATUS_FAILURE,
    STATUS_SUCCESS,
    STATUS_TIMEOUT,
    Artifact,
)
from headspace.core.states import State
from headspace.core.store import HOME_ENV_VAR, Store
from headspace.core.workspace import (
    ARTIFACT_FIELD_MAP,
    DISPOSITION_ADOPTED,
    DISPOSITION_QUARANTINED,
    DISPOSITION_REAPED,
    DISPOSITION_REPORTED,
    DISPOSITION_RESOLVED,
    INTENT_CREATE,
    INTENT_REMOVE,
    INTENT_RUN,
    MAX_RETAINED_JOBS,
    PHASE_ABANDONED,
    PHASE_INTENDED,
    PHASE_SETTLED,
    ArtifactDeclaration,
    Orchestrator,
    Reconciliation,
    exit_code_for_status,
    new_workspace_id,
    result_artifact,
)
from headspace.providers.base import ByteStream, ProviderError
from headspace.providers.fake import FakeProvider, JobPlan

WS = "ws-alpha"
ECHO = ("echo", "hello")


# --- fixtures and helpers -------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_store_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every test at a throwaway store root, never the real ~/.headspace."""
    root = tmp_path / "headspace-home"
    monkeypatch.setenv(HOME_ENV_VAR, str(root))
    return root


@pytest.fixture
def store(isolated_store_home: Path) -> Store:
    return Store()


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider(script={ECHO: JobPlan.succeeding("hello\n")})


@pytest.fixture
def orch(provider: FakeProvider, store: Store) -> Orchestrator:
    return Orchestrator(provider, store)


def lifecycle(store: Store, workspace_id: str = WS) -> str:
    """The lifecycle state as it is actually persisted on disk."""
    return str(store.read_state(workspace_id).state["state"])


def artifacts_on_disk(store: Store, workspace_id: str = WS) -> list[dict[str, Any]]:
    return list(store.read_state(workspace_id).state["artifacts"])


def journal_entries(store: Store, workspace_id: str = WS) -> list[dict[str, Any]]:
    return [entry.entry for entry in store.read_journal(workspace_id)]


def raw_journal(root: Path, workspace_id: str) -> list[dict[str, Any]]:
    """Read the journal file directly, bypassing the Store entirely.

    Used to prove an intent was *durably on disk* at a given instant, without
    borrowing any of the machinery under test.
    """
    path = root / "workspaces" / workspace_id / "journal.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def open_intents(store: Store, workspace_id: str = WS) -> list[dict[str, Any]]:
    """Journalled intents with no settling entry — the crash signature."""
    settled = {
        e["intent_id"] for e in journal_entries(store, workspace_id) if e["phase"] != PHASE_INTENDED
    }
    return [
        e
        for e in journal_entries(store, workspace_id)
        if e["phase"] == PHASE_INTENDED and e["intent_id"] not in settled
    ]


@dataclasses.dataclass(eq=False)
class CrashingStore(Store):
    """A store that dies exactly where a killed CLI would.

    ``KeyboardInterrupt`` rather than a custom exception on purpose: it is a
    BaseException, so every ``except CliError`` handler in the orchestrator
    ignores it and the flow unwinds with no compensating journal entry — which
    is precisely what a ``SIGINT``-killed process leaves behind.
    """

    crash_before_state: str = ""

    def write_state(self, workspace_id: str, state: Mapping[str, Any]) -> Any:
        if state.get("state") == self.crash_before_state:
            raise KeyboardInterrupt(f"simulated kill before writing '{self.crash_before_state}'")
        return super().write_state(workspace_id, state)


class JournalWatchingProvider(FakeProvider):
    """Records what was durably journalled at the instant the engine was called."""

    def __init__(self, root: Path, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._root = root
        self.journal_seen_at_create: list[dict[str, Any]] = []

    def create(self, workspace_id: str, environment: str, policy: Any) -> Any:
        self.journal_seen_at_create = raw_journal(self._root, workspace_id)
        return super().create(workspace_id, environment, policy)


def unenforceable_host() -> CapabilitySnapshot:
    """A host that cannot cap memory — a policy resolved against it fails closed."""
    return CapabilitySnapshot(engine="fake", api_version="1", memory_enforceable=False)


# --- criterion 1: create --------------------------------------------------


def test_create_walks_requested_provisioning_ready_and_persists_engine_facts(
    orch: Orchestrator, store: Store
) -> None:
    package = orch.create(workspace_id=WS)

    assert lifecycle(store) == State.READY.value
    record = store.read_state(WS).state
    assert record["profile"] == DEFAULT_PROFILE
    assert record["provider"] == "fake"
    assert record["descriptor"]["workspace_id"] == WS
    assert record["descriptor"]["environment_digest"].startswith("sha256:")
    assert package.status == STATUS_SUCCESS
    assert package.provenance.workspace_id == WS
    assert package.provenance.profile == DEFAULT_PROFILE
    assert package.provenance.image_digest == record["descriptor"]["environment_digest"]


def test_create_journals_its_intent_before_the_engine_is_touched(
    isolated_store_home: Path, store: Store
) -> None:
    """Order is the whole design: journal first, engine second.

    Proved by asking the *engine* what was already on disk when it was called,
    read straight from the journal file rather than through the Store.
    """
    provider = JournalWatchingProvider(isolated_store_home)
    Orchestrator(provider, store).create(workspace_id=WS)

    entries = [line["entry"] for line in provider.journal_seen_at_create]
    assert [e["intent"] for e in entries] == [INTENT_CREATE]
    assert entries[0]["phase"] == PHASE_INTENDED
    assert entries[0]["detail"]["profile"] == DEFAULT_PROFILE
    # ... and the pairing entry only exists once the engine call came back.
    assert [e["phase"] for e in journal_entries(store)] == [PHASE_INTENDED, PHASE_SETTLED]


def test_create_records_the_recovery_payload_a_reconciler_needs(
    orch: Orchestrator, store: Store
) -> None:
    orch.create(workspace_id=WS)
    intent = journal_entries(store)[0]
    assert set(intent["detail"]) >= {"profile", "environment", "policy", "capabilities"}
    assert intent["provider"] == "fake"


def test_create_refuses_a_policy_the_host_cannot_enforce_before_creating_anything(
    store: Store,
) -> None:
    provider = FakeProvider(capabilities=unenforceable_host())
    orch = Orchestrator(provider, store)

    with pytest.raises(PolicyError) as exc_info:
        orch.create(workspace_id=WS)

    assert exc_info.value.code == 3
    assert not store.exists(WS)
    assert not raw_journal(store.root, WS)
    with pytest.raises(CliError):
        provider.inspect(WS)


def test_create_rejects_a_duplicate_workspace_id(orch: Orchestrator) -> None:
    orch.create(workspace_id=WS)
    with pytest.raises(CliError) as exc_info:
        orch.create(workspace_id=WS)
    assert exc_info.value.code == EXIT_USER_ERROR


def test_create_generates_a_store_safe_workspace_id_when_none_is_given(
    orch: Orchestrator, store: Store
) -> None:
    package = orch.create()
    workspace_id = package.provenance.workspace_id
    assert store.exists(workspace_id)
    assert store.workspace_dir(workspace_id).is_dir()  # id passes the store's own validation


def test_new_workspace_ids_are_unique() -> None:
    assert len({new_workspace_id() for _ in range(64)}) == 64


# --- criterion 1: run (and deviation d4 — `running` is a state we occupy) ---


def test_run_drives_ready_running_ready_and_running_is_observable_mid_job(
    store: Store, isolated_store_home: Path
) -> None:
    """The whole point of deviation d4: `running` is a state a workspace occupies.

    The fake calls ``JobPlan.during`` with the job genuinely in flight, so the
    assertion below reads the persisted state from a *separate* Store instance
    while the engine is mid-command.
    """
    seen: list[str] = []
    provider = FakeProvider(
        script={ECHO: JobPlan.succeeding("hello\n", during=lambda: seen.append(lifecycle(Store())))}
    )
    orch = Orchestrator(provider, store)
    orch.create(workspace_id=WS)

    package = orch.run(WS, ECHO)

    assert seen == [State.RUNNING.value], "the workspace must sit in `running` while a job runs"
    assert lifecycle(store) == State.READY.value
    assert package.status == STATUS_SUCCESS


def test_one_workspace_hosts_many_jobs(orch: Orchestrator, store: Store) -> None:
    """FR-07: a session is reused across jobs — `running -> ready` is what allows it."""
    orch.create(workspace_id=WS)
    first = orch.run(WS, ECHO)
    second = orch.run(WS, ECHO)

    assert first.provenance.job_id != second.provenance.job_id
    assert lifecycle(store) == State.READY.value
    assert len(store.read_state(WS).state["jobs"]) == 2


def test_a_failing_job_leaves_the_workspace_ready(
    orch: Orchestrator, provider: FakeProvider, store: Store
) -> None:
    """A job's status is not the workspace's state — conflating them burnt d4."""
    provider.script_command(("false",), JobPlan.failing(exit_status=3))
    orch.create(workspace_id=WS)

    package = orch.run(WS, ("false",))

    assert package.status == STATUS_FAILURE
    assert exit_code_for_status(package.status) == EXIT_COMPUTATION_FAILED
    assert lifecycle(store) == State.READY.value
    assert any("exit status 3" in finding for finding in package.key_findings)


def test_a_timed_out_job_is_reported_as_a_timeout(
    orch: Orchestrator, provider: FakeProvider, store: Store
) -> None:
    provider.script_command(("sleep",), JobPlan.timing_out())
    orch.create(workspace_id=WS)

    package = orch.run(WS, ("sleep",))

    assert package.status == STATUS_TIMEOUT
    assert exit_code_for_status(package.status) == EXIT_TIMEOUT
    assert lifecycle(store) == State.READY.value


def test_infrastructure_failure_is_never_reported_as_computational_failure(
    orch: Orchestrator, provider: FakeProvider, store: Store
) -> None:
    """NFR-07. The engine breaking must raise, never come back as a failed job."""
    orch.create(workspace_id=WS)
    provider.break_next("run", "the engine died")

    with pytest.raises(ProviderError) as exc_info:
        orch.run(WS, ECHO)

    assert exc_info.value.code == EXIT_INFRASTRUCTURE_FAILURE
    # The workspace is left usable, and the journal says the intent was
    # abandoned — a handled failure is not a crash, so it leaves no orphan.
    assert lifecycle(store) == State.READY.value
    assert not open_intents(store)
    assert journal_entries(store)[-1]["phase"] == PHASE_ABANDONED


def test_run_on_a_workspace_that_is_not_ready_is_refused(orch: Orchestrator, store: Store) -> None:
    orch.create(workspace_id=WS)
    record = store.read_state(WS).state
    record["state"] = State.CANCELLED.value
    store.write_state(WS, record)

    with pytest.raises(CliError) as exc_info:
        orch.run(WS, ECHO)
    assert "'cancelled' -> 'running'" in exc_info.value.message


def test_captured_output_is_bounded_and_the_compression_is_declared(
    store: Store,
) -> None:
    flood = "x" * 4096
    provider = FakeProvider(script={("flood",): JobPlan.flooding(flood)})
    orch = Orchestrator(provider, store)
    orch.create(workspace_id=WS, policy=Policy(budget=ResourceBudget(output_bytes=64)))

    package = orch.run(WS, ("flood",))

    assert package.resource_usage.output_bytes == 4096
    assert any("truncat" in warning for warning in package.warnings)
    assert package.evidence and len(package.evidence[0].excerpt) <= 64


def test_run_declares_the_artifacts_the_job_promises(orch: Orchestrator, store: Store) -> None:
    orch.create(workspace_id=WS)
    orch.run(
        WS, ECHO, declares=[ArtifactDeclaration("plot.png", "the regression plot", "image/png")]
    )

    records = artifacts_on_disk(store)
    assert [r["name"] for r in records] == ["plot.png"]
    assert records[0]["retention"] == RETENTION_DECLARED
    assert records[0]["content_type"] == "image/png"
    assert records[0]["sha256"] is None, "an unexported artifact must not carry a fabricated digest"


# --- criterion 1: inspect -------------------------------------------------


def test_inspect_reports_lifecycle_state_engine_facts_and_pending_work(
    orch: Orchestrator,
) -> None:
    orch.create(workspace_id=WS)
    orch.run(WS, ECHO, declares=[ArtifactDeclaration("out.csv", "the measurements")])

    package = orch.inspect(WS)

    assert package.status == STATUS_SUCCESS
    assert any(State.READY.value in finding for finding in package.key_findings)
    assert any("out.csv" in item for item in package.attention)
    assert package.provenance.workspace_id == WS


def test_inspect_of_an_unknown_workspace_is_a_user_error(orch: Orchestrator) -> None:
    with pytest.raises(CliError) as exc_info:
        orch.inspect("ws-nope")
    assert exc_info.value.code == EXIT_USER_ERROR


# --- criterion 1: export --------------------------------------------------


def test_export_publishes_the_file_and_records_its_digest_and_reference(
    orch: Orchestrator, store: Store, tmp_path: Path
) -> None:
    orch.create(workspace_id=WS)
    orch.run(WS, ECHO, declares=[ArtifactDeclaration("out.csv", "the measurements", "text/csv")])
    destination = tmp_path / "out.csv"

    package = orch.export(WS, "out.csv", io.BytesIO(b"a,b\n1,2\n"), destination)

    assert destination.read_bytes() == b"a,b\n1,2\n"
    record = artifacts_on_disk(store)[0]
    assert record["retention"] == RETENTION_EXPORTED
    assert record["size_bytes"] == 8
    assert len(record["sha256"]) == 64
    assert store.read_state(WS).state["artifact_references"]["out.csv"] == str(destination)

    (entry,) = package.artifacts
    assert entry.name == "out.csv"
    assert entry.media_type == "text/csv"
    assert entry.digest == record["sha256"]
    assert entry.reference == str(destination)


def test_exporting_an_undeclared_artifact_is_refused(orch: Orchestrator, tmp_path: Path) -> None:
    orch.create(workspace_id=WS)
    with pytest.raises(CliError) as exc_info:
        orch.export(WS, "surprise.bin", io.BytesIO(b"x"), tmp_path / "surprise.bin")
    assert exc_info.value.code == EXIT_USER_ERROR
    assert "declared" in exc_info.value.message


def test_a_failed_export_publishes_nothing_and_leaves_no_orphan(
    orch: Orchestrator, store: Store, tmp_path: Path
) -> None:
    orch.create(workspace_id=WS)
    orch.run(WS, ECHO, declares=[ArtifactDeclaration("out.csv", "the measurements")])
    destination = tmp_path / "out.csv"

    with pytest.raises(CliError):
        orch.export(WS, "out.csv", io.BytesIO(b"payload"), destination, expected_sha256="0" * 64)

    assert not destination.exists()
    assert artifacts_on_disk(store)[0]["retention"] == RETENTION_DECLARED
    assert not open_intents(store), "a handled failure is not a crash"


# --- export pulls the bytes through the seam (issue #3 / deviation d6) ----


WRITER = ("write", "out.csv")
WRITTEN = b"a,b\n" + b"1,2\n" * 4096


def _writing_provider(cls: type[FakeProvider] = FakeProvider) -> FakeProvider:
    """A fake whose scripted job really leaves a file in the workspace."""
    provider = cls(script={ECHO: JobPlan.succeeding("hello\n")})
    provider.script_command(WRITER, JobPlan.succeeding(writes={"out.csv": WRITTEN}))
    return provider


def _prepared(store: Store, provider: FakeProvider | None = None) -> Orchestrator:
    """A workspace whose job has run and declared the artifact it wrote."""
    orch = Orchestrator(provider if provider is not None else _writing_provider(), store)
    orch.create(workspace_id=WS)
    orch.run(WS, WRITER, declares=[ArtifactDeclaration("out.csv", "the measurements", "text/csv")])
    return orch


def test_export_pulls_the_bytes_from_the_provider_when_no_source_is_given(
    store: Store, tmp_path: Path
) -> None:
    """The gap issue #3 named: the CLI has no bytes to hand over, only a name."""
    orch = _prepared(store)
    destination = tmp_path / "out.csv"

    package = orch.export(WS, "out.csv", destination=destination)

    assert destination.read_bytes() == WRITTEN
    record = artifacts_on_disk(store)[0]
    assert record["retention"] == RETENTION_EXPORTED
    assert record["size_bytes"] == len(WRITTEN)
    assert record["sha256"] == hashlib.sha256(WRITTEN).hexdigest()
    (entry,) = package.artifacts
    assert entry.digest == hashlib.sha256(WRITTEN).hexdigest()
    assert entry.reference == str(destination)


def test_a_provider_pulled_export_still_verifies_an_expected_digest(
    store: Store, tmp_path: Path
) -> None:
    """The durability boundary did not move: the digest is still the gate."""
    orch = _prepared(store)
    destination = tmp_path / "out.csv"

    orch.export(WS, "out.csv", destination=destination, expected_sha256=hashlib.sha256(WRITTEN).hexdigest())
    assert destination.read_bytes() == WRITTEN

    with pytest.raises(CliError):
        orch.export(WS, "out.csv", destination=tmp_path / "again.csv", expected_sha256="0" * 64)
    assert not (tmp_path / "again.csv").exists()


def test_export_reads_the_workspace_path_when_it_differs_from_the_artifact_name(
    store: Store, tmp_path: Path
) -> None:
    provider = _writing_provider()
    orch = Orchestrator(provider, store)
    orch.create(workspace_id=WS)
    orch.run(WS, ECHO, declares=[ArtifactDeclaration("report", "the measurements")])
    provider.write_file(WS, "results/final.csv", WRITTEN)

    orch.export(WS, "report", destination=tmp_path / "report", path="results/final.csv")

    assert (tmp_path / "report").read_bytes() == WRITTEN


def test_a_caller_that_already_holds_the_bytes_never_touches_the_provider(
    store: Store, tmp_path: Path
) -> None:
    """The existing parameter keeps working, and keeps short-circuiting the read."""

    class Unreadable(FakeProvider):
        def read(self, workspace_id: str, path: str, *, chunk_size: int = 1) -> Any:
            raise AssertionError("export must not read a workspace it was handed bytes for")

    orch = _prepared(store, provider=_writing_provider(Unreadable))
    destination = tmp_path / "out.csv"

    orch.export(WS, "out.csv", io.BytesIO(b"handed over"), destination)

    assert destination.read_bytes() == b"handed over"


def test_export_consumes_the_provider_stream_in_bounded_chunks(
    store: Store, tmp_path: Path
) -> None:
    """Streaming end to end: the export writes what the provider yields, chunk by chunk.

    Observed, not asserted about: the stream records what it actually handed
    over, so a provider (or an export) that materialised the artifact would show
    a single chunk the size of the whole file.
    """
    handed: list[int] = []

    class RecordingProvider(FakeProvider):
        def read(self, workspace_id: str, path: str, *, chunk_size: int = 1 << 20) -> ByteStream:
            inner = super().read(workspace_id, path, chunk_size=1024)

            def recorded() -> Any:
                for chunk in inner:
                    handed.append(len(chunk))
                    yield chunk

            return ByteStream(recorded(), release=inner.close)

    orch = _prepared(store, provider=_writing_provider(RecordingProvider))
    orch.export(WS, "out.csv", destination=tmp_path / "out.csv")

    assert len(handed) > 1, "the artifact was handed over as one object, not streamed"
    assert max(handed) <= 1024
    assert sum(handed) == len(WRITTEN)


def test_export_of_a_path_the_workspace_does_not_hold_is_a_user_error(
    store: Store, tmp_path: Path
) -> None:
    """A missing file is the caller's mistake — exit 1, and nothing published."""
    orch = _prepared(store)
    destination = tmp_path / "gone.csv"

    with pytest.raises(CliError) as exc_info:
        orch.export(WS, "out.csv", destination=destination, path="never-written.csv")

    assert exc_info.value.code == EXIT_USER_ERROR
    assert not isinstance(exc_info.value, ProviderError)
    assert not destination.exists()
    assert artifacts_on_disk(store)[0]["retention"] == RETENTION_DECLARED
    assert not open_intents(store), "a handled failure is not a crash"


def test_export_reports_a_broken_engine_as_infrastructure_failure(
    store: Store, tmp_path: Path
) -> None:
    """NFR-07 on the export path: retry the fetch, do not conclude the work is lost."""
    provider = _writing_provider()
    orch = _prepared(store, provider=provider)
    destination = tmp_path / "out.csv"
    provider.break_next("read")

    with pytest.raises(ProviderError) as exc_info:
        orch.export(WS, "out.csv", destination=destination)

    assert exc_info.value.code == EXIT_INFRASTRUCTURE_FAILURE
    assert not destination.exists()
    assert artifacts_on_disk(store)[0]["retention"] == RETENTION_DECLARED
    assert not open_intents(store)


def test_a_failed_export_releases_the_stream_it_pulled(store: Store, tmp_path: Path) -> None:
    """A digest that does not verify must not also strand the backend's resources."""
    released: list[int] = []

    class TrackingProvider(FakeProvider):
        def read(self, workspace_id: str, path: str, *, chunk_size: int = 1 << 20) -> ByteStream:
            inner = super().read(workspace_id, path, chunk_size=chunk_size)
            return ByteStream(inner, release=lambda: released.append(1))

    orch = _prepared(store, provider=_writing_provider(TrackingProvider))
    with pytest.raises(CliError):
        orch.export(WS, "out.csv", destination=tmp_path / "out.csv", expected_sha256="0" * 64)

    assert released == [1]


def test_export_without_a_destination_is_refused(store: Store, tmp_path: Path) -> None:
    orch = _prepared(store)
    with pytest.raises(CliError) as exc_info:
        orch.export(WS, "out.csv")
    assert exc_info.value.code == EXIT_USER_ERROR
    assert exc_info.value.remediation


# --- criterion 3: the destroy guard ---------------------------------------


def test_destroy_refuses_unexported_declared_artifacts_and_removes_nothing(
    orch: Orchestrator, provider: FakeProvider, store: Store, tmp_path: Path
) -> None:
    """The one unrecoverable mistake this product can make, refused."""
    orch.create(workspace_id=WS)
    orch.run(
        WS,
        ECHO,
        declares=[
            ArtifactDeclaration("kept.csv", "already safe"),
            ArtifactDeclaration("lost.csv", "never exported"),
        ],
    )
    safe = tmp_path / "kept.csv"
    orch.export(WS, "kept.csv", io.BytesIO(b"safe"), safe)

    with pytest.raises(CliError) as exc_info:
        orch.destroy(WS)

    err = exc_info.value
    assert err.code == EXIT_USER_ERROR
    assert "lost.csv" in err.message
    assert "kept.csv" not in err.message
    assert "force" in err.remediation

    # "removes nothing", asserted positively rather than by absence of an effect.
    assert store.exists(WS)
    assert lifecycle(store) == State.READY.value
    assert provider.inspect(WS).workspace_id == WS
    retention = {r["name"]: r["retention"] for r in artifacts_on_disk(store)}
    assert retention == {"kept.csv": RETENTION_EXPORTED, "lost.csv": RETENTION_DECLARED}
    assert safe.read_bytes() == b"safe"
    assert not [e for e in journal_entries(store) if e["intent"] == INTENT_REMOVE]


def test_forced_destroy_proceeds_and_lists_the_discarded_artifacts(
    orch: Orchestrator, provider: FakeProvider, store: Store
) -> None:
    orch.create(workspace_id=WS)
    orch.run(WS, ECHO, declares=[ArtifactDeclaration("lost.csv", "never exported")])

    package = orch.destroy(WS, force=True)

    assert not store.exists(WS)
    with pytest.raises(CliError):
        provider.inspect(WS)
    discarded = [item for item in package.attention if "lost.csv" in item]
    assert discarded, "a forced destroy must name every artifact it discarded"
    assert "digest" in discarded[0]
    assert any("force" in warning for warning in package.warnings)


def test_destroy_after_everything_is_exported_reports_the_disposition(
    orch: Orchestrator, provider: FakeProvider, store: Store, tmp_path: Path
) -> None:
    orch.create(workspace_id=WS)
    orch.run(WS, ECHO, declares=[ArtifactDeclaration("out.csv", "the measurements")])
    orch.export(WS, "out.csv", io.BytesIO(b"payload"), tmp_path / "out.csv")

    package = orch.destroy(WS)

    assert not store.exists(WS)
    with pytest.raises(CliError):
        provider.inspect(WS)
    findings = " ".join(package.key_findings)
    assert "removed" in findings and "runtime" in findings and "storage" in findings
    assert "retained" in findings and "unverified" in findings
    assert (tmp_path / "out.csv").read_bytes() == b"payload", "an export outlives its workspace"


def test_destroy_during_an_active_job_refuses_in_the_lifecycle_tables_own_words(
    store: Store,
) -> None:
    """The documented path (c24): refuse, never tear down work in flight.

    The refusal is raised by a *second* Orchestrator over a *second* Store —
    the in-process stand-in for a concurrent CLI invocation — reading the
    persisted `running` state that deviation d4 made real.
    """
    refusals: list[CliError] = []
    provider = FakeProvider()

    def concurrent_destroy() -> None:
        # A second orchestrator over a second Store, against the same engine.
        rival = Orchestrator(provider, Store())
        try:
            rival.destroy(WS)
        except CliError as err:
            refusals.append(err)

    provider.script_command(ECHO, JobPlan.succeeding("hello\n", during=concurrent_destroy))
    orch = Orchestrator(provider, store)
    orch.create(workspace_id=WS)

    package = orch.run(WS, ECHO)

    assert len(refusals) == 1
    assert "'running' -> 'destroyed'" in refusals[0].message
    assert refusals[0].remediation
    # The job was never disturbed and the workspace survived intact.
    assert package.status == STATUS_SUCCESS
    assert lifecycle(store) == State.READY.value
    assert store.exists(WS)
    assert not [e for e in journal_entries(store) if e["intent"] == INTENT_REMOVE]


def test_destroy_of_an_unknown_workspace_is_a_user_error(orch: Orchestrator) -> None:
    with pytest.raises(CliError) as exc_info:
        orch.destroy("ws-nope")
    assert exc_info.value.code == EXIT_USER_ERROR


def test_destroy_walks_only_edges_the_lifecycle_table_allows(orch: Orchestrator) -> None:
    """`ready -> destroyed` is not an edge; destroy walks the path that is.

    Reported rather than merely performed: c2 asks for observable transitions.
    """
    orch.create(workspace_id=WS)
    package = orch.destroy(WS)
    assert f"lifecycle path: {State.CANCELLED.value} -> {State.DESTROYED.value}" in (
        package.key_findings
    )


# --- criterion 2: a real crash, and the reconciliation that follows -------


def crash_after_create(root: Path, provider: FakeProvider) -> None:
    """Create a workspace, dying between the engine call and the state write."""
    crashing = Orchestrator(provider, CrashingStore(root, crash_before_state=State.READY.value))
    with pytest.raises(KeyboardInterrupt):
        crashing.create(workspace_id=WS)


def test_a_crash_between_the_engine_call_and_the_state_write_leaves_an_orphan(
    provider: FakeProvider, store: Store
) -> None:
    """The situation the recovery path exists for, asserted on disk."""
    crash_after_create(store.root, provider)

    # The engine holds a workspace the store does not know is ready...
    assert provider.inspect(WS).state is State.READY
    assert lifecycle(store) == State.PROVISIONING.value
    # ...and the pair that makes recovery possible: a journalled, unsettled intent.
    (intent,) = open_intents(store)
    assert intent["intent"] == INTENT_CREATE
    assert intent["detail"]["profile"] == DEFAULT_PROFILE


def test_the_next_invocation_adopts_the_orphan_and_reports_it_in_attention(
    provider: FakeProvider, store: Store
) -> None:
    crash_after_create(store.root, provider)

    # A fresh orchestrator over a fresh store: nothing survives from the
    # crashed invocation but the store root on disk and the live engine.
    revived = Orchestrator(provider, Store())
    package = revived.inspect(WS)

    assert lifecycle(store) == State.READY.value
    assert store.read_state(WS).state["descriptor"]["workspace_id"] == WS
    reported = [item for item in package.attention if WS in item]
    assert reported, "silent cleanup is as bad as a leak"
    assert DISPOSITION_ADOPTED in reported[0]
    assert not open_intents(store), "reconciliation settles the intent it acted on"


def test_reconcile_returns_the_disposition_it_took(provider: FakeProvider, store: Store) -> None:
    crash_after_create(store.root, provider)

    (disposition,) = Orchestrator(provider, Store()).reconcile()

    assert isinstance(disposition, Reconciliation)
    assert disposition.workspace_id == WS
    assert disposition.intent == INTENT_CREATE
    assert disposition.disposition == DISPOSITION_ADOPTED
    assert disposition.detail
    assert WS in disposition.summary()


def test_reconciliation_is_idempotent(provider: FakeProvider, store: Store) -> None:
    crash_after_create(store.root, provider)
    Orchestrator(provider, Store()).reconcile()

    assert Orchestrator(provider, Store()).reconcile() == []
    assert lifecycle(store) == State.READY.value


def test_a_crash_before_the_engine_call_is_reaped(provider: FakeProvider, store: Store) -> None:
    """Journalled, but the engine never got the call: nothing exists to adopt."""
    crashing = Orchestrator(
        provider, CrashingStore(store.root, crash_before_state=State.REQUESTED.value)
    )
    with pytest.raises(KeyboardInterrupt):
        crashing.create(workspace_id=WS)

    assert raw_journal(store.root, WS), "the intent is journalled before anything else"
    assert not store.exists(WS)

    (disposition,) = Orchestrator(provider, Store()).reconcile()

    assert disposition.disposition == DISPOSITION_REAPED
    assert not store.workspace_dir(WS).exists()


def test_a_crash_during_a_job_is_resolved_back_to_ready(
    provider: FakeProvider, store: Store
) -> None:
    """Deviation d4's second dividend: `running -> ready` is also the recovery edge."""
    Orchestrator(provider, store).create(workspace_id=WS)
    crashing = Orchestrator(
        provider, CrashingStore(store.root, crash_before_state=State.READY.value)
    )
    with pytest.raises(KeyboardInterrupt):
        crashing.run(WS, ECHO)

    assert lifecycle(store) == State.RUNNING.value
    assert open_intents(store)[0]["intent"] == INTENT_RUN

    revived = Orchestrator(provider, Store())
    package = revived.inspect(WS)

    assert lifecycle(store) == State.READY.value
    assert any(DISPOSITION_RESOLVED in item for item in package.attention)


def test_reconciliation_quarantines_a_vanished_engine_object_holding_declared_work(
    provider: FakeProvider, store: Store
) -> None:
    """Never discard a record of work no one can recover — say what was lost."""
    orch = Orchestrator(provider, store)
    orch.create(workspace_id=WS)
    orch.run(WS, ECHO, declares=[ArtifactDeclaration("lost.csv", "never exported")])
    # The engine object disappears behind headspace's back (an operator's
    # `docker rm`), and the CLI dies before it could record anything.
    crashing = Orchestrator(
        provider, CrashingStore(store.root, crash_before_state=State.READY.value)
    )
    with pytest.raises(KeyboardInterrupt):
        crashing.run(WS, ECHO)
    provider.remove(WS)

    (disposition,) = Orchestrator(provider, Store()).reconcile()

    assert disposition.disposition == DISPOSITION_QUARANTINED
    assert "lost.csv" in disposition.detail
    assert store.exists(WS), "the record of unrecoverable work is kept, not deleted"
    assert lifecycle(store) == State.CANCELLED.value


def test_reconciliation_reaps_a_vanished_engine_object_with_nothing_at_stake(
    provider: FakeProvider, store: Store
) -> None:
    crash_after_create(store.root, provider)
    provider.remove(WS)

    (disposition,) = Orchestrator(provider, Store()).reconcile()

    assert disposition.disposition == DISPOSITION_REAPED
    assert not store.workspace_dir(WS).exists()


def test_reconciliation_reports_rather_than_fails_when_the_engine_is_unreachable(
    provider: FakeProvider, store: Store
) -> None:
    """An unverifiable orphan is reported; it must not fail the caller's verb."""
    crash_after_create(store.root, provider)
    provider.break_next("inspect", "engine unreachable")

    revived = Orchestrator(provider, Store())
    dispositions = revived.reconcile()

    assert [d.disposition for d in dispositions] == [DISPOSITION_REPORTED]
    assert "unreachable" in dispositions[0].detail
    assert (
        lifecycle(store) == State.PROVISIONING.value
    ), "nothing was changed on an unverified guess"
    assert open_intents(store), "an unverified intent stays open"


def test_reconciliation_leaves_another_backends_workspace_alone(
    provider: FakeProvider, store: Store
) -> None:
    crash_after_create(store.root, provider)

    other = Orchestrator(FakeProvider(name="other-backend"), Store())
    dispositions = other.reconcile()

    assert [d.disposition for d in dispositions] == [DISPOSITION_REPORTED]
    assert "other-backend" in dispositions[0].detail or "fake" in dispositions[0].detail
    assert lifecycle(store) == State.PROVISIONING.value


def test_reconciliation_never_blocks_on_a_workspace_another_process_holds(
    provider: FakeProvider, store: Store
) -> None:
    """A hung CLI is worse than a deferred orphan, so reconciliation never waits."""
    crash_after_create(store.root, provider)

    holder = Store()  # a distinct open file description == a distinct process
    with holder.lock(WS):
        dispositions = Orchestrator(provider, Store()).reconcile()

    assert [d.disposition for d in dispositions] == [DISPOSITION_REPORTED]
    assert "busy" in dispositions[0].detail


def test_a_verb_reports_reconciliation_once_not_on_every_call(
    provider: FakeProvider, store: Store
) -> None:
    crash_after_create(store.root, provider)
    revived = Orchestrator(provider, Store())

    first = revived.inspect(WS)
    second = revived.inspect(WS)

    assert any(WS in item for item in first.attention)
    assert not any(DISPOSITION_ADOPTED in item for item in second.attention)


def test_an_unreadable_workspace_is_reported_not_raised_during_reconciliation(
    store: Store, provider: FakeProvider
) -> None:
    """One corrupt record must not take the whole invocation down with it."""
    directory = store.workspace_dir("ws-corrupt")
    directory.mkdir(parents=True)
    (directory / "journal.jsonl").write_text("{not json}\n", encoding="utf-8")

    dispositions = Orchestrator(provider, Store()).reconcile()

    assert [d.disposition for d in dispositions] == [DISPOSITION_REPORTED]
    assert "ws-corrupt" == dispositions[0].workspace_id


# --- deviation d3: the artifact mapping -----------------------------------


def test_the_artifact_mapping_covers_every_field_on_both_sides() -> None:
    """The mapping is explicit and total, so a new field on either side breaks here."""
    record_fields = {f.name for f in dataclasses.fields(ArtifactRecord)}
    wire_fields = {f.name for f in dataclasses.fields(Artifact)}

    assert set(ARTIFACT_FIELD_MAP) == record_fields
    carried = {target for target in ARTIFACT_FIELD_MAP.values() if target is not None}
    assert carried <= wire_fields
    # `retention` is storage-side only; `reference` has no storage-side source.
    assert ARTIFACT_FIELD_MAP["retention"] is None
    assert wire_fields - carried == {"reference"}


def test_result_artifact_translates_the_two_names_that_diverge() -> None:
    record = ArtifactRecord(
        name="plot.png",
        content_type="image/png",
        purpose="the regression plot",
        retention=RETENTION_EXPORTED,
        size_bytes=11,
        sha256="a" * 64,
    )

    wire = result_artifact(record, reference="/exports/plot.png")

    assert wire == Artifact(
        name="plot.png",
        purpose="the regression plot",
        media_type="image/png",
        digest="a" * 64,
        size_bytes=11,
        reference="/exports/plot.png",
    )
    assert not hasattr(wire, "retention")


def test_result_artifact_refuses_a_record_that_was_never_exported() -> None:
    """A wire artifact promises retrievability; a declared one cannot keep it."""
    record = ArtifactRecord(
        name="lost.csv",
        content_type="text/csv",
        purpose="never exported",
        retention=RETENTION_DECLARED,
    )
    with pytest.raises(CliError) as exc_info:
        result_artifact(record)
    assert exc_info.value.code == EXIT_USER_ERROR
    assert "lost.csv" in exc_info.value.message


def test_only_exported_artifacts_reach_the_result_packages_artifact_section(
    orch: Orchestrator, tmp_path: Path
) -> None:
    orch.create(workspace_id=WS)
    orch.run(
        WS,
        ECHO,
        declares=[
            ArtifactDeclaration("kept.csv", "already safe"),
            ArtifactDeclaration("lost.csv", "never exported"),
        ],
    )
    orch.export(WS, "kept.csv", io.BytesIO(b"safe"), tmp_path / "kept.csv")

    package = orch.inspect(WS)

    assert [entry.name for entry in package.artifacts] == ["kept.csv"]
    assert any("lost.csv" in item for item in package.attention)


# --- the exit-code bridge the CLI verbs need ------------------------------


@pytest.mark.parametrize(
    "status,code",
    [
        (STATUS_SUCCESS, 0),
        (STATUS_FAILURE, EXIT_COMPUTATION_FAILED),
        (STATUS_TIMEOUT, EXIT_TIMEOUT),
    ],
)
def test_exit_code_for_status_uses_the_documented_taxonomy(status: str, code: int) -> None:
    assert exit_code_for_status(status) == code


def test_exit_code_for_an_unknown_status_is_refused() -> None:
    with pytest.raises(CliError):
        exit_code_for_status("invented")


# --- layering -------------------------------------------------------------


def test_orchestration_imports_the_seam_only_and_never_a_backend() -> None:
    """This layer must work against any Provider, so it may not name one."""
    tree = ast.parse(Path(workspace_module.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    assert not {name for name in imported if name == "docker" or name.startswith("docker.")}
    provider_imports = {name for name in imported if name.startswith("headspace.providers")}
    assert provider_imports == {"headspace.providers.base"}


def test_the_workspace_lock_is_taken_for_every_state_mutation(
    orch: Orchestrator, store: Store
) -> None:
    """c24: interleaved verbs serialise; a busy workspace refuses rather than corrupts."""
    orch.create(workspace_id=WS)
    holder = Store()
    with holder.lock(WS):
        with pytest.raises(CliError) as exc_info:
            Orchestrator(orch_provider(orch), Store()).destroy(WS)
    assert exc_info.value.code == EXIT_ENV_ERROR
    assert store.exists(WS)


def orch_provider(orch: Orchestrator) -> FakeProvider:
    """The provider an orchestrator was built over (tests only)."""
    provider = orch.provider
    assert isinstance(provider, FakeProvider)
    return provider


def test_a_network_enabled_policy_is_recorded_as_the_engine_configured_it(
    orch: Orchestrator, store: Store
) -> None:
    orch.create(workspace_id=WS, policy=Policy(network=NetworkPosture.ENABLED))
    assert store.read_state(WS).state["descriptor"]["network_enabled"] is True
    package = orch.inspect(WS)
    assert any("network" in finding for finding in package.key_findings)


# --- failure paths a reviewer should see exercised ------------------------


def test_an_engine_that_fails_during_create_leaves_a_destroyable_failed_record(
    orch: Orchestrator, provider: FakeProvider, store: Store
) -> None:
    """The record is kept because the engine may hold a half-built workspace."""
    provider.break_next("create", "the engine died mid-create")

    with pytest.raises(ProviderError):
        orch.create(workspace_id=WS)

    assert lifecycle(store) == State.FAILED.value
    assert not open_intents(store), "a handled failure is not a crash"
    assert journal_entries(store)[-1]["phase"] == PHASE_ABANDONED
    # ...and it can be cleaned up deliberately: failed -> destroyed is legal.
    package = Orchestrator(provider, Store()).destroy(WS)
    assert not store.exists(WS)
    assert any("unverified" in finding for finding in package.key_findings)


def test_an_unreachable_engine_fails_inspect_as_infrastructure_not_as_a_result(
    orch: Orchestrator, provider: FakeProvider
) -> None:
    """NFR-07 again, on a read path: a broken engine is never a quiet `None`."""
    orch.create(workspace_id=WS)
    provider.break_next("inspect", "the engine is unreachable")

    with pytest.raises(ProviderError) as exc_info:
        orch.inspect(WS)
    assert exc_info.value.code == EXIT_INFRASTRUCTURE_FAILURE


def test_inspecting_a_quarantined_workspace_reports_stale_facts_as_stale(
    orch: Orchestrator, provider: FakeProvider, store: Store
) -> None:
    orch.create(workspace_id=WS)
    orch.run(WS, ECHO, declares=[ArtifactDeclaration("lost.csv", "never exported")])
    provider.remove(WS)

    package = Orchestrator(provider, Store()).inspect(WS)

    assert any("no longer holds" in warning for warning in package.warnings)
    assert package.provenance.image_digest, "the last observed facts are still reported"
    assert store.exists(WS)


def test_a_dead_session_whose_engine_object_vanished_is_reaped_through_cancelled(
    provider: FakeProvider, store: Store
) -> None:
    """The `running -> cancelled -> destroyed` walk the lifecycle table prescribes."""
    Orchestrator(provider, store).create(workspace_id=WS)
    crashing = Orchestrator(
        provider, CrashingStore(store.root, crash_before_state=State.READY.value)
    )
    with pytest.raises(KeyboardInterrupt):
        crashing.run(WS, ECHO)
    assert lifecycle(store) == State.RUNNING.value
    provider.remove(WS)

    (disposition,) = Orchestrator(provider, Store()).reconcile()

    assert disposition.disposition == DISPOSITION_REAPED
    assert not store.workspace_dir(WS).exists()


def test_a_lost_state_record_is_rebuilt_from_the_journalled_intent(
    provider: FakeProvider, store: Store
) -> None:
    """What the intent's detail payload is *for*: facts no engine can hand back."""
    crash_after_create(store.root, provider)
    (store.workspace_dir(WS) / "state.json").unlink()

    (disposition,) = Orchestrator(provider, Store()).reconcile()

    assert disposition.disposition == DISPOSITION_ADOPTED
    rebuilt = store.read_state(WS).state
    assert rebuilt["profile"] == DEFAULT_PROFILE
    assert rebuilt["policy"]["network"] == NetworkPosture.DISABLED.value
    assert rebuilt["capabilities"]["engine"] == "fake"
    assert rebuilt["state"] == State.READY.value


def test_an_interrupted_export_is_reported_and_the_artifact_stays_guarded(
    provider: FakeProvider, store: Store, tmp_path: Path
) -> None:
    orch = Orchestrator(provider, store)
    orch.create(workspace_id=WS)
    orch.run(WS, ECHO, declares=[ArtifactDeclaration("out.csv", "the measurements")])
    destination = tmp_path / "out.csv"

    # The publish is atomic and lands; the ledger write is what gets killed.
    crashing = Orchestrator(
        provider, CrashingStore(store.root, crash_before_state=State.READY.value)
    )
    with pytest.raises(KeyboardInterrupt):
        crashing.export(WS, "out.csv", io.BytesIO(b"payload"), destination)

    assert destination.read_bytes() == b"payload"
    assert artifacts_on_disk(store)[0]["retention"] == RETENTION_DECLARED

    (disposition,) = Orchestrator(provider, Store()).reconcile()

    assert disposition.disposition == DISPOSITION_REPORTED
    assert "out.csv" in disposition.detail and "re-export" in disposition.detail
    assert not open_intents(store), "the caller was told once; it is not re-reported forever"
    # The artifact is still guarded, which is the fail-safe direction.
    with pytest.raises(CliError):
        Orchestrator(provider, Store()).destroy(WS)


def test_a_workspace_with_an_unreadable_state_record_is_reported_not_raised(
    provider: FakeProvider, store: Store
) -> None:
    crash_after_create(store.root, provider)
    (store.workspace_dir(WS) / "state.json").write_text("{}", encoding="utf-8")

    (disposition,) = Orchestrator(provider, Store()).reconcile()

    assert disposition.disposition == DISPOSITION_REPORTED
    assert "unreadable" in disposition.detail


def test_a_record_headspace_did_not_write_is_refused_rather_than_interpreted(
    orch: Orchestrator, store: Store
) -> None:
    store.write_state(WS, {"workspace_id": WS, "something": "else"})
    with pytest.raises(CliError) as exc_info:
        orch.inspect(WS)
    assert exc_info.value.code == EXIT_ENV_ERROR
    assert "lifecycle state" in exc_info.value.message


def test_a_malformed_stored_policy_fails_closed_rather_than_running_a_job(
    orch: Orchestrator, store: Store
) -> None:
    orch.create(workspace_id=WS)
    record = store.read_state(WS).state
    record["policy"] = {"network": "sideways"}
    store.write_state(WS, record)

    with pytest.raises(CliError) as exc_info:
        orch.run(WS, ECHO)
    assert exc_info.value.code == EXIT_ENV_ERROR
    assert "policy" in exc_info.value.message


def test_a_malformed_stored_capability_snapshot_fails_closed(
    orch: Orchestrator, store: Store
) -> None:
    orch.create(workspace_id=WS)
    record = store.read_state(WS).state
    record["capabilities"] = {"engine": "fake", "invented_field": True}
    store.write_state(WS, record)

    with pytest.raises(CliError) as exc_info:
        orch.inspect(WS)
    assert exc_info.value.code == EXIT_ENV_ERROR
    assert "capability snapshot" in exc_info.value.message


# --- session bookkeeping --------------------------------------------------


def test_declare_registers_an_output_without_running_anything(
    orch: Orchestrator, store: Store
) -> None:
    orch.create(workspace_id=WS)
    record = orch.declare(WS, "out.csv", purpose="the measurements", content_type="text/csv")

    assert record.retention == RETENTION_DECLARED
    assert [entry["name"] for entry in artifacts_on_disk(store)] == ["out.csv"]


def test_storage_measured_by_a_job_updates_the_workspaces_engine_facts(
    store: Store,
) -> None:
    provider = FakeProvider(script={ECHO: JobPlan.succeeding("hi\n", storage_bytes=4096)})
    orch = Orchestrator(provider, store)
    orch.create(workspace_id=WS)

    orch.run(WS, ECHO)

    assert store.read_state(WS).state["descriptor"]["storage_bytes"] == 4096
    assert orch.inspect(WS).resource_usage.storage_bytes == 4096


def test_job_history_is_bounded_and_says_so(orch: Orchestrator, store: Store) -> None:
    """Compression may hide volume, never its own existence."""
    orch.create(workspace_id=WS)
    record = store.read_state(WS).state
    record["jobs"] = [{"job_id": f"job-{n}", "usage": {}} for n in range(MAX_RETAINED_JOBS)]
    store.write_state(WS, record)

    package = orch.run(WS, ECHO)

    jobs = store.read_state(WS).state["jobs"]
    assert len(jobs) == MAX_RETAINED_JOBS
    assert store.read_state(WS).state["jobs_dropped"] == 1
    assert any("dropped" in warning for warning in package.warnings)
    assert any("dropped" in warning for warning in orch.inspect(WS).warnings)


def test_a_reconciliation_is_serialisable_for_json_output() -> None:
    disposition = Reconciliation(WS, INTENT_CREATE, DISPOSITION_ADOPTED, "because")
    assert disposition.to_dict() == {
        "workspace_id": WS,
        "intent": INTENT_CREATE,
        "disposition": DISPOSITION_ADOPTED,
        "detail": "because",
    }


def test_an_orchestrator_exposes_the_pieces_it_was_built_over(
    orch: Orchestrator, provider: FakeProvider, store: Store
) -> None:
    assert orch.provider is provider
    assert orch.store is store
