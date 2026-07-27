"""Binds the in-memory fake to the conformance suite, and guards the seam itself.

Two jobs, deliberately in one file:

1. **The binding.** :class:`TestFakeProviderConformance` is the reference
   implementation of the mechanism ``tests/conformance.py`` documents — a
   ``Test*`` class inheriting :class:`ProviderConformance` and overriding the
   single ``provider_case`` fixture. The Docker provider task copies this shape
   and changes only the fixture body.
2. **The guards the suite cannot express.** The conformance suite is
   backend-agnostic by construction, so it cannot assert anything about
   *headspace's own* provider modules: that they never import the docker SDK,
   that the fake touches no filesystem or network, that the removal path table
   still matches ``headspace/core/states.py``. Those live here, next to the
   only implementation they describe.

Written before ``headspace/providers/`` existed (TDD): the first run failed at
import, which is the intended failure — the interface did not exist yet.
"""

from __future__ import annotations

import ast
import dataclasses
import itertools
from collections.abc import Sequence
from pathlib import Path

import pytest

from headspace.cli._errors import (
    EXIT_INFRASTRUCTURE_FAILURE,
    EXIT_USER_ERROR,
    CliError,
)
from headspace.core import profiles
from headspace.core.policy import CapabilitySnapshot
from headspace.core.result import (
    STATUS_INFRASTRUCTURE_FAILURE,
    STATUS_POLICY_DENIED,
    STATUS_SUCCESS,
    STATUS_TIMEOUT,
    ResourceUsage,
)
from headspace.core.states import TRANSITIONS, State, validate_transition
from headspace.providers.base import (
    ENVIRONMENT_DIGEST_RE,
    REMOVABLE_RESOURCES,
    REMOVAL_PATHS,
    JobOutcome,
    OpaqueRef,
    Provider,
    ProviderError,
    RemovalDisposition,
    WorkspaceDescriptor,
    environment_digest,
    guard_removable,
    requested_limit,
    require_command,
)
from headspace.providers.fake import FakeProvider, JobPlan
from tests.conformance import (
    ProviderCase,
    ProviderConformance,
    effective_policy,
    key_words,
    walk_structure,
)

PROVIDERS_DIR = Path(__file__).resolve().parents[1] / "headspace" / "providers"

# Commands the conformance binding scripts on the fake. They are opaque tokens
# to the fake — it looks them up in its script table — which is exactly how a
# real backend treats an argv it hands to an engine.
SUCCEEDING = ("headspace-echo", "conformance-ok")
FAILING = ("headspace-exit", "7")
SLOW = ("headspace-sleep", "forever")
FLOODING = ("headspace-flood",)
ECHO_TEXT = "conformance-ok"
FLOOD_BUDGET = 4096


def _scripted(provider: FakeProvider) -> FakeProvider:
    provider.script_command(SUCCEEDING, JobPlan.succeeding(output=f"{ECHO_TEXT}\n"))
    provider.script_command(FAILING, JobPlan.failing(exit_status=7, output="boom\n"))
    provider.script_command(SLOW, JobPlan.timing_out())
    # A genuine over-budget capture: the fake really truncates this string, so
    # the conformance assertion exercises the same code path a live engine does.
    provider.script_command(FLOODING, JobPlan.flooding("headspace " * 8192))
    return provider


def _case_for(provider: FakeProvider) -> ProviderCase:
    return ProviderCase(
        provider=_scripted(provider),
        environment=profiles.resolve(profiles.DEFAULT_PROFILE),
        succeeding_command=SUCCEEDING,
        echo_text=ECHO_TEXT,
        failing_command=FAILING,
        failing_exit_status=7,
        slow_command=SLOW,
        slow_seconds=1,
        flooding_command=FLOODING,
        flood_budget_bytes=FLOOD_BUDGET,
        break_engine=lambda: provider.break_next("run"),
        workspace_prefix="conf-fake",
    )


class TestFakeProviderConformance(ProviderConformance):
    """The whole suite, bound to the in-memory fake. No daemon, no network."""

    @pytest.fixture
    def provider_case(self) -> ProviderCase:
        return _case_for(FakeProvider())


# --- the abstraction holds: no docker anywhere it must not be ---------------


def _imported_roots(path: Path) -> set[str]:
    """Every top-level module name ``path`` imports, however it imports it."""
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


def test_shared_provider_modules_never_import_the_docker_sdk() -> None:
    """The seam and the fake must build without the SDK — CI has no daemon.

    A backend module named after its engine (``docker.py``) is the one place
    the SDK may appear; everything else importing it would make the whole
    package, and therefore every unit test, depend on a live engine.
    """
    checked = 0
    for module in sorted(PROVIDERS_DIR.glob("*.py")):
        if module.stem == "docker":
            continue
        checked += 1
        assert "docker" not in _imported_roots(module), f"{module.name} imports the docker SDK"
    assert checked >= 3, "expected __init__.py, base.py and fake.py to exist"


def test_importing_the_providers_package_pulls_in_no_backend() -> None:
    """``import headspace.providers`` must not drag a backend (or its SDK) in."""
    source = (PROVIDERS_DIR / "__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module and node.module != "__future__"
    }
    assert imported <= {"headspace.providers.base"}, (
        "headspace/providers/__init__.py may re-export the seam only; backends are "
        f"imported from their own modules, found {sorted(imported)}"
    )


def test_the_fake_touches_no_filesystem_network_or_clock_of_the_host() -> None:
    """ "Runnable with no daemon" is only true if the fake really is in-memory."""
    forbidden = {
        "asyncio",
        "http",
        "io",
        "os",
        "pathlib",
        "requests",
        "shutil",
        "socket",
        "ssl",
        "subprocess",
        "tempfile",
        "threading",
        "time",
        "urllib",
    }
    leaked = _imported_roots(PROVIDERS_DIR / "fake.py") & forbidden
    assert not leaked, f"the fake imports host-touching modules: {sorted(leaked)}"


# --- the neutrality scanner has teeth --------------------------------------
#
# Acceptance criterion 2 is only as good as the scanner enforcing it. A scanner
# that never fires would let the Docker provider ship any leak at all, so these
# feed it structures a leaky backend would plausibly return and assert it
# objects. Without them, "no provider-specific field appears" is a test that
# passes because it checks nothing.


@pytest.mark.parametrize(
    "leak",
    [
        {"container_id": "abc"},
        {"ContainerId": "abc"},
        {"HostConfig": {"Memory": 1}},
        {"host_config": {"memory": 1}},
        {"image": "python"},
        {"labels": {"a": "b"}},
        {"volume_name": "vol"},
        {"docker_socket": "/x"},
        {"mounts": []},
        {"NetworkMode": "none"},
        {"where": "/var/run/docker.sock"},
        {"where": "unix:///var/run/docker.sock"},
        {"note": "cgroup v2 driver"},
        {"handle": "a" * 64},
        {"digest": "sha256:" + "b" * 64},
    ],
)
def test_the_scanner_rejects_a_structure_a_leaky_backend_would_return(
    leak: dict[str, object],
) -> None:
    assert walk_structure(leak, "leaky").violations


def test_the_scanner_rejects_a_leaf_that_is_not_a_json_primitive() -> None:
    """A docker SDK object handed back verbatim cannot survive the walk."""

    class EngineObject:
        pass

    walk = walk_structure({"thing": EngineObject()}, "leaky")
    assert any("non-JSON type" in violation for violation in walk.violations)


def test_the_scanner_accepts_the_neutral_shapes_the_seam_actually_uses() -> None:
    neutral = {
        "workspace_id": "ws-1",
        "provider": "docker",  # naming a backend is not leaking one
        "state": "ready",
        "environment_digest": "sha256:" + "c" * 64,
        "truncated": False,  # must not trip the 'runc' rule
        "network_enabled": False,
        "removed": ["runtime", "storage"],
        "ref": {"opaque": "4f2a" * 16},
    }
    walk = walk_structure(neutral, "neutral")
    assert not walk.violations
    assert walk.opaque == ["4f2a" * 16]


def test_the_scanner_does_not_police_the_caller_s_own_job_output() -> None:
    """A job that prints a socket path leaked nothing — it printed its own text."""
    payload = {"output": "connecting to /var/run/docker.sock ... " + "d" * 64}
    assert not walk_structure(payload, "run").violations
    # ...but the same string in a field headspace fills in is still a leak.
    assert walk_structure({"detail": "/var/run/docker.sock"}, "run").violations


def test_the_conformance_suite_itself_fails_a_provider_that_leaks() -> None:
    """The teeth are in the *suite*, not only in the helper it calls.

    :class:`~headspace.providers.base.WorkspaceDescriptor` is frozen with a
    fixed field set, so a backend cannot add a leaky field — the remaining way
    to leak is to echo the opaque handle into a field callers do read. This
    builds exactly that provider and drives the real conformance test method
    over it, asserting the suite objects.
    """

    class EchoingProvider(FakeProvider):
        def _describe(self, record: object) -> WorkspaceDescriptor:
            described = super()._describe(record)  # type: ignore[arg-type]
            return dataclasses.replace(described, created_at=described.ref.token)

    provider = EchoingProvider()
    case = _case_for(provider)
    counter = itertools.count()

    def make(policy: object = None) -> WorkspaceDescriptor:
        return provider.create(
            f"leaky-{next(counter)}", case.environment, effective_policy(provider)
        )

    suite = ProviderConformance()
    with pytest.raises(AssertionError, match="opaque backend handle"):
        suite.test_returned_structures_are_backend_neutral(provider, case, make)


def test_key_words_splits_the_naming_styles_a_backend_might_use() -> None:
    assert key_words("HostConfig") == {"host", "config"}
    assert key_words("container_id") == {"container", "id"}
    assert key_words("truncated") == {"truncated"}


# --- the removal path table still matches the lifecycle table ---------------


def test_every_removal_path_is_legal_under_the_state_table() -> None:
    """``remove`` never invents an edge; it walks ones states.py already allows."""
    for start, path in REMOVAL_PATHS.items():
        current = start
        for target in path:
            validate_transition(current, target)  # raises if the table disagrees
            current = target
        assert current is State.DESTROYED


def test_running_and_destroyed_have_no_removal_path() -> None:
    """The two deliberate omissions, asserted so a later edit cannot smuggle one in."""
    assert State.RUNNING not in REMOVAL_PATHS
    assert State.DESTROYED not in REMOVAL_PATHS
    assert State.DESTROYED not in TRANSITIONS[State.RUNNING]


def test_guard_removable_refuses_while_a_job_is_in_flight() -> None:
    with pytest.raises(CliError) as caught:
        guard_removable(State.READY, active_jobs=1)
    assert caught.value.code == EXIT_USER_ERROR
    assert "'running' -> 'destroyed'" in caught.value.message


def test_guard_removable_refuses_an_already_destroyed_workspace() -> None:
    with pytest.raises(CliError):
        guard_removable(State.DESTROYED, active_jobs=0)


def test_destroy_during_a_running_job_takes_the_documented_path() -> None:
    """The documented path is "refuse", and the refusal comes from the table."""
    provider = FakeProvider()
    descriptor = provider.create(
        "ws-busy", profiles.resolve(profiles.DEFAULT_PROFILE), effective_policy(provider)
    )
    refusals: list[CliError] = []

    def destroy_mid_job() -> None:
        try:
            provider.remove(descriptor.workspace_id)
        except CliError as err:
            refusals.append(err)

    provider.script_command(("busy",), JobPlan.succeeding(during=destroy_mid_job))
    outcome = provider.run("ws-busy", ("busy",), effective_policy(provider), job_id="j1")

    assert outcome.status == STATUS_SUCCESS
    assert len(refusals) == 1
    assert "'running' -> 'destroyed'" in refusals[0].message
    # Refused means removed nothing: the workspace survives the attempt.
    assert provider.inspect("ws-busy").state is State.READY


# --- the failure taxonomy is structural, not a convention ------------------


@pytest.mark.parametrize("status", [STATUS_INFRASTRUCTURE_FAILURE, STATUS_POLICY_DENIED])
def test_a_job_outcome_cannot_claim_a_raised_status(status: str) -> None:
    """Infrastructure failure and policy denial are raised, never returned."""
    with pytest.raises(CliError) as caught:
        JobOutcome(
            job_id="j",
            workspace_id="w",
            status=status,
            exit_status=1,
            output="",
            truncated=False,
            started_at="t0",
            finished_at="t1",
            usage=ResourceUsage(),
        )
    assert caught.value.code == EXIT_USER_ERROR
    assert "raised" in caught.value.remediation


@pytest.mark.parametrize(
    "status,exit_status",
    [
        (STATUS_SUCCESS, 3),  # success with a non-zero status is a contradiction
        ("failure", 0),  # failure with a zero status likewise
        (STATUS_TIMEOUT, 0),  # a killed job never produced an exit status
        (STATUS_SUCCESS, None),  # a completed job always did
    ],
)
def test_a_job_outcome_rejects_an_inconsistent_exit_status(
    status: str, exit_status: int | None
) -> None:
    with pytest.raises(CliError):
        JobOutcome(
            job_id="j",
            workspace_id="w",
            status=status,
            exit_status=exit_status,
            output="",
            truncated=False,
            started_at="t0",
            finished_at="t1",
            usage=ResourceUsage(),
        )


def test_a_job_outcome_cannot_report_less_volume_than_it_captured() -> None:
    """Captured output can never exceed the volume the job actually produced."""
    with pytest.raises(CliError):
        JobOutcome(
            job_id="j",
            workspace_id="w",
            status=STATUS_SUCCESS,
            exit_status=0,
            output="twelve chars",
            truncated=False,
            started_at="t0",
            finished_at="t1",
            usage=ResourceUsage(output_bytes=3),
        )


def test_provider_error_is_a_cli_error_in_the_infrastructure_slot() -> None:
    err = ProviderError("engine unreachable")
    assert isinstance(err, CliError)
    assert err.code == EXIT_INFRASTRUCTURE_FAILURE
    assert err.category == "infrastructure_failure"
    assert err.remediation


@pytest.mark.parametrize("operation", ["capabilities", "create", "inspect", "remove"])
def test_every_verb_reports_engine_breakage_as_infrastructure_failure(operation: str) -> None:
    provider = FakeProvider()
    policy = effective_policy(provider)
    provider.create("ws-break", profiles.resolve(profiles.DEFAULT_PROFILE), policy)
    calls = {
        "capabilities": lambda: provider.capabilities(),
        "create": lambda: provider.create("ws-break-2", "env", policy),
        "inspect": lambda: provider.inspect("ws-break"),
        "remove": lambda: provider.remove("ws-break"),
    }
    provider.break_next(operation)
    with pytest.raises(ProviderError) as caught:
        calls[operation]()
    assert caught.value.code == EXIT_INFRASTRUCTURE_FAILURE


# --- removal disposition vocabulary ----------------------------------------


def test_a_disposition_rejects_a_resource_outside_the_closed_vocabulary() -> None:
    with pytest.raises(CliError) as caught:
        RemovalDisposition(workspace_id="w", removed=("container",))
    assert "runtime" in caught.value.remediation


def test_a_disposition_rejects_a_resource_counted_twice() -> None:
    with pytest.raises(CliError):
        RemovalDisposition(workspace_id="w", removed=("storage",), retained=("storage",))


def test_a_disposition_partitions_the_resources_it_names() -> None:
    disposition = RemovalDisposition(
        workspace_id="w", removed=("runtime",), unverified=("storage",)
    )
    assert set(disposition.removed + disposition.unverified) == set(REMOVABLE_RESOURCES)


# --- environment identity ---------------------------------------------------


def test_a_pinned_reference_yields_the_digest_it_was_pinned_to() -> None:
    reference = profiles.resolve(profiles.DEFAULT_PROFILE)
    digest = environment_digest(reference)
    assert reference.endswith(f"@{digest}")
    assert ENVIRONMENT_DIGEST_RE.match(digest)


def test_an_unpinned_reference_still_yields_a_stable_content_address() -> None:
    first = environment_digest("some-runtime:v3")
    assert first == environment_digest("some-runtime:v3")
    assert first != environment_digest("some-runtime:v4")
    assert ENVIRONMENT_DIGEST_RE.match(first)


# --- opaque handles ---------------------------------------------------------


def test_an_opaque_ref_does_not_reveal_its_token_when_rendered() -> None:
    ref = OpaqueRef("container-4f2a")
    assert "container-4f2a" not in repr(ref)
    assert "container-4f2a" not in str(ref)
    # It still round-trips, because a backend needs it back next invocation.
    assert OpaqueRef.from_dict(ref.to_dict()) == ref


# --- fake-specific behaviour the suite deliberately does not cover ---------


def test_the_fake_satisfies_the_protocol_and_names_itself() -> None:
    provider = FakeProvider()
    assert isinstance(provider, Provider)
    assert provider.name == "fake"


def test_the_fake_reports_the_capability_snapshot_it_was_given() -> None:
    snapshot = CapabilitySnapshot(engine="fake", api_version="9.9", storage_enforceable=True)
    provider = FakeProvider(capabilities=snapshot)
    assert provider.capabilities() == snapshot
    assert provider.capabilities().storage_enforceable is True


def test_an_unscripted_command_succeeds_quietly() -> None:
    """The default plan is a silent success, so tests script only what they assert."""
    provider = FakeProvider()
    policy = effective_policy(provider)
    provider.create("ws-default", "env", policy)
    outcome = provider.run("ws-default", ("anything",), policy, job_id="j")
    assert outcome.status == STATUS_SUCCESS
    assert outcome.output == ""
    assert outcome.usage.output_bytes == 0


def test_the_fake_truncates_capture_at_the_budget_and_reports_the_true_volume() -> None:
    provider = FakeProvider()
    provider.script_command(("flood",), JobPlan.flooding("x" * 10_000))
    policy = effective_policy(provider, output_bytes=1000)
    provider.create("ws-flood", "env", policy)
    outcome = provider.run("ws-flood", ("flood",), policy, job_id="j")
    assert len(outcome.output.encode("utf-8")) == 1000
    assert outcome.truncated is True
    assert outcome.usage.output_bytes == 10_000


def test_the_fake_times_out_relative_to_the_budget_it_is_given() -> None:
    """Timeout is derived from the policy, never from a wall clock the fake reads."""
    provider = FakeProvider()
    provider.script_command(("slow",), JobPlan(wall_time_seconds=30.0))
    generous = effective_policy(provider, wall_clock_seconds=60)
    strict = effective_policy(provider, wall_clock_seconds=5)
    provider.create("ws-clock", "env", generous)

    assert provider.run("ws-clock", ("slow",), generous, job_id="a").status == STATUS_SUCCESS
    timed_out = provider.run("ws-clock", ("slow",), strict, job_id="b")
    assert timed_out.status == STATUS_TIMEOUT
    assert timed_out.exit_status is None
    assert timed_out.usage.wall_time_seconds == 5.0


def test_a_scripted_infrastructure_failure_raises_rather_than_returning() -> None:
    provider = FakeProvider()
    provider.script_command(("broken",), JobPlan.breaking("volume driver offline"))
    policy = effective_policy(provider)
    provider.create("ws-infra", "env", policy)
    with pytest.raises(ProviderError) as caught:
        provider.run("ws-infra", ("broken",), policy, job_id="j")
    assert "volume driver offline" in caught.value.message


def test_the_fake_accumulates_workspace_storage_across_jobs() -> None:
    provider = FakeProvider()
    provider.script_command(("write",), JobPlan.succeeding(storage_bytes=2048))
    policy = effective_policy(provider)
    provider.create("ws-store", "env", policy)
    provider.run("ws-store", ("write",), policy, job_id="a")
    provider.run("ws-store", ("write",), policy, job_id="b")
    assert provider.inspect("ws-store").storage_bytes == 4096


def test_the_fake_refuses_a_workspace_id_it_cannot_name() -> None:
    provider = FakeProvider()
    with pytest.raises(CliError) as caught:
        provider.create("  ", "env", effective_policy(provider))
    assert caught.value.code == EXIT_USER_ERROR


def test_a_descriptor_refuses_an_engine_id_dressed_up_as_a_content_digest() -> None:
    """The one structured field a backend fills in is validated, not trusted."""
    with pytest.raises(CliError) as caught:
        WorkspaceDescriptor(
            workspace_id="w",
            provider="p",
            state=State.READY,
            environment_digest="a" * 64,  # a bare engine object id
            created_at="t0",
        )
    assert "content address" in caught.value.message


def test_a_descriptor_refuses_a_stored_field_this_build_cannot_model() -> None:
    """Fail closed on unknown state, exactly as the store does for its schema."""
    payload = WorkspaceDescriptor(
        workspace_id="w", provider="p", state=State.READY, environment_digest="", created_at="t0"
    ).to_dict()
    with pytest.raises(CliError) as caught:
        WorkspaceDescriptor.from_dict({**payload, "container_id": "abc"})
    assert "container_id" in caught.value.message


def test_a_descriptor_refuses_a_malformed_stored_record() -> None:
    with pytest.raises(CliError) as caught:
        WorkspaceDescriptor.from_dict({"workspace_id": "w"})
    assert "malformed" in caught.value.message


def test_a_command_that_is_not_argv_at_all_is_refused() -> None:
    """A bare string is the classic shell-injection shape; it is not an argv."""
    with pytest.raises(CliError) as caught:
        require_command("rm -rf /")  # type: ignore[arg-type]
    assert caught.value.code == EXIT_USER_ERROR
    with pytest.raises(CliError):
        require_command(("ok", 7))  # type: ignore[arg-type]


def test_only_known_operations_can_be_broken() -> None:
    provider = FakeProvider()
    with pytest.raises(CliError) as caught:
        provider.break_next("teleport")
    assert "capabilities" in caught.value.remediation


def test_requested_limit_names_the_limit_it_could_not_find() -> None:
    provider = FakeProvider()
    policy = effective_policy(provider)
    assert requested_limit(policy, "output_bytes") == 10 * 1024 * 1024
    with pytest.raises(CliError) as caught:
        requested_limit(policy, "warp_drive")
    assert "warp_drive" in caught.value.message


def test_a_command_must_be_a_sequence_of_strings() -> None:
    provider = FakeProvider()
    policy = effective_policy(provider)
    provider.create("ws-argv", "env", policy)
    bad: Sequence[str] = ()
    with pytest.raises(CliError) as caught:
        provider.run("ws-argv", bad, policy, job_id="j")
    assert caught.value.code == EXIT_USER_ERROR
