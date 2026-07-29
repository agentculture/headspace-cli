"""Tests for the orchestrator's copy-in verb: journal, lock, inputs ledger, reconciliation.

Written first, against the four acceptance criteria this task was given:

1. the journal shows the put intent opened *before* any engine call and settled
   or abandoned on every exit path, and every guard is evaluated *after* the
   lock is acquired (never before it — issue #11's open TOCTOU in ``destroy``
   is the pattern this verb must not reproduce);
2. a put issued while a job is in flight refuses immediately, naming the job and
   pointing at ``stop``, rather than waiting up to the wall-clock budget;
3. copied-in files land in an inputs ledger distinct from the artifact
   inventory — the destroy guard never sees them — and every recorded surface
   carries the file's path and sha256 and not one byte of its contents;
4. a copy-in killed mid-flight is settled by the next verb's reconciliation, its
   staging residue reaped, and the workspace left usable.

Three things about how these are proved:

**The kill is real, not mocked.** Two shapes, because a copy-in can die in two
materially different places. :class:`CopyInProvider` with ``kill_after_staging``
raises ``KeyboardInterrupt`` from *inside* the engine call, after it has staged
bytes — the case that leaves residue. :class:`LedgerCrashingStore` raises the
same ``BaseException`` from the state write that follows a completed engine
call — the case where the bytes landed but the ledger never did. Nothing catches
either: the orchestrator handles ``CliError`` only, so no ``abandoned`` entry is
written and the flow unwinds exactly as a ``SIGINT``-killed CLI would leave it.

**The contents marker is grepped, not reasoned about.** The file copied in
carries a distinctive string in its *bytes*. Every surface headspace composes —
``outcome_summary``, ``key_findings``, ``provenance.inputs``, ``journal.jsonl``
and ``state.json``, read as raw text off disk — is searched for it, and searched
for the path and digest that are supposed to be there instead.

**The provider is a stub defined here, not the shipped fake.** The seam's
inbound verb lands in :mod:`headspace.providers.fake` and
:mod:`headspace.providers.docker` in sibling tasks; this module's subject is the
orchestration around that verb, so it drives a local double implementing
``write`` with the pinned signature. That also buys the two behaviours no real
backend would offer on request: a kill in the middle of the engine call, and an
inspectable staging area.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import pytest

from headspace.cli._errors import (
    EXIT_ENV_ERROR,
    EXIT_POLICY_DENIED,
    EXIT_USER_ERROR,
    CliError,
)
from headspace.core.artifacts import ByteSource
from headspace.core.inputs import expand_input
from headspace.core.policy import Policy, ResourceBudget
from headspace.core.states import State
from headspace.core.store import HOME_ENV_VAR, Store
from headspace.core.workspace import (
    DISPOSITION_REPORTED,
    DISPOSITION_RESOLVED,
    INTENT_PUT,
    INTENT_RUN,
    INTENTS,
    PHASE_ABANDONED,
    PHASE_INTENDED,
    PHASE_RECONCILED,
    PHASE_SETTLED,
    STAGING_REAPER,
    ArtifactDeclaration,
    Orchestrator,
    _open_input,
)
from headspace.providers.base import ProviderError
from headspace.providers.fake import FakeProvider, JobPlan

WS = "ws-put"
ECHO = ("echo", "hello")
MARKER = "SUPER-SECRET-MARKER-9f3a"
PAYLOAD = f"key = {MARKER}\n".encode()
PAYLOAD_DIGEST = hashlib.sha256(PAYLOAD).hexdigest()


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
def payload(tmp_path: Path) -> Path:
    """One host file whose *contents* carry a marker no surface may repeat."""
    path = tmp_path / "inputs" / "config.env"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(PAYLOAD)
    return path


def lifecycle(store: Store, workspace_id: str = WS) -> str:
    return str(store.read_state(workspace_id).state["state"])


def state_record(store: Store, workspace_id: str = WS) -> dict[str, Any]:
    return dict(store.read_state(workspace_id).state)


def ledger(store: Store, workspace_id: str = WS) -> list[dict[str, Any]]:
    """The inputs ledger as it is actually persisted on disk."""
    return list(state_record(store, workspace_id).get("inputs", []))


def journal_entries(store: Store, workspace_id: str = WS) -> list[dict[str, Any]]:
    return [entry.entry for entry in store.read_journal(workspace_id)]


def raw_journal(root: Path, workspace_id: str = WS) -> list[dict[str, Any]]:
    """Read the journal file directly, bypassing the Store entirely.

    Used to prove an intent was *durably on disk* at a given instant, without
    borrowing any of the machinery under test.
    """
    path = root / "workspaces" / workspace_id / "journal.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def raw_text(root: Path, filename: str, workspace_id: str = WS) -> str:
    """One store file as bytes-on-disk text, for grepping a recording surface."""
    path = root / "workspaces" / workspace_id / filename
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def open_intents(store: Store, workspace_id: str = WS) -> list[dict[str, Any]]:
    """Journalled intents with no settling entry — the crash signature."""
    entries = journal_entries(store, workspace_id)
    closed = {e["intent_id"] for e in entries if e["phase"] != PHASE_INTENDED}
    return [e for e in entries if e["phase"] == PHASE_INTENDED and e["intent_id"] not in closed]


def put_intents(store: Store, workspace_id: str = WS) -> list[dict[str, Any]]:
    return [e for e in journal_entries(store, workspace_id) if e["intent"] == INTENT_PUT]


class CopyInProvider(FakeProvider):
    """A fake that can receive bytes, and remembers exactly what it was handed.

    ``write`` mirrors the seam's pinned inbound signature and the two contracts
    the orchestrator depends on: the digest is verified while the source is
    consumed, and an existing destination is refused unless ``overwrite``.

    Two dials no shipped backend would offer. ``kill_after_staging`` raises
    ``KeyboardInterrupt`` once, from inside the engine call and after staging —
    a CLI killed mid-copy, which is the only way to leave residue behind.
    ``refuse`` names destinations the engine rejects, for the partial-copy path.
    """

    def __init__(
        self,
        *,
        root: Path | None = None,
        kill_after_staging: bool = False,
        refuse: Iterable[str] = (),
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._root = root
        self.kill_after_staging = kill_after_staging
        self._refuse = set(refuse)
        self.writes: list[dict[str, Any]] = []
        self.staged: dict[str, list[str]] = {}
        self.reap_calls: list[str] = []
        self.journal_at_write: list[dict[str, Any]] = []

    def write(
        self,
        workspace_id: str,
        path: str,
        source: ByteSource,
        *,
        expected_sha256: str,
        overwrite: bool = False,
    ) -> None:
        if self._root is not None and not self.journal_at_write:
            self.journal_at_write = raw_journal(self._root, workspace_id)

        payload = b"".join(_chunks(source))
        nonce = f".headspace-staging/{len(self.writes)}-{path.replace('/', '_')}"
        self.staged.setdefault(workspace_id, []).append(nonce)
        if self.kill_after_staging:
            raise KeyboardInterrupt("simulated kill after staging, before finalize")

        digest = hashlib.sha256(payload).hexdigest()
        if digest != expected_sha256:
            self.staged[workspace_id].remove(nonce)
            raise ProviderError(f"staged bytes hash {digest}, not {expected_sha256}")
        if path in self._refuse:
            self.staged[workspace_id].remove(nonce)
            raise CliError(
                code=EXIT_USER_ERROR,
                message=f"the engine refused destination '{path}'",
                remediation="name a destination the workspace can hold",
            )
        if path in self.files(workspace_id) and not overwrite:
            self.staged[workspace_id].remove(nonce)
            raise CliError(
                code=EXIT_USER_ERROR,
                message=f"destination '{path}' already exists",
                remediation="pass overwrite to replace it deliberately",
            )
        self.write_file(workspace_id, path, payload)
        self.staged[workspace_id].remove(nonce)
        self.writes.append(
            {
                "workspace_id": workspace_id,
                "path": path,
                "expected_sha256": expected_sha256,
                "overwrite": overwrite,
                "size_bytes": len(payload),
            }
        )

    def reap_staging(self, workspace_id: str) -> list[str]:
        """Clear the workspace's staging area, reporting what was there."""
        self.reap_calls.append(workspace_id)
        residue = self.staged.pop(workspace_id, [])
        return list(residue)

    def files(self, workspace_id: str) -> dict[str, bytes]:
        """What the workspace holds — the fake's volume, read for assertions."""
        return dict(self._workspaces[workspace_id].files)

    def set_active_jobs(self, workspace_id: str, count: int) -> None:
        """Make the engine report a job in flight without running one."""
        self._workspaces[workspace_id].active_jobs = count


class NoReaperProvider(CopyInProvider):
    """A backend that receives bytes but exposes no staging reaper at all."""

    reap_staging = None  # type: ignore[assignment]


class BreakingProvider(CopyInProvider):
    """A backend whose engine dies during the copy, without staging anything."""

    def write(self, *args: Any, **kwargs: Any) -> None:
        raise ProviderError("the engine died mid-copy")


@dataclasses.dataclass(eq=False)
class LedgerCrashingStore(Store):
    """A store that dies exactly where a killed CLI would: at the ledger write.

    ``KeyboardInterrupt`` rather than a custom exception on purpose — it is a
    BaseException, so every ``except CliError`` handler in the orchestrator
    ignores it and the flow unwinds with no compensating journal entry.
    """

    def write_state(self, workspace_id: str, state: Mapping[str, Any]) -> Any:
        if state.get("inputs"):
            raise KeyboardInterrupt("simulated kill before the inputs ledger landed")
        return super().write_state(workspace_id, state)


@dataclasses.dataclass(eq=False)
class RecordingStore(Store):
    """Records the order in which the lock is taken and state is read."""

    events: list[str] = dataclasses.field(default_factory=list)

    def lock(self, workspace_id: str, *, blocking: bool = True) -> Any:
        self.events.append("lock")
        return super().lock(workspace_id, blocking=blocking)

    def read_state(self, workspace_id: str) -> Any:
        self.events.append("read_state")
        return super().read_state(workspace_id)


def _chunks(source: ByteSource) -> Iterable[bytes]:
    read = getattr(source, "read", None)
    if callable(read):
        while True:
            chunk = read(1 << 16)
            if not chunk:
                return
            yield chunk
    else:
        yield from source  # type: ignore[misc]


def prepared(
    store: Store,
    provider: CopyInProvider | None = None,
    *,
    policy: Policy | None = None,
) -> tuple[Orchestrator, CopyInProvider]:
    """A ready workspace on a backend that can receive bytes."""
    provider = provider if provider is not None else CopyInProvider(root=store.root)
    orch = Orchestrator(provider, store)
    orch.create(workspace_id=WS, policy=policy)
    return orch, provider


def tree(root: Path) -> Path:
    """A small host directory: two files, one of them nested."""
    (root / "harness").mkdir(parents=True, exist_ok=True)
    (root / "harness" / "run.sh").write_bytes(b"#!/bin/sh\necho hi\n")
    (root / "harness" / "lib").mkdir(exist_ok=True)
    (root / "harness" / "lib" / "helper.py").write_bytes(b"VALUE = 1\n")
    return root / "harness"


# --- criterion 1: journal before the engine, settled on every exit path ----


def test_put_journals_its_intent_before_the_engine_receives_a_byte(
    store: Store, payload: Path
) -> None:
    """Order is the whole design: journal first, engine second.

    Proved by asking the *engine* what was already on disk when it was called,
    read straight from the journal file rather than through the Store.
    """
    orch, provider = prepared(store)

    orch.put(WS, payload, "config.env")

    entries = [line["entry"] for line in provider.journal_at_write]
    (intent,) = [e for e in entries if e["intent"] == INTENT_PUT]
    assert intent["phase"] == PHASE_INTENDED
    assert intent["provider"] == "fake"
    # ...and the pairing entry only exists once the engine call came back.
    assert [e["phase"] for e in put_intents(store)] == [PHASE_INTENDED, PHASE_SETTLED]
    assert not open_intents(store)


def test_the_put_intent_payload_names_paths_and_digests_a_reconciler_needs(
    store: Store, payload: Path
) -> None:
    orch, _ = prepared(store)

    orch.put(WS, payload, "config.env")

    intent = put_intents(store)[0]
    (recorded,) = intent["detail"]["inputs"]
    assert recorded["destination"] == "config.env"
    assert recorded["sha256"] == PAYLOAD_DIGEST
    assert recorded["size_bytes"] == len(PAYLOAD)
    assert recorded["source"] == str(payload)


def test_an_engine_that_refuses_the_copy_abandons_the_intent_and_leaves_no_orphan(
    store: Store, payload: Path
) -> None:
    """A handled failure is not a crash: it closes its own intent on the way out."""
    orch, provider = prepared(store, CopyInProvider(root=store.root, refuse={"config.env"}))

    with pytest.raises(CliError) as exc_info:
        orch.put(WS, payload, "config.env")

    assert exc_info.value.code == EXIT_USER_ERROR
    assert [e["phase"] for e in put_intents(store)] == [PHASE_INTENDED, PHASE_ABANDONED]
    assert not open_intents(store), "a handled failure is not a crash"
    assert put_intents(store)[-1]["detail"]["error"]
    assert not ledger(store), "nothing landed, so nothing is claimed to have landed"


def test_a_broken_engine_is_infrastructure_failure_and_still_closes_its_intent(
    store: Store, payload: Path
) -> None:
    """NFR-07 on the copy-in path: the engine breaking is never a quiet success."""
    orch, _ = prepared(store, BreakingProvider(root=store.root))

    with pytest.raises(ProviderError):
        orch.put(WS, payload, "config.env")

    assert [e["phase"] for e in put_intents(store)] == [PHASE_INTENDED, PHASE_ABANDONED]
    assert not open_intents(store)
    assert lifecycle(store) == State.READY.value, "a failed copy-in leaves a usable workspace"


def test_every_guard_is_evaluated_after_the_lock_is_acquired(
    isolated_store_home: Path, payload: Path
) -> None:
    """Issue #11's open TOCTOU in destroy is the pattern this verb must not copy.

    Asserted structurally rather than by review: the store records the order of
    its own operations, and the lock must come before the first read of the
    state every guard is evaluated against.
    """
    recording = RecordingStore(isolated_store_home)
    orch, _ = prepared(recording)
    recording.events.clear()

    orch.put(WS, payload, "config.env")

    assert recording.events[0] == "lock"
    assert recording.events.index("lock") < recording.events.index("read_state")


def test_a_put_into_an_unknown_workspace_is_a_user_error(store: Store, payload: Path) -> None:
    orch = Orchestrator(CopyInProvider(root=store.root), store)
    with pytest.raises(CliError) as exc_info:
        orch.put("ws-nope", payload, "config.env")
    assert exc_info.value.code == EXIT_USER_ERROR
    assert not raw_journal(store.root, "ws-nope")


def test_a_put_into_a_workspace_that_can_no_longer_host_a_job_is_refused(
    store: Store, payload: Path
) -> None:
    """A copy-in feeds a job that has not run yet; a dead workspace has none."""
    orch, provider = prepared(store)
    record = state_record(store)
    record["state"] = State.CANCELLED.value
    store.write_state(WS, record)

    with pytest.raises(CliError) as exc_info:
        orch.put(WS, payload, "config.env")

    assert exc_info.value.code == EXIT_USER_ERROR
    assert State.CANCELLED.value in exc_info.value.message
    assert not put_intents(store), "refused upstream of the journal and the engine"
    assert not provider.writes


def test_a_missing_host_path_is_refused_before_the_journal_and_the_engine(
    store: Store, tmp_path: Path
) -> None:
    orch, provider = prepared(store)

    with pytest.raises(CliError) as exc_info:
        orch.put(WS, tmp_path / "nope.env", "config.env")

    assert exc_info.value.code == EXIT_USER_ERROR
    assert not put_intents(store)
    assert not provider.writes


def test_a_payload_over_the_remaining_storage_budget_is_refused_before_the_engine(
    store: Store, payload: Path
) -> None:
    """The budget precheck runs inside the lock and upstream of the journal."""
    orch, provider = prepared(store, policy=Policy(budget=ResourceBudget(storage_bytes=4)))

    with pytest.raises(CliError) as exc_info:
        orch.put(WS, payload, "config.env")

    assert exc_info.value.code == EXIT_POLICY_DENIED
    assert "storage_bytes" in exc_info.value.remediation
    assert not put_intents(store)
    assert not provider.writes


def test_the_budget_counts_what_earlier_copy_ins_already_put_there(
    store: Store, tmp_path: Path
) -> None:
    """A stale engine measurement must not let two puts each spend the budget once."""
    first = tmp_path / "first.bin"
    first.write_bytes(b"x" * 600)
    second = tmp_path / "second.bin"
    second.write_bytes(b"y" * 600)
    orch, _ = prepared(store, policy=Policy(budget=ResourceBudget(storage_bytes=1000)))

    orch.put(WS, first, "first.bin")

    with pytest.raises(CliError) as exc_info:
        orch.put(WS, second, "second.bin")
    assert exc_info.value.code == EXIT_POLICY_DENIED


# --- criterion 2: a job in flight refuses immediately ---------------------


def test_a_put_during_a_running_job_refuses_immediately_naming_the_job_and_stop(
    store: Store, payload: Path
) -> None:
    """h35: the refusal arrives *during* the job, not after it.

    Raised by a *second* Orchestrator over a *second* Store — the in-process
    stand-in for a concurrent CLI invocation — from inside the fake's
    mid-job callback, so a refusal that waited for the lock could not be
    recorded here at all.
    """
    refusals: list[CliError] = []
    provider = CopyInProvider(root=store.root)

    def concurrent_put() -> None:
        rival = Orchestrator(provider, Store())
        try:
            rival.put(WS, payload, "config.env")
        except CliError as err:
            refusals.append(err)

    provider.script_command(ECHO, JobPlan.succeeding("hello\n", during=concurrent_put))
    orch = Orchestrator(provider, store)
    orch.create(workspace_id=WS)

    package = orch.run(WS, ECHO)

    (refusal,) = refusals
    job_id = package.provenance.job_id
    assert job_id and job_id in refusal.message
    assert "stop" in refusal.remediation
    assert refusal.code == EXIT_USER_ERROR
    # Nothing was copied, and no intent was opened for a copy that never began.
    assert not provider.writes
    assert not put_intents(store)
    assert lifecycle(store) == State.READY.value


def test_the_in_flight_refusal_names_the_job_by_id_and_never_by_its_command_line(
    store: Store, payload: Path
) -> None:
    """The argv this feature exists to keep out of the record stays out of refusals too."""
    refusals: list[CliError] = []
    secret_command = ("train", f"--api-key={MARKER}")
    provider = CopyInProvider(root=store.root)

    def concurrent_put() -> None:
        rival = Orchestrator(provider, Store())
        try:
            rival.put(WS, payload, "config.env")
        except CliError as err:
            refusals.append(err)

    provider.script_command(secret_command, JobPlan.succeeding("done\n", during=concurrent_put))
    orch = Orchestrator(provider, store)
    orch.create(workspace_id=WS)

    orch.run(WS, secret_command)

    (refusal,) = refusals
    assert MARKER not in refusal.message
    assert MARKER not in refusal.remediation


def test_a_put_after_the_job_completes_succeeds_unchanged(store: Store, payload: Path) -> None:
    """h35's other half: the refusal is about the job, not about the workspace."""
    orch, provider = prepared(store)
    orch.run(WS, ECHO)

    package = orch.put(WS, payload, "config.env")

    assert provider.files(WS)["config.env"] == PAYLOAD
    assert package.status == "success"
    assert [row["destination"] for row in ledger(store)] == ["config.env"]


def test_a_workspace_busy_with_another_verb_refuses_without_inventing_a_job(
    store: Store, payload: Path
) -> None:
    """No open run intent means no job to name — say what is true instead."""
    orch, provider = prepared(store)
    holder = Store()  # a distinct open file description == a distinct process

    with holder.lock(WS):
        with pytest.raises(CliError) as exc_info:
            orch.put(WS, payload, "config.env")

    assert exc_info.value.code == EXIT_ENV_ERROR
    assert "job" not in exc_info.value.message
    assert not provider.writes
    assert not put_intents(store)


def test_a_workspace_the_engine_says_is_busy_refuses_from_inside_the_lock(
    store: Store, payload: Path
) -> None:
    """The in-lock guard, for the job whose CLI died but whose container did not.

    Reconciliation runs first and leaves this one alone precisely because the
    engine reports the job as genuinely in flight, so the persisted ``running``
    survives to be guarded against — and the caller gets the same guidance the
    busy-lock path gives, because the situation is the same one.
    """
    orch, provider = prepared(store)
    record = state_record(store)
    record["state"] = State.RUNNING.value
    store.write_state(WS, record)
    provider.set_active_jobs(WS, 1)
    store.append_journal(
        WS,
        {
            "intent": INTENT_RUN,
            "intent_id": "abc123",
            "phase": PHASE_INTENDED,
            "provider": "fake",
            "detail": {"job_id": "job-stranded", "command": ["sleep"]},
        },
    )

    with pytest.raises(CliError) as exc_info:
        orch.put(WS, payload, "config.env")

    assert "job-stranded" in exc_info.value.message
    assert "stop" in exc_info.value.remediation
    assert not provider.writes


# --- criterion 3: an inputs ledger, distinct from artifacts ---------------


def test_copied_in_files_land_in_the_inputs_ledger_not_the_artifact_inventory(
    store: Store, payload: Path
) -> None:
    orch, _ = prepared(store)

    package = orch.put(WS, payload, "config.env")

    record = state_record(store)
    (row,) = record["inputs"]
    assert row["destination"] == "config.env"
    assert row["sha256"] == PAYLOAD_DIGEST
    assert row["size_bytes"] == len(PAYLOAD)
    assert record["artifacts"] == [], "an input is not an artifact"
    assert package.artifacts == [], "and it never renders as one"
    assert any("config.env" in finding for finding in package.key_findings)


def test_destroying_a_workspace_holding_only_copied_in_inputs_is_not_refused(
    store: Store, payload: Path
) -> None:
    """h31: the destroy guard protects unexported *products*, and an input is not one."""
    orch, provider = prepared(store)
    orch.put(WS, payload, "config.env")

    package = orch.destroy(WS)  # no force: the guard must not fire

    assert not store.exists(WS)
    assert package.status == "success"
    with pytest.raises(CliError):
        provider.inspect(WS)


def test_the_destroy_guard_still_fires_for_a_declared_artifact_beside_an_input(
    store: Store, payload: Path
) -> None:
    """The ledger is additive: it neither arms nor disarms the guard."""
    orch, _ = prepared(store)
    orch.run(WS, ECHO, declares=[ArtifactDeclaration("lost.csv", "never exported")])
    orch.put(WS, payload, "config.env")

    with pytest.raises(CliError) as exc_info:
        orch.destroy(WS)

    assert "lost.csv" in exc_info.value.message
    assert "config.env" not in exc_info.value.message


def test_no_recording_surface_carries_a_byte_of_the_files_contents(
    store: Store, isolated_store_home: Path, payload: Path
) -> None:
    """The whole point of the feature (issues #13/#14): path and digest, never contents."""
    orch, _ = prepared(store)

    package = orch.put(WS, payload, "config.env")

    surfaces = {
        "outcome_summary": package.outcome_summary,
        "key_findings": " ".join(package.key_findings),
        "provenance.inputs": " ".join(package.provenance.inputs),
        "journal.jsonl": raw_text(isolated_store_home, "journal.jsonl"),
        "state.json": raw_text(isolated_store_home, "state.json"),
    }
    for name, text in surfaces.items():
        assert MARKER not in text, f"{name} leaked the file's contents"
        assert "config.env" in text, f"{name} lost the path"
        assert PAYLOAD_DIGEST in text, f"{name} lost the digest"


def test_a_directory_copy_in_records_every_file_with_its_own_digest(
    store: Store, tmp_path: Path
) -> None:
    orch, provider = prepared(store)

    package = orch.put(WS, tree(tmp_path), "harness")

    assert [row["destination"] for row in ledger(store)] == [
        "harness/lib/helper.py",
        "harness/run.sh",
    ]
    assert sorted(provider.files(WS)) == ["harness/lib/helper.py", "harness/run.sh"]
    for row in ledger(store):
        assert row["sha256"] == hashlib.sha256(provider.files(WS)[row["destination"]]).hexdigest()
    assert package.resource_usage.storage_bytes == sum(row["size_bytes"] for row in ledger(store))


def test_a_multi_file_copy_in_that_fails_part_way_records_only_what_landed(
    store: Store, tmp_path: Path
) -> None:
    """A partial copy is a fact about the workspace; the ledger must state it."""
    orch, provider = prepared(store, CopyInProvider(root=store.root, refuse={"harness/run.sh"}))
    source_tree = tree(tmp_path)

    with pytest.raises(CliError):
        orch.put(WS, source_tree, "harness")

    assert [row["destination"] for row in ledger(store)] == ["harness/lib/helper.py"]
    settled = put_intents(store)[-1]
    assert settled["phase"] == PHASE_ABANDONED
    assert settled["detail"]["landed"] == 1
    assert settled["detail"]["of"] == 2
    assert not open_intents(store)


def test_the_overwrite_decision_is_the_callers_and_is_passed_through(
    store: Store, payload: Path
) -> None:
    orch, provider = prepared(store)
    orch.put(WS, payload, "config.env")
    assert provider.writes[-1]["overwrite"] is False

    with pytest.raises(CliError) as exc_info:
        orch.put(WS, payload, "config.env")
    assert "already exists" in exc_info.value.message

    orch.put(WS, payload, "config.env", overwrite=True)
    assert provider.writes[-1]["overwrite"] is True
    assert len(ledger(store)) == 1, "one destination is one ledger row, however often it is written"


def test_the_engine_is_handed_the_digest_the_host_measured(store: Store, payload: Path) -> None:
    orch, provider = prepared(store)

    orch.put(WS, payload, "config.env")

    (written,) = provider.writes
    assert written["expected_sha256"] == PAYLOAD_DIGEST
    assert written["path"] == "config.env"
    assert written["size_bytes"] == len(PAYLOAD)


def test_inspect_reports_the_inputs_ledger_distinctly_from_artifacts(
    store: Store, payload: Path
) -> None:
    orch, _ = prepared(store)
    orch.put(WS, payload, "config.env")

    package = orch.inspect(WS)

    assert any("input" in finding for finding in package.key_findings)
    assert package.artifacts == []


def test_the_inputs_ledger_survives_verbs_that_know_nothing_about_it(
    store: Store, payload: Path, tmp_path: Path
) -> None:
    """Plan risk r2: an older binary round-trips the record, so the ledger persists.

    Every verb reads the whole state record and writes the whole record back, so
    a key a build does not know about is carried rather than dropped. ``declare``
    and ``run`` stand in here for a build that predates the ledger entirely.
    """
    orch, _ = prepared(store)
    orch.put(WS, payload, "config.env")
    before = ledger(store)

    orch.declare(WS, "out.csv", purpose="the measurements")
    orch.run(WS, ECHO)

    assert ledger(store) == before


# --- criterion 4: a copy-in killed mid-flight ------------------------------


def kill_mid_copy(store: Store, payload: Path) -> CopyInProvider:
    """A workspace whose copy-in died inside the engine call, after staging."""
    provider = CopyInProvider(root=store.root, kill_after_staging=True)
    orch = Orchestrator(provider, store)
    orch.create(workspace_id=WS)
    with pytest.raises(KeyboardInterrupt):
        orch.put(WS, payload, "config.env")
    return provider


def test_a_copy_in_killed_inside_the_engine_call_leaves_an_orphan_and_residue(
    store: Store, payload: Path
) -> None:
    """The situation the recovery path exists for, asserted on disk."""
    provider = kill_mid_copy(store, payload)

    (intent,) = open_intents(store)
    assert intent["intent"] == INTENT_PUT
    assert intent["detail"]["inputs"][0]["sha256"] == PAYLOAD_DIGEST
    assert provider.staged[WS], "the engine staged bytes the store knows nothing about"
    assert "config.env" not in provider.files(WS)
    assert not ledger(store)


def test_the_next_verb_settles_the_put_reaps_the_residue_and_reports_it(
    store: Store, payload: Path
) -> None:
    provider = kill_mid_copy(store, payload)

    # A fresh orchestrator over a fresh store: nothing survives from the killed
    # invocation but the store root on disk and the live engine.
    revived = Orchestrator(provider, Store())
    package = revived.inspect(WS)

    assert not open_intents(store), "reconciliation settles the intent it acted on"
    assert journal_entries(store)[-1]["phase"] == PHASE_RECONCILED
    assert not provider.staged.get(WS), "staging residue was reaped"
    assert provider.reap_calls == [WS]
    reported = [item for item in package.attention if WS in item]
    assert reported, "silent cleanup is as bad as a leak"
    assert DISPOSITION_RESOLVED in reported[0]
    assert "config.env" in reported[0]


def test_the_workspace_stays_usable_and_a_rerun_of_the_copy_in_succeeds(
    store: Store, payload: Path
) -> None:
    provider = kill_mid_copy(store, payload)
    provider.kill_after_staging = False

    revived = Orchestrator(provider, Store())
    package = revived.put(WS, payload, "config.env")

    assert package.status == "success"
    assert provider.files(WS)["config.env"] == PAYLOAD
    assert lifecycle(store) == State.READY.value
    assert [row["destination"] for row in ledger(store)] == ["config.env"]


def test_reconciliation_of_a_put_never_rebuilds_lifecycle_state_from_the_engine(
    store: Store, payload: Path
) -> None:
    """A copy-in makes no lifecycle move, so it must not be read as a lifecycle crash."""
    kill_mid_copy(store, payload)
    assert lifecycle(store) == State.READY.value

    (disposition,) = Orchestrator(CopyInProvider(root=store.root), Store()).reconcile()

    assert disposition.intent == INTENT_PUT
    assert lifecycle(store) == State.READY.value
    assert store.exists(WS), "a workspace with a failed copy-in is not a workspace to reap"


def test_a_kill_after_the_engine_call_settles_without_pretending_bytes_are_recorded(
    store: Store, payload: Path
) -> None:
    """The other kill: the file landed, the ledger write is what died."""
    provider = CopyInProvider(root=store.root)
    Orchestrator(provider, store).create(workspace_id=WS)
    crashing = Orchestrator(provider, LedgerCrashingStore(store.root))
    with pytest.raises(KeyboardInterrupt):
        crashing.put(WS, payload, "config.env")

    assert provider.files(WS)["config.env"] == PAYLOAD, "the engine kept what it was given"
    assert not ledger(store), "the ledger says only what actually landed in it"
    assert open_intents(store)[0]["intent"] == INTENT_PUT

    (disposition,) = Orchestrator(provider, Store()).reconcile()

    assert disposition.intent == INTENT_PUT
    assert "config.env" in disposition.detail
    assert not open_intents(store)


def test_reconciliation_defers_a_put_rather_than_reaping_under_a_live_invocation(
    store: Store, payload: Path
) -> None:
    """Reaping by prefix under a concurrent put would delete its staged bytes."""
    provider = kill_mid_copy(store, payload)
    holder = Store()

    with holder.lock(WS):
        (disposition,) = Orchestrator(provider, Store()).reconcile()

    assert disposition.disposition == DISPOSITION_REPORTED
    assert "busy" in disposition.detail
    assert provider.staged[WS], "nothing was reaped under the invocation that holds the lock"
    assert open_intents(store), "an undecided intent stays open"


def test_a_backend_with_no_staging_reaper_is_reported_rather_than_assumed_clean(
    store: Store, payload: Path
) -> None:
    provider = NoReaperProvider(root=store.root, kill_after_staging=True)
    orch = Orchestrator(provider, store)
    orch.create(workspace_id=WS)
    with pytest.raises(KeyboardInterrupt):
        orch.put(WS, payload, "config.env")

    (disposition,) = Orchestrator(provider, Store()).reconcile()

    assert disposition.disposition == DISPOSITION_REPORTED
    assert STAGING_REAPER in disposition.detail
    assert not open_intents(store), "the intent is still settled; only the residue is unresolved"


def test_a_reaper_that_fails_reports_it_and_never_fails_the_callers_verb(
    store: Store, payload: Path
) -> None:
    """Reconciliation never fails a verb — the module's second load-bearing rule."""

    class UnreapableProvider(CopyInProvider):
        def reap_staging(self, workspace_id: str) -> list[str]:
            raise ProviderError("the engine could not be reached")

    provider = UnreapableProvider(root=store.root, kill_after_staging=True)
    orch = Orchestrator(provider, store)
    orch.create(workspace_id=WS)
    with pytest.raises(KeyboardInterrupt):
        orch.put(WS, payload, "config.env")

    package = Orchestrator(provider, Store()).inspect(WS)

    reported = [item for item in package.attention if WS in item]
    assert reported and "could not be reached" in reported[0]


# --- the vocabulary -------------------------------------------------------


def test_the_copy_in_intent_joins_the_closed_vocabulary() -> None:
    assert INTENT_PUT in INTENTS
    assert len(set(INTENTS)) == len(INTENTS)


def test_a_recovered_copy_in_is_reported_once_not_on_every_call(
    store: Store, payload: Path
) -> None:
    provider = kill_mid_copy(store, payload)
    provider.kill_after_staging = False
    revived = Orchestrator(provider, Store())

    first = revived.inspect(WS)
    second = revived.inspect(WS)

    assert any(WS in item for item in first.attention)
    assert not second.attention


def test_a_source_swapped_for_a_symlink_after_measurement_is_refused(tmp_path: Path) -> None:
    """The streaming open refuses to follow a link the measuring open would not have.

    ``expand_input`` measures with ``O_NOFOLLOW``; ``_copy_in`` streams the same
    file a moment later. If those two opens disagree, a path swapped to a symlink
    in between would be hashed as one file and read as another. The digest check
    downstream would catch the mismatch, but it would blame the transfer for
    something the host did — so the read refuses at the source instead.
    """
    payload = tmp_path / "payload.txt"
    payload.write_bytes(b"the bytes that were measured\n")
    elsewhere = tmp_path / "elsewhere.txt"
    elsewhere.write_bytes(b"not the bytes that were measured\n")

    manifest = expand_input(payload, "payload.txt")
    entry = manifest.entries[0]

    # The swap: same path, now a link to a different file.
    payload.unlink()
    payload.symlink_to(elsewhere)

    with pytest.raises(CliError) as caught:
        _open_input(entry)
    assert caught.value.code == EXIT_ENV_ERROR
