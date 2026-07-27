"""Tests for the state store: fail-closed schema, crash-atomic writes, cross-process locking.

Every test runs against a ``HEADSPACE_HOME`` pointed at ``tmp_path`` (autouse
fixture below) — no test may touch the operator's real ``~/.headspace``.

The locking tests deliberately use real processes (``multiprocessing`` with the
fork context, and ``subprocess``): ``fcntl.flock`` is held per open file
description, so a thread-only test would pass vacuously.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

import headspace.core.store as store_module
from headspace.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from headspace.cli._output import emit_error
from headspace.core.store import (
    HOME_ENV_VAR,
    SCHEMA_VERSION,
    JournalEntry,
    StateRecord,
    Store,
    store_root,
)

WS = "ws-alpha"


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


def _state_path(store: Store, workspace_id: str = WS) -> Path:
    return store.workspace_dir(workspace_id) / "state.json"


def _journal_path(store: Store, workspace_id: str = WS) -> Path:
    return store.workspace_dir(workspace_id) / "journal.jsonl"


def _plant_state_file(store: Store, document: dict[str, Any], workspace_id: str = WS) -> Path:
    """Write a state file by hand, bypassing the store, to simulate foreign state."""
    path = _state_path(store, workspace_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _plant_journal_file(store: Store, documents: list[dict[str, Any]]) -> Path:
    path = _journal_path(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(doc) + "\n" for doc in documents), encoding="utf-8")
    return path


@contextmanager
def _fail_after(seconds: float) -> Iterator[None]:
    """Turn a lock deadlock into a test failure instead of a hung CI job."""

    def _raise(signum: int, frame: Any) -> None:
        raise AssertionError(f"blocked for more than {seconds}s — probable lock deadlock")

    previous = signal.signal(signal.SIGALRM, _raise)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


# --- criterion 1: a future schema_version fails every load ----------------


def test_future_schema_version_fails_read_state(store: Store) -> None:
    _plant_state_file(store, {"schema_version": SCHEMA_VERSION + 1, "state": {"phase": "ready"}})

    with pytest.raises(CliError) as exc:
        store.read_state(WS)

    assert exc.value.code == EXIT_ENV_ERROR
    assert str(SCHEMA_VERSION + 1) in exc.value.message
    assert exc.value.remediation


def test_future_schema_version_fails_every_state_load_entry_point(store: Store) -> None:
    _plant_state_file(store, {"schema_version": SCHEMA_VERSION + 1, "state": {"phase": "ready"}})

    loads = {
        "read_state": lambda: store.read_state(WS),
        "read_all_states": store.read_all_states,
        "update_state": lambda: store.update_state(WS, lambda state: {**state, "touched": True}),
    }
    for name, load in loads.items():
        with pytest.raises(CliError) as exc:
            load()
        assert exc.value.code == EXIT_ENV_ERROR, name
        assert exc.value.remediation, name


def test_future_schema_version_fails_journal_load(store: Store) -> None:
    _plant_journal_file(store, [{"schema_version": SCHEMA_VERSION + 1, "entry": {"intent": "x"}}])

    with pytest.raises(CliError) as exc:
        store.read_journal(WS)

    assert exc.value.code == EXIT_ENV_ERROR
    assert exc.value.remediation


def test_future_schema_version_mutates_nothing(store: Store) -> None:
    path = _plant_state_file(
        store, {"schema_version": SCHEMA_VERSION + 1, "state": {"phase": "ready"}}
    )
    before = path.read_bytes()

    for load in (
        lambda: store.read_state(WS),
        store.read_all_states,
        lambda: store.update_state(WS, lambda state: {**state, "touched": True}),
    ):
        with pytest.raises(CliError):
            load()

    assert path.read_bytes() == before
    assert sorted(p.name for p in store.workspace_dir(WS).iterdir()) == [".lock", "state.json"]


def test_schema_error_renders_through_the_cli_error_contract(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    _plant_state_file(store, {"schema_version": SCHEMA_VERSION + 1, "state": {}})

    with pytest.raises(CliError) as exc:
        store.read_state(WS)
    emit_error(exc.value, json_mode=False)

    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err
    assert "Traceback" not in err


def test_missing_schema_version_fails_closed(store: Store) -> None:
    _plant_state_file(store, {"state": {"phase": "ready"}})

    with pytest.raises(CliError) as exc:
        store.read_state(WS)

    assert exc.value.code == EXIT_ENV_ERROR
    assert "schema_version" in exc.value.message
    assert exc.value.remediation


def test_unrecognised_older_schema_version_fails_closed(store: Store) -> None:
    _plant_state_file(store, {"schema_version": 0, "state": {"phase": "ready"}})

    with pytest.raises(CliError) as exc:
        store.read_state(WS)

    assert exc.value.code == EXIT_ENV_ERROR
    assert exc.value.remediation


def test_corrupt_state_file_fails_closed(store: Store) -> None:
    path = _state_path(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(CliError) as exc:
        store.read_state(WS)

    assert exc.value.code == EXIT_ENV_ERROR
    assert exc.value.remediation


# --- criterion 2a: two processes contending on one lock serialize ---------


def _hold_lock_and_record(
    root: str, workspace_id: str, out_dir: str, barrier: Any, hold: float
) -> None:
    """Child process: take the workspace lock, hold it, record the interval afterwards."""
    child_store = Store(Path(root))
    barrier.wait(timeout=30)
    with child_store.lock(workspace_id):
        entered = time.monotonic()
        time.sleep(hold)
        left = time.monotonic()
    # Written outside the critical section so the record file itself cannot be
    # what serializes the processes.
    Path(out_dir, f"{os.getpid()}.json").write_text(json.dumps([entered, left]), encoding="utf-8")


def test_workspace_lock_serializes_contending_processes(store: Store, tmp_path: Path) -> None:
    store.write_state(WS, {"phase": "created"})
    out_dir = tmp_path / "intervals"
    out_dir.mkdir()
    processes, hold = 4, 0.15

    ctx = mp.get_context("fork")
    barrier = ctx.Barrier(processes)
    children = [
        ctx.Process(
            target=_hold_lock_and_record,
            args=(str(store.root), WS, str(out_dir), barrier, hold),
        )
        for _ in range(processes)
    ]
    for child in children:
        child.start()
    for child in children:
        child.join(60)
        if child.is_alive():  # pragma: no cover - only on a lock deadlock
            child.kill()
            child.join(5)

    assert [child.exitcode for child in children] == [0] * processes
    intervals = sorted(json.loads(path.read_text(encoding="utf-8")) for path in out_dir.iterdir())
    assert len(intervals) == processes
    for (_, previous_exit), (next_enter, _) in zip(intervals, intervals[1:]):
        assert next_enter >= previous_exit, f"lock holds overlapped: {intervals}"
    span = intervals[-1][1] - intervals[0][0]
    assert span >= hold * processes * 0.95, f"holds did not serialize: {intervals}"


_BUSY_PROBE = """
import sys
from pathlib import Path

from headspace.cli._errors import CliError
from headspace.core.store import Store

store = Store(Path(sys.argv[1]))
try:
    with store.lock(sys.argv[2], blocking=False):
        print("ACQUIRED")
        raise SystemExit(9)
except CliError as err:
    print(f"BUSY {err.code} {bool(err.remediation)}")
"""


def test_non_blocking_lock_reports_busy_to_a_separate_process(store: Store) -> None:
    store.write_state(WS, {"phase": "created"})

    with store.lock(WS):
        probe = subprocess.run(
            [sys.executable, "-c", _BUSY_PROBE, str(store.root), WS],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == f"BUSY {EXIT_ENV_ERROR} True"


def test_nested_lock_is_reentrant_within_one_store(store: Store) -> None:
    store.write_state(WS, {"generation": 0})

    with _fail_after(10):
        with store.lock(WS):
            with store.lock(WS):
                store.write_state(WS, {"generation": 1})

    assert store.read_state(WS).state == {"generation": 1}


# --- criterion 2b: writes are atomic via temp-and-rename ------------------


def _rewrite_state_repeatedly(root: str, workspace_id: str, rounds: int, width: int) -> None:
    """Child process: rewrite one workspace's state over and over."""
    child_store = Store(Path(root))
    for generation in range(rounds):
        marker = ("a" if generation % 2 == 0 else "b") * width
        child_store.write_state(workspace_id, {"generation": generation, "marker": marker})


def test_concurrent_reader_never_observes_a_partial_state_file(store: Store) -> None:
    width = 8192
    store.write_state(WS, {"generation": -1, "marker": "a" * width})
    path = _state_path(store)

    ctx = mp.get_context("fork")
    child = ctx.Process(target=_rewrite_state_repeatedly, args=(str(store.root), WS, 40, width))
    child.start()
    observations = 0
    while child.is_alive():
        raw = path.read_bytes()
        document = json.loads(raw)  # a partial file would not parse
        assert document["schema_version"] == SCHEMA_VERSION
        marker = document["state"]["marker"]
        assert marker == marker[0] * len(marker), "observed a spliced state file"
        assert len(marker) == width
        observations += 1
    child.join(60)

    assert child.exitcode == 0
    assert observations > 20, f"only {observations} observations — the race was not exercised"


def test_interrupted_write_leaves_the_prior_content_intact(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.write_state(WS, {"generation": 1})
    before = _state_path(store).read_bytes()

    def _interrupted(src: Any, dst: Any) -> None:
        raise OSError("simulated crash before the rename lands")

    monkeypatch.setattr(store_module.os, "replace", _interrupted)
    with pytest.raises(CliError) as exc:
        store.write_state(WS, {"generation": 2})
    monkeypatch.undo()

    assert exc.value.code == EXIT_ENV_ERROR
    assert exc.value.remediation
    assert _state_path(store).read_bytes() == before
    assert store.read_state(WS).state == {"generation": 1}
    # No temp file survives the failure, so nothing partial is observable.
    assert sorted(p.name for p in store.workspace_dir(WS).iterdir()) == [".lock", "state.json"]


def test_unserialisable_state_is_rejected_without_touching_disk(store: Store) -> None:
    store.write_state(WS, {"generation": 1})
    before = _state_path(store).read_bytes()

    with pytest.raises(CliError) as exc:
        store.write_state(WS, {"generation": 2, "unserialisable": object()})

    assert exc.value.code == EXIT_USER_ERROR
    assert exc.value.remediation
    assert _state_path(store).read_bytes() == before
    assert sorted(p.name for p in store.workspace_dir(WS).iterdir()) == [".lock", "state.json"]


def test_successful_writes_leave_no_temporary_files(store: Store) -> None:
    for generation in range(5):
        store.write_state(WS, {"generation": generation})
    store.append_journal(WS, {"intent": "create"})

    names = sorted(path.name for path in store.workspace_dir(WS).iterdir())
    assert names == [".lock", "journal.jsonl", "state.json"]


# --- store root resolution ------------------------------------------------


def test_store_root_honours_the_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(HOME_ENV_VAR, str(tmp_path / "elsewhere"))
    assert store_root() == tmp_path / "elsewhere"


def test_store_root_defaults_to_home_headspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(HOME_ENV_VAR, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    resolved = store_root()

    assert resolved == tmp_path / ".headspace"
    assert not resolved.exists(), "resolving the root must not create it"


def test_unusable_store_root_is_an_environment_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setenv(HOME_ENV_VAR, str(blocker))

    with pytest.raises(CliError) as exc:
        Store().write_state(WS, {"phase": "created"})

    assert exc.value.code == EXIT_ENV_ERROR
    assert exc.value.remediation


# --- workspace id validation ----------------------------------------------


@pytest.mark.parametrize("bad_id", ["", "..", "../escape", "a/b", ".hidden", "x" * 65, "ws alpha"])
def test_unsafe_workspace_ids_are_rejected(store: Store, bad_id: str) -> None:
    for call in (
        lambda: store.read_state(bad_id),
        lambda: store.write_state(bad_id, {"phase": "created"}),
        lambda: store.workspace_dir(bad_id),
    ):
        with pytest.raises(CliError) as exc:
            call()
        assert exc.value.code == EXIT_USER_ERROR
        assert exc.value.remediation
    assert not store.root.exists() or list(store.root.glob("**/state.json")) == []


# --- ordinary store behaviour ---------------------------------------------


def test_write_state_stamps_the_schema_version(store: Store) -> None:
    record = store.write_state(WS, {"phase": "created"})

    assert isinstance(record, StateRecord)
    assert record.workspace_id == WS
    assert record.state == {"phase": "created"}
    assert record.updated_at.endswith("+00:00")

    document = json.loads(_state_path(store).read_text(encoding="utf-8"))
    assert document["schema_version"] == SCHEMA_VERSION
    assert document["workspace_id"] == WS
    assert document["state"] == {"phase": "created"}


def test_state_round_trips(store: Store) -> None:
    store.write_state(WS, {"phase": "running", "jobs": [{"id": "j1"}]})

    record = store.read_state(WS)

    assert record.state == {"phase": "running", "jobs": [{"id": "j1"}]}


def test_read_missing_workspace_is_a_user_error(store: Store) -> None:
    with pytest.raises(CliError) as exc:
        store.read_state("ws-missing")

    assert exc.value.code == EXIT_USER_ERROR
    assert exc.value.remediation


def test_update_state_applies_a_read_modify_write(store: Store) -> None:
    store.write_state(WS, {"phase": "created", "jobs": []})

    record = store.update_state(WS, lambda state: {**state, "phase": "running"})

    assert record.state == {"phase": "running", "jobs": []}
    assert store.read_state(WS).state["phase"] == "running"


def test_update_state_rejects_a_mutator_returning_nothing(store: Store) -> None:
    store.write_state(WS, {"phase": "created"})

    with pytest.raises(CliError) as exc:
        store.update_state(WS, lambda state: None)  # type: ignore[arg-type,return-value]

    assert exc.value.code == EXIT_USER_ERROR
    assert store.read_state(WS).state == {"phase": "created"}


def test_exists_and_list_workspaces(store: Store) -> None:
    assert store.list_workspaces() == []
    assert not store.exists(WS)

    store.write_state("ws-beta", {"phase": "created"})
    store.write_state(WS, {"phase": "created"})

    assert store.exists(WS)
    assert store.list_workspaces() == [WS, "ws-beta"]


def test_read_all_states_returns_every_workspace(store: Store) -> None:
    store.write_state(WS, {"phase": "created"})
    store.write_state("ws-beta", {"phase": "running"})

    states = store.read_all_states()

    assert set(states) == {WS, "ws-beta"}
    assert states["ws-beta"].state == {"phase": "running"}


def test_delete_workspace_removes_its_directory(store: Store) -> None:
    store.write_state(WS, {"phase": "created"})

    store.delete_workspace(WS)

    assert not store.exists(WS)
    assert not store.workspace_dir(WS).exists()
    assert store.list_workspaces() == []


def test_delete_missing_workspace_is_a_user_error(store: Store) -> None:
    with pytest.raises(CliError) as exc:
        store.delete_workspace("ws-missing")

    assert exc.value.code == EXIT_USER_ERROR


def test_journal_appends_and_reads_back_in_order(store: Store) -> None:
    store.write_state(WS, {"phase": "created"})
    store.append_journal(WS, {"intent": "create-container"})
    store.append_journal(WS, {"intent": "remove-container"})

    entries = store.read_journal(WS)

    assert [entry.entry["intent"] for entry in entries] == [
        "create-container",
        "remove-container",
    ]
    assert isinstance(entries[0], JournalEntry)
    assert entries[0].recorded_at.endswith("+00:00")

    lines = _journal_path(store).read_text(encoding="utf-8").splitlines()
    assert all(json.loads(line)["schema_version"] == SCHEMA_VERSION for line in lines)


def test_read_journal_of_a_workspace_without_one_is_empty(store: Store) -> None:
    store.write_state(WS, {"phase": "created"})

    assert store.read_journal(WS) == []


def test_corrupt_journal_line_fails_closed(store: Store) -> None:
    store.write_state(WS, {"phase": "created"})
    store.append_journal(WS, {"intent": "create-container"})
    with _journal_path(store).open("a", encoding="utf-8") as handle:
        handle.write("{truncated\n")

    with pytest.raises(CliError) as exc:
        store.read_journal(WS)

    assert exc.value.code == EXIT_ENV_ERROR
    assert exc.value.remediation
