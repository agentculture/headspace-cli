"""Tests for the policy and budget model (headspace.core.policy).

Each test traces to one of this task's two acceptance criteria:

1. Policy objects declare network, filesystem, and resource budgets; the
   effective-policy view labels every limit enforced or measured.
2. Requesting a policy the capability snapshot cannot satisfy raises a
   policy error before any job runs.
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

import pytest

from headspace.core import policy

# --- helpers ----------------------------------------------------------------


def _capable_snapshot(**overrides: object) -> policy.CapabilitySnapshot:
    """A snapshot where the engine can enforce everything asked of it, except
    storage on the local volume driver -- the canonical measured case."""
    fields: dict[str, object] = {
        "engine": "docker",
        "api_version": "1.52",
        "network_disable_supported": True,
        "filesystem_scope_enforceable": True,
        "memory_enforceable": True,
        "cpu_enforceable": True,
        "pids_enforceable": True,
        "storage_enforceable": False,
    }
    fields.update(overrides)
    return policy.CapabilitySnapshot(**fields)  # type: ignore[arg-type]


# --- Policy declares network, filesystem, and resource budget ---------------


def test_policy_defaults_are_closed() -> None:
    p = policy.Policy()
    assert p.network is policy.NetworkPosture.DISABLED
    assert p.filesystem.host_paths == ()
    # Closed does not mean unset: every budget field is a small, explicit
    # ceiling, never None / unlimited.
    for value in (
        p.budget.memory_bytes,
        p.budget.cpu_limit,
        p.budget.pids_limit,
        p.budget.storage_bytes,
        p.budget.wall_clock_seconds,
        p.budget.output_bytes,
        p.budget.concurrency,
    ):
        assert value is not None
        assert value > 0


def test_policy_and_children_are_immutable() -> None:
    p = policy.Policy()
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.network = policy.NetworkPosture.ENABLED  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.budget.memory_bytes = 1  # type: ignore[misc]


def test_capability_snapshot_field_shape_is_stable() -> None:
    """Locks the exact field set the Docker provider (a later task) must
    produce -- this module defines the shape; the provider fills it in."""
    names = {f.name for f in dataclasses.fields(policy.CapabilitySnapshot)}
    assert names == {
        "engine",
        "api_version",
        "network_disable_supported",
        "filesystem_scope_enforceable",
        "memory_enforceable",
        "cpu_enforceable",
        "pids_enforceable",
        "storage_enforceable",
    }


def test_policy_module_never_imports_a_provider_or_docker_sdk() -> None:
    """headspace/core/__init__.py's layering rule: core never imports a
    provider or the docker SDK. CapabilitySnapshot is pure data handed to
    this module -- enforced here at the source level so the rule can't
    silently regress as the module grows."""
    source = Path(policy.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden = {
        name
        for name in imported
        if name == "docker" or name.startswith("docker.") or name.startswith("headspace.providers")
    }
    assert not forbidden


# --- effective-policy view: every limit is labelled enforced or measured ----


def test_effective_policy_labels_every_limit() -> None:
    effective = policy.resolve(policy.Policy(), _capable_snapshot())
    seen = {limit.name for limit in effective}
    assert seen == set(policy.LIMIT_NAMES)
    for limit in effective:
        assert limit.status in (policy.LimitStatus.ENFORCED, policy.LimitStatus.MEASURED)


def test_effective_policy_is_iterable_and_sized() -> None:
    effective = policy.resolve(policy.Policy(), _capable_snapshot())
    assert len(effective) == len(policy.LIMIT_NAMES)
    assert len(list(effective)) == len(policy.LIMIT_NAMES)


def test_storage_is_measured_on_default_local_volume_driver() -> None:
    """The canonical measured case named in the task design."""
    effective = policy.resolve(policy.Policy(), _capable_snapshot(storage_enforceable=False))
    assert effective.status_of("storage") is policy.LimitStatus.MEASURED


def test_storage_is_enforced_when_the_driver_supports_a_quota() -> None:
    effective = policy.resolve(policy.Policy(), _capable_snapshot(storage_enforceable=True))
    assert effective.status_of("storage") is policy.LimitStatus.ENFORCED


def test_engine_backed_limits_are_enforced_when_the_snapshot_supports_them() -> None:
    effective = policy.resolve(policy.Policy(), _capable_snapshot())
    for name in ("network", "filesystem", "memory", "cpu", "pids"):
        assert effective.status_of(name) is policy.LimitStatus.ENFORCED


def test_cli_side_limits_are_always_enforced_by_headspace_itself() -> None:
    """Wall-clock, output-byte, and concurrency limits are applied by the
    headspace process, not the engine -- they never depend on the snapshot."""
    minimal_snapshot = policy.CapabilitySnapshot(engine="docker", api_version="1.52")
    effective = policy.resolve(policy.Policy(), minimal_snapshot)
    for name in ("wall_clock", "output_bytes", "concurrency"):
        assert effective.status_of(name) is policy.LimitStatus.ENFORCED


def test_network_enabled_never_needs_host_isolation_support() -> None:
    """Enabling network has nothing to violate -- only *disabling* it is a
    claim the host has to back up."""
    p = policy.Policy(network=policy.NetworkPosture.ENABLED)
    snapshot = _capable_snapshot(network_disable_supported=False)
    effective = policy.resolve(p, snapshot)
    assert effective.status_of("network") is policy.LimitStatus.ENFORCED


def test_empty_filesystem_scope_never_needs_host_mount_support() -> None:
    p = policy.Policy()  # default: no host paths
    snapshot = _capable_snapshot(filesystem_scope_enforceable=False)
    effective = policy.resolve(p, snapshot)
    assert effective.status_of("filesystem") is policy.LimitStatus.ENFORCED


# --- fail closed: an unsatisfiable policy raises before any job runs --------


def test_unsatisfiable_memory_limit_raises_before_any_job_runs() -> None:
    snapshot = _capable_snapshot(memory_enforceable=False)
    with pytest.raises(policy.PolicyError) as exc_info:
        policy.resolve(policy.Policy(), snapshot)
    err = exc_info.value
    assert err.code == 1
    assert "memory" in err.remediation


def test_unsatisfiable_network_isolation_raises_before_any_job_runs() -> None:
    snapshot = _capable_snapshot(network_disable_supported=False)
    with pytest.raises(policy.PolicyError) as exc_info:
        policy.resolve(policy.Policy(), snapshot)  # default policy: network DISABLED
    assert "network" in exc_info.value.remediation


def test_unsatisfiable_filesystem_scope_expansion_raises() -> None:
    p = policy.Policy(filesystem=policy.FilesystemScope(host_paths=("/data",)))
    snapshot = _capable_snapshot(filesystem_scope_enforceable=False)
    with pytest.raises(policy.PolicyError) as exc_info:
        policy.resolve(p, snapshot)
    assert "filesystem" in exc_info.value.remediation


def test_unsatisfiable_cpu_and_pids_raise_with_both_named() -> None:
    snapshot = _capable_snapshot(cpu_enforceable=False, pids_enforceable=False)
    with pytest.raises(policy.PolicyError) as exc_info:
        policy.resolve(policy.Policy(), snapshot)
    remediation = exc_info.value.remediation
    assert "cpu" in remediation
    assert "pids" in remediation


def test_policy_error_never_weakens_the_requested_limit() -> None:
    """resolve() must not return a degraded EffectivePolicy when it cannot be
    satisfied -- it raises, full stop; the caller never sees a silently
    lowered limit."""
    snapshot = _capable_snapshot(memory_enforceable=False)
    try:
        policy.resolve(policy.Policy(), snapshot)
    except policy.PolicyError:
        pass
    else:
        pytest.fail("resolve() must raise, not return a degraded EffectivePolicy")


def test_policy_error_to_dict_matches_the_cli_error_shape() -> None:
    """Mirrors headspace.cli._errors.CliError's {code, message, remediation}
    shape (by convention, not by import -- see the module docstring) so a
    future CLI-layer catch can translate it directly."""
    err = policy.PolicyError(message="m", remediation="r")
    assert err.to_dict() == {"code": 1, "message": "m", "remediation": "r"}


def test_resolve_is_pure_and_deterministic() -> None:
    snap = _capable_snapshot()
    first = policy.resolve(policy.Policy(), snap)
    second = policy.resolve(policy.Policy(), snap)
    assert first == second
