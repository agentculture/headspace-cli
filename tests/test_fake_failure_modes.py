"""Pins the fake provider's two newest scripted failure modes, in isolation.

``tests/conformance.py`` will eventually hold both backends to these behaviours
(task t6) — this file is not that suite. It exists so the capability t6 needs
is testable the moment it lands, without waiting on the conformance binding or
on a live Docker engine: every test here drives :class:`FakeProvider` and
:class:`JobPlan` directly, which is also what proves the fake's new behaviour
needs no engine at all.

Written before the conformance cases existed (TDD): both new
:class:`JobPlan` constructors were designed against these tests first.
"""

from __future__ import annotations

from headspace.core import profiles
from headspace.core.result import STATUS_FAILURE, STATUS_RESOURCE_EXHAUSTED, STATUS_TIMEOUT
from headspace.providers.fake import (
    EXIT_COMMAND_NOT_EXECUTABLE,
    EXIT_COMMAND_NOT_FOUND,
    EXIT_OOM_KILLED,
    FakeProvider,
    JobPlan,
)
from tests.conformance import effective_policy

WS = "ws-failure-modes"


def _ready(provider: FakeProvider, workspace_id: str = WS) -> FakeProvider:
    provider.create(
        workspace_id, profiles.resolve(profiles.DEFAULT_PROFILE), effective_policy(provider)
    )
    return provider


# --- a command the image cannot execute -------------------------------------


def test_a_not_executable_plan_defaults_to_command_not_found() -> None:
    """The bare call is the common case: nothing exists under that name."""
    provider = _ready(FakeProvider())
    provider.script_command(("ghost",), JobPlan.not_executable())
    policy = effective_policy(provider)

    outcome = provider.run(WS, ("ghost",), policy, job_id="j1")

    assert outcome.status == STATUS_FAILURE
    assert outcome.exit_status == EXIT_COMMAND_NOT_FOUND
    assert outcome.exit_status == 127


def test_a_not_executable_plan_can_script_found_but_not_executable() -> None:
    """A directory, or a file missing its execute bit: found, but not runnable."""
    provider = _ready(FakeProvider())
    provider.script_command(
        ("no-x-bit",), JobPlan.not_executable(exit_status=EXIT_COMMAND_NOT_EXECUTABLE)
    )
    policy = effective_policy(provider)

    outcome = provider.run(WS, ("no-x-bit",), policy, job_id="j1")

    assert outcome.status == STATUS_FAILURE
    assert outcome.exit_status == EXIT_COMMAND_NOT_EXECUTABLE
    assert outcome.exit_status == 126


def test_a_not_executable_plan_is_never_reported_as_infrastructure_failure() -> None:
    """The caller's mistake must never masquerade as a broken engine (NFR-07)."""
    provider = _ready(FakeProvider())
    provider.script_command(("ghost",), JobPlan.not_executable())
    policy = effective_policy(provider)

    outcome = provider.run(WS, ("ghost",), policy, job_id="j1")

    # A run() that raised ProviderError would never reach this assertion at
    # all; reaching it is itself part of what is being pinned.
    assert outcome.status != "infrastructure_failure"


def test_a_not_executable_plan_carries_the_report_a_test_scripts() -> None:
    provider = _ready(FakeProvider())
    report = "headspace: the command was not run — no such executable 'ghost'\n"
    provider.script_command(("ghost",), JobPlan.not_executable(output=report))
    policy = effective_policy(provider)

    outcome = provider.run(WS, ("ghost",), policy, job_id="j1")

    assert outcome.output == report


# --- an OOM kill -------------------------------------------------------------


def test_an_oom_killed_plan_reports_resource_exhausted() -> None:
    provider = _ready(FakeProvider())
    provider.script_command(("hog",), JobPlan.oom_killed())
    policy = effective_policy(provider)

    outcome = provider.run(WS, ("hog",), policy, job_id="j1")

    assert outcome.status == STATUS_RESOURCE_EXHAUSTED
    assert outcome.exit_status == EXIT_OOM_KILLED
    assert outcome.exit_status == 137


def test_an_oom_killed_plan_accepts_an_explicit_exit_status() -> None:
    provider = _ready(FakeProvider())
    provider.script_command(("hog",), JobPlan.oom_killed(exit_status=9))
    policy = effective_policy(provider)

    outcome = provider.run(WS, ("hog",), policy, job_id="j1")

    assert outcome.status == STATUS_RESOURCE_EXHAUSTED
    assert outcome.exit_status == 9


def test_a_wall_clock_kill_outranks_a_scripted_oom_kill() -> None:
    """Mirrors docker.py: headspace stopping a job on purpose outranks the kernel.

    A plan can be scripted as *both* an OOM kill and one that outruns its
    budget — the same way a real container can be memory-pressed and also
    outrun its wall clock. The wall-clock enforcer must still win, exactly as
    task t3 requires of the Docker provider's own ``_status()``.
    """
    provider = _ready(FakeProvider())
    provider.script_command(("hog",), JobPlan.oom_killed(wall_time_seconds=30.0))
    policy = effective_policy(provider, wall_clock_seconds=1)

    outcome = provider.run(WS, ("hog",), policy, job_id="j1")

    assert outcome.status == STATUS_TIMEOUT
    assert outcome.exit_status is None


def test_an_oom_killed_plan_is_never_reported_as_infrastructure_failure() -> None:
    provider = _ready(FakeProvider())
    provider.script_command(("hog",), JobPlan.oom_killed())
    policy = effective_policy(provider)

    outcome = provider.run(WS, ("hog",), policy, job_id="j1")

    assert outcome.status != "infrastructure_failure"


def test_an_oom_killed_plan_carries_the_output_a_test_scripts() -> None:
    provider = _ready(FakeProvider())
    provider.script_command(("hog",), JobPlan.oom_killed(output="allocated until killed\n"))
    policy = effective_policy(provider)

    outcome = provider.run(WS, ("hog",), policy, job_id="j1")

    assert outcome.output == "allocated until killed\n"
