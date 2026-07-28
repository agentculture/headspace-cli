"""The Docker backend: the one module in headspace that knows an engine exists.

WHY this module exists
----------------------
:mod:`headspace.providers.base` draws a line and promises that nothing about a
backend crosses it. A promise like that needs somewhere to *put* the backend,
and this is it. The docker SDK is imported here and nowhere else in the
package, which buys three things at once: "swap the backend" stays a one-file
question, a CI lane with no daemon can still import, lint and test everything
above the seam, and a reviewer looking for the blast radius of Docker has
exactly one file to read. A test in ``tests/test_provider_fake.py`` enforces
the quarantine mechanically rather than trusting this paragraph.

What a workspace actually is
----------------------------
Two engine objects, both carrying headspace's own workspace id as a label:

``workspace container``
    a long-lived idle container that holds the workspace's *posture*. It is the
    ``runtime`` resource a removal reports, the record :meth:`DockerProvider.inspect`
    reads its facts from, and — because its ``HostConfig`` **is** the
    closed-by-default configuration — the auditable evidence that the box was
    closed rather than merely intended to be. It is started, not just created,
    because a posture that has never been applied is a claim, not a fact.
``workspace volume``
    the ``storage`` resource: one bounded volume mounted at
    :data:`WORKSPACE_MOUNT_PATH`, and the only writable state that outlives a
    job. Jobs share the workspace through it and through nothing else.

Jobs get their **own** containers rather than ``exec`` calls into the workspace
container. Three reasons, heaviest first:

* a log driver's ``max-size`` applies to a container, not to an exec, so
  per-job containers are the only way to cap output at capture (FR-16);
* an exec has no kill API — enforcing a wall-clock budget against one means
  reaching inside the container for a pid, which is exactly the sort of
  privilege the posture spends its effort removing;
* a job that wedges its runtime costs one container, not the workspace.

Why labels, and never handles
-----------------------------
headspace is daemonless: the process that created a container is usually gone
by the time anything asks about it, and a CLI killed between the engine call
and the state write never got a handle back at all. So ownership is expressed
the only way that survives that — as labels on the engine objects themselves.
:data:`LABEL_WORKSPACE_ID` makes every orphan of a crashed run discoverable
with one ``docker ps --filter``, and every verb here finds its objects by
label from the workspace id alone. The container id travels back to the caller
only inside an :class:`~headspace.providers.base.OpaqueRef`, where nothing
above the seam may read it.

The labels carry more than identity. The create-time
:class:`~headspace.core.policy.CapabilitySnapshot` and every resolved limit's
``enforced``/``measured`` status are written onto the workspace container
(:data:`LABEL_CAPABILITY_PREFIX`, :data:`LABEL_LIMIT_PREFIX`), so the answer to
"what did this host promise when this workspace was made, and what did it only
measure?" outlives the process that asked — and stays answerable even when the
state store is gone.

Closed by default, and closed at the engine
-------------------------------------------
:meth:`DockerProvider._sealed_kwargs` builds one configuration used by both the
workspace container and every job container, so there is no second, laxer path
for a job to take. Network is ``none`` unless the policy said otherwise; the
memory, cpu and pids ceilings come from the caller's budget; swap is pinned to
the memory ceiling, because a container allowed to swap has not really been
given a memory limit; privilege escalation is refused. Nothing is bind-mounted
from the host — not a path, not a device, and above all not the engine socket,
which would hand a job the engine itself. The only mount is the workspace
volume. What is *absent* matters as much as what is set, which is why the tests
assert those absences against a real ``HostConfig`` instead of trusting this
docstring.

Bounded at capture, honest about the volume
-------------------------------------------
Two mechanisms, doing two different jobs:

* the log driver's ``max-size``/``max-file`` (:func:`log_cap_bytes`) bound what
  a job can write to the host's disk, whatever headspace does next. This is the
  one that stops a runaway job filling a developer's machine.
* the attach stream bounds what reaches the caller. :meth:`DockerProvider.run`
  attaches **before** starting the container and reads the live stream, keeping
  the first ``output_bytes`` of the caller's budget and *counting* the rest. So
  the reported :attr:`~headspace.core.result.ResourceUsage.output_bytes` is the
  volume the job really produced, not the volume that fitted, and ``truncated``
  says the difference exists. Reading the log after the fact would have made
  the two indistinguishable: rotation would have silently eaten the evidence
  that anything was dropped.

One honest limitation: stdout and stderr arrive as separate frames, so the
capture preserves the engine's arrival order rather than a guaranteed emission
order — which is all any backend can promise across two pipes, and why
:class:`~headspace.providers.base.JobOutcome` promises ordering rather than
separation.

Getting an artifact back out
----------------------------
The read verb asks the engine's archive endpoint (``get_archive``) for one path
inside the **workspace container** — the object this provider already owns — and
untars a single member out of the transfer stream as it arrives. Three
properties made that the choice over the obvious alternative, a short-lived
helper container mounting the same volume:

* **It works on a workspace whose runtime has exited.** The engine resolves the
  path through the container's mounts, not through a running process, so the
  artifacts survive the box that produced them. That is the case the verb
  exists for: a container that died is exactly when a caller most needs its
  results, and a read that required a live runtime would lose them.
* **It creates nothing.** A helper container is a second engine object that has
  to be reaped on every failure path, including the ones nobody thought of.
  "The engine's object count does not move" is a guarantee no reaper can match,
  and a test asserts it.
* **It widens nothing.** No host mount, no network, no socket — the read reuses
  the box the posture already closed rather than opening a new one beside it.

Nothing is materialised on the way. The engine's chunks feed a one-chunk-deep
reader (:class:`_ChunkReader`), which feeds :mod:`tarfile` in stream mode
(:data:`ARCHIVE_STREAM_MODE`), which yields the member's bytes in the caller's
own chunk size — so peak memory tracks the chunk, not the artifact. A test
reads a 16 MiB artifact under :mod:`tracemalloc` and holds the peak to a
quarter of it, because "streamed" is a claim worth measuring rather than
asserting.

The connection is the one exception to "one connection per verb": the bytes are
still arriving when the caller gets its stream, so the client, the transfer
archive and the archive reader are handed to the stream's release
(a :class:`contextlib.ExitStack`) instead of being closed at the end of a block.
Abandoning a read half way — which the export path does whenever a digest fails
to verify — therefore releases exactly what a completed read releases.

Only a regular file's bytes cross. A directory or a symlink is refused as a
user error, and the symlink refusal is load-bearing rather than fastidious: a
job can plant a link to a path outside the workspace volume, and the archive
endpoint resolves link targets within the container's filesystem quite happily.

Measured, not assumed
---------------------
:meth:`DockerProvider.capabilities` interrogates the engine — version, API
version, the cgroup controllers ``/info`` reports, the network and volume
drivers it offers — instead of returning optimistic booleans. The one that
matters is ``storage_enforceable``: the local volume driver *reports* a
volume's size and *caps* nothing, so it is reported ``False`` and
:func:`headspace.core.policy.resolve` labels storage ``measured``. That is the
canonical measured-not-enforced case the whole policy module exists to keep
visible, and quietly claiming otherwise here is exactly the silent weakening it
was built to prevent.

Raised, never returned
----------------------
Every engine call runs inside :meth:`DockerProvider._engine`, which turns any
transport or API failure into :class:`~headspace.providers.base.ProviderError`
(exit 7). NFR-07 is structural, not conventional: a
:class:`~headspace.providers.base.JobOutcome` cannot express "the engine
broke", so a caller can never be told to try a different algorithm when what it
needed was a running daemon. The connection itself is opened per verb rather
than cached, which matches production exactly — each CLI invocation is its own
process — and keeps the failure honest: an engine that dies between two verbs
is discovered by the second one.
"""

from __future__ import annotations

import contextlib
import math
import re
import tarfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import fields
from typing import IO, Any

import docker
from docker.errors import APIError, DockerException, ImageNotFound, NotFound
from docker.models.containers import Container
from docker.types import LogConfig, Mount

from headspace.cli._errors import EXIT_USER_ERROR, CliError
from headspace.core.policy import CapabilitySnapshot, EffectivePolicy
from headspace.core.result import STATUS_FAILURE, STATUS_SUCCESS, STATUS_TIMEOUT, ResourceUsage
from headspace.core.states import State, validate_transition
from headspace.providers.base import (
    DEFAULT_READ_CHUNK_BYTES,
    RESOURCE_RUNTIME,
    RESOURCE_STORAGE,
    ByteStream,
    JobOutcome,
    OpaqueRef,
    ProviderError,
    RemovalDisposition,
    WorkspaceDescriptor,
    environment_digest,
    guard_removable,
    missing_workspace_path,
    requested_limit,
    require_chunk_size,
    require_command,
    require_workspace_id,
    require_workspace_path,
    unknown_workspace,
    unreadable_workspace_path,
    utc_now,
)

# --- identity: how a daemonless CLI recognises its own objects --------------

#: The backend identifier stamped into every descriptor.
PROVIDER_NAME = "docker"

#: Name prefix for every container and volume this provider creates. Names are
#: cosmetic to the code (lookup is by label) and load-bearing to a human: an
#: operator scanning ``docker ps`` must be able to tell headspace's objects from
#: their own without consulting a database.
OBJECT_PREFIX = "headspace-"

#: The label that carries headspace's workspace id. The single most important
#: value in this module: it is how a crashed run's orphans are found again.
LABEL_WORKSPACE_ID = "headspace.workspace_id"
LABEL_PROVIDER = "headspace.provider"
LABEL_ROLE = "headspace.role"
LABEL_JOB_ID = "headspace.job_id"
LABEL_CREATED_AT = "headspace.created_at"
#: The digest-pinned environment reference the workspace was created from —
#: kept so a job container can be built from the same environment without the
#: caller having to hand it back, and so provenance survives the process.
LABEL_ENVIRONMENT = "headspace.environment"
#: ``headspace.capability.<field>`` — the create-time capability probe, one
#: label per :class:`~headspace.core.policy.CapabilitySnapshot` field.
LABEL_CAPABILITY_PREFIX = "headspace.capability."
#: ``headspace.limit.<name>`` — each resolved limit's ``enforced``/``measured``
#: status, so the enforced-vs-measured distinction is persisted, not recomputed.
LABEL_LIMIT_PREFIX = "headspace.limit."

ROLE_WORKSPACE = "workspace"
ROLE_JOB = "job"
ROLE_STORAGE = "storage"

# --- the shape of a workspace ----------------------------------------------

#: Where the workspace volume is mounted, and the working directory of every
#: job. The one path a job may assume outlives it.
WORKSPACE_MOUNT_PATH = "/workspace"

#: The volume driver headspace creates workspace storage on. Named rather than
#: defaulted because :meth:`DockerProvider.capabilities` reasons about it: it
#: reports usage and caps nothing.
WORKSPACE_VOLUME_DRIVER = "local"

#: Volume drivers that accept a hard size cap through this provider. Empty
#: today, and deliberately a set rather than a ``False``: the ``local`` driver's
#: ``o=size=`` option only applies to tmpfs- and btrfs-backed mounts, neither of
#: which headspace uses, so no driver on offer can enforce a storage ceiling.
#: A future driver that can earns its entry here and nowhere else.
SIZE_CAPPING_VOLUME_DRIVERS: frozenset[str] = frozenset()

#: The engine's driver for "no network at all". Its presence is what
#: ``network_disable_supported`` actually checks.
NULL_NETWORK_DRIVER = "null"

#: What the workspace container runs: a portable idle loop. ``sleep infinity``
#: is not portable (busybox rejects it), and the workspace container exists to
#: hold a posture, not to do work — so it must be something every base image
#: can run and nothing can make exit on its own.
IDLE_COMMAND: tuple[str, ...] = ("/bin/sh", "-c", "while :; do sleep 86400; done")

#: Container statuses that mean the object is still alive. Anything else means
#: the workspace's runtime is gone, which :meth:`DockerProvider.inspect`
#: reports as ``failed`` rather than papering over as ``ready``.
LIVE_STATUSES: frozenset[str] = frozenset({"created", "running", "restarting", "paused"})

# --- capture bounds ---------------------------------------------------------

#: The log file is the engine's spill buffer while headspace reads the live
#: stream. It is sized off the caller's own output budget so a caller asking to
#: keep little cannot be made to pay for a large one...
LOG_CAP_MULTIPLE = 2
#: ...but floored, because a cap below the smallest useful log makes truncation
#: undetectable rather than bounded...
MIN_LOG_CAP_BYTES = 64 * 1024
#: ...and ceilinged, because the host's disk is not the caller's to spend.
MAX_LOG_CAP_BYTES = 16 * 1024 * 1024
#: Rotation generations kept. Two, so a rotation still leaves one whole
#: generation behind for the fallback reader; total on-disk stays bounded by
#: ``max-size * LOG_FILE_COUNT``.
LOG_FILE_COUNT = 2

# --- timing -----------------------------------------------------------------

#: How often the wall-clock budget is checked. Small enough that a one-second
#: budget is honoured recognisably, large enough not to spin.
POLL_INTERVAL_SECONDS = 0.05
#: How often resource usage is sampled while a job runs. Docker keeps no
#: post-mortem accounting for an exited container, so anything not sampled in
#: flight is lost — and sampling every poll would cost more than it measures.
STATS_INTERVAL_SECONDS = 0.5
#: How long a killed container is given to actually die before the engine is
#: declared broken. A ``SIGKILL`` the engine cannot deliver is not a slow job.
KILL_GRACE_SECONDS = 30.0
#: How long the capture reader is given to drain after the job settles.
CAPTURE_GRACE_SECONDS = 10.0

#: Characters an engine object name may carry. Everything else is folded to
#: ``-`` so a job id chosen upstream can never make a name the engine rejects.
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_NAME_PART = 32

# --- reading an artifact back out -------------------------------------------

#: How the transfer archive is opened. The trailing ``|`` is the load-bearing
#: character: it selects :mod:`tarfile`'s *stream* mode, which never seeks and
#: never holds the archive, so the artifact is consumed as it arrives. Plain
#: ``"r"`` would require a seekable file and would therefore mean buffering the
#: whole transfer first — the one thing this path must not do.
ARCHIVE_STREAM_MODE = "r|"

#: Everything a failing engine can throw at this module, in one tuple so no
#: call site can catch a narrower set by accident.
#:
#: ``requests`` transport errors subclass :class:`OSError`, which is how the
#: whole HTTP surface is covered without importing the SDK's own stack. A
#: :class:`tarfile.TarError` joins them because a truncated or malformed
#: transfer archive is the engine failing to deliver bytes it said it had — a
#: broken engine (exit 7), never a caller that asked for the wrong thing.
_ENGINE_FAILURES: tuple[type[BaseException], ...] = (DockerException, OSError, tarfile.TarError)


def log_cap_bytes(output_budget: int) -> int:
    """The on-disk log ceiling for a job whose caller wants ``output_budget`` kept.

    Clamped at both ends on purpose (see :data:`MIN_LOG_CAP_BYTES` and
    :data:`MAX_LOG_CAP_BYTES`) and rounded to whole KiB, because that is the
    unit the log driver's ``max-size`` is expressed in and a value that has to
    be re-rounded at the boundary is a value two layers disagree about.
    """
    capped = min(max(output_budget * LOG_CAP_MULTIPLE, MIN_LOG_CAP_BYTES), MAX_LOG_CAP_BYTES)
    return math.ceil(capped / 1024) * 1024


def engine_unavailable() -> str:
    """Why this host cannot run Docker-backed workspaces; ``""`` when it can.

    A *reason*, not a boolean, because every caller of this has to explain
    itself to somebody: a doctor check prints it, a test skip names it. It
    pings rather than merely connecting — a socket that accepts and then
    answers nothing is not an available engine.

    Never raises. This is the one function here that is asked "is the engine
    there?" rather than "do the thing", so turning the answer into an exception
    would put the burden back on every caller.
    """
    client: docker.DockerClient | None = None
    try:
        client = docker.from_env()
        client.ping()
        return ""
    except (DockerException, OSError) as err:
        return f"docker engine unreachable: {err}"
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()


def _slug(value: str) -> str:
    """A name fragment the engine will accept, from arbitrary caller text."""
    cleaned = _UNSAFE_NAME_CHARS.sub("-", value).strip("-._")[:_MAX_NAME_PART]
    return cleaned or "x"


def _label_value(value: Any) -> str:
    """Label values are strings; booleans must round-trip readably."""
    return str(value).lower() if isinstance(value, bool) else str(value)


def _quietly(close: Callable[[], Any]) -> None:
    """Run one release step, never letting its own failure mask the real one.

    Cleanup on the failure path is exactly where a second exception does the
    most damage: it replaces the diagnosis with the tidy-up's complaint about
    a socket that was already dead.
    """
    with contextlib.suppress(Exception):
        close()


class _ChunkReader:
    """A ``read()``-able view over the engine's chunk iterator, one chunk deep.

    :mod:`tarfile` wants a file object; the engine hands back an iterator of
    byte chunks. Adapting one to the other is all this does, and the constraint
    that shapes it is that it must never hold more than the chunk it is
    currently serving. An artifact larger than the host's memory is precisely
    the case the streaming read exists for, so a buffer that grew with the
    artifact would defeat the whole path while still passing every
    small-artifact test.
    """

    def __init__(self, chunks: Iterator[bytes]) -> None:
        self._chunks = chunks
        self._buffer = bytearray()

    def read(self, size: int = -1) -> bytes:
        """Serve exactly ``size`` bytes, pulling from the engine only as needed."""
        while size < 0 or len(self._buffer) < size:
            chunk = next(self._chunks, b"")
            if not chunk:
                break
            self._buffer += chunk
        if size < 0:
            size = len(self._buffer)
        taken = bytes(self._buffer[:size])
        # Consume in place: a slice-and-rebind would hold both halves at once.
        del self._buffer[:size]
        return taken


def _streamed(member: IO[bytes], chunk_size: int, action: str) -> Iterator[bytes]:
    """Yield one archived file's bytes, bounded, one chunk in flight at a time.

    The engine can die *during* a transfer as easily as before one, and an
    artifact that stops arriving half way is still an infrastructure failure —
    so the taxonomy is applied here too, not only at the call that opened the
    stream. A caller told exit 7 retries the fetch; one told exit 6 would
    conclude its work was lost.
    """
    while True:
        try:
            chunk = member.read(chunk_size)
        except _ENGINE_FAILURES as err:
            raise ProviderError(f"the execution engine failed while {action}: {err}") from err
        if not chunk:
            return
        yield chunk


class _Capture:
    """Reads a job's live output, keeping a bounded prefix and counting the rest.

    Runs on its own thread because the main thread is enforcing the wall-clock
    budget, and because a job whose output nobody reads eventually blocks on the
    engine's side — an unread stream would turn "capped output" into "stalled
    job". ``produced`` is the true volume; ``text`` is only what fitted.

    The cut is on a byte boundary with a lenient decode, so a multi-byte
    codepoint straddling the boundary is dropped rather than emitted as
    mojibake — the same rule :mod:`headspace.core.result` uses when it excerpts.
    """

    def __init__(self, stream: Iterator[bytes], budget: int) -> None:
        self._stream = stream
        self._budget = max(0, budget)
        self._kept = bytearray()
        self.produced = 0
        self.failure: str = ""
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._pump, name="headspace-capture", daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        try:
            for chunk in self._stream:
                self.produced += len(chunk)
                room = self._budget - len(self._kept)
                if room > 0:
                    self._kept += chunk[:room]
        except Exception as err:  # noqa: BLE001 - a reader that dies must not kill the job
            self.failure = f"{type(err).__name__}: {err}"

    def join(self, grace: float) -> None:
        if self._thread is not None:
            self._thread.join(grace)
            if self._thread.is_alive():
                self.failure = self.failure or "the output stream did not drain"
        with contextlib.suppress(Exception):
            close = getattr(self._stream, "close", None)
            if callable(close):
                close()

    @property
    def text(self) -> str:
        return self._kept.decode("utf-8", "ignore")

    @property
    def truncated(self) -> bool:
        return self.produced > len(self._kept)


class _Usage:
    """Resource accounting sampled while the job is alive — the only time it exists.

    Docker retains no cgroup accounting for an exited container: ask it
    afterwards and every counter reads zero. So this samples in flight and keeps
    the maxima, and a sample that fails is dropped rather than allowed to fail
    the job — accounting is evidence about work, never a precondition for it.
    The memory figure is therefore an observed maximum at
    :data:`STATS_INTERVAL_SECONDS` resolution, not a guaranteed peak.
    """

    def __init__(self) -> None:
        self.cpu_nanoseconds = 0
        self.max_memory_bytes = 0
        self._next_sample = 0.0

    def sample(self, container: Container) -> None:
        now = time.monotonic()
        if now < self._next_sample:
            return
        self._next_sample = now + STATS_INTERVAL_SECONDS
        try:
            stats = container.stats(stream=False, one_shot=True)
        except (DockerException, OSError):
            return
        cpu = ((stats.get("cpu_stats") or {}).get("cpu_usage") or {}).get("total_usage") or 0
        memory = (stats.get("memory_stats") or {}).get("usage") or 0
        self.cpu_nanoseconds = max(self.cpu_nanoseconds, int(cpu))
        self.max_memory_bytes = max(self.max_memory_bytes, int(memory))


class DockerProvider:
    """A :class:`~headspace.providers.base.Provider` backed by a Docker engine."""

    def __init__(self, *, connect: Callable[[], docker.DockerClient] | None = None) -> None:
        """Construct without touching the engine.

        ``--help``, ``doctor`` and every unit test must work on a host with no
        daemon, so nothing here connects; the first verb does. ``connect`` is a
        seam for pointing the provider at a non-default engine, defaulting to
        the environment the way every other docker client does.
        """
        self._connect = connect or docker.from_env
        self._snapshot: CapabilitySnapshot | None = None

    # --- the seam ---------------------------------------------------------

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    def capabilities(self) -> CapabilitySnapshot:
        """Probe what this engine can actually enforce; cache the answer.

        Cached per provider instance because the snapshot is a create-time fact
        about a host that is not changing underneath a single invocation, and
        because :func:`headspace.core.policy.resolve` is called on every verb.
        """
        if self._snapshot is None:
            self._snapshot = self._probe()
        return self._snapshot

    def create(
        self, workspace_id: str, environment: str, policy: EffectivePolicy
    ) -> WorkspaceDescriptor:
        """Provision the volume and the workspace container, closed by default.

        The image is pulled here and only here: :func:`headspace.core.profiles.resolve`
        hands over a digest-pinned reference and performs no I/O, so this is
        where a reference becomes a local image. Already-present images are not
        re-fetched — a digest cannot have come to mean something else.
        """
        workspace_id = require_workspace_id(workspace_id)
        snapshot = self.capabilities()
        created_at = utc_now()
        network_enabled = requested_limit(policy, "network") == "enabled"

        with self._engine(f"creating workspace {workspace_id}") as client:
            if self._find(client, workspace_id, ROLE_WORKSPACE) is not None:
                raise CliError(
                    code=EXIT_USER_ERROR,
                    message=f"workspace {workspace_id} already exists",
                    remediation="choose a fresh workspace id, or remove the existing workspace",
                )

            # The provisioning walk is real: every hop is checked against the
            # lifecycle table, so a table change breaks this backend exactly as
            # it would break the fake.
            state = State.REQUESTED
            for target in (State.PROVISIONING, State.READY):
                validate_transition(state, target)
                state = target

            labels = self._workspace_labels(workspace_id, environment, created_at, snapshot, policy)
            self._ensure_image(client, environment)
            volume = client.volumes.create(
                name=self._volume_name(workspace_id),
                driver=WORKSPACE_VOLUME_DRIVER,
                labels={**labels, LABEL_ROLE: ROLE_STORAGE},
            )
            try:
                container = client.containers.create(
                    image=environment,
                    command=list(IDLE_COMMAND),
                    name=self._container_name(workspace_id),
                    labels=labels,
                    **self._sealed_kwargs(policy, network_enabled, workspace_id),
                )
            except Exception:
                # A volume with no container is an orphan nothing will ever
                # reconcile: the workspace it belonged to never existed.
                with contextlib.suppress(DockerException, OSError):
                    volume.remove(force=True)
                raise
            try:
                container.start()
            except Exception:
                with contextlib.suppress(DockerException, OSError):
                    container.remove(force=True)
                with contextlib.suppress(DockerException, OSError):
                    volume.remove(force=True)
                raise
            container.reload()
            return self._describe(client, container)

    def run(
        self,
        workspace_id: str,
        command: Sequence[str],
        policy: EffectivePolicy,
        *,
        job_id: str,
    ) -> JobOutcome:
        """Execute one command in its own container, bounded at capture."""
        workspace_id = require_workspace_id(workspace_id)
        argv = require_command(command)
        wall_budget = float(requested_limit(policy, "wall_clock"))
        output_budget = int(requested_limit(policy, "output_bytes"))

        with self._engine(f"running a job in workspace {workspace_id}") as client:
            anchor = self._require(client, workspace_id)
            container = client.containers.create(
                image=anchor.labels.get(LABEL_ENVIRONMENT, anchor.attrs["Config"]["Image"]),
                command=list(argv),
                name=f"{self._container_name(workspace_id)}-{_slug(job_id)}-{uuid.uuid4().hex[:8]}",
                labels={
                    LABEL_WORKSPACE_ID: workspace_id,
                    LABEL_PROVIDER: PROVIDER_NAME,
                    LABEL_ROLE: ROLE_JOB,
                    LABEL_JOB_ID: job_id,
                    LABEL_CREATED_AT: utc_now(),
                },
                **self._sealed_kwargs(
                    policy,
                    # A job may never open a door the workspace kept shut: the
                    # narrower of the two postures wins, always.
                    self._network_enabled(anchor)
                    and requested_limit(policy, "network") == "enabled",
                    workspace_id,
                ),
            )
            started_at = utc_now()
            started = time.monotonic()
            try:
                # Attached BEFORE the container starts, so the first byte is
                # ours. Reading the log afterwards would race the driver's own
                # rotation and lose the evidence that anything was dropped.
                capture = _Capture(
                    container.attach(stream=True, logs=True, stdout=True, stderr=True, demux=False),
                    output_budget,
                )
                capture.start()
                container.start()
                usage = _Usage()
                timed_out = self._await_exit(container, wall_budget, usage)
                capture.join(CAPTURE_GRACE_SECONDS)
                exit_status = int(container.attrs["State"].get("ExitCode") or 0)
                output, produced, truncated = self._captured(container, capture, output_budget)
                storage_bytes = self._volume_bytes(client, workspace_id)
            finally:
                # A job container that outlives its job is a stray, and the
                # removal is best-effort on purpose: a successful job must not
                # be reported as an engine failure because the tidy-up lost a
                # race. The label makes it findable, and `remove` reaps it.
                with contextlib.suppress(DockerException, OSError):
                    container.remove(force=True)

        return JobOutcome(
            job_id=job_id,
            workspace_id=workspace_id,
            status=self._status(timed_out, exit_status),
            exit_status=None if timed_out else exit_status,
            output=output,
            truncated=truncated,
            started_at=started_at,
            finished_at=utc_now(),
            usage=ResourceUsage(
                # headspace's own measurement, deliberately: it is the clock the
                # wall-clock budget was enforced against, so a caller comparing
                # the two is comparing like with like.
                wall_time_seconds=round(time.monotonic() - started, 6),
                cpu_seconds=usage.cpu_nanoseconds / 1e9,
                max_memory_bytes=usage.max_memory_bytes,
                storage_bytes=storage_bytes,
                output_bytes=produced,
            ),
        )

    def inspect(self, workspace_id: str) -> WorkspaceDescriptor:
        """Report what the engine says about a workspace right now."""
        workspace_id = require_workspace_id(workspace_id)
        with self._engine(f"inspecting workspace {workspace_id}") as client:
            return self._describe(client, self._require(client, workspace_id))

    def read(
        self, workspace_id: str, path: str, *, chunk_size: int = DEFAULT_READ_CHUNK_BYTES
    ) -> ByteStream:
        """Stream one file out of a workspace's storage, live runtime or not.

        Everything that can be refused locally is refused before a socket is
        opened: the path is normalised and bounded by the seam's own rule (see
        :func:`~headspace.providers.base.require_workspace_path`, and note that
        the engine would happily resolve ``..`` if asked), and the chunk size is
        checked. Only then is the engine involved.

        The client outlives this call, which is the one place this module
        departs from "one connection per verb": the bytes are still arriving
        when the caller gets the stream back, so the connection, the transfer
        archive and the archive reader are all handed to the stream's release
        instead. :class:`contextlib.ExitStack` owns them from the moment each is
        acquired, so a failure part-way through setup unwinds exactly what had
        been opened — and the same stack becomes the release, which is what
        makes a stream that is abandoned half way as tidy as one read to the end.
        """
        workspace_id = require_workspace_id(workspace_id)
        relative = require_workspace_path(path)
        size = require_chunk_size(chunk_size)
        action = f"reading '{relative}' from workspace {workspace_id}"

        try:
            client = self._connect()
        except (DockerException, OSError) as err:
            raise ProviderError(
                f"could not reach the execution engine while {action}: {err}"
            ) from err

        release = contextlib.ExitStack()
        release.callback(_quietly, client.close)
        try:
            with self._engine_failures(action):
                anchor = self._require(client, workspace_id)
                member = self._archived_file(anchor, workspace_id, relative, size, release)
        except BaseException:
            release.close()
            raise
        return ByteStream(_streamed(member, size, action), release=release.close)

    @staticmethod
    def _archived_file(
        anchor: Container,
        workspace_id: str,
        relative: str,
        chunk_size: int,
        release: contextlib.ExitStack,
    ) -> IO[bytes]:
        """Open the engine's transfer archive, positioned at one file's bytes.

        ``get_archive`` is asked of the *workspace* container — the one this
        provider already owns — for two reasons. It works on a container that
        has exited, because the engine reads the path through the container's
        mounts rather than through a running process, so an artifact survives
        the runtime that produced it (which is the case this verb exists for).
        And it creates nothing: a helper container mounting the same volume
        would be a second engine object to reap on every failure path, and
        "nothing was created" is a stronger guarantee than any reaper.

        Exactly one member is read, and it must be a regular file. A directory
        arrives as a directory entry followed by its contents, and a symlink
        arrives as a link — neither is an artifact, and the link case is a
        boundary check rather than fussiness: a job can plant one pointing
        outside the workspace volume.
        """
        try:
            archive_stream, _stat = anchor.get_archive(
                f"{WORKSPACE_MOUNT_PATH}/{relative}", chunk_size=chunk_size
            )
        except NotFound as err:
            # The engine answered, and the answer was "no such file" — the
            # caller's mistake, not a broken engine.
            raise missing_workspace_path(workspace_id, relative) from err
        release.callback(_quietly, archive_stream.close)

        archive = tarfile.open(mode=ARCHIVE_STREAM_MODE, fileobj=_ChunkReader(archive_stream))
        release.callback(_quietly, archive.close)
        entry = archive.next()
        if entry is None:
            raise ProviderError(
                f"the engine returned an empty archive for '{relative}' in "
                f"workspace {workspace_id}"
            )
        if entry.isdir():
            raise unreadable_workspace_path(workspace_id, relative, "directory")
        if entry.issym() or entry.islnk():
            raise unreadable_workspace_path(workspace_id, relative, "link")
        member = archive.extractfile(entry) if entry.isfile() else None
        if member is None:
            raise unreadable_workspace_path(workspace_id, relative, "special file")
        return member

    def remove(self, workspace_id: str) -> RemovalDisposition:
        """Tear the workspace down and report what actually went away.

        Every resource is *verified* gone before it is named as removed. A
        destruction report whose claims were never checked is the one report
        nobody can afford to be optimistic in.
        """
        workspace_id = require_workspace_id(workspace_id)
        with self._engine(f"removing workspace {workspace_id}") as client:
            anchor = self._require(client, workspace_id)

            # Refuses in-flight work in the lifecycle table's own words, and
            # walks only edges the table already allows — no ready -> destroyed
            # shortcut is invented here.
            state = self._state_of(anchor)
            for target in guard_removable(state, self._active_jobs(client, workspace_id)):
                validate_transition(state, target)
                state = target

            for job in self._find_all(client, workspace_id, ROLE_JOB):
                with contextlib.suppress(DockerException, OSError):
                    job.remove(force=True)
            with contextlib.suppress(DockerException, OSError):
                anchor.remove(force=True)
            with contextlib.suppress(DockerException, OSError):
                client.volumes.get(self._volume_name(workspace_id)).remove(force=True)

            removed: list[str] = []
            unverified: list[str] = []
            runtime_gone = self._find(client, workspace_id, ROLE_WORKSPACE) is None
            (removed if runtime_gone else unverified).append(RESOURCE_RUNTIME)
            try:
                client.volumes.get(self._volume_name(workspace_id))
                unverified.append(RESOURCE_STORAGE)
            except NotFound:
                removed.append(RESOURCE_STORAGE)

        return RemovalDisposition(
            workspace_id=workspace_id,
            removed=tuple(removed),
            unverified=tuple(unverified),
        )

    # --- engine plumbing --------------------------------------------------

    @contextmanager
    def _engine_failures(self, action: str) -> Iterator[None]:
        """Turn every engine failure inside this block into exit 7, and nothing else.

        Split out from :meth:`_engine` because ``read`` needs the taxonomy
        without the connection lifetime: its client has to outlive the block
        that opened it. See :data:`_ENGINE_FAILURES` for what counts as the
        engine failing.
        """
        try:
            yield
        except _ENGINE_FAILURES as err:
            raise ProviderError(f"the execution engine failed while {action}: {err}") from err

    @contextmanager
    def _engine(self, action: str) -> Iterator[docker.DockerClient]:
        """One connection per verb, and one place engine failure becomes exit 7.

        Per verb rather than cached because that is production: each CLI
        invocation is its own process, so a provider object is not a session and
        pretending otherwise would hide an engine that died between two verbs.
        """
        try:
            client = self._connect()
        except (DockerException, OSError) as err:
            raise ProviderError(
                f"could not reach the execution engine while {action}: {err}"
            ) from err
        try:
            with self._engine_failures(action):
                yield client
        finally:
            with contextlib.suppress(Exception):
                client.close()

    def _probe(self) -> CapabilitySnapshot:
        """Ask the engine what it can enforce. Nothing here is assumed."""
        with self._engine("probing the engine's capabilities") as client:
            version = client.version()
            info = client.info()

        server = str(version.get("Version") or info.get("ServerVersion") or "unknown")
        plugins = info.get("Plugins") or {}
        network_drivers = tuple(plugins.get("Network") or ())
        volume_drivers = tuple(plugins.get("Volume") or ())
        return CapabilitySnapshot(
            engine=f"{PROVIDER_NAME} {server}",
            api_version=str(version.get("ApiVersion") or "unknown"),
            # "No network" is a driver like any other; if the engine does not
            # offer it, isolation cannot be promised.
            network_disable_supported=NULL_NETWORK_DRIVER in network_drivers,
            # This backend offers no host bind mount at all (see the module
            # docstring), so no filesystem-scope expansion can be honoured.
            # Reported honestly here so `resolve` refuses one before a job runs
            # rather than after it has read the host.
            filesystem_scope_enforceable=False,
            # The engine's own cgroup-controller report. `nano_cpus` is
            # implemented with CFS quota and period, so cpu needs both.
            memory_enforceable=bool(info.get("MemoryLimit")),
            cpu_enforceable=bool(info.get("CpuCfsQuota") and info.get("CpuCfsPeriod")),
            pids_enforceable=bool(info.get("PidsLimit")),
            # The canonical measured case: usage is reported through
            # ``/system/df``, and no driver on offer takes a size cap.
            storage_enforceable=any(
                driver in SIZE_CAPPING_VOLUME_DRIVERS for driver in volume_drivers
            ),
        )

    def _ensure_image(self, client: docker.DockerClient, environment: str) -> None:
        """Make the environment locally available, pulling only if it is not.

        A digest-pinned reference cannot have come to mean something else since
        it was last fetched, so re-pulling one already present would cost a
        registry round trip to learn nothing.
        """
        try:
            client.images.get(environment)
        except ImageNotFound:
            client.images.pull(environment)

    def _sealed_kwargs(
        self, policy: EffectivePolicy, network_enabled: bool, workspace_id: str
    ) -> dict[str, Any]:
        """The closed-by-default configuration, shared by every container made here.

        One builder, so a job cannot be given a laxer box than the workspace it
        runs in. What is missing is the point: no ``binds``, no ``devices``, no
        ``volumes_from``, no ``privileged``, and above all no engine socket.
        """
        memory_bytes = int(requested_limit(policy, "memory"))
        return {
            "detach": True,
            "network_mode": "bridge" if network_enabled else "none",
            "mem_limit": memory_bytes,
            # Swap pinned to the memory ceiling: a container allowed to swap has
            # not really been limited, it has been slowed down.
            "memswap_limit": memory_bytes,
            "nano_cpus": int(float(requested_limit(policy, "cpu")) * 1_000_000_000),
            "pids_limit": int(requested_limit(policy, "pids")),
            "privileged": False,
            "security_opt": ["no-new-privileges"],
            "mounts": [
                Mount(
                    target=WORKSPACE_MOUNT_PATH,
                    source=self._volume_name(workspace_id),
                    type="volume",
                    read_only=False,
                )
            ],
            "working_dir": WORKSPACE_MOUNT_PATH,
            "log_config": self._log_config(int(requested_limit(policy, "output_bytes"))),
        }

    @staticmethod
    def _log_config(output_budget: int) -> LogConfig:
        cap = log_cap_bytes(output_budget)
        return LogConfig(
            type="json-file",
            config={"max-size": f"{cap // 1024}k", "max-file": str(LOG_FILE_COUNT)},
        )

    def _workspace_labels(
        self,
        workspace_id: str,
        environment: str,
        created_at: str,
        snapshot: CapabilitySnapshot,
        policy: EffectivePolicy,
    ) -> dict[str, str]:
        """Identity, provenance, and the create-time enforced-vs-measured record.

        Written onto the engine object rather than only into headspace's state
        store, so the answer to "what did this host promise for this workspace?"
        survives a lost store, a crashed CLI, and an operator who only has
        ``docker inspect``.
        """
        labels = {
            LABEL_WORKSPACE_ID: workspace_id,
            LABEL_PROVIDER: PROVIDER_NAME,
            LABEL_ROLE: ROLE_WORKSPACE,
            LABEL_CREATED_AT: created_at,
            LABEL_ENVIRONMENT: environment,
        }
        for field in fields(snapshot):
            labels[f"{LABEL_CAPABILITY_PREFIX}{field.name}"] = _label_value(
                getattr(snapshot, field.name)
            )
        for limit in policy.limits:
            labels[f"{LABEL_LIMIT_PREFIX}{limit.name}"] = limit.status.value
        return labels

    # --- lookup by label, never by handle ---------------------------------

    @staticmethod
    def _container_name(workspace_id: str) -> str:
        return f"{OBJECT_PREFIX}{workspace_id}"

    @staticmethod
    def _volume_name(workspace_id: str) -> str:
        return f"{OBJECT_PREFIX}{workspace_id}"

    @staticmethod
    def _find_all(client: docker.DockerClient, workspace_id: str, role: str) -> list[Container]:
        return client.containers.list(
            all=True,
            filters={"label": [f"{LABEL_WORKSPACE_ID}={workspace_id}", f"{LABEL_ROLE}={role}"]},
        )

    @classmethod
    def _find(cls, client: docker.DockerClient, workspace_id: str, role: str) -> Container | None:
        found = cls._find_all(client, workspace_id, role)
        return found[0] if found else None

    def _require(self, client: docker.DockerClient, workspace_id: str) -> Container:
        anchor = self._find(client, workspace_id, ROLE_WORKSPACE)
        if anchor is None:
            raise unknown_workspace(workspace_id)
        return anchor

    def _active_jobs(self, client: docker.DockerClient, workspace_id: str) -> int:
        return sum(
            1
            for job in self._find_all(client, workspace_id, ROLE_JOB)
            if job.status in LIVE_STATUSES
        )

    @staticmethod
    def _network_enabled(anchor: Container) -> bool:
        """What the engine actually configured, never what was asked for."""
        return (anchor.attrs["HostConfig"].get("NetworkMode") or "none") != "none"

    @staticmethod
    def _state_of(anchor: Container) -> State:
        """``ready`` across every job — but ``failed`` if the runtime is gone.

        A workspace never sits in ``running`` (see
        :mod:`headspace.providers.base`); in-flight work is a count. A workspace
        container that has exited, though, is a workspace whose runtime died,
        and reporting that as ``ready`` would hand a caller a box that is not
        there.
        """
        status = str((anchor.attrs.get("State") or {}).get("Status") or "")
        return State.READY if status in LIVE_STATUSES else State.FAILED

    def _volume_bytes(self, client: docker.DockerClient, workspace_id: str) -> int:
        """Measured workspace storage, from the engine's own usage report.

        ``/system/df`` is the only place the engine exposes a volume's size, and
        it is a report, not a cap — which is exactly why the policy view labels
        storage ``measured``.
        """
        name = self._volume_name(workspace_id)
        for entry in client.df().get("Volumes") or []:
            if entry.get("Name") == name:
                return int((entry.get("UsageData") or {}).get("Size") or 0)
        return 0

    def _describe(self, client: docker.DockerClient, anchor: Container) -> WorkspaceDescriptor:
        labels = anchor.labels
        workspace_id = labels[LABEL_WORKSPACE_ID]
        return WorkspaceDescriptor(
            workspace_id=workspace_id,
            provider=PROVIDER_NAME,
            state=self._state_of(anchor),
            environment_digest=environment_digest(
                labels.get(LABEL_ENVIRONMENT) or anchor.attrs["Config"]["Image"]
            ),
            # From the label, not from the engine's own ``Created`` field: the
            # descriptor a caller got at create time and the one `inspect`
            # returns later must be the same fact, in the same format.
            created_at=labels.get(LABEL_CREATED_AT) or utc_now(),
            network_enabled=self._network_enabled(anchor),
            storage_bytes=self._volume_bytes(client, workspace_id),
            active_jobs=self._active_jobs(client, workspace_id),
            # The engine's own handle, sealed. Nothing above the seam may read
            # it, and the conformance scanner proves it appears nowhere else.
            ref=OpaqueRef(anchor.id),
        )

    # --- running one job --------------------------------------------------

    def _await_exit(self, container: Container, wall_budget: float, usage: _Usage) -> bool:
        """Block until the job settles or outruns its budget; kill it if it does.

        Polls rather than blocking on the engine's ``wait``: a wait that times
        out and an engine that died are the same exception from the transport,
        and a provider that cannot tell those apart is one careless ``except``
        away from reporting a broken daemon as a timed-out computation.
        Re-reading the container's state answers both questions unambiguously.
        """
        deadline = time.monotonic() + wall_budget
        killed = False
        while True:
            container.reload()
            if str(container.attrs["State"].get("Status") or "") not in LIVE_STATUSES:
                return killed
            usage.sample(container)
            if time.monotonic() >= deadline:
                if killed:
                    raise ProviderError(
                        "the engine did not stop a job that outran its wall-clock budget "
                        f"within {KILL_GRACE_SECONDS:.0f}s"
                    )
                try:
                    container.kill()
                except APIError:
                    # It exited between the poll and the signal: the job
                    # finished on its own, and calling that a timeout would
                    # discard a real exit status.
                    return False
                killed = True
                deadline = time.monotonic() + KILL_GRACE_SECONDS
            time.sleep(POLL_INTERVAL_SECONDS)

    def _captured(
        self, container: Container, capture: _Capture, budget: int
    ) -> tuple[str, int, bool]:
        """The job's output, bounded, plus the true volume it stood in for.

        Falls back to the engine's retained log only if the live reader failed
        — and says so through ``truncated``, because a fallback read can only
        ever see what the log driver's cap left behind.
        """
        if not capture.failure:
            return capture.text, capture.produced, capture.truncated
        kept = bytearray()
        produced = 0
        with contextlib.suppress(DockerException, OSError):
            for chunk in container.logs(stream=True, follow=False, stdout=True, stderr=True):
                produced += len(chunk)
                room = budget - len(kept)
                if room > 0:
                    kept += chunk[:room]
        return kept.decode("utf-8", "ignore"), produced, True

    @staticmethod
    def _status(timed_out: bool, exit_status: int) -> str:
        if timed_out:
            return STATUS_TIMEOUT
        return STATUS_SUCCESS if exit_status == 0 else STATUS_FAILURE
