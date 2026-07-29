"""Pins the seam contract task t1 adds: ``write``, ``run``'s ``env``, ``stop``.

This is signature-and-docstring work only (no backend implements these verbs
yet — that lands in later tasks), so the tests here cannot exercise behavior.
What they *can* pin, mechanically, is the contract itself:

1. The :class:`~headspace.providers.base.Provider` protocol declares ``write``,
   ``stop``, and an ``env`` parameter on ``run`` — each with a docstring, and
   ``env`` with a genuinely empty, immutable mapping default so the
   closed-by-default posture is structural rather than remembered.
2. A stand-in provider missing any one of the three fails to satisfy the
   protocol.

``@runtime_checkable`` protocols only check *attribute presence*, not method
signatures (see ``Provider``'s own docstring: "isinstance only ..."). That
means ``isinstance()`` catches a stand-in missing ``write`` or ``stop``
outright, but it would happily pass a stand-in whose ``run`` exists yet lacks
``env`` — the method is still named ``run``. That gap is exactly why
acceptance criterion 2 says "or an equivalent structural check": the
env-lacking case is pinned with :func:`inspect.signature` instead, both on the
protocol itself (``env`` must exist, with an empty-mapping default) and on the
stand-in (``env`` is absent), so the non-conformance is demonstrated rather
than silently passed by ``isinstance``.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from types import MappingProxyType

from headspace.core.artifacts import ByteSource
from headspace.core.policy import CapabilitySnapshot, EffectivePolicy
from headspace.providers.base import (
    ByteStream,
    JobOutcome,
    Provider,
    RemovalDisposition,
    StopOutcome,
    WorkspaceDescriptor,
)

# --- a stand-in that implements every verb, for a positive control ----------
#
# These are plain classes, not subclasses of Provider (Provider is a
# typing.Protocol — structural typing is the entire point, so a stand-in
# proves conformance by shape, never by inheritance). Bodies are unreachable:
# a runtime_checkable isinstance() check and inspect.signature() never call
# them, and this file is signature-only work, not behavior.


class _FullStandIn:
    """Every verb present, ``run`` carrying ``env`` — the positive control."""

    name = "stand-in"

    def capabilities(self) -> CapabilitySnapshot:
        raise NotImplementedError

    def create(
        self, workspace_id: str, environment: str, policy: EffectivePolicy
    ) -> WorkspaceDescriptor:
        raise NotImplementedError

    def run(
        self,
        workspace_id: str,
        command: Sequence[str],
        policy: EffectivePolicy,
        *,
        job_id: str,
        env: Mapping[str, str] = MappingProxyType({}),
    ) -> JobOutcome:
        raise NotImplementedError

    def inspect(self, workspace_id: str) -> WorkspaceDescriptor:
        raise NotImplementedError

    def read(self, workspace_id: str, path: str, *, chunk_size: int = 1) -> ByteStream:
        raise NotImplementedError

    def write(
        self,
        workspace_id: str,
        path: str,
        source: ByteSource,
        *,
        sha256: str,
        overwrite: bool = False,
    ) -> None:
        raise NotImplementedError

    def stop(self, workspace_id: str) -> StopOutcome:
        raise NotImplementedError

    def remove(self, workspace_id: str) -> RemovalDisposition:
        raise NotImplementedError


class _MissingWrite(_FullStandIn):
    """Every verb except ``write``."""

    write = None  # type: ignore[assignment]


class _MissingStop(_FullStandIn):
    """Every verb except ``stop``."""

    stop = None  # type: ignore[assignment]


class _RunWithoutEnv(_FullStandIn):
    """``run`` present, but without the ``env`` parameter.

    ``isinstance`` cannot see this gap — see the module docstring. It exists
    to drive the ``inspect.signature`` checks below, which can.
    """

    def run(  # type: ignore[override]
        self,
        workspace_id: str,
        command: Sequence[str],
        policy: EffectivePolicy,
        *,
        job_id: str,
    ) -> JobOutcome:
        raise NotImplementedError


# --- acceptance criterion 2: missing verbs fail the protocol check ----------


def test_full_stand_in_satisfies_the_protocol() -> None:
    """Sanity check: the protocol as declared is actually satisfiable."""
    assert isinstance(_FullStandIn(), Provider)


def test_provider_lacking_write_fails_the_protocol_check() -> None:
    assert not isinstance(_MissingWrite(), Provider)


def test_provider_lacking_stop_fails_the_protocol_check() -> None:
    assert not isinstance(_MissingStop(), Provider)


def test_isinstance_alone_cannot_catch_run_missing_env() -> None:
    """Documents the runtime_checkable gap the module docstring names.

    A ``run`` without ``env`` is still a method named ``run``, so structural
    ``isinstance`` has nothing to object to. This is not a bug in the test —
    it is exactly the limitation acceptance criterion 2 calls out, and the
    next two tests are the "equivalent structural check" it asks for instead.
    """
    assert isinstance(_RunWithoutEnv(), Provider)


def test_run_missing_env_fails_a_signature_check() -> None:
    """The structural check ``isinstance`` cannot perform: does ``run`` carry ``env``?"""
    params = inspect.signature(_RunWithoutEnv.run).parameters
    assert "env" not in params


def test_protocol_run_requires_env_with_a_closed_default() -> None:
    """Pins criterion 1 directly on the protocol: ``env`` exists, closed by default."""
    params = inspect.signature(Provider.run).parameters
    assert "env" in params
    env_param = params["env"]
    assert env_param.kind is inspect.Parameter.KEYWORD_ONLY
    # The default must be an actual empty mapping instance, not merely
    # falsy — a caller that inspects it (or a backend that forwards it
    # unchanged) must find something Mapping-shaped.
    assert isinstance(env_param.default, Mapping)
    assert dict(env_param.default) == {}


# --- acceptance criterion 1: shape and docstrings of the three additions ---


def test_provider_declares_write_stop_and_run() -> None:
    for verb in ("write", "stop", "run"):
        assert hasattr(Provider, verb), f"Provider is missing {verb!r}"


def test_write_signature_matches_the_documented_contract() -> None:
    sig = inspect.signature(Provider.write)
    params = sig.parameters
    assert list(params)[:4] == ["self", "workspace_id", "path", "source"]
    assert params["sha256"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["sha256"].default is inspect.Parameter.empty
    assert params["overwrite"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["overwrite"].default is False


def test_stop_takes_only_the_workspace_id() -> None:
    sig = inspect.signature(Provider.stop)
    assert list(sig.parameters) == ["self", "workspace_id"]


def test_write_docstring_states_liveness_and_boundary_and_raises() -> None:
    doc = Provider.write.__doc__ or ""
    assert doc.strip(), "write must carry a docstring"
    # Liveness contract: the live-runtime requirement, contrasted with read.
    assert "live" in doc.lower()
    assert "read" in doc
    # Boundary contract: the overwrite refusal.
    assert "overwrite" in doc
    # What raises what.
    assert "CliError" in doc
    assert "ProviderError" in doc
    assert "require_workspace_path" in doc


def test_stop_docstring_states_liveness_and_boundary_and_raises() -> None:
    doc = Provider.stop.__doc__ or ""
    assert doc.strip(), "stop must carry a docstring"
    # Liveness contract: nothing running is a fact, not an error.
    assert "StopOutcome" in doc
    # Boundary contract: the single-state-writer / no-lock invariant.
    assert "lock" in doc.lower()
    assert "store" in doc.lower() or "state" in doc.lower()
    # What raises what.
    assert "CliError" in doc
    assert "ProviderError" in doc


def test_run_docstring_states_envs_boundary_contract() -> None:
    doc = Provider.run.__doc__ or ""
    assert "env" in doc
    # The confidentiality boundary: reaches the job process and nothing else,
    # and the provider never records it.
    assert "never" in doc.lower()
    assert "environment" in doc.lower()


def test_stop_outcome_is_a_plain_serialisable_structure() -> None:
    """``StopOutcome`` crosses the seam, so it must be JSON-primitive, all the way down."""
    import dataclasses
    import json

    outcome = StopOutcome(workspace_id="ws-1", job_id="job-1", stopped=True)
    payload = outcome.to_dict()
    json.dumps(payload)  # must not raise
    assert set(payload) == {f.name for f in dataclasses.fields(StopOutcome)}

    idle = StopOutcome(workspace_id="ws-1", job_id=None, stopped=False)
    assert idle.to_dict() == {"workspace_id": "ws-1", "job_id": None, "stopped": False}
