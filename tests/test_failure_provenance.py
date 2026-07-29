"""Provenance invariants for the widened failure taxonomy (qodo review, PR #8).

Every test here defends the same principle from a different side: a failure
report must be grounded in what headspace *observed*, never inferred from a
number that more than one situation can produce. The taxonomy exists to tell a
caller what to do next, and a confident report about the wrong thing is worse
than a vague one about the right thing.

The four cases, and the review finding each answers:

1. ``exit status 126/127 means the caller's argv[0] was refused`` is an
   inference, not an observation: a command that *ran* can return either status
   on its own account. ``/bin/sh -c missing-tool`` runs the shell perfectly and
   returns 127 because the tool the shell looked for is absent.
2. ``resource_exhausted`` is the only job status with no coherence rule, so a
   provider could report a memory kill that exited 0.
3. The synthesized refusal report bypasses ``_captured`` and so escapes the
   caller's declared output budget.
4. The persisted job record labels an OOM measurement a sampled *peak* while
   the rendered package calls the same number a sampled *floor*.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from headspace.cli._errors import CliError
from headspace.core.policy import Policy, ResourceBudget
from headspace.core.result import (
    MEMORY_BASIS_SAMPLED_FLOOR,
    MEMORY_BASIS_SAMPLED_PEAK,
    STATUS_FAILURE,
    STATUS_RESOURCE_EXHAUSTED,
    ResourceUsage,
    ResultPackage,
)
from headspace.core.store import HOME_ENV_VAR, Store
from headspace.core.workspace import Orchestrator
from headspace.providers.base import JobOutcome
from headspace.providers.fake import EXIT_COMMAND_NOT_EXECUTABLE, FakeProvider, JobPlan


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Store:
    """A throwaway store root, never the real ``~/.headspace``."""
    monkeypatch.setenv(HOME_ENV_VAR, str(tmp_path / "headspace-home"))
    return Store()


def _findings(plan: JobPlan, command: tuple[str, ...], store: Store) -> str:
    """The key findings one scripted job produces, through the real orchestrator."""
    orchestrator = Orchestrator(FakeProvider(script={command: plan}), store)
    orchestrator.create(
        workspace_id="ws-provenance",
        policy=Policy(budget=ResourceBudget(memory_bytes=134217728)),
    )
    package: ResultPackage = orchestrator.run("ws-provenance", command)
    return " ".join(package.key_findings)


def _outcome(**overrides: object) -> JobOutcome:
    base: dict[str, object] = {
        "job_id": "job-provenance",
        "workspace_id": "ws-provenance",
        "status": STATUS_FAILURE,
        "exit_status": 127,
        "output": "",
        "truncated": False,
        "started_at": "2026-07-29T00:00:00+00:00",
        "finished_at": "2026-07-29T00:00:01+00:00",
    }
    base.update(overrides)
    return JobOutcome(**base)  # type: ignore[arg-type]


# --- 1. a status the command chose is not a refusal headspace observed -------


def test_a_command_that_ran_and_returned_127_is_not_reported_as_a_missing_argv0(
    store: Store,
) -> None:
    """The bug qodo found: ``/bin/sh -c missing-tool`` blamed ``/bin/sh``.

    The shell was found, executed, and did its job. 127 came from the shell,
    about a *different* name. Naming argv[0] here sends the caller to fix an
    executable that was never the problem.
    """
    command = ("/bin/sh", "-c", "missing-tool")
    joined = _findings(JobPlan.failing(exit_status=127), command, store)
    assert "/bin/sh" not in joined, f"blamed argv[0] for a status the command chose: {joined}"
    assert "could not run" not in joined
    assert "127" in joined


def test_a_refusal_the_provider_observed_still_names_the_profile_and_argv0(
    store: Store,
) -> None:
    """The honest case must keep working — this is what makes the fix a narrowing."""
    command = ("definitely-not-a-binary",)
    joined = _findings(JobPlan.not_executable(), command, store)
    assert "definitely-not-a-binary" in joined
    assert "could not run" in joined


def test_a_refused_non_executable_is_distinguished_from_a_refused_absent_command(
    store: Store,
) -> None:
    command = ("/etc/hostname",)
    joined = _findings(
        JobPlan.not_executable(exit_status=EXIT_COMMAND_NOT_EXECUTABLE), command, store
    )
    assert "not executable" in joined
    assert "/etc/hostname" in joined


# --- 2. resource_exhausted needs a coherence rule like every other status ----


def test_an_exhausted_job_cannot_report_that_it_exited_zero() -> None:
    """A kill that reports success is a contradiction the model must refuse."""
    with pytest.raises(CliError) as exc:
        _outcome(status=STATUS_RESOURCE_EXHAUSTED, exit_status=0)
    assert "0" in exc.value.message
    assert exc.value.remediation


def test_an_exhausted_job_with_the_kill_status_is_accepted() -> None:
    outcome = _outcome(status=STATUS_RESOURCE_EXHAUSTED, exit_status=137)
    assert outcome.status == STATUS_RESOURCE_EXHAUSTED


def test_the_fake_cannot_script_an_exhausted_job_that_exited_zero() -> None:
    """The gap qodo reached through: the fake's override made it constructible."""
    from headspace.providers.fake import JobPlan

    plan = JobPlan.oom_killed(exit_status=0)
    with pytest.raises(CliError):
        _outcome(status=plan.status, exit_status=plan.exit_status)


# --- 3. a synthesized report is output, and obeys the output budget ---------


def test_a_refusal_report_over_the_budget_is_clipped_and_says_so() -> None:
    """`JobOutcome.output` is contractually bounded before it is ever persisted.

    The refusal report is synthesized rather than captured, so it bypasses
    ``_captured`` — but a caller who declared a small output budget declared it
    for all of the job's output, not just the parts that came from a container.
    """
    from headspace.providers.docker import _RefusedCommand

    refused = _RefusedCommand(exit_status=127, report="x" * 500)
    output, produced, truncated = refused.bounded(100)

    assert len(output.encode("utf-8")) <= 100
    assert produced == 500, "the volume really produced must survive the clipping"
    assert truncated is True


def test_a_refusal_report_inside_the_budget_is_kept_whole() -> None:
    from headspace.providers.docker import _RefusedCommand

    refused = _RefusedCommand(exit_status=126, report="short report")
    output, produced, truncated = refused.bounded(4096)

    assert output == "short report"
    assert produced == len("short report")
    assert truncated is False


def test_clipping_never_splits_a_multibyte_character() -> None:
    from headspace.providers.docker import _RefusedCommand

    # 'é' is two bytes; a budget landing mid-character must not emit a partial one.
    refused = _RefusedCommand(exit_status=127, report="é" * 10)
    output, _, truncated = refused.bounded(5)

    assert truncated is True
    output.encode("utf-8").decode("utf-8")  # would raise if a character were halved
    assert "�" not in output


# --- 4. the stored record and the rendered package must agree ---------------


def test_a_persisted_exhausted_outcome_labels_its_memory_a_floor() -> None:
    """The audit record must not contradict the result the caller was shown."""
    record = _outcome(
        status=STATUS_RESOURCE_EXHAUSTED,
        exit_status=137,
        usage=ResourceUsage(max_memory_bytes=5320704),
    ).to_dict()
    assert record["usage"]["max_memory_basis"] == MEMORY_BASIS_SAMPLED_FLOOR


def test_a_persisted_ordinary_outcome_still_labels_its_memory_a_peak() -> None:
    record = _outcome(usage=ResourceUsage(max_memory_bytes=4820992)).to_dict()
    assert record["usage"]["max_memory_basis"] == MEMORY_BASIS_SAMPLED_PEAK
