"""Execution backends, behind one seam. Import a backend, never the reverse.

This package depends on :mod:`headspace.core` and never the other way around —
that direction is what keeps the lifecycle and result contracts backend-neutral
(spec claim c4), and ``headspace/core/__init__.py`` holds up the other end of
the same rule.

``__init__`` re-exports the seam and **must never import a backend module**.
Backends have real dependencies: :mod:`headspace.providers.docker` imports the
docker SDK, and importing it from here would mean ``import headspace.providers``
— hence every unit test, every ``--help``, every CI lint run — needed a working
engine. Import a backend from its own module instead::

    from headspace.providers.base import Provider     # the seam
    from headspace.providers.fake import FakeProvider # in-memory, no daemon
    from headspace.providers.docker import DockerProvider  # needs the SDK

A test asserts this file imports nothing but ``headspace.providers.base``.
"""

from __future__ import annotations

from headspace.providers.base import (
    ENVIRONMENT_DIGEST_RE,
    JOB_STATUSES,
    OPAQUE_KEY,
    REMOVABLE_RESOURCES,
    REMOVAL_PATHS,
    RESOURCE_RUNTIME,
    RESOURCE_STORAGE,
    JobOutcome,
    OpaqueRef,
    Provider,
    ProviderError,
    RemovalDisposition,
    WorkspaceDescriptor,
    environment_digest,
    guard_removable,
    removal_path,
    requested_limit,
    utc_now,
)

__all__ = [
    "ENVIRONMENT_DIGEST_RE",
    "JOB_STATUSES",
    "OPAQUE_KEY",
    "REMOVABLE_RESOURCES",
    "REMOVAL_PATHS",
    "RESOURCE_RUNTIME",
    "RESOURCE_STORAGE",
    "JobOutcome",
    "OpaqueRef",
    "Provider",
    "ProviderError",
    "RemovalDisposition",
    "WorkspaceDescriptor",
    "environment_digest",
    "guard_removable",
    "removal_path",
    "requested_limit",
    "utc_now",
]
