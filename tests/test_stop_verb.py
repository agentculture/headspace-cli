"""Tests for ``headspace stop``: preview by default, ``--apply`` to end a job.

Written first, against the two acceptance criteria this task was given:

1. ``stop`` without ``--apply`` lists the in-flight job — id, command, and the
   wall clock it has spent — and *provably* changes nothing; with ``--apply``
   the job ends and the workspace returns to ``ready``;
2. the stopped job's outcome is the operator-stopped status ``cancelled``,
   never ``computation_failed`` and never ``infrastructure_failure``, and the
   verb exits ``5`` — the taxonomy slot that already existed and was unused.

Three things about how those are proved.

**"Changes nothing" is proved against three surfaces at once, not asserted.**
The preview runs over an :class:`UntouchableProvider`, whose every seam verb
raises — so a preview that touched the engine *at all*, let alone signalled a
job, fails rather than being reasoned about. It runs over a
:class:`LockRefusingStore`, whose ``lock`` raises — and because every mutating
method on :class:`~headspace.core.store.Store` goes through that one lock, a
store that cannot be locked is a store that cannot be written. And the raw
bytes of ``state.json`` and ``journal.jsonl`` are read off disk either side of
the call and compared. Engine untouched, lock untaken, files byte-identical.

**The lock double is the structural proof, and it guards both modes.**
:meth:`~headspace.core.workspace.Orchestrator.run` holds a workspace's flock
for a job's entire duration, so a ``stop`` that took that lock would not fail —
it would *hang*, against the very run it exists to interrupt. Proving the
absence of a lock by racing a real one would mean a test that hangs when the
property is broken; the double turns that hang into an immediate, named
assertion failure instead, and it is applied to ``--apply`` as well as to the
preview, because the mode that reaches the engine is the mode where a lock
would deadlock.

**The in-flight case is real, not fabricated.** A workspace is only genuinely
``running`` from *inside* a job, so every in-flight test drives its ``stop``
from :attr:`~headspace.providers.fake.JobPlan.during`, the fake's hook for
"while the job is executing". At that moment the lifecycle state on disk really
is ``running``, the run intent in the journal really is open, and the run
invocation really is blocked in the provider holding the workspace lock — which
is the whole situation this verb was built for.

One gap these tests deliberately do not paper over
---------------------------------------------------
Criterion 2 has two halves, and only one of them is settled below this seam.
The ``stop`` verb's own package reports ``cancelled`` and exits 5 on every
backend — that is orchestration, and it is asserted here and verified live
against Docker. The *job's* recorded outcome is written by the ``run``
invocation from whatever its backend reports, and only the fake can currently
report ``cancelled``: :meth:`headspace.providers.docker.DockerProvider._status`
has no branch that produces it, so a live job ended by ``stop --apply`` is
recorded as ``failure`` with exit status 137 and its ``run`` exits 6. That is a
backend gap, not an orchestration one, and it is left visible here rather than
asserted away — the fake tests below pin the contract the Docker backend must
grow into, and none of them pretends it already has.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, NoReturn

import pytest

import headspace.cli as cli_module
from headspace.cli import main
from headspace.cli._commands import create as create_cmd
from headspace.cli._errors import (
    EXIT_CANCELLED,
    EXIT_SUCCESS,
    EXIT_USER_ERROR,
    CliError,
)
from headspace.core.result import STATUS_CANCELLED, STATUS_SUCCESS
from headspace.core.store import HOME_ENV_VAR, Store
from headspace.core.workspace import Orchestrator, exit_code_for_status
from headspace.providers.base import StopOutcome
from headspace.providers.fake import FakeProvider, JobPlan

WS = "ws-stop"
JOB = "job-under-test"
SLEEP = ("sleep", "600")


# --- doubles ----------------------------------------------------------------


class CountingFake(FakeProvider):
    """The shipped fake, plus a tally of every ``stop`` it was asked for.

    A subclass rather than a mock: the point of an assertion like
    ``stop_calls == []`` is that the *real* backend semantics were available and
    still went unused, which a stub that could not have stopped anything would
    not establish.
    """

    def __init__(self, **overrides: Any) -> None:
        super().__init__(**overrides)
        self.stop_calls: list[str] = []

    def stop(self, workspace_id: str) -> StopOutcome:
        self.stop_calls.append(workspace_id)
        return super().stop(workspace_id)


class UntouchableProvider:
    """A backend that fails the test if any verb of the seam is called at all.

    Stronger than counting ``stop``: a preview must not signal a job, and it
    must not ask the engine anything either — its answer comes from headspace's
    own records, which is what makes it inert rather than merely gentle.
    ``name`` stays a plain attribute because it is data, not a call.
    """

    name = "untouchable"

    @staticmethod
    def _refuse(verb: str) -> NoReturn:
        raise AssertionError(f"the engine was contacted: {verb}() was called")

    def capabilities(self, *args: Any, **kwargs: Any) -> NoReturn:
        self._refuse("capabilities")

    def create(self, *args: Any, **kwargs: Any) -> NoReturn:
        self._refuse("create")

    def run(self, *args: Any, **kwargs: Any) -> NoReturn:
        self._refuse("run")

    def inspect(self, *args: Any, **kwargs: Any) -> NoReturn:
        self._refuse("inspect")

    def read(self, *args: Any, **kwargs: Any) -> NoReturn:
        self._refuse("read")

    def write(self, *args: Any, **kwargs: Any) -> NoReturn:
        self._refuse("write")

    def stop(self, *args: Any, **kwargs: Any) -> NoReturn:
        self._refuse("stop")

    def remove(self, *args: Any, **kwargs: Any) -> NoReturn:
        self._refuse("remove")


class LockRefusingStore(Store):
    """A store whose lock cannot be taken — and therefore cannot be written to.

    One override buys both halves of the contract: every mutating method on
    :class:`~headspace.core.store.Store` (``write_state``, ``append_journal``,
    ``update_state``, ``delete_workspace``) acquires this lock first, so a verb
    that cannot lock cannot journal, cannot advance a lifecycle state, and
    cannot delete. Reads are untouched, because reads take no lock — which is
    exactly the asymmetry ``stop`` relies on to describe a workspace another
    invocation is holding.
    """

    def lock(  # type: ignore[override]
        self, workspace_id: str, *, blocking: bool = True
    ) -> NoReturn:
        raise AssertionError(
            f"stop took the workspace lock for {workspace_id} — it must not: "
            "run holds that lock for the whole job it is trying to interrupt"
        )


# --- fixtures and helpers ---------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_store_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every test at a throwaway store root, never the real ~/.headspace."""
    root = tmp_path / "headspace-home"
    monkeypatch.setenv(HOME_ENV_VAR, str(root))
    create_cmd.reset_providers()
    yield root
    create_cmd.reset_providers()


@pytest.fixture
def store(isolated_store_home: Path) -> Store:
    return Store()


@pytest.fixture
def fake() -> CountingFake:
    return CountingFake()


@pytest.fixture
def orchestrator(fake: CountingFake, isolated_store_home: Path) -> Orchestrator:
    """The invocation that runs the job — a plain store, so it really locks."""
    return Orchestrator(fake)


def watcher(provider: Any) -> Orchestrator:
    """A second invocation, the way an operator's ``stop`` really arrives.

    Its store refuses every lock, so this orchestrator can only perform work
    that takes none. That is the whole claim under test, applied to the
    orchestrator the test drives rather than to a comment about it.
    """
    return Orchestrator(provider, LockRefusingStore())


def files(store: Store, workspace_id: str = WS) -> dict[str, bytes]:
    """Every byte the store holds for a workspace, keyed by file name."""
    directory = store.workspace_dir(workspace_id)
    return {path.name: path.read_bytes() for path in sorted(directory.iterdir()) if path.is_file()}


def rendered(package: Any) -> str:
    """The package's prose, flattened — what a caller actually reads."""
    return "\n".join([package.outcome_summary, *package.key_findings, *package.attention])


def stoppable_plan(during: Any) -> JobPlan:
    """A job that is in flight, stoppable, and ends as ``cancelled``.

    ``during`` runs while the workspace's lifecycle state on disk is ``running``
    and its run intent is open — the only moment ``stop`` has anything to do.
    The scripted outcome is the one a real engine reports for a job an operator
    ended: no exit status of its own, and the status ``cancelled``.
    """
    return JobPlan(
        status=STATUS_CANCELLED,
        exit_status=None,
        stoppable=True,
        during=during,
    )


def subparsers() -> dict[str, Any]:
    parser = cli_module._build_parser()
    for action in parser._actions:
        if hasattr(action, "choices") and isinstance(action.choices, dict):
            return dict(action.choices)
    raise AssertionError("the top-level parser has no subparsers")


# --- criterion 1a: preview changes nothing ----------------------------------


def test_preview_names_the_job_and_touches_nothing(
    store: Store, fake: CountingFake, orchestrator: Orchestrator
) -> None:
    """The default mode reports the job and leaves engine, lock and store alone."""
    seen: dict[str, Any] = {}

    def during() -> None:
        before = files(store)
        seen["package"] = watcher(UntouchableProvider()).stop(WS)
        seen["after"] = files(store)
        seen["before"] = before

    fake.script_command(SLEEP, stoppable_plan(during))
    orchestrator.create(workspace_id=WS)
    orchestrator.run(WS, SLEEP, job_id=JOB)

    package = seen["package"]
    text = rendered(package)
    assert JOB in text
    assert "sleep 600" in text
    assert re.search(r"\d+\.\d+s", text), text
    # Reported, but not as a cancellation: nothing was cancelled.
    assert package.status == STATUS_SUCCESS
    assert exit_code_for_status(package.status) == EXIT_SUCCESS
    # Nothing was signalled, and nothing on disk moved.
    assert fake.stop_calls == []
    assert seen["before"] == seen["after"]


def test_preview_leaves_the_job_running(
    store: Store, fake: CountingFake, orchestrator: Orchestrator
) -> None:
    """A preview is repeatable: it consumes nothing, so the job survives both."""
    seen: dict[str, Any] = {}

    def during() -> None:
        watcher(UntouchableProvider()).stop(WS)
        watcher(UntouchableProvider()).stop(WS)
        # Only now is the engine asked, and it still finds the job to end.
        seen["applied"] = watcher(fake).stop(WS, apply=True)

    fake.script_command(SLEEP, stoppable_plan(during))
    orchestrator.create(workspace_id=WS)
    orchestrator.run(WS, SLEEP, job_id=JOB)

    assert seen["applied"].status == STATUS_CANCELLED
    assert fake.stop_calls == [WS]


# --- criterion 1b: --apply ends the job, the workspace returns to ready -----


def test_apply_ends_the_job_and_the_workspace_returns_to_ready(
    store: Store, fake: CountingFake, orchestrator: Orchestrator
) -> None:
    """One verb ends the job; the run that started it narrates and records it."""
    seen: dict[str, Any] = {}

    def during() -> None:
        seen["package"] = watcher(fake).stop(WS, apply=True)

    fake.script_command(SLEEP, stoppable_plan(during))
    orchestrator.create(workspace_id=WS)
    run_package = orchestrator.run(WS, SLEEP, job_id=JOB)

    assert fake.stop_calls == [WS]
    assert seen["package"].status == STATUS_CANCELLED
    assert JOB in rendered(seen["package"])

    # The run invocation observed the ending and wrote the record itself.
    assert run_package.status == STATUS_CANCELLED
    record = store.read_state(WS).state
    assert record["state"] == "ready"
    assert record["jobs"][-1]["job_id"] == JOB
    assert record["jobs"][-1]["status"] == STATUS_CANCELLED


def test_stop_never_takes_the_store_lock_in_either_mode(
    fake: CountingFake, orchestrator: Orchestrator
) -> None:
    """Both modes run to completion over a store whose lock raises on contact."""
    orchestrator.create(workspace_id=WS)

    assert watcher(UntouchableProvider()).stop(WS).status == STATUS_SUCCESS
    assert watcher(fake).stop(WS, apply=True).status == STATUS_SUCCESS


# --- criterion 2: the operator-stopped status, and its exit code ------------


def test_a_stopped_job_reports_cancelled_and_exits_five(
    fake: CountingFake, orchestrator: Orchestrator
) -> None:
    """The status is the taxonomy's operator slot, and maps to exit 5."""
    seen: dict[str, Any] = {}

    def during() -> None:
        seen["package"] = watcher(fake).stop(WS, apply=True)

    fake.script_command(SLEEP, stoppable_plan(during))
    orchestrator.create(workspace_id=WS)
    run_package = orchestrator.run(WS, SLEEP, job_id=JOB)

    for package in (seen["package"], run_package):
        assert package.status == STATUS_CANCELLED
        assert exit_code_for_status(package.status) == EXIT_CANCELLED
    assert EXIT_CANCELLED == 5


# --- nothing running is a fact, not a failure -------------------------------


def test_nothing_running_is_reported_rather_than_raised(
    fake: CountingFake, orchestrator: Orchestrator
) -> None:
    """An operator racing a job that already finished made no mistake."""
    orchestrator.create(workspace_id=WS)

    preview = watcher(UntouchableProvider()).stop(WS)
    assert preview.status == STATUS_SUCCESS
    assert "no job" in rendered(preview)
    assert fake.stop_calls == []

    applied = watcher(fake).stop(WS, apply=True)
    assert applied.status == STATUS_SUCCESS
    assert exit_code_for_status(applied.status) == EXIT_SUCCESS
    assert "no job" in rendered(applied)
    # The engine really was asked; it answered "nothing here", which is a fact.
    assert fake.stop_calls == [WS]


def test_an_unknown_workspace_is_refused_before_the_engine() -> None:
    """A workspace the store has never heard of is the caller's mistake, exit 1."""
    stopper = watcher(UntouchableProvider())
    with pytest.raises(CliError) as caught:
        stopper.stop("ws-never-existed", apply=True)
    assert caught.value.code == EXIT_USER_ERROR


# --- the CLI surface --------------------------------------------------------


def test_stop_is_registered_and_carries_the_common_flags() -> None:
    registered = subparsers()
    assert "stop" in registered
    handler = registered["stop"].get_default("func")
    assert handler is not None
    assert handler.__module__ == "headspace.cli._commands.stop"
    options = {opt for action in registered["stop"]._actions for opt in action.option_strings}
    assert {"--apply", "--json", "--provider", "--max-result-bytes"} <= options


def test_the_cli_previews_by_default_and_exits_five_on_apply(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End to end, through ``main``, against a job that is genuinely in flight.

    The stop invocations run from inside the job, so they race a ``run`` that
    holds the workspace's flock in a *different* :class:`Store` instance — a
    genuine second file description, not a re-entrant one. A ``stop`` that took
    that lock would block here rather than fail, which is precisely why the
    structural proof above uses a double; this test proves the whole CLI path
    works in the situation the double abstracts.
    """
    provider = create_cmd.provider_for("fake")
    assert isinstance(provider, FakeProvider)
    codes: dict[str, int] = {}

    def during() -> None:
        codes["preview"] = main(["stop", "--provider", "fake", WS])
        codes["apply"] = main(["stop", "--provider", "fake", WS, "--apply"])

    provider.script_command(SLEEP, stoppable_plan(during))
    assert main(["create", "--provider", "fake", "--workspace-id", WS]) == EXIT_SUCCESS
    capsys.readouterr()

    assert main(["run", "--provider", "fake", WS, *SLEEP]) == EXIT_CANCELLED
    assert codes["preview"] == EXIT_SUCCESS
    assert codes["apply"] == EXIT_CANCELLED

    body = capsys.readouterr().out
    assert "sleep 600" in body


def test_the_cli_emits_json_on_demand(capsys: pytest.CaptureFixture[str]) -> None:
    import json

    assert main(["create", "--provider", "fake", "--workspace-id", WS]) == EXIT_SUCCESS
    capsys.readouterr()
    assert main(["stop", "--provider", "fake", WS, "--json"]) == EXIT_SUCCESS
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == STATUS_SUCCESS
    assert "no job" in payload["outcome_summary"]
