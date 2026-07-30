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

Getting a file back in
----------------------
The write verb is the inbound counterpart, and it is deliberately **not** the
read verb run backwards. The difference is that the engine's archive endpoint
offers no commit point in this direction: ``put_archive`` extracts a tar
wherever it is pointed and answers ``True``, and there is no moment at which
the daemon says "these bytes are now that file, and they are the bytes you
sent". Everything below exists to build that moment out of pieces the engine
does provide.

So a write is three steps against the **workspace container**, and the bytes
never touch the caller's own destination until the last one:

1. **prepare** (:data:`PREPARE_STAGING_SCRIPT`) — an exec that checks the image
   really carries the tools the verification needs
   (:data:`REQUIRED_WRITE_TOOLS`) and creates a fresh, headspace-owned staging
   directory under :data:`STAGING_DIR_NAME` inside the volume. The tool check
   comes first because the alternative is worse than a refusal: a distroless
   image would otherwise fail somewhere in the middle of a copy, and a caller
   would be told the engine broke when the truth is that the profile it chose
   has no ``sha256sum``. The reserved staging prefix is itself resolved and
   bounded rather than trusted — reserving a name does not stop a job creating
   it as a link out of the volume, and a prepare step that trusted its own
   reservation would make streaming bytes *outside* the volume the very first
   thing a copy-in did.
2. **transfer** — one tar with exactly one member, streamed into that staging
   directory and never at the caller's destination. Pointing the transfer at
   the destination directly would publish unverified bytes under the name the
   caller will read back, which is the failure this whole shape avoids. It is
   also why the staging directory lives *inside the volume*: the final rename
   is atomic only within one filesystem, and the container's own writable layer
   is a different one.
3. **finalize** (:data:`FINALIZE_WRITE_SCRIPT`) — an exec that re-hashes the
   staged file with ``sha256sum``, resolves the destination's parent with
   ``realpath`` and refuses anything landing outside the volume root, refuses
   an existing destination unless the caller asked for a replacement, and only
   then renames within the volume.

The in-container re-hash is the load-bearing step, not a belt on top of the
host's braces. The host already hashed what it *sent*; only the container can
hash what is *there*. Between the archive landing and the rename, a job
container shares the same volume and is not a headspace verb, so no lock
excludes it from rewriting the staged file — and a host-side digest would then
certify bytes that were swapped after they were hashed. The parent resolution
is the write-path twin of the read verb's symlink refusal, and it exists for
the same reason stated the other way round: engine-side path resolution follows
links, so a job that plants ``results -> /etc`` turns a copy-in into a write
through it. ``realpath`` inside the container is what notices; nothing in the
archive endpoint would.

The one failure a copy-in cannot tidy up after is the one where it does not
survive: a process killed between the transfer and the rename leaves a staging
directory whose nonce died with it, and ``write``'s signature hands no staging
token to an orchestrator that might have kept one. That is what makes the
staging prefix *reserved* rather than merely conventional —
:meth:`DockerProvider.reap_staging` can clear the whole of it precisely because
nothing but headspace is entitled to put anything there. Reconciliation cannot
name the residue; it can only ask the backend to clear its own prefix, and get
back the list of what actually went.

Two things this verb needs that ``read`` does not, and one it refuses to want:

* **A live runtime.** ``read`` must work on a workspace whose container has
  exited, because that is the case it exists for. Copy-in has no equivalent
  salvage case — it feeds a job that has not run yet — and it cannot verify
  anything without an exec, so a stopped anchor is refused honestly rather than
  worked around. Recorded live (Docker 29.1.3 / API 1.52, 2026-07-29): a
  ``put_archive`` against a *stopped* container succeeds. The refusal is
  therefore about verification, not about transport, and the message says so.
* **A shell and five tools in the image.** Named in the refusal along with the
  profile that lacks them, because "your engine broke" would send an agent to
  restart a daemon over a choice of base image.
* **No helper container.** A short-lived container mounting the same volume
  would make staging trivial and would be a second engine object to reap on
  every failure path — and copy-in has *more* failure paths than read, not
  fewer. "Nothing was created" is a stronger guarantee than any reaper, and a
  test asserts the engine's object count does not move.

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

There is exactly one exception, and it proves the rule rather than bending it:
an engine 400 that says *the container's init step could not exec the command*
is not the engine failing, it is the caller naming a binary the image does not
have. :func:`not_executable_exit_status` recognises that one case by the
engine's own marker and :meth:`DockerProvider._start` returns it as a failed
:class:`~headspace.providers.base.JobOutcome` (126 or 127) instead of exit 7.
Everything the matcher does not recognise re-raises untouched, so the exception
can only ever narrow the exit-7 set — never widen it.

Killed for exceeding a ceiling, not merely killed
--------------------------------------------------
A job's exit status alone cannot distinguish a kernel OOM kill from a program
that chose 137 for its own reasons — verified live: a genuine memory kill
reports ``State.OOMKilled=true, ExitCode=137``, and
``python -c "raise SystemExit(137)"`` reports ``OOMKilled=false, ExitCode=137``.
So :meth:`DockerProvider._status` never infers from the number; it reads
``State['OOMKilled']`` — fetched in :meth:`run` at the same point as
``ExitCode``, off the same ``reload()`` :meth:`_await_exit` already performed,
so classifying the kill costs no engine call of its own — and reports
:data:`~headspace.core.result.STATUS_RESOURCE_EXHAUSTED` only when the engine
itself says the kernel did this.

Precedence still has to be decided, because headspace's own wall-clock
enforcer (:meth:`_await_exit`) also stops a container with ``kill()``, and that
yields the same 137 a kernel OOM kill does. ``_status`` checks ``timed_out``
first: when headspace stopped the job deliberately, that outranks the kernel's
budget, so a container that is *both* timed out and ``OOMKilled`` is still
reported ``timeout`` — a case a test pins directly rather than leaving to
branch order.

A secret channel that is not ``argv`` and not a label (issue #13)
-------------------------------------------------------------------
``run``'s ``env`` keyword exists because the two channels a job already had
were both confessions. ``argv`` is recorded verbatim in every surface that
renders a command and is readable off a live process's own
``/proc/<pid>/cmdline`` — a job's command line was never a private place. A
label is worse: this module's own identity scheme depends on labels being
readable by anyone who can run ``docker inspect`` (see "Why labels, and never
handles" above), so a value put there is deliberately public, the opposite of
what a caller reaching for ``env`` wants.

So ``env`` crosses exactly one boundary: the job container's own
``environment=`` creation kwarg, built in :meth:`run` and nowhere else in this
module. It is not folded into :meth:`_sealed_kwargs`, the one builder shared by
every container this provider makes (see "Closed by default, and closed at the
engine" above), because that sharing is precisely what must *not* happen here —
:meth:`create`'s anchor container is long-lived and outlives every job a
workspace ever runs, and an env value baked into it would leak a secret handed
to one job into every job that workspace runs afterward, forever, with no
caller having asked for that. Threading ``env`` through ``run`` alone, instead,
makes the job container — already the shortest-lived object this provider
creates (see "Jobs get their own containers" above) — the only place the
secret is ever readable, for exactly as long as that job runs.

Ending a job without becoming a second state writer (issue #13)
-------------------------------------------------------------------
:meth:`run` is synchronous, blocking, and holds the workspace's flock for a
job's entire duration — which is what makes it the store's single writer for
that workspace, for as long as the job runs. An operator ending a runaway job
cannot go through ``run`` to do it, because that call is already in progress
and will not return until the job it is watching does. ``stop`` is therefore a
second, narrower verb, engine-side only: it looks the job container up by
label (the same :data:`LABEL_WORKSPACE_ID` / :data:`LABEL_ROLE` pair every
other verb here uses), signals it, and returns. It never opens
``headspace.core.store``, never takes the workspace lock, and never writes a
byte under the store root — a ``stop`` that did any of those would be racing
the ``run`` call it exists to interrupt, which is exactly the corruption the
store's per-workspace lock exists to prevent. The still-blocked ``run`` call
discovers the ending on its own — :meth:`_await_exit` is already polling the
same engine object with ``reload()`` — and journals the outcome itself, so the
store keeps exactly one writer even while two verbs act on the same workspace
at once.

Graceful before forceful, and only if it has to be: :meth:`stop` sends
``SIGTERM`` (``container.stop(timeout=...)``) and gives the job
:data:`STOP_GRACE_SECONDS` to end itself before escalating to ``SIGKILL``
(``container.kill()``) — the same "ask nicely, then force it" shape
:meth:`_await_exit` already gives a job that outran its wall-clock budget. A
workspace with nothing running is not an error: a caller racing a job that
happened to finish on its own between its decision and this call landing is
the ordinary case, not a mistake, so it is reported as a fact — no job id,
nothing stopped — rather than raised.

Recording that an operator ended it, and not that it failed (issue #16)
-----------------------------------------------------------------------
The section above ends with the two verbs agreeing on who writes state: ``run``
narrates, ``stop`` only signals. That leaves ``run`` having to *know* something
only ``stop`` did — and it cannot work it out. What a stop leaves at the engine
is a container that exited 137 with ``OOMKilled=false``, which is byte for byte
what ``python -c "raise SystemExit(137)"`` leaves (the same live probe recorded
under "Killed for exceeding a ceiling" above). No heuristic separates those, so
before this channel existed a job an operator deliberately ended was recorded
``failure`` at exit 6 — telling an operator, in the job's own record, that their
work had failed.

The fix is a **positive signal**, never an inference — and, because one signal
turned out to be able to lie, a signal in two phases:

``intent`` (:data:`CANCELLATION_INTENT_MARKER_NAME`)
    written by :meth:`stop` into the workspace volume *before* it signals,
    carrying the job id it is about to end. It records that an operator asked.
``countersignal`` (:data:`CANCELLATION_SIGNALLED_MARKER_NAME`)
    written by :meth:`stop` through the anchor once the signalling has run its
    course, carrying the same id. It records that the ask took effect, and it
    is the only one of the two that can, because it cannot exist until it has.

:meth:`run`, on any outcome at all, reads both names back and reports
:data:`~headspace.core.result.STATUS_CANCELLED` when — and only when — the
*countersignal* names the job that just ran. Intent alone, neither marker, or
either one naming another job leaves the classification exactly as it was.

Why one marker was not enough is the whole of the second phase. A single
sentinel had to be written before the signal, or the still-blocked :meth:`run`
would miss it; but a sentinel written before the signal is a claim about the
future. A ``stop`` killed in the gap — the operator's own ``SIGINT``, a lost
connection, a signal the engine refused — left a sentinel naming a job it never
ended. That job would often go on to fail entirely on its own account, find the
sentinel naming *itself*, and be recorded ``cancelled``: headspace asserting, in
a job's permanent record, that a human deliberately stopped work that in truth
broke by itself. An agent reading that stops looking for the defect; an operator
reading it is told they did something they did not do. A missing cancellation
says less than the truth, which is survivable; a fabricated one says something
else entirely, which is not. So the two phases split "asked" from "happened",
and only "happened" is allowed to speak.

The intent marker still earns its keep, and not as corroboration — it is what
makes a countersignal written after the signal *readable at all*. The two
processes wake up from the same starting gun: ``stop`` blocks until the
container it signalled has exited, and :meth:`run` is polling that same
container, so at the moment the job dies ``stop`` still owes one exec round trip
and :meth:`run` is already on its way to classifying. Without a warning,
:meth:`run` would read the volume in that gap and record almost every completed
cancellation as a failure. An intent marker naming the job that just settled is
that warning, and :meth:`run` holds the door open for
:data:`CANCELLATION_SETTLE_SECONDS` — a wait that can only ever turn a missed
cancellation into a recorded one, because what it waits for is still the
countersignal and nothing else.

The volume is the only channel available, and that is not a convenience. The
two verbs are two processes; ``run`` holds the workspace's flock for the job's
whole duration, so headspace's own state is closed to ``stop`` by construction
(see the section above). What is left is the engine, and inside the engine the
one writable thing that outlives a job is the workspace volume. So both markers
go there, through mechanisms both verbs already own: an exec against the anchor
to write them — the same object and the same shell a copy-in stages through —
and ``get_archive`` against that anchor to read them, the same call the ``read``
verb makes and for the same reason, since by classification time the job's own
container has been reaped and the anchor may itself have exited.

Four properties this shape has to carry, none of them optional:

* **Unforgeable from inside the box.** A job shares the volume and can write
  anything it likes at either marker's path, so what makes a marker believable
  cannot be the write — it has to be the *payload*, and the payload has to
  contain something the job cannot obtain. It contains two things: the job id,
  and a per-workspace secret (:data:`LABEL_CANCEL_TOKEN`) minted at
  :meth:`create` and carried on the anchor container as an engine label. The
  pair is compared verbatim, never parsed, so a ``job_id`` holding the
  separator cannot shift the boundary between them.

  Neither half is reachable from inside a container. The id travels as a
  container *name* and a ``headspace.job_id`` label, and a process can read
  neither from within its own container — a container's hostname is its engine
  id, not its name; :meth:`run` puts it in no environment variable and no argv
  entry, held to the same placement discipline as ``env`` and tested the same
  way, across seven surfaces at once
  (``test_no_surface_inside_a_job_container_carries_its_own_job_id``). The
  token is a label on a *different container* again, and labels are engine
  metadata no process inside any container can read.

  The id alone was not enough, and the reason is worth keeping: ``--job-id`` is
  the **caller's** to choose, so a caller who picks predictable ids *and* runs
  code it does not trust would have handed that code the only string it was
  missing. That was real — a live test forged a cancellation exactly that way
  before the token existed, and the same test now asserts the refusal
  (``test_a_countersignal_a_job_planted_for_itself_is_refused``). With the
  token the job must guess 128 bits it can never observe, whatever the caller
  named the job.

  The boundary is job-versus-record and nothing more is claimed. Whoever can
  reach the Docker socket can read the label; whoever can reach
  ``~/.headspace`` can edit the record directly. The adversary this defends
  against is the untrusted code inside the box, which is the one that cannot
  be reasoned with.
* **Hardened against what a job can plant, both directions.** The write renames
  onto a marker's name rather than redirecting at it, because a redirection
  follows a link a job planted and a rename replaces it; it refuses outright
  when anything that is not a regular file already stands there, because
  ``mv -f file dir`` does not fail but *relocates* — a planted directory would
  otherwise make the write appear to succeed while the marker went inside it.
  The read accepts only a regular file, because the archive endpoint resolves
  link targets within the container's filesystem quite happily. Those are the
  same refusals :data:`FINALIZE_WRITE_SCRIPT` and :meth:`_archived_file`
  already make, for the same reasons, applied to the two paths headspace writes
  on its own account.
* **Consumed, always, and under both names.** :meth:`run` clears whatever it
  found on *every* path, exit 0 included — a ``stop`` that raced a job
  finishing on its own leaves markers no failing branch would ever reach, and
  one nobody consumes waits in the volume for whichever later job happens to
  fail. Clearing only the name a classification happened to read is the easy
  way to reintroduce that wedge with a phase still standing in it, so the
  obligation is stated over :data:`CANCELLATION_MARKER_NAMES` rather than over
  whichever marker mattered. Nothing else ever collects them: reconciliation
  reads the journal and reaches the engine, and neither knows these names
  exist, so a ``run`` that crashed before it could clear leaves them for the
  *next* run in that workspace to take away.
* **Failing towards silence, never towards invention.** Every partial state
  this channel can be caught in — a marker that cannot be written, one that
  cannot be read, one a job tampered with, a ``stop`` that died owing its
  countersignal — costs a true cancellation its name and classifies exactly as
  the code before this channel existed. None of them can produce a
  ``cancelled`` that did not happen. The one irreducible window is in
  :meth:`stop`, between the signal landing and the countersignal being written,
  and it fails in that same direction by construction rather than by luck.

Both halves therefore degrade towards today's behaviour rather than towards a
wrong answer. A marker that cannot be written does not withhold the signal —
ending the runaway job is what the operator came for, and naming it is the
improvement on top. A marker that cannot be read does not raise: the job
already ran and its outcome is in hand, and turning that into "the engine
broke, retry" over an auxiliary file would discard the work ``run`` exists to
report.
"""

from __future__ import annotations

import contextlib
import hashlib
import math
import posixpath
import re
import secrets
import tarfile
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, fields
from types import MappingProxyType
from typing import IO, Any

import docker
from docker.errors import APIError, DockerException, ImageNotFound, NotFound
from docker.models.containers import Container
from docker.types import LogConfig, Mount

from headspace.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from headspace.core import profiles
from headspace.core.artifacts import ByteSource
from headspace.core.policy import CapabilitySnapshot, EffectivePolicy
from headspace.core.result import (
    STATUS_CANCELLED,
    STATUS_FAILURE,
    STATUS_RESOURCE_EXHAUSTED,
    STATUS_SUCCESS,
    STATUS_TIMEOUT,
    ResourceUsage,
)
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
    StopOutcome,
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
#: The per-workspace secret that makes a cancellation marker *unforgeable*
#: rather than merely hard to guess. Minted at :meth:`DockerProvider.create`,
#: written onto the **anchor container only** — never the volume, never a job
#: container, never headspace's own state — and required verbatim in both
#: markers before ``run`` will read a cancellation out of them.
#:
#: A label is exactly the right home and the reason is structural. ``stop`` and
#: ``run`` are separate processes that share no memory, so a secret held in
#: either one's variables is a secret the other cannot check; both, however,
#: already hold the anchor. And a label is engine metadata: it is readable by
#: anyone who can talk to the daemon and by *no process inside any container* —
#: not even the anchor's own, and the job runs in a different container again.
#:
#: What this closes is the gap that made ``job_id`` alone insufficient. That id
#: is the caller's to choose (``--job-id``), so a caller who picks predictable
#: ids and runs code it does not trust has handed that code the only string it
#: was missing. With a token in the payload the job would have to guess 128
#: bits it can never observe, whatever the caller named the job.
#:
#: The boundary is job-versus-record, and nothing more is claimed: whoever can
#: reach the Docker socket can read this label, and whoever can reach
#: ``~/.headspace`` can edit the record directly. The adversary this defends
#: against is the untrusted code *inside* the box.
#:
#: The ``nosec`` is a false positive worth naming rather than skipping
#: repo-wide: B105 matches on the *variable name* ending in ``token``, and what
#: is hardcoded here is the label's **key**. The value it names is minted per
#: workspace by :func:`secrets.token_hex` at :meth:`DockerProvider.create` and
#: appears in no source file.
LABEL_CANCEL_TOKEN = "headspace.cancel_token"  # nosec B105

#: Bytes of entropy behind that token. 128 bits, from :mod:`secrets` rather
#: than :mod:`random`, because this one is guessed against rather than merely
#: collided with.
CANCEL_TOKEN_BYTES = 16

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

#: The default for ``run(..., env=...)``: a real empty mapping, never a bare
#: ``{}`` default value. Created once at import time and made immutable
#: (:class:`~types.MappingProxyType`), so a caller that never mentions ``env``
#: can never be made to share a mutable default with every other such caller —
#: the same mutable-default hazard every keyword default in this codebase is
#: built to avoid structurally rather than by convention. See
#: :meth:`DockerProvider.run` for where it lands and where it deliberately
#: never does; ``headspace.providers.fake.EMPTY_ENV`` closes the identical gap
#: on the other backend, the same way, for the same reason.
EMPTY_ENV: Mapping[str, str] = MappingProxyType({})

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
#: How long :meth:`DockerProvider.stop` gives a job to end itself after
#: ``SIGTERM`` before escalating to ``SIGKILL``. Ten seconds is the engine's
#: own ``docker stop`` default, chosen deliberately rather than reused by
#: accident: a job's cleanup handlers get the grace period every other tool
#: already trained an operator to expect, not a headspace-specific surprise
#: in either direction.
STOP_GRACE_SECONDS = 10

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

# --- a command the image cannot execute -------------------------------------
#
# :data:`_ENGINE_FAILURES` catches :class:`DockerException`, and
# :class:`APIError` is a subclass of it — so before this section existed, an
# engine 400 saying "your command is not in the image" arrived at the caller as
# exit 7 with a "retry" hint. That is wrong twice over. An autonomous consumer
# that believes the hint retries a deterministic failure forever, and the raw
# API string it retries on carries a container id and the engine's internal
# endpoint URL into the very context headspace exists to keep clean.
#
# The reclassification below is deliberately *narrow*, and the direction of the
# safety is the reason. Reading a caller's mistake as a broken engine costs a
# retry loop; reading a broken engine as a caller's mistake makes an agent
# abandon work it should have retried, which is the worse error. So anything
# that does not match confidently re-raises untouched and keeps exit 7 — this
# whole section can only ever move cases *out* of exit 7, never into it.

#: The engine's own marker for "the container's init step could not exec what it
#: was handed". Probed against Docker 29.1.3 / API 1.52 (2026-07-28): all four
#: shapes of not-runnable command — absent from ``$PATH``, an absolute path that
#: is not there, a directory, and a file without the execute bit — carry this
#: substring and nothing else the provider sees does. It is the whole matcher:
#: "any 400" would sweep up port conflicts, name conflicts and busy devices,
#: which are the engine's problem and must stay retryable.
EXEC_INIT_MARKER = "error during container init: exec:"

#: The shell's two answers, kept apart on purpose. Collapsing both to 127 would
#: tell an agent to re-spell a command that was sitting right there — the fix is
#: for *what* to do next, so the two cases cannot share a number.
EXIT_COMMAND_NOT_EXECUTABLE = 126
EXIT_COMMAND_NOT_FOUND = 127

#: Reason wordings that mean nothing exists under that name. Everything else in
#: the exec-init family is "there, but not runnable" — the conservative default,
#: because 126 never sends anyone off to fix a spelling that was already right.
NOT_FOUND_WORDINGS: tuple[str, ...] = (
    "executable file not found",
    "no such file or directory",
)

#: What closes the caller's own ``argv[0]`` in the exec step's detail, which is
#: quoted and followed by the engine's reason. Split on the *first* occurrence so
#: the reason is read *past* the command — a caller who runs ``/tmp/no such file
#: or directory`` must not be told their command was missing when the engine said
#: it was there and unrunnable. A partition rather than a regex: "up to the first
#: ``":``" is precisely what partition means, and saying it as a lazy quantifier
#: only invited a backtracking question the string method never raises.
_EXEC_INIT_SEPARATOR = '":'

#: What replaces an engine handle or endpoint that must not reach a caller.
REDACTED = "[redacted]"

#: The transport envelope ``requests`` wraps an engine error in — the HTTP
#: status phrase and the internal endpoint, which is where the container id
#: leaked from. Stripped rather than trusted absent, because the fallback path
#: below may have to fall back to ``str(err)``.
_TRANSPORT_ENVELOPE_RE = re.compile(r"\b\d{3} (?:Client|Server) Error for \S+\s*")

#: Any URL-shaped token. The endpoint above is the one that matters; this is the
#: net under it, so a wording change in the SDK cannot restore the leak.
_ENGINE_URL_RE = re.compile(r"\b[A-Za-z][\w.+-]*://\S*")

#: The prefix length the engine and its CLI use for a short container id. Both
#: forms are redacted, because either identifies the object.
_SHORT_HANDLE_CHARS = 12


def _exec_init_reason(detail: str) -> str | None:
    """The engine's reason with the caller's quoted ``argv[0]`` read off the front.

    ``None`` when the detail is not shaped that way at all, which is the signal
    to fall back to the whole string rather than to a half-parsed piece of it.
    The trailing ``or rest[-1]`` covers a reason that is nothing but whitespace:
    the separator was still found, so the detail *was* shaped correctly, and
    reporting no reason at all would lose that.
    """
    stripped = detail.lstrip()
    if not stripped.startswith('"'):
        return None
    _argv0, separator, rest = stripped[1:].partition(_EXEC_INIT_SEPARATOR)
    if not separator or not rest:
        return None
    return rest.lstrip() or rest[-1]


def not_executable_exit_status(message: str) -> int | None:
    """``126``/``127`` if this engine message means the image cannot run the command.

    ``None`` for everything else, and ``None`` is what keeps this honest: the
    caller re-raises on it, so an unmatched message is a no-op rather than a
    guess. Pure and public so the mapping can be pinned against the engine's
    recorded wordings without an engine.
    """
    if EXEC_INIT_MARKER not in message:
        return None
    detail = message.partition(EXEC_INIT_MARKER)[2]
    found = _exec_init_reason(detail)
    reason = found if found is not None else detail
    if any(wording in reason for wording in NOT_FOUND_WORDINGS):
        return EXIT_COMMAND_NOT_FOUND
    return EXIT_COMMAND_NOT_EXECUTABLE


def redact_engine_text(text: str, handle: str = "") -> str:
    """An engine string with its transport envelope and its handles removed.

    Two mechanisms because they fail differently: ``handle`` is exact knowledge
    (the provider holds the id it just created, so it can remove it by name),
    and the patterns are the general net for whatever else the engine puts in
    front of its own sentence. What survives is the engine's *diagnosis*, which
    is the part a reader needs and the part that names nothing internal.
    """
    cleaned = _TRANSPORT_ENVELOPE_RE.sub("", text)
    cleaned = _ENGINE_URL_RE.sub(REDACTED, cleaned)
    if handle:
        for token in (handle, handle[:_SHORT_HANDLE_CHARS]):
            cleaned = cleaned.replace(token, REDACTED)
    return cleaned.strip()


# --- copying a file in: stage, verify, then rename --------------------------
#
# The whole shape, and why it is this shape rather than the obvious one, is in
# the module docstring under "Getting a file back in". What lives here is the
# vocabulary the two scripts and the provider have to agree on, in one place so
# they cannot drift apart: the paths, the tools, and the exit statuses that
# carry a refusal back out of the container.

#: The reserved directory every copy-in stages through, relative to
#: :data:`WORKSPACE_MOUNT_PATH`. Dot-prefixed and namespaced so a human reading
#: the volume can tell headspace's scratch space from a job's, and *inside the
#: volume* rather than in the container's own ``/tmp`` — which is what makes
#: the final rename atomic, since a rename is only atomic within one
#: filesystem and the container's writable layer is a different one.
STAGING_DIR_NAME = ".headspace-staging"

#: The name the transfer archive's single member carries inside the staging
#: directory. Fixed rather than derived from the caller's path, so nothing
#: inspecting the staging area can mistake an unverified copy for the artifact
#: it is about to become — the destination's name only ever exists once the
#: bytes under it have been verified.
STAGED_FILE_NAME = "payload"

#: The mode the staged member carries. World-readable on purpose: the finalize
#: exec runs as whatever user the image declares, and a mode only ``root``
#: could read would make a non-root profile fail for a reason that has nothing
#: to do with the bytes.
STAGED_FILE_MODE = 0o644

#: What :data:`FINALIZE_WRITE_SCRIPT` needs the image to provide. Passed *into*
#: the prepare script as arguments rather than repeated inside it, so the
#: preflight and the step it is protecting cannot drift apart. ``rm`` earns its
#: place with the others: the cleanup path is the one that runs after something
#: has already gone wrong, and a cleanup that fails then leaves residue in the
#: caller's own volume.
REQUIRED_WRITE_TOOLS: tuple[str, ...] = ("mkdir", "mv", "realpath", "rm", "sha256sum")

#: The shell both scripts run under — the same interpreter :data:`IDLE_COMMAND`
#: already requires of the image, so a workspace that could be created can
#: always be written to.
WRITE_SHELL = "/bin/sh"

#: ``$0`` for both scripts. Every value the scripts touch — the caller's path,
#: the digest, the staging paths — is passed as an *argument* rather than
#: interpolated into the script text. That is not tidiness: ``require_workspace_path``
#: bounds a path's shape but says nothing about quotes or ``$``, and a script
#: built by interpolation would let a destination name spell a command.
SCRIPT_ARGV0 = "headspace-write"

#: How the scripts hand one piece of evidence back: a single line whose tail is
#: the detail belonging to the exit status beside it — the missing tool, the
#: digest that was actually found, the path something resolved to. Everything
#: else the script prints is the shell's own noise and is treated as such.
SCRIPT_DETAIL_MARKER = "headspace-write:"

#: The statuses the scripts exit with to name a condition the provider must
#: classify rather than report as a broken engine. Chosen above 10 and below
#: 126 so they cannot collide with the shell's own conventions — ``1`` and ``2``
#: for its usage errors, ``126``/``127`` for a command it could not run, and
#: ``128+n`` for a signal. Anything the provider does not recognise here is an
#: engine failure with the script's output kept as evidence, which is the
#: fail-safe direction: a condition invented later cannot quietly become a
#: successful write.
WRITE_MISSING_TOOL = 11
WRITE_STAGING_UNUSABLE = 12
WRITE_DIGEST_MISMATCH = 13
WRITE_ESCAPES_VOLUME = 14
WRITE_DESTINATION_EXISTS = 15
WRITE_DESTINATION_IS_DIRECTORY = 16
WRITE_STAGED_FILE_MISSING = 17
#: The staged path is a symlink rather than the regular file the transfer put
#: there. Checked before anything reads it, because ``[ -f ]`` and ``sha256sum``
#: both *follow* a link: a job sharing the volume can race the window between
#: the transfer and this exec, replace the staged file with a link to a path it
#: controls, and have the digest certify the link's target. Every later check
#: would then pass and ``mv`` would rename the link itself into the caller's
#: destination — a file inside the workspace resolving outside the volume,
#: whose bytes the job can still rewrite after the copy-in reported success.
#: The digest is what makes this fatal rather than untidy: it would be a true
#: statement about bytes nobody can rely on afterwards.
#:
#: Tested twice, and the second test is the honest part. The first, before the
#: hash, defeats the reliable attack: swap the file, let the digest certify the
#: link's target, walk away. The second, immediately before the rename, narrows
#: what is left — a job would have to land the swap inside the gap between that
#: test and ``mv``, with no way to observe when the gap opens. **It is narrowed,
#: not closed**: POSIX offers no "rename only if this is not a symlink", the
#: same shape of admission :data:`FINALIZE_WRITE_SCRIPT` already makes about
#: resolving the destination's parent. What stays guaranteed is that a link
#: sitting there at either checkpoint is refused and nothing is renamed.
WRITE_STAGED_PATH_IS_A_LINK = 18

#: How large a bite is taken out of the caller's source at a time.
WRITE_CHUNK_BYTES = 1 << 20

#: The engine's own marker for "the exec's binary is not in this image".
#: Probed against Docker 29.1.3 / API 1.52 (2026-07-29), and the probe recorded
#: something worth stating plainly: an exec whose binary is missing does **not**
#: raise. It comes back as a perfectly ordinary :class:`ExecResult` carrying
#: exit 127 and the OCI diagnostic as its *output*. A provider that only caught
#: :class:`APIError` would therefore read a distroless image's answer as the
#: script failing on its own account and report a broken engine — so the output
#: is inspected for this marker, and only alongside 126/127, which keeps the
#: match narrow enough that a script printing the phrase itself cannot trip it.
EXEC_START_MARKER = "unable to start container process: exec:"

#: The engine's own wording for an exec against a container that has stopped,
#: recorded from the same probe: a 409 whose explanation reads ``container
#: <64 hex> is not running``. Matched so the liveness race — the anchor stopping
#: between the check and the exec — reaches the caller as the same honest
#: condition the check itself produces, rather than as a raw engine error
#: carrying a container id.
NOT_RUNNING_MARKER = "is not running"

#: What a refusal says when the shell could not resolve the path it was asked
#: about. ``realpath`` failing is not the same as resolving somewhere outside
#: the volume, but both end the same way — nothing is renamed — and the caller
#: is owed a phrase that does not pretend to know more than the script did.
UNRESOLVABLE_PATH = "somewhere unresolvable"

#: The one container status a copy-in accepts. Deliberately narrower than
#: :data:`LIVE_STATUSES`: ``created`` and ``paused`` both mean the runtime
#: object exists, and neither can execute the verification this verb depends
#: on. A status that cannot run an exec is a stopped anchor as far as this verb
#: is concerned, whatever the lifecycle view makes of it.
RUNNING_STATUS = "running"

#: Create the staging directory, having first proved the image can finish the
#: job. ``$1`` volume root, ``$2`` staging root, ``$3`` this write's staging
#: directory, ``$4...`` the tools :data:`REQUIRED_WRITE_TOOLS` names.
#:
#: The staging root is resolved before it is used rather than after: a job
#: sharing the volume can create ``.headspace-staging`` as a link out of it, and
#: ``mkdir -p`` through such a link would put headspace's scratch space —
#: and then the caller's bytes — outside the volume before anything checked.
#: The per-write directory below it is a plain ``mkdir`` on a fresh nonce, so a
#: name that somehow already exists is an error rather than a directory shared
#: with something else.
PREPARE_STAGING_SCRIPT = """
set -eu
root=$1
staging_root=$2
staging=$3
shift 3
for tool in "$@"; do
    command -v "$tool" >/dev/null 2>&1 || { echo "headspace-write: $tool"; exit 11; }
done
if [ -e "$staging_root" ] || [ -L "$staging_root" ]; then
    resolved=$(realpath "$staging_root" 2>/dev/null || true)
    case "$resolved" in
        "$root"/*) ;;
        *) echo "headspace-write: ${resolved:-unresolvable}"; exit 12 ;;
    esac
    [ -d "$staging_root" ] || { echo "headspace-write: not a directory"; exit 12; }
else
    mkdir "$staging_root"
fi
mkdir "$staging"
"""

#: Verify the staged bytes, bound the destination, then commit with a rename.
#: ``$1`` volume root, ``$2`` staging directory, ``$3`` staged file, ``$4``
#: destination's parent, ``$5`` destination, ``$6`` expected digest, ``$7``
#: ``1`` to replace an existing destination.
#:
#: Three parts, in the order that makes each one meaningful:
#:
#: * the re-hash, before anything is looked at, because bytes that are not the
#:   caller's bytes make every later question moot;
#: * the boundary. The *deepest existing* ancestor of the destination's parent
#:   is resolved first, so a planted link is caught before ``mkdir -p`` can
#:   create anything through it; the parent is resolved again afterwards, which
#:   narrows the window in which a link could be planted between the two. It
#:   does not close it — no POSIX shell can — and what stays guaranteed is the
#:   part that matters: the rename below never runs unless the destination's
#:   parent was inside the volume when it was last looked at, so a lost race
#:   costs an empty directory and never a byte of the caller's payload;
#: * the destination itself. A real directory is refused outright, because a
#:   rename cannot atomically replace one with a file and ``mv`` would move the
#:   payload *into* it instead — doing something adjacent to what was asked. A
#:   link is not followed: ``mv`` renames over the link itself, so a job that
#:   pointed one outside the volume cannot turn a copy-in into a write through
#:   it.
#:
#: The trap is the cleanup, and it is an ``EXIT`` trap rather than a line at the
#: end so that every refusal above tidies up as thoroughly as a success does.
FINALIZE_WRITE_SCRIPT = """
set -eu
root=$1
staging=$2
staged=$3
parent=$4
dest=$5
expected=$6
overwrite=$7
trap 'rm -rf "$staging" 2>/dev/null || true' EXIT
[ ! -L "$staged" ] || { echo "headspace-write: $staged"; exit 18; }
[ -f "$staged" ] || { echo "headspace-write: $staged"; exit 17; }
actual=$(sha256sum "$staged")
actual=${actual%% *}
[ "$actual" = "$expected" ] || { echo "headspace-write: $actual"; exit 13; }
probe=$parent
while [ ! -d "$probe" ]; do
    next=${probe%/*}
    [ -n "$next" ] || next=/
    [ "$next" != "$probe" ] || break
    probe=$next
done
resolved=$(realpath "$probe" 2>/dev/null || true)
case "$resolved" in
    "$root"|"$root"/*) ;;
    *) echo "headspace-write: ${resolved:-unresolvable}"; exit 14 ;;
esac
mkdir -p "$parent"
resolved=$(realpath "$parent" 2>/dev/null || true)
case "$resolved" in
    "$root"|"$root"/*) ;;
    *) echo "headspace-write: ${resolved:-unresolvable}"; exit 14 ;;
esac
if [ -d "$dest" ] && [ ! -L "$dest" ]; then
    echo "headspace-write: $dest"; exit 16
fi
if [ -e "$dest" ] || [ -L "$dest" ]; then
    [ "$overwrite" = 1 ] || { echo "headspace-write: $dest"; exit 15; }
fi
[ ! -L "$staged" ] || { echo "headspace-write: $staged"; exit 18; }
mv -f "$staged" "$dest"
"""

#: Remove one write's staging directory. Run only when the finalize step did
#: not run to completion — when it did, its own ``EXIT`` trap has already done
#: this — so the volume is left as it was found whether the write failed before
#: the payload was framed, during the transfer, or in the exec itself.
DISCARD_STAGING_SCRIPT = """
set -eu
rm -rf "$1"
"""

#: What :data:`REAP_STAGING_SCRIPT` needs the image to provide — a strict subset
#: of :data:`REQUIRED_WRITE_TOOLS`, and deliberately its own tuple rather than a
#: reuse of it. Reaping only has to resolve a path and delete one, so an image
#: that has lost the ability to *write* (no ``sha256sum``, say) can still be
#: cleaned up. Requiring the wider set here would refuse to reap exactly the
#: workspaces most likely to be holding residue.
REQUIRED_REAP_TOOLS: tuple[str, ...] = ("realpath", "rm")

#: Clear everything under the reserved staging prefix, and name what actually
#: went. ``$1`` volume root, ``$2`` staging root, ``$3...`` the tools
#: :data:`REQUIRED_REAP_TOOLS` names.
#:
#: A copy-in killed between the transfer and the rename leaves a staging
#: directory nobody holds a handle to any more: the nonce lived in the process
#: that died, and :meth:`DockerProvider.write`'s signature carries no staging
#: token an orchestrator could keep. So reconciliation cannot name the residue —
#: it can only ask the backend to clear its own reserved prefix, which is what
#: makes that prefix reserved in the first place.
#:
#: Two properties are worth stating because they are the ones a reconciler
#: reports on. Each entry is removed and then *checked gone* before it is named,
#: the same discipline :meth:`DockerProvider.remove` keeps for a teardown — a
#: cleanup report whose claims were never verified is the one report nobody can
#: afford to be optimistic in. And an empty prefix, or no prefix at all, exits
#: ``0`` having said nothing: there being no residue is the ordinary case, not a
#: failure, and it must not read as one.
REAP_STAGING_SCRIPT = """
set -eu
root=$1
staging_root=$2
shift 2
for tool in "$@"; do
    command -v "$tool" >/dev/null 2>&1 || { echo "headspace-write: $tool"; exit 11; }
done
[ -d "$staging_root" ] || exit 0
resolved=$(realpath "$staging_root" 2>/dev/null || true)
case "$resolved" in
    "$root"/*) ;;
    *) echo "headspace-write: ${resolved:-unresolvable}"; exit 12 ;;
esac
for entry in "$staging_root"/*; do
    [ -e "$entry" ] || [ -L "$entry" ] || continue
    rm -rf "$entry" 2>/dev/null || true
    [ -e "$entry" ] || [ -L "$entry" ] || echo "headspace-write: $entry"
done
"""

# --- naming a job an operator ended, across two processes (issue #16) -------
#
# The whole shape, the live probe that forced it and the fabrication window
# that forced the *second* phase are in the module docstring under "Recording
# that an operator ended it". What lives here is the vocabulary ``stop`` and
# ``run`` have to agree on, in one place so the two halves of a cross-process
# handshake cannot drift apart: the two names, the size bound, and the scripts
# that put a marker there and take it away.

#: The name ``stop`` writes *before* it signals, carrying the job id it is
#: about to end. Dot-prefixed and namespaced like :data:`STAGING_DIR_NAME`, and
#: for the same two reasons: a human reading the volume can tell headspace's
#: own state from a job's, and the name is *reserved*, so anything standing
#: there that headspace did not put there is a job reaching for a channel that
#: is not its own.
#:
#: It records an *ask*, and an ask is not an ending — nothing is ever
#: classified ``cancelled`` from this name alone. What it does is tell the
#: still-blocked ``run`` that a countersignal is on its way, which is the only
#: reason a countersignal written after the signal can be waited for rather
#: than missed. See :data:`CANCELLATION_SETTLE_SECONDS`.
CANCELLATION_INTENT_MARKER_NAME = ".headspace-cancel-requested"

#: The name ``stop`` writes *after* the signal has landed, carrying the same
#: job id. This is the one that classifies: it is the only evidence in the
#: volume that the operator's ask actually took effect, because it cannot be
#: written until it has. A ``stop`` that dies before this point leaves the
#: intent name behind and nothing else, and the job is recorded exactly as it
#: would have been before this channel existed.
#:
#: Deliberately unlike the intent name at a glance rather than one letter from
#: it: these two strings are compared, written and cleared by two processes
#: that never speak, and a pair a reader can confuse is a pair a maintainer can
#: swap.
CANCELLATION_SIGNALLED_MARKER_NAME = ".headspace-cancelled"

#: Both reserved names, in the order ``run`` reads them — the evidence first,
#: the warning second. Every obligation that applies to one applies to both:
#: they are written by the same script, refused on the same grounds, and above
#: all *cleared together*, because a channel that tidies one name and leaves
#: the other has only moved the wedge.
CANCELLATION_MARKER_NAMES = (
    CANCELLATION_SIGNALLED_MARKER_NAME,
    CANCELLATION_INTENT_MARKER_NAME,
)

#: Where those names resolve inside every container this provider makes.
#: Derived rather than written twice, because a marker the writer and the
#: reader disagree about is a marker that silently never works.
CANCELLATION_INTENT_MARKER_PATH = f"{WORKSPACE_MOUNT_PATH}/{CANCELLATION_INTENT_MARKER_NAME}"
CANCELLATION_SIGNALLED_MARKER_PATH = f"{WORKSPACE_MOUNT_PATH}/{CANCELLATION_SIGNALLED_MARKER_NAME}"

#: How long :meth:`DockerProvider.run` holds the door open for a countersignal
#: when an intent marker names the job that just settled — and *only* then.
#:
#: The two writes cannot both precede the signal without the second one lying,
#: so the countersignal necessarily lands after the container has died. That is
#: the moment ``run`` wakes up: it is polling the very container ``stop``
#: blocked on, so the two processes are racing from the same starting gun, with
#: ``stop`` still owing one exec round trip. Without a wait, a completed
#: cancellation would be recorded as a failure whenever ``stop`` lost that race
#: — which is most of the time, and worst exactly when the stop was forceful.
#:
#: Two seconds is an engine round trip's order of magnitude with room for a
#: loaded daemon, and it is spent only on the path where an operator really did
#: ask about this job. The ordinary job never reaches it. What the budget buys
#: is that the *ending* of the wait is a decision rather than a hang: a ``stop``
#: that died owing a countersignal is never coming back, and holding a finished
#: job's result hostage to it would be a worse failure than the misreport.
CANCELLATION_SETTLE_SECONDS = 2.0

#: The most of a marker that is ever read. A job id is tens of bytes; this is
#: three orders of magnitude of headroom and still small enough that a job
#: which fills the volume at this path cannot make ``run`` read its way through
#: it. Anything larger is not a marker by definition, and is treated as the
#: residue it is rather than truncated into a comparison.
MARKER_MAX_BYTES = 4096

#: ``$0`` for the two scripts below, and deliberately not
#: :data:`SCRIPT_ARGV0`: an operator reading ``ps`` inside a workspace should
#: see which of headspace's channels is running, not a copy-in that is not
#: happening. The same rule the copy-in scripts follow still applies — every
#: value the scripts touch, the job id above all, arrives as an *argument* and
#: is never interpolated into the text.
MARKER_ARGV0 = "headspace-cancel"

#: Write one marker: ``$1`` its path, ``$2`` a nonce path beside it, ``$3``
#: the job id being signalled. Both phases use this same script, because both
#: write the same shape of thing at an equally reserved name, and one script is
#: one place for the refusals below to be right.
#:
#: Two moves, and the second is what makes the first safe. The bytes land on
#: the nonce path and are *renamed* onto the marker's name — never redirected
#: at it. A shell redirection follows a symlink, so ``> "$marker"`` through a
#: link a job planted would write headspace's own bytes wherever that job
#: pointed, outside the volume and over a file the job could not otherwise
#: touch. ``mv`` renames over the link itself, so the same planted link buys
#: nothing. This is the identical reasoning :data:`FINALIZE_WRITE_SCRIPT`
#: applies to a copy-in's destination, restated for the paths headspace writes
#: on its own account.
#:
#: The explicit ``-L`` refusals in front of both paths are the checkpoint that
#: says so out loud rather than leaving the safety implicit in ``mv``'s
#: semantics. Nothing legitimate puts a link at either name — the markers' are
#: reserved and the nonce's is unguessable — so one standing there is refused
#: (exit 21) rather than quietly tidied away, and the refusal reaches the
#: provider as a write that did not happen.
#:
#: A directory needs a refusal of its own (exit 22), and it is the one case
#: where ``mv``'s safety genuinely runs out. ``mv -f file dir`` does not fail:
#: it *relocates*, moving the file to ``dir/file`` and exiting 0 — verified on
#: a POSIX host, coreutils and busybox alike. So a job that plants a directory
#: at a reserved name would make headspace's write appear to succeed while what
#: actually stands at that name is a directory holding a nonce nobody will ever
#: read, and the job would have quietly suppressed the naming of its own
#: cancellation. The check refuses anything that exists and is not a regular
#: file, which takes a FIFO and a device node with it for the price of the
#: directory: nothing legitimate is ever any of those at these two names, and
#: only a regular file can be renamed over without surprises.
#:
#: Narrowed, not closed, and in exactly the way :data:`FINALIZE_WRITE_SCRIPT`
#: already admits about its own destination: a job could land a link in the gap
#: between the check and the rename, and POSIX offers no "rename only if this
#: is not a symlink". What stays guaranteed is the part that matters — the
#: rename never follows a link — so a lost race costs a marker, never a byte
#: written outside the volume.
#:
#: The trap is an ``EXIT`` trap rather than a trailing line so that every
#: refusal above leaves the volume as tidy as a success does: a workspace is
#: the caller's, and headspace littering it with a nonce nobody can name is a
#: cost the caller never agreed to.
WRITE_CANCELLATION_SCRIPT = """
set -eu
marker=$1
staged=$2
job=$3
trap 'rm -f "$staged" 2>/dev/null || true' EXIT
[ ! -L "$marker" ] || { echo "headspace-cancel: $marker"; exit 21; }
[ ! -e "$marker" ] || [ -f "$marker" ] || { echo "headspace-cancel: $marker"; exit 22; }
[ ! -e "$staged" ] && [ ! -L "$staged" ] || { echo "headspace-cancel: $staged"; exit 21; }
printf '%s' "$job" > "$staged"
mv -f "$staged" "$marker"
"""

#: Take one marker away again: ``$1`` its path.
#:
#: ``-r`` because what stands there is not always the file headspace wrote. A
#: job that planted a directory or a link at a reserved name would otherwise
#: wedge the channel permanently — every later ``stop`` would meet its own
#: write refusal, and every later ``run`` would meet a marker it must not read.
#: Clearing whatever is there is what lets the channel recover from an attack
#: on it, and ``rm`` never recurses *through* a link, so removing one takes the
#: link and never its target. This is the other half of the refusals above: the
#: write says no to a planted object, and this says the job that planted it has
#: cost its own cancellation a name and nothing more.
CLEAR_CANCELLATION_SCRIPT = """
set -eu
rm -rf "$1"
"""

#: Every name at the workspace root that belongs to headspace rather than to a
#: caller, and which :meth:`DockerProvider.write` therefore refuses as a
#: copy-in destination.
#:
#: "Reserved" was, until this tuple existed, a word three docstrings used and
#: nothing enforced. A ``put`` naming :data:`CANCELLATION_SIGNALLED_MARKER_NAME`
#: as its destination landed a regular file at the countersignal's path and
#: reported success — verified live, not reasoned about — which let a caller
#: pre-plant the record of an ending that never happened for a job it had not
#: started yet. That is the one thing this channel exists to make impossible,
#: reached without running any code at all, so the reservation is a check now
#: and not a convention.
#:
#: Matched on the *first segment* of a normalised destination, so a copy-in can
#: no more write ``.headspace-staging/x`` than ``.headspace-staging`` itself:
#: everything under a reserved name is equally headspace's, and the marker
#: names are files, so a path descending through one is nonsense in any case.
#: This is a refusal about *names*, deliberately — the volume is shared, and a
#: job with a shell can still write anything it likes at these paths. What it
#: closes is the route that needs no job.
RESERVED_ROOT_NAMES = (STAGING_DIR_NAME,) + CANCELLATION_MARKER_NAMES

#: What separates the two fields of a marker's payload. Never parsed on — the
#: whole string is compared verbatim — so a ``job_id`` containing this
#: character cannot shift the boundary between the secret and the name.
MARKER_FIELD_SEPARATOR = ":"


def cancellation_evidence(anchor: Container, job_id: str) -> str | None:
    """The exact bytes a marker must hold to name *this* job, or ``None``.

    One function so the writer and the reader cannot drift: ``stop`` puts this
    string in the volume and ``run`` compares against it, and a handshake whose
    two halves each build their own payload is a handshake one refactor away
    from silently never matching.

    Compared **verbatim**, never split. Parsing on
    :data:`MARKER_FIELD_SEPARATOR` would let a ``job_id`` containing that
    character move the boundary between the secret and the name, which is the
    classic way a two-field credential becomes a one-field one.

    ``None`` means *no cancellation can be read here at all*, and it is
    returned for a workspace whose anchor carries no
    :data:`LABEL_CANCEL_TOKEN` — one created before this channel required a
    token. That is deliberately not a fallback to comparing bare job ids: the
    fallback is the hole. Such a workspace classifies a stopped job exactly as
    the code before any of this existed, which is the same direction every
    other partial state of this channel fails in.
    """
    token = anchor.labels.get(LABEL_CANCEL_TOKEN)
    if not token or not job_id:
        return None
    return f"{token}{MARKER_FIELD_SEPARATOR}{job_id}"


@dataclass(frozen=True)
class _Marker:
    """What stood at a marker's path in the volume, and what it said.

    Two fields, because one probe answers two different questions and
    collapsing them would lose the second. ``present`` is "the volume holds
    *something* under this name", and it is what drives the clearing step: a
    directory or a link a job planted says nothing and still has to go, or it
    wedges the channel for every ``stop`` and ``run`` that follows. ``value``
    is "and it was a regular file, small enough to be a marker, saying
    this" — the only shape that is allowed to name a job.

    Kept as a type rather than a tuple because both phases are read by the same
    helper and reasoned about in the same words: the intent name and the
    countersignal differ in what they are allowed to *conclude*, never in how
    they are read, written or taken away.
    """

    present: bool
    value: str | None = None


#: The answer for a name the volume holds nothing under — which is the answer
#: for very nearly every job that ever runs, since almost none is stopped by an
#: operator. Named so the ordinary case reads as a fact rather than as a
#: default constructed three times.
_NO_MARKER = _Marker(present=False)


_HEX_DIGITS = frozenset("0123456789abcdef")
_DIGEST_LENGTH = 64


def _script_details(output: str) -> tuple[str, ...]:
    """Every evidence line a staging script emitted, in the order it emitted them.

    Read out of the output rather than off a second channel because an exec has
    one: ``demux=False`` interleaves stdout and stderr exactly as ``run`` does
    for a job. The marker is what separates the script's deliberate sentences
    from the shell's own noise around them, and requiring it at the *start* of a
    line is what stops a path that happens to contain the marker from inventing
    an extra one.

    A tuple rather than a single line because the two shapes of script genuinely
    differ: prepare and finalize name one condition, while the reaper names
    every piece of residue it removed. Collapsing that to "the first one" would
    have made a reconciler's count quietly wrong rather than loudly broken.
    """
    details = []
    for line in output.splitlines():
        marker, separator, detail = line.partition(SCRIPT_DETAIL_MARKER)
        if separator and not marker.strip():
            details.append(detail.strip())
    return tuple(details)


def _first_detail(details: Sequence[str]) -> str:
    """The one condition a single-answer script reported, or ``""``."""
    return details[0] if details else ""


def _exec_binary_missing(status: int, output: str) -> str | None:
    """The binary an exec could not start, or ``None`` for anything else.

    Narrow in both directions on purpose (see :data:`EXEC_START_MARKER`): the
    status has to be one of the shell's two "cannot run that" numbers *and* the
    output has to carry the engine's own marker, so a script that merely prints
    the phrase cannot be mistaken for one that never started. ``None`` means
    "not ours", and the caller then treats the status as the script's own —
    which is the fail-safe direction, since it can only ever move cases *into*
    the ordinary classification and never quietly out of it.
    """
    if status not in (EXIT_COMMAND_NOT_FOUND, EXIT_COMMAND_NOT_EXECUTABLE):
        return None
    if EXEC_START_MARKER not in output:
        return None
    detail = output.partition(EXEC_START_MARKER)[2].lstrip()
    if not detail.startswith('"'):
        return WRITE_SHELL
    return detail[1:].partition('"')[0] or WRITE_SHELL


def _describe_environment(environment: str) -> str:
    """Name the environment the way a caller chose it, when it can.

    A workspace records the digest-pinned reference it was built from, not the
    profile name the caller typed — but a refusal that says only
    ``python:3.12-slim@sha256:57cd…`` asks a reader to work out which of their
    own choices produced it. The registry is a plain dict and the lookup is
    pure, so naming the profile costs nothing and the reference is kept beside
    it: the profile is what the caller can change, the reference is what was
    actually run.
    """
    for profile in profiles.REGISTRY.values():
        if profile.image == environment:
            return f"profile {profile.name} ({environment})"
    return f"environment {environment}"


def _require_expected_digest(value: str, relative: str) -> str:
    """Refuse a digest that is not the shape ``sha256sum`` will be compared to.

    Bare 64-character lowercase hex — the shape
    :mod:`headspace.core.inputs` produces and the shape ``sha256sum`` prints.
    Checked here rather than left to the comparison because the failure modes
    are not equally honest: an ``sha256:``-prefixed or upper-cased digest would
    compare unequal to what the container computed and be reported as a
    *mismatch*, which is an infrastructure failure a caller is told to retry.
    It would never succeed. Naming the shape up front keeps the mismatch
    meaning only what it should mean — that the bytes changed.
    """
    digest = value.strip() if isinstance(value, str) else ""
    if len(digest) != _DIGEST_LENGTH or not set(digest) <= _HEX_DIGITS:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"malformed sha256 for '{relative}': {value!r}",
            remediation=(
                f"pass the digest as {_DIGEST_LENGTH} lowercase hex characters with no "
                "'sha256:' prefix — the form headspace.core.inputs computes and the form "
                "the workspace re-computes it in"
            ),
        )
    return digest


def _anchor_not_running(workspace_id: str, status: str) -> ProviderError:
    """A workspace container that exists but is not running: honest, and named.

    Neither a caller mistake nor a generic crash, so neither exit 1 nor a
    traceback. The remediation carries the asymmetry with ``read`` because a
    caller that concludes "this workspace is finished" would throw away work it
    can still have: a stopped workspace still hands its artifacts back, it just
    cannot verify anything new arriving.
    """
    return ProviderError(
        f"the workspace container for {workspace_id} is not running "
        f"({status or 'the engine did not name its state'}), and a verified copy-in has to "
        "hash and rename the bytes inside it",
        remediation=(
            f"run 'headspace inspect {workspace_id}' to see the runtime's state; a copy-in "
            "needs the workspace container up, so create a fresh workspace and copy in "
            "again — reading artifacts back out still works on a workspace whose runtime "
            "has exited"
        ),
    )


def _missing_write_tool(workspace_id: str, environment: str, tool: str) -> CliError:
    """The image cannot finish a verified copy-in: a setup fact, stated once.

    Exit 2 rather than 7 because the exit-code policy already has a slot that
    says exactly this — "environment / setup error (tool not installed)" — and
    because the two differ in what they tell an autonomous caller to do next.
    Exit 7 says retry; retrying is precisely what will not help, since the image
    will lack ``sha256sum`` just as thoroughly the second time.
    """
    return CliError(
        code=EXIT_ENV_ERROR,
        message=(
            f"workspace {workspace_id} runs {_describe_environment(environment)}, which "
            f"provides no '{tool}' — a copy-in cannot verify bytes it cannot hash, resolve "
            "or rename"
        ),
        remediation=(
            "a verified copy-in needs a POSIX shell and "
            f"{', '.join(REQUIRED_WRITE_TOOLS)} inside the workspace image; create the "
            "workspace from a profile whose image provides them, or have a job produce the "
            "file instead of copying it in"
        ),
    )


def _landed_digest_disagrees(
    workspace_id: str, relative: str, expected: str, actual: str
) -> ProviderError:
    """The bytes in the volume are not the bytes the caller sent.

    Reported as an infrastructure failure rather than a caller error, and the
    reason is what was already ruled out: the host checked its own stream
    against ``expected`` before anything was transferred, so a stale digest
    cannot reach here. What is left is the transfer being corrupted or the
    staged file being rewritten after it landed — neither of which the caller
    did, and both of which a retry can genuinely resolve.
    """
    return ProviderError(
        f"the bytes staged in workspace {workspace_id} for '{relative}' hash to "
        f"{actual or 'nothing the image could compute'}, not the {expected} the caller "
        "declared — nothing was renamed into place",
        remediation=(
            "nothing was written: either the transfer was corrupted, or something sharing "
            "the workspace volume rewrote the staged bytes before they were verified — "
            "retry the copy-in, and if it repeats, stop any job writing to the workspace "
            "first"
        ),
    )


def _staged_file_vanished(workspace_id: str, relative: str) -> ProviderError:
    """The transfer reported success and the staged file was not there.

    Named rather than left to the unexplained-failure fallback, because the
    fallback's message would send a reader looking for a broken engine when the
    engine did its job: ``put_archive`` returned, so the bytes were accepted,
    and something inside the workspace removed them before they could be
    verified. That is the same neighbour the digest mismatch and the symlink
    swap have — a job sharing the volume — and a caller who sees all three
    worded as one family can act on them as one.
    """
    return ProviderError(
        f"the file staged in workspace {workspace_id} for '{relative}' was gone before it "
        "could be verified — nothing was renamed into place",
        remediation=(
            "nothing was written: the transfer succeeded and something sharing the workspace "
            "volume then removed the staged file. Stop any job running in the workspace, "
            "then retry the copy-in"
        ),
    )


def _staged_path_is_a_link(workspace_id: str, relative: str) -> ProviderError:
    """The staged file became a symlink between the transfer and the check.

    Nothing legitimate produces this. The transfer writes one regular file into
    a directory this exec created a moment earlier under a name no caller
    chooses, so a link standing there means something else sharing the volume
    put it there — and the only thing it buys is the one thing the verification
    exists to prevent. ``[ -f ]`` and ``sha256sum`` both follow a link, so a
    link pointing at a job-controlled copy of the payload hashes to exactly the
    digest the caller declared; every later check passes, and the rename moves
    *the link* into the destination. The caller is then told a path holds bytes
    with a verified digest, when it holds a pointer to bytes the job can
    rewrite at will.

    Infrastructure rather than caller error, for the same reason a digest
    mismatch is: the caller's own stream was already checked host-side, so
    nothing they passed can cause this. Something raced them inside their
    workspace.
    """
    return ProviderError(
        f"the file staged in workspace {workspace_id} for '{relative}' was replaced by a "
        "symlink before it could be verified — nothing was renamed into place",
        remediation=(
            "nothing was written: something sharing the workspace volume swapped the staged "
            "file for a link while the copy-in was in flight. Stop any job running in the "
            "workspace, then retry the copy-in"
        ),
    )


def _escapes_workspace_volume(workspace_id: str, relative: str, resolved: str) -> CliError:
    """A destination whose parent is not in the volume, whatever it is named.

    The same refusal class the read verb applies to a symlinked artifact, and
    the same reason read gives for it: engine-side path resolution follows
    links quite happily, so the only thing standing between a planted
    ``results -> /etc`` and a copy-in writing through it is a check that looks
    at where the path actually goes. A caller error rather than an engine one
    because it is answerable — the destination can be renamed, or the link
    removed by a job — while "the engine broke" is not.
    """
    return CliError(
        code=EXIT_USER_ERROR,
        message=(
            f"workspace {workspace_id} cannot receive '{relative}': its parent directory "
            f"resolves to {resolved}, outside the workspace volume"
        ),
        remediation=(
            "a copy-in never writes outside the workspace volume, and never follows a link "
            "out of it — choose a destination whose directories are inside the workspace, "
            "or have a job remove the link that leads out of it"
        ),
    )


def _destination_exists(workspace_id: str, relative: str) -> CliError:
    return CliError(
        code=EXIT_USER_ERROR,
        message=f"workspace {workspace_id} already holds something at '{relative}'",
        remediation=(
            "pass overwrite=True to replace it deliberately, or choose a destination the "
            "workspace does not already hold — a copy-in never silently replaces work a "
            "job produced but nothing has exported yet"
        ),
    )


def _destination_is_a_directory(workspace_id: str, relative: str) -> CliError:
    return CliError(
        code=EXIT_USER_ERROR,
        message=f"workspace {workspace_id} holds a directory at '{relative}', not a file",
        remediation=(
            "name the file itself; a copy-in commits with a rename, which cannot replace a "
            "directory with a file — not even with overwrite"
        ),
    )


def _destination_is_reserved(workspace_id: str, relative: str, reserved: str) -> CliError:
    # Named twice only when the destination reaches *through* a reserved name;
    # a caller who typed the reserved name itself does not need it read back.
    reached = (
        f"'{relative}' would write into '{reserved}'"
        if relative != reserved
        else f"'{relative}' is"
    )
    return CliError(
        code=EXIT_USER_ERROR,
        message=(
            f"{reached} at the root of workspace {workspace_id}, "
            "a name headspace reserves for its own state"
        ),
        remediation=(
            "choose a destination outside the reserved names "
            f"({', '.join(RESERVED_ROOT_NAMES)}) — they carry headspace's own staging and "
            "the record of who ended a job, and a copy-in that could land there could "
            "write that record"
        ),
    )


def _staging_unusable(workspace_id: str, resolved: str) -> CliError:
    return CliError(
        code=EXIT_USER_ERROR,
        message=(
            f"workspace {workspace_id} holds something at "
            f"'{STAGING_DIR_NAME}' that resolves to {resolved}, so a copy-in has nowhere "
            "inside the volume to stage through"
        ),
        remediation=(
            f"'{STAGING_DIR_NAME}' at the workspace root is reserved for headspace's own "
            "staging — have a job remove or rename whatever is there, then copy in again"
        ),
    )


@dataclass(frozen=True)
class _Landing:
    """Every path one copy-in touches, derived once from the caller's own.

    A structure rather than six arguments threaded through four methods,
    because the relationships between these paths *are* the design: the staged
    file is under the staging directory, the staging directory is under the
    reserved root, the reserved root is under the same volume root the finalize
    step bounds the destination against. Deriving them in one place is what
    makes "the transfer never targets the destination" checkable by reading
    rather than by tracing.
    """

    workspace_id: str
    relative: str
    root: str
    staging_root: str
    staging: str
    staged: str
    parent: str
    destination: str
    expected_sha256: str
    overwrite: bool

    @classmethod
    def of(
        cls, workspace_id: str, relative: str, expected_sha256: str, overwrite: bool
    ) -> _Landing:
        root = WORKSPACE_MOUNT_PATH
        staging_root = f"{root}/{STAGING_DIR_NAME}"
        # A fresh nonce per write, so two copy-ins racing each other stage into
        # different directories and neither can see — or clean up — the other's
        # bytes. The verbs do not share a lock; this is what makes them safe
        # without one.
        staging = f"{staging_root}/{uuid.uuid4().hex}"
        destination = f"{root}/{relative}"
        return cls(
            workspace_id=workspace_id,
            relative=relative,
            root=root,
            staging_root=staging_root,
            staging=staging,
            staged=f"{staging}/{STAGED_FILE_NAME}",
            parent=posixpath.dirname(destination),
            destination=destination,
            expected_sha256=_require_expected_digest(expected_sha256, relative),
            overwrite=bool(overwrite),
        )

    @property
    def prepare_args(self) -> tuple[str, ...]:
        return (self.root, self.staging_root, self.staging, *REQUIRED_WRITE_TOOLS)

    @property
    def finalize_args(self) -> tuple[str, ...]:
        return (
            self.root,
            self.staging,
            self.staged,
            self.parent,
            self.destination,
            self.expected_sha256,
            "1" if self.overwrite else "0",
        )


def _source_chunks(source: ByteSource, chunk_size: int) -> Iterator[bytes]:
    """Yield ``source`` in chunks, lazily — a reader reads, anything else iterates.

    The same two-shaped contract, and the same ordering, that
    :mod:`headspace.core.artifacts` uses on the way out: the ``.read()`` branch
    comes first because iterating a binary file yields *lines*, which split on
    newlines that mean nothing in a payload and buffer arbitrarily much when
    there are none. Restated here rather than imported because that module's
    version is private to it, and a copy-in reading its source differently from
    the way an export reads one would be a difference nobody meant.
    """
    read = getattr(source, "read", None)
    if callable(read):
        while True:
            chunk = read(chunk_size)
            if not chunk:
                return
            yield chunk
    else:
        yield from source  # type: ignore[misc]


@contextmanager
def _framed_payload(source: ByteSource, landing: _Landing) -> Iterator[IO[bytes]]:
    """One tar, one member, measured on the host and ready to stream at the engine.

    The engine's archive endpoint takes a tar, and a tar states a member's size
    in a header that precedes its first content byte — while a
    :data:`~headspace.core.artifacts.ByteSource` declares no size at all. So the
    payload has to be measured before it can be framed, and it is written into a
    host temporary file to do it. The header block is *reserved* rather than
    buffered: a USTAR header is exactly one block, so the content is written
    from offset :data:`tarfile.BLOCKSIZE` and the header is filled in with a
    seek once the size is known. The payload is therefore copied exactly once
    and never held in memory, however large it is — the alternative, a second
    temporary file to prepend a header to, would cost a whole extra copy of it.

    The digest computed on the way past is checked against the caller's before
    anything is transferred. That check is not the verification this verb rests
    on — the container's re-hash is, and it is the only one that can see what
    actually landed — but answering the host-side question on the host is what
    lets the container-side refusal mean the narrower thing it means: not "your
    digest was stale" but "these bytes changed after they left".
    """
    with tempfile.TemporaryFile() as transfer:
        transfer.seek(tarfile.BLOCKSIZE)
        digest = hashlib.sha256()
        size = 0
        for chunk in _source_chunks(source, WRITE_CHUNK_BYTES):
            if not isinstance(chunk, (bytes, bytearray, memoryview)):
                raise CliError(
                    code=EXIT_USER_ERROR,
                    message=(
                        f"the source for '{landing.relative}' yielded "
                        f"{type(chunk).__name__}, expected bytes"
                    ),
                    remediation="stream the payload as bytes (open the source in binary mode)",
                )
            digest.update(chunk)
            size += len(chunk)
            transfer.write(chunk)

        actual = digest.hexdigest()
        if actual != landing.expected_sha256:
            raise CliError(
                code=EXIT_USER_ERROR,
                message=(
                    f"the source for '{landing.relative}' hashes to {actual}, not the "
                    f"{landing.expected_sha256} declared for it"
                ),
                remediation=(
                    "nothing was transferred; re-measure the source and pass the digest of "
                    "the bytes this call will actually read"
                ),
            )

        # End of archive: the member's own padding to a block boundary, the two
        # zero blocks that terminate a tar, then padding out to a whole record.
        # The record padding is what a conventional writer emits, and matching
        # it keeps the transfer in the shape the engine was probed against
        # rather than in a technically-legal one it has never been handed.
        transfer.write(b"\0" * (-size % tarfile.BLOCKSIZE + 2 * tarfile.BLOCKSIZE))
        transfer.write(b"\0" * (-transfer.tell() % tarfile.RECORDSIZE))

        info = tarfile.TarInfo(STAGED_FILE_NAME)
        info.size = size
        info.mode = STAGED_FILE_MODE
        info.mtime = int(time.time())
        transfer.seek(0)
        transfer.write(info.tobuf(tarfile.USTAR_FORMAT))
        transfer.seek(0)
        yield transfer


@dataclass(frozen=True)
class _RefusedCommand:
    """The engine refused to exec the caller's command: a job outcome, not a fault.

    Carries the two things :meth:`DockerProvider.run` needs to report it — the
    POSIX status and the text that goes on the job's captured-output path —
    rather than letting either be recomputed at the call site.
    """

    exit_status: int
    report: str

    @property
    def produced(self) -> int:
        return len(self.report.encode("utf-8"))

    def bounded(self, budget: int) -> tuple[str, int, bool]:
        """The report clipped to ``budget``, with what it really produced.

        Mirrors what :meth:`DockerProvider._captured` does for a container's own
        output: keep a bounded prefix, report the *full* volume as produced, and
        say plainly whether anything was dropped. Clipping on the encoded bytes
        and decoding with ``ignore`` keeps a multi-byte character from being cut
        in half at the boundary.
        """
        raw = self.report.encode("utf-8")
        if len(raw) <= budget:
            return self.report, self.produced, False
        return raw[:budget].decode("utf-8", "ignore"), self.produced, True


def _refused_command(
    err: APIError, argv: Sequence[str], environment: str, handle: str
) -> _RefusedCommand | None:
    """Classify a failed ``start``; ``None`` means "not ours — re-raise unchanged".

    The report is built from what *headspace* knows — the environment reference
    and the caller's own ``argv[0]`` — and the engine's sentence is appended
    beneath it, redacted, rather than interpolated into it. That ordering is the
    contract: the first line is the fact the caller acts on, and the engine's
    words are evidence kept for the case where this classification was wrong.
    """
    message = str(err)
    exit_status = not_executable_exit_status(message)
    if exit_status is None:
        return None
    command = argv[0] if argv else ""
    reason = (
        f"no executable named {command!r} exists in this environment"
        if exit_status == EXIT_COMMAND_NOT_FOUND
        else f"{command!r} exists in this environment but cannot be executed"
    )
    detail = redact_engine_text(str(err.explanation) if err.explanation else message, handle)
    return _RefusedCommand(
        exit_status=exit_status,
        report=(
            f"headspace: the command was not run — {reason} "
            f"(exit status {exit_status}).\n"
            f"environment: {environment}\n"
            f"engine: {detail}\n"
        ),
    )


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

    ``read`` is the entire surface on purpose. Under
    :data:`ARCHIVE_STREAM_MODE` :mod:`tarfile` reads through ``_Stream``, which
    only ever calls ``fileobj.read(bufsize)`` and — because the object was
    passed in rather than opened by it — never closes it. Type stubs describe
    that parameter as a full ``IO[bytes]``, which the documented stream-mode
    contract does not require and which no bytes here could honestly satisfy:
    ``seek``, ``tell`` and ``write`` on a one-shot chunk iterator would each
    have to be a lie. Closing the underlying stream stays where it belongs,
    with the :class:`~contextlib.ExitStack` at the call site.
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
                    # The cancellation token goes on the anchor and on nothing
                    # else — not the volume above, not the job containers
                    # `run` builds, which assemble their labels from scratch.
                    # One object holds it, and it is the one object both `stop`
                    # and `run` already fetch.
                    labels={**labels, LABEL_CANCEL_TOKEN: secrets.token_hex(CANCEL_TOKEN_BYTES)},
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
        env: Mapping[str, str] = EMPTY_ENV,
    ) -> JobOutcome:
        """Execute one command in its own container, bounded at capture.

        ``env`` (issue #13) is the job process's declared environment, and it
        crosses this seam into exactly one place: the ``environment=`` keyword
        of *this* container's own creation call, passed separately from
        :meth:`_sealed_kwargs` rather than folded into it. That separation is
        deliberate, not incidental — ``_sealed_kwargs`` is the one builder
        shared by every container this provider makes, including the anchor
        :meth:`create` starts and leaves running for the workspace's whole
        life, and a value threaded through it would land on that anchor too. A
        secret handed to one job has no business outliving that job, let alone
        becoming visible to every later job the same workspace happens to run
        — so ``env`` is accepted here, in the one verb whose container is
        already the shortest-lived object this provider creates, and nowhere
        else in this module accepts it at all.

        Two surfaces ``env`` must never reach, because this module already
        treats both as public: the container's ``labels`` — readable by
        anyone who can run ``docker inspect``, and already the channel this
        provider uses for everything it means to be discoverable (see the
        module docstring, "Why labels, and never handles") — and ``command``,
        which is recorded verbatim wherever a job's argv is rendered and is
        readable off a live process's own ``/proc/<pid>/cmdline``. Neither the
        label dict nor ``argv`` below is built from ``env`` or with knowledge
        that it exists; a value placed in ``env`` cannot leak into either
        without this method's own code changing to put it there.

        Defaults to :data:`EMPTY_ENV`, a real empty mapping created once at
        import time, rather than an unset keyword — so a caller that never
        mentions ``env`` and a caller that explicitly passes ``{}`` reach the
        engine identically: the job's own image environment and nothing
        headspace added. That is what "closed by default" has to mean for a
        channel whose entire purpose is controlling what a job process can
        read, the same way
        :attr:`~headspace.providers.base.WorkspaceDescriptor.network_enabled`
        defaults closed rather than trusting every caller to ask for isolation
        explicitly.

        ``job_id`` (issue #16) is held to the *opposite* placement rule, and by
        the same code. It reaches the engine in exactly two places, both built
        below: the container's ``name``, and the :data:`LABEL_JOB_ID` label.
        Both are readable from outside the container and from nowhere within
        it — a container's own hostname is its engine id, not its name, and a
        label is not visible to the process at all. That is load-bearing rather
        than incidental, because the cancellation markers this method reads
        back are trustworthy only while a job cannot name itself: a job shares
        the workspace volume and can write whatever it likes at either marker's
        path, and the one string it cannot put there is the id that would make
        a marker be believed. So ``job_id`` reaches no ``environment`` entry
        and no ``argv`` entry — the two channels a process really can read from
        inside its own container — and neither is built from it or with any
        knowledge that it exists.

        Both markers are read once the job has settled, on every path including
        exit 0, and both are cleared if anything was found. Only the
        countersignal can make this outcome ``cancelled``; an intent marker
        naming this job buys the other process a bounded moment to finish
        writing one, and nothing else. Why the channel exists, why it is a
        written signal rather than an inference from exit 137, why it takes two
        phases, and why every partial state of it fails towards today's
        classification rather than towards a fabricated cancellation, is in the
        module docstring under "Recording that an operator ended it".
        """
        workspace_id = require_workspace_id(workspace_id)
        argv = require_command(command)
        wall_budget = float(requested_limit(policy, "wall_clock"))
        output_budget = int(requested_limit(policy, "output_bytes"))

        with self._engine(f"running a job in workspace {workspace_id}") as client:
            anchor = self._require(client, workspace_id)
            environment = anchor.labels.get(LABEL_ENVIRONMENT, anchor.attrs["Config"]["Image"])
            container = client.containers.create(
                image=environment,
                command=list(argv),
                name=f"{self._container_name(workspace_id)}-{_slug(job_id)}-{uuid.uuid4().hex[:8]}",
                labels={
                    LABEL_WORKSPACE_ID: workspace_id,
                    LABEL_PROVIDER: PROVIDER_NAME,
                    LABEL_ROLE: ROLE_JOB,
                    LABEL_JOB_ID: job_id,
                    LABEL_CREATED_AT: utc_now(),
                },
                # The job's own environment, and only the job's — see the
                # docstring above for why this is not part of
                # `_sealed_kwargs` and never reaches the anchor container.
                environment=dict(env),
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
                refused = self._start(container, argv, environment)
                usage = _Usage()
                timed_out = refused is None and self._await_exit(container, wall_budget, usage)
                capture.join(CAPTURE_GRACE_SECONDS)
                if refused is None:
                    state = container.attrs["State"]
                    exit_status = int(state.get("ExitCode") or 0)
                    # ``reload()`` inside `_await_exit` already made this fresh;
                    # no second engine call is spent to learn it. Read here and
                    # nowhere else — see :meth:`_status` for why the exit status
                    # itself is never trusted to mean the same thing.
                    oom_killed = bool(state.get("OOMKilled"))
                    output, produced, truncated = self._captured(container, capture, output_budget)
                else:
                    # Nothing ran, so there is nothing to have captured: the
                    # report *is* the job's whole output, and the usage figures
                    # below are honestly zero rather than absent. It still obeys
                    # the caller's declared output budget — a synthesized report
                    # is output like any other, and `JobOutcome.output` is
                    # contractually bounded before it is ever persisted.
                    exit_status = refused.exit_status
                    oom_killed = False
                    output, produced, truncated = refused.bounded(output_budget)
                storage_bytes = self._volume_bytes(client, workspace_id)
                # Asked on every path, exit 0 included, and under both reserved
                # names. A `stop` that raced this job finishing on its own
                # leaves markers no failing branch would ever reach, and one
                # nobody consumes is one that waits in the volume for whichever
                # later job happens to fail.
                cancelled = self._ended_by_an_operator(anchor, job_id)
            finally:
                # A job container that outlives its job is a stray, and the
                # removal is best-effort on purpose: a successful job must not
                # be reported as an engine failure because the tidy-up lost a
                # race. The label makes it findable, and `remove` reaps it.
                with contextlib.suppress(DockerException, OSError):
                    container.remove(force=True)

        status = self._status(cancelled, timed_out, oom_killed, exit_status)
        return JobOutcome(
            job_id=job_id,
            workspace_id=workspace_id,
            status=status,
            # Derived from the status rather than from the conditions that
            # produced it, so the one rule stays in one place. The seam refuses
            # an exit status on either of these two (see ``JobOutcome``'s own
            # ``_NO_EXIT_STATUSES``) and it is right to: a job somebody else
            # ended never produced an answer of its own, and the 137 sitting in
            # ``State.ExitCode`` is the signal's number, not the command's.
            exit_status=None if status in (STATUS_CANCELLED, STATUS_TIMEOUT) else exit_status,
            output=output,
            truncated=truncated,
            # Only this branch watched the exec fail, so only it may assert the
            # refusal — downstream must never re-derive it from the status.
            command_refused=refused is not None,
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

    def write(
        self,
        workspace_id: str,
        path: str,
        source: ByteSource,
        *,
        expected_sha256: str,
        overwrite: bool = False,
    ) -> None:
        """Land one file's bytes in a workspace, verified inside the workspace.

        The inbound counterpart to :meth:`read`, and the module docstring's
        "Getting a file back in" says why it is not that verb reversed. In
        short: the archive endpoint has no commit point, so one is built here
        out of a staging directory this provider created, a re-hash performed
        by the container, and a rename within the volume's own filesystem. The
        caller's destination does not exist until every one of those has
        succeeded, which is the property that makes a failed copy-in leave the
        workspace exactly as it found it.

        Everything refusable without a socket is refused before one is opened —
        the workspace id, the path (bounded by
        :func:`~headspace.providers.base.require_workspace_path`, since the
        engine would resolve ``..`` quite happily), the destination's *name*
        against :data:`RESERVED_ROOT_NAMES`, and the digest's shape.
        Then, in order: the anchor is required to be *running*, because a
        stopped one cannot run the verification even though ``put_archive``
        against it would succeed; the staging directory is created and the
        image's tools are proved present; the payload is framed and streamed
        into that directory and nowhere else; and the finalize exec verifies,
        bounds and commits.

        Cleanup is a ``finally``, not a trailing call — the same discipline
        :meth:`run` uses to reap a job container. The finalize script removes
        its own staging directory through an ``EXIT`` trap, so the provider's
        own removal runs exactly when that script did not complete: a source
        that died mid-stream, a transfer the engine refused, an exec that never
        returned. Between them, every failure this call *survives* leaves the
        volume as it found it. The one it cannot survive is being killed, and
        that residue is not lost so much as unaddressed until something asks —
        see :meth:`reap_staging`, which exists precisely because a ``finally``
        cannot run in a process that is no longer there.
        """
        workspace_id = require_workspace_id(workspace_id)
        relative = require_workspace_path(path)
        # Before the socket, and on the *name* rather than on what happens to
        # stand there: a reservation that only held when the name was occupied
        # would be no reservation at all for the countersignal, whose whole
        # point is that it is usually absent.
        reserved = relative.split("/", 1)[0]
        if reserved in RESERVED_ROOT_NAMES:
            raise _destination_is_reserved(workspace_id, relative, reserved)
        landing = _Landing.of(workspace_id, relative, expected_sha256, overwrite)
        action = f"writing '{relative}' into workspace {workspace_id}"

        with self._engine(action) as client:
            anchor = self._require(client, workspace_id)
            environment = anchor.labels.get(LABEL_ENVIRONMENT) or anchor.attrs["Config"]["Image"]
            self._require_running(anchor, workspace_id)

            finalized = False
            try:
                self._prepare_staging(anchor, landing, environment)
                with _framed_payload(source, landing) as transfer:
                    anchor.put_archive(landing.staging, transfer)
                status, details = self._staging_script(
                    anchor,
                    workspace_id,
                    environment,
                    FINALIZE_WRITE_SCRIPT,
                    landing.finalize_args,
                )
                # The script's own trap has run by now, whatever it decided, so
                # the staging directory is gone even on the refusals below.
                finalized = True
                self._classify_finalize(landing, status, _first_detail(details))
            finally:
                if not finalized:
                    self._discard_staging(anchor, landing)

    def reap_staging(self, workspace_id: str) -> Sequence[str]:
        """Clear this backend's reserved staging prefix; name what actually went.

        The reconciliation half of :meth:`write`. A copy-in killed between the
        transfer and the rename leaves a staging directory nobody holds a handle
        to any more — the nonce lived in the process that died, and ``write``'s
        signature carries no staging token an orchestrator could have kept. So
        nothing above the seam can *name* the residue; it can only ask the
        backend to clear the prefix it reserved, which is the whole reason that
        prefix is reserved rather than merely conventional.

        Returns the workspace-relative path of every entry that is verified gone
        — removed, then re-checked, the same discipline :meth:`remove` keeps for
        a teardown, because a cleanup report whose claims were never checked is
        exactly the report a reconciler must not be optimistic in. An entry that
        would not delete is left out rather than counted, so a caller's tally is
        of residue actually cleared and never of residue merely attempted.

        An empty result is the ordinary answer, not a failure: a workspace that
        was never mid-copy-in has nothing under the prefix, and a workspace that
        has never had one written to it has no prefix at all. Neither raises.

        Liveness is ``write``'s, for ``write``'s reason: the reap runs as an exec
        and there is no way to delete a path inside a volume through the archive
        endpoint, so a stopped anchor is refused honestly here too. That refusal
        is a :class:`~headspace.providers.base.ProviderError` a reconciler is
        expected to catch and report as "residue left in place" — which is the
        honest thing to say, and the reason this returns what it reaped rather
        than a bare count: a caller that cannot distinguish "nothing was there"
        from "nothing could be done" cannot report either one truthfully.
        """
        workspace_id = require_workspace_id(workspace_id)
        staging_root = f"{WORKSPACE_MOUNT_PATH}/{STAGING_DIR_NAME}"

        with self._engine(f"reaping staging residue in workspace {workspace_id}") as client:
            anchor = self._require(client, workspace_id)
            environment = anchor.labels.get(LABEL_ENVIRONMENT) or anchor.attrs["Config"]["Image"]
            self._require_running(anchor, workspace_id)

            status, details = self._staging_script(
                anchor,
                workspace_id,
                environment,
                REAP_STAGING_SCRIPT,
                (WORKSPACE_MOUNT_PATH, staging_root, *REQUIRED_REAP_TOOLS),
            )
            if status == WRITE_MISSING_TOOL:
                raise _missing_write_tool(
                    workspace_id, environment, _first_detail(details) or "a POSIX tool"
                )
            if status == WRITE_STAGING_UNUSABLE:
                raise _staging_unusable(workspace_id, _first_detail(details) or UNRESOLVABLE_PATH)
            if status != 0:
                raise self._script_broke(
                    workspace_id,
                    f"under '{STAGING_DIR_NAME}'",
                    "reaping staging residue",
                    status,
                    _first_detail(details),
                )

        # Reported the way every other path in this seam reports a location:
        # relative to the workspace root. The container's absolute path is an
        # engine-side detail, and a reconciler rendering it would be publishing
        # a layout headspace does not promise anyone.
        prefix = f"{WORKSPACE_MOUNT_PATH}/"
        return tuple(
            detail[len(prefix) :] if detail.startswith(prefix) else detail for detail in details
        )

    # --- copying a file in ------------------------------------------------

    def _require_running(self, anchor: Container, workspace_id: str) -> None:
        """Refuse a workspace whose container cannot execute the verification.

        Checked before anything is staged so the refusal costs one listing
        rather than a transfer, and checked against ``running`` alone rather
        than :data:`LIVE_STATUSES`: a ``created`` or ``paused`` container is a
        runtime object that exists and cannot run an exec, which for this verb
        is the same situation as an exited one.
        """
        status = str((anchor.attrs.get("State") or {}).get("Status") or "")
        if status != RUNNING_STATUS:
            raise _anchor_not_running(workspace_id, status)

    def _prepare_staging(self, anchor: Container, landing: _Landing, environment: str) -> None:
        """Create this write's staging directory, having proved the image can finish."""
        status, details = self._staging_script(
            anchor, landing.workspace_id, environment, PREPARE_STAGING_SCRIPT, landing.prepare_args
        )
        detail = _first_detail(details)
        if status == WRITE_MISSING_TOOL:
            raise _missing_write_tool(landing.workspace_id, environment, detail or "a POSIX tool")
        if status == WRITE_STAGING_UNUSABLE:
            raise _staging_unusable(landing.workspace_id, detail or UNRESOLVABLE_PATH)
        if status != 0:
            raise self._script_broke(
                landing.workspace_id,
                f"for '{landing.relative}'",
                "preparing a staging directory",
                status,
                detail,
            )

    def _classify_finalize(self, landing: _Landing, status: int, detail: str) -> None:
        """Turn the finalize script's exit status into this project's own taxonomy.

        One mapping, in one place, so the shell and the exit-code policy meet
        exactly once. Everything the table does not name is an engine failure
        with the script's own line kept as evidence — the fail-safe direction,
        because a status invented later cannot silently become a successful
        write, only a loud unexplained one.

        :data:`FINALIZE_WRITE_SCRIPT` can exit with exactly 18, 17, 13, 14, 15
        and 16, and every one of them is named below. The list is written out so
        a status added to the script and forgotten here is visible by reading
        the two side by side, rather than only when a caller meets an
        unexplained failure.
        """
        if status == 0:
            return
        if status == WRITE_STAGED_PATH_IS_A_LINK:
            raise _staged_path_is_a_link(landing.workspace_id, landing.relative)
        if status == WRITE_STAGED_FILE_MISSING:
            raise _staged_file_vanished(landing.workspace_id, landing.relative)
        if status == WRITE_DIGEST_MISMATCH:
            raise _landed_digest_disagrees(
                landing.workspace_id, landing.relative, landing.expected_sha256, detail
            )
        if status == WRITE_ESCAPES_VOLUME:
            raise _escapes_workspace_volume(
                landing.workspace_id, landing.relative, detail or UNRESOLVABLE_PATH
            )
        if status == WRITE_DESTINATION_EXISTS:
            raise _destination_exists(landing.workspace_id, landing.relative)
        if status == WRITE_DESTINATION_IS_DIRECTORY:
            raise _destination_is_a_directory(landing.workspace_id, landing.relative)
        raise self._script_broke(
            landing.workspace_id,
            f"for '{landing.relative}'",
            "verifying the staged file",
            status,
            detail,
        )

    def _staging_script(
        self,
        anchor: Container,
        workspace_id: str,
        environment: str,
        script: str,
        args: Sequence[str],
    ) -> tuple[int, tuple[str, ...]]:
        """Run one staging script in the workspace container; return status and evidence.

        Every value the script reads arrives as an argument (see
        :data:`SCRIPT_ARGV0`), never interpolated into the text, so a
        destination path is data to the shell rather than something the shell
        might read as syntax.

        Two engine answers are recognised here and nowhere else. An exec whose
        binary the image lacks comes back as *output*, not as an exception (see
        :data:`EXEC_START_MARKER`), so a distroless image produces a named
        refusal rather than a report that the engine broke. And an exec against
        a container that stopped between the liveness check and this call
        raises a 409 carrying the container's own id, which is redacted out of
        existence and answered with the same honest condition the check itself
        produces. Anything else re-raises untouched and stays exit 7.
        """
        try:
            result = anchor.exec_run([WRITE_SHELL, "-c", script, SCRIPT_ARGV0, *args])
        except APIError as err:
            if NOT_RUNNING_MARKER in str(err):
                raise _anchor_not_running(workspace_id, "it stopped mid-write") from err
            raise
        status = int(result.exit_code or 0)
        output = (result.output or b"").decode("utf-8", "ignore")
        missing = _exec_binary_missing(status, output)
        if missing is not None:
            raise _missing_write_tool(workspace_id, environment, missing)
        return status, _script_details(output)

    @staticmethod
    def _script_broke(
        workspace_id: str, subject: str, doing: str, status: int, detail: str
    ) -> ProviderError:
        """A staging script failed in a way this provider has no name for.

        Exit 7 with the script's own evidence attached, because the honest
        thing to say about an unrecognised status is that headspace does not
        know — and the direction of the safety matters: an unnamed failure that
        reported the caller's mistake would send an agent off to change a path
        that was never the problem.
        """
        return ProviderError(
            f"the workspace container failed while {doing} {subject} in "
            f"workspace {workspace_id} (status {status})"
            + (f": {redact_engine_text(detail)}" if detail else ""),
            remediation=(
                "the workspace volume was left as it was found — every script here "
                "refuses before it changes anything — so retry, and run "
                f"'headspace inspect {workspace_id}' if it repeats"
            ),
        )

    def _discard_staging(self, anchor: Container, landing: _Landing) -> None:
        """Remove one write's staging residue, never masking why it was needed.

        Best-effort on purpose, and suppressed for the same reason
        :meth:`run`'s job-container removal is: this runs on the failure path,
        where a second exception would replace the diagnosis with the tidy-up's
        complaint about a container that had already gone.
        """
        with contextlib.suppress(DockerException, OSError):
            anchor.exec_run(
                [WRITE_SHELL, "-c", DISCARD_STAGING_SCRIPT, SCRIPT_ARGV0, landing.staging]
            )

    # --- the markers one process leaves for the other (issue #16) -----------

    @staticmethod
    def _read_marker(anchor: Container, name: str) -> _Marker:
        """What the volume holds under one marker name, read with the job gone.

        ``get_archive`` against the *anchor*, exactly as :meth:`read` does and
        for the same reason restated for a different case: the engine resolves
        the path through the container's mounts rather than through a running
        process. By the time :meth:`run` classifies, the job's own container
        has already been reaped in its ``finally``, and the anchor may itself
        have exited — so an answer that needed a live runtime would be no
        answer at all, in precisely the situation the answer matters.

        Only a regular file no larger than :data:`MARKER_MAX_BYTES` yields a
        value. The symlink refusal is the read-side twin of the write's, and it
        is the boundary :meth:`read` already draws for an artifact: the archive
        endpoint resolves a link target within the container's filesystem quite
        happily, so honouring one would let a job nominate any file on that
        filesystem as the thing naming it. A directory says nothing at all, and
        a file too large to be a job id is a job filling the volume rather than
        a marker. All three are reported ``present`` without a value, because
        the caller still has to take them away.

        Every engine failure here reads as "nothing there", and the direction
        of that safety is the whole of it. This probe is *auxiliary* to an
        outcome the engine has already produced: the job ran, its exit status
        is in hand, and its output has been captured. Letting an unreadable
        marker raise would turn a real computation into exit 7 — telling an
        agent to retry work that already finished — over a file that, on the
        overwhelmingly common path, was never supposed to exist. Falling back
        to "no marker" costs exactly the classification the caller would have
        received before this channel existed.
        """
        try:
            stream, _stat = anchor.get_archive(
                f"{WORKSPACE_MOUNT_PATH}/{name}", chunk_size=MARKER_MAX_BYTES
            )
        except _ENGINE_FAILURES:
            # A 404 is the *ordinary* answer, not an error: almost no job is
            # ever stopped by an operator, so almost every probe finds nothing
            # under this name. Everything else that can fail here is the engine
            # failing, and it lands in the same place deliberately — see above
            # for why an auxiliary probe must never cost a caller their result.
            return _NO_MARKER

        with contextlib.ExitStack() as release:
            release.callback(_quietly, stream.close)
            try:
                archive = tarfile.open(mode=ARCHIVE_STREAM_MODE, fileobj=_ChunkReader(stream))
                release.callback(_quietly, archive.close)
                entry = archive.next()
                if entry is None or not entry.isfile() or entry.size > MARKER_MAX_BYTES:
                    return _Marker(present=True)
                member = archive.extractfile(entry)
                if member is None:
                    return _Marker(present=True)
                text = member.read(MARKER_MAX_BYTES).decode("utf-8", "ignore")
            except _ENGINE_FAILURES:
                return _Marker(present=True)
        return _Marker(present=True, value=text.strip())

    @staticmethod
    def _write_marker(anchor: Container, name: str, value: str) -> bool:
        """Put one marker in the volume; say whether it is actually there.

        The nonce is minted here rather than in the script because the shell
        has no unguessable source of one — and unguessable is the property that
        matters: the staging path is the only moment the bytes exist under a
        name a job could reach for, and a job that cannot name it cannot race
        it. The same nonce discipline :meth:`write` uses for a copy-in's
        staging directory, for the same reason.

        Returns rather than raises, because the caller is :meth:`stop`, and
        ending a runaway job is the operator's actual need while naming it is
        the improvement on top. An engine that refuses the exec — a stopped
        anchor, an image with no shell — must not be allowed to withhold the
        signal, so the failure is a ``False`` the caller can proceed past.
        """
        marker = f"{WORKSPACE_MOUNT_PATH}/{name}"
        staged = f"{marker}.{uuid.uuid4().hex}"
        try:
            result = anchor.exec_run(
                [WRITE_SHELL, "-c", WRITE_CANCELLATION_SCRIPT, MARKER_ARGV0, marker, staged, value]
            )
        except _ENGINE_FAILURES:
            return False
        return int(result.exit_code or 0) == 0

    @staticmethod
    def _clear_marker(anchor: Container, name: str) -> None:
        """Take one marker away, never letting the tidy-up become the story.

        Best-effort for the same reason :meth:`run`'s job-container removal and
        :meth:`_discard_staging` are: this runs after an outcome has already
        been decided, and a second exception here would replace a job's real
        result with a complaint about a cleanup. What is lost when it fails is
        a marker the *next* run will meet, read, find naming a job that is not
        its own, and clear again.
        """
        with contextlib.suppress(*_ENGINE_FAILURES):
            anchor.exec_run(
                [
                    WRITE_SHELL,
                    "-c",
                    CLEAR_CANCELLATION_SCRIPT,
                    MARKER_ARGV0,
                    f"{WORKSPACE_MOUNT_PATH}/{name}",
                ]
            )

    def _ended_by_an_operator(self, anchor: Container, job_id: str) -> bool:
        """Did a ``stop`` in another process actually *end* this job?

        Two probes, one answer and one obligation, and the obligation is not
        conditional on the answer.

        The answer is yes only when the **countersignal** names the job that
        just ran. That is the whole of the two-phase shape: the intent marker
        says an operator asked, and an ask is not an ending — a ``stop`` that
        wrote its intent and then died left one behind for a job that may well
        go on to fail entirely on its own account, and reading that as
        ``cancelled`` would put a human's decision in the record of a failure
        nobody caused. The countersignal cannot be written before the signal has
        landed, so it is the only thing in the volume that says the ending
        happened. A marker of either kind naming some *other* job is residue
        from a ``stop`` that raced a job finishing on its own, and letting it
        convict the next failure would be a worse bug than the one this channel
        fixes.

        The intent marker earns its place by buying the countersignal a reader.
        Written after the signal, the countersignal necessarily lands after the
        container died — the same instant this process wakes up — so ``run``
        would usually read the volume just before ``stop`` finished writing to
        it. An intent marker naming *this* job is the one warning that a
        countersignal is coming, and :meth:`_await_countersignal` spends a
        bounded moment on it. It can only ever turn a missed cancellation into
        a recorded one: what the wait waits for is still the countersignal, so
        no amount of intent invents an ending.

        The obligation is that *both* names are cleared, whoever they named and
        whatever shape they were in. Clearing only the name this classification
        happened to read is the easiest way to reintroduce the wedge a
        two-phase channel adds: a ``stop`` that races a job finishing naturally
        leaves both, no failing branch would ever consume them, and whichever
        one survives waits in the volume for a later job.

        Clearing only what was found is what keeps an ordinary job cheap: the
        overwhelmingly common answer is "nothing there", and that answer costs
        two archive probes and no exec at all — so :meth:`run` never acquires a
        requirement for an anchor that can execute a shell, which it has never
        had and must not grow silently.
        """
        # What a marker has to say to be about this job at all. `None` when
        # this workspace's anchor carries no token — see
        # :func:`cancellation_evidence` for why that is a refusal to classify
        # rather than a fallback to comparing bare job ids.
        evidence = cancellation_evidence(anchor, job_id)
        signalled = self._read_marker(anchor, CANCELLATION_SIGNALLED_MARKER_NAME)
        intent = self._read_marker(anchor, CANCELLATION_INTENT_MARKER_NAME)
        if evidence is not None and intent.value == evidence and signalled.value != evidence:
            signalled = self._await_countersignal(anchor, evidence, signalled)
        # Walked over the names rather than over the two locals, so the
        # clearing obligation is spelled by :data:`CANCELLATION_MARKER_NAMES`
        # itself: a phase added to that tuple and forgotten here raises rather
        # than quietly leaving its marker in the volume forever.
        found = {
            CANCELLATION_SIGNALLED_MARKER_NAME: signalled,
            CANCELLATION_INTENT_MARKER_NAME: intent,
        }
        for name in CANCELLATION_MARKER_NAMES:
            if found[name].present:
                self._clear_marker(anchor, name)
        return evidence is not None and signalled.value == evidence

    def _await_countersignal(self, anchor: Container, evidence: str, found: _Marker) -> _Marker:
        """Give the other process its bounded moment to finish saying so.

        Entered only when an intent marker names the job that just settled, so
        the cost is confined to workspaces where an operator really did ask
        about this job — never to the ordinary run, which reaches this method
        not at all. What comes back is the *last* thing read, not merely the
        one that matched: a stale countersignal naming another job is still
        residue the caller has to clear, and losing its ``present`` on the way
        out would leave it in the volume.

        Polled at :data:`POLL_INTERVAL_SECONDS`, the same interval
        :meth:`_await_exit` has already been spending on this job for its whole
        duration — so the probes here are a rounding error against the polling
        that got us here, and the budget rather than the interval is what
        deserves the thought (see :data:`CANCELLATION_SETTLE_SECONDS`).

        The deadline is monotonic and the loop cannot outlive it, because the
        alternative — waiting until the marker appears — would hang a finished
        job's result on a process that may already be gone.
        """
        deadline = time.monotonic() + CANCELLATION_SETTLE_SECONDS
        while time.monotonic() < deadline:
            time.sleep(POLL_INTERVAL_SECONDS)
            found = self._read_marker(anchor, CANCELLATION_SIGNALLED_MARKER_NAME)
            if found.value == evidence:
                return found
        return found

    # --- stopping a job -----------------------------------------------------

    def stop(self, workspace_id: str) -> StopOutcome:
        """End whatever job is running in a workspace, right now — and nothing else.

        Boundary contract, and the one that matters most: this method never
        opens ``headspace.core.store``. :meth:`run` is synchronous and
        blocking, and holds the workspace's flock for a job's *entire*
        duration — which is what makes it the single writer of that
        workspace's stored state for as long as the job runs. An operator
        ending a runaway job cannot go through ``run`` to do it, because that
        call is already in progress and will not return until the job it is
        watching does. So ``stop`` reaches the engine directly, on its own
        connection (:meth:`_engine`), and touches nothing under
        ``~/.headspace``: no store is constructed, no lock is taken, no record
        is written — a ``stop`` that did any of those would be racing the
        ``run`` call it exists to interrupt, exactly the corruption the
        store's per-workspace lock exists to prevent. The still-blocked
        ``run`` call discovers the ending on its own next poll of the very
        engine object this method just signalled (:meth:`_await_exit` already
        calls ``reload()`` on it every iteration) and journals the outcome
        itself: one verb ends the job, the other narrates it, and the store
        keeps exactly one writer even while both act on the same workspace at
        once.

        Found by label, like everything else this provider looks up
        (:data:`LABEL_ROLE` = :data:`ROLE_JOB`, scoped by
        :data:`LABEL_WORKSPACE_ID`), never by a handle this call was given —
        there is no handle to be given. The process calling ``stop`` is not
        the process that called ``run``, and a daemonless CLI keeps no
        in-memory table connecting the two; the label is the only thing that
        survives between them. :meth:`_find` returns the first match, which in
        the steady state is the only one there is: a workspace holds at most
        one live job container at a time, because ``run`` blocks for that
        container's whole life and reaps it in a ``finally`` the moment it
        settles (see :meth:`run`). A job container this call finds but that is
        not in :data:`LIVE_STATUSES` is therefore stale reap residue, not a
        live job, and is treated exactly like there being none at all.

        Graceful first, forceful only if that fails: ``container.stop()``
        sends ``SIGTERM`` and waits :data:`STOP_GRACE_SECONDS` for the job to
        end itself; only a job still alive after that grace period is
        escalated to ``container.kill()`` (``SIGKILL``). A job that catches
        the polite signal and exits inside its own window is never killed at
        all — the same "ask nicely before you force it" shape
        :meth:`_await_exit` already gives a job that outran its wall-clock
        budget instead of jumping straight to a kill.

        Two markers, in two phases, and the order is the evidence (issue #16).
        Before it signals, this method writes
        :data:`CANCELLATION_INTENT_MARKER_NAME` into the workspace volume
        naming the job it is about to end; once the signalling above has run
        its course it writes :data:`CANCELLATION_SIGNALLED_MARKER_NAME` with
        the same id. :meth:`run`, in the other process, reads a cancellation
        off the second one alone. Both go through the anchor container, because
        the anchor outlives every job the workspace runs and the job's own
        container is gone by the time the second write happens. Neither write
        touches ``~/.headspace``, takes a lock or constructs a store — the
        boundary above is exactly as it was before this method wrote anything
        anywhere, and a test proves it against both writes rather than trusting
        this paragraph.

        Neither write may withhold the signal. An engine that refuses the exec
        — a stopped anchor, an image with no shell, a job that planted
        something at a reserved name — makes :meth:`_write_marker` answer
        ``False``, and this method carries on. Ending the runaway job is what
        the operator came for; naming it correctly afterwards is the
        improvement on top, and an improvement that could cancel the thing it
        improves would be a bad trade.

        **The residual window, and which way it fails.** Between the signal
        landing and the countersignal being written, this process holds
        knowledge that exists nowhere else. A ``stop`` killed in that gap — the
        operator's own ``SIGINT``, a lost connection, the machine going away —
        ends the job and never records that it did. The job's ``run`` then
        finds an intent marker naming it, waits
        :data:`CANCELLATION_SETTLE_SECONDS` for a countersignal that is not
        coming, and writes the outcome down as ``failure``: a genuine
        cancellation misreported as the thing every such job was reported as
        before this channel existed.

        That direction is chosen, not merely tolerated. The window cannot be
        closed from here — no sequence of two writes and one signal makes the
        second write survive the process that owes it — so the only question is
        which way it fails when it does. Recording the countersignal *first*
        would close it in the other direction, and that failure is
        unaffordable: a ``stop`` that died before its signal landed would leave
        behind a completed-looking record of an ending that never happened, and
        a job that subsequently failed on its own account would be written down
        as one a human deliberately stopped. A missing cancellation is a record
        that says less than the truth; a fabricated one says something else
        entirely, and an agent or an operator reading it stops looking for the
        real defect. So this channel is built to lose evidence rather than to
        invent it, and every partial state it can be caught in — intent alone,
        neither marker, a marker a job tampered with — classifies exactly as
        the code before it did.

        Reports honestly rather than raising for the ordinary race: a
        workspace with no live job container — because nothing was ever
        started, because the last job already finished, or because it
        finished in the instant between an operator's decision and this call
        landing — is not a mistake, so it comes back as
        a :class:`~headspace.providers.base.StopOutcome` naming no job
        (``job_id`` is ``None``, ``stopped`` is ``False``) instead of a
        :class:`~headspace.providers.base.CliError`. That is deliberately the
        same fact/failure split the seam draws: a caller racing a job that just
        finished on its own gets a true statement back, not an error it has to
        parse to learn the race was harmless.

        Raises :class:`~headspace.providers.base.CliError` (exit 1) for a
        workspace id this backend holds no anchor container for at all — the
        same "unknown workspace" refusal every other verb here raises via
        :func:`~headspace.providers.base.unknown_workspace`. Raises
        :class:`ProviderError` (exit 7) when the engine breaks while reaching
        or signalling the job, exactly as every other verb in this module
        does.
        """
        workspace_id = require_workspace_id(workspace_id)
        with self._engine(f"stopping the job running in workspace {workspace_id}") as client:
            anchor = self._require(client, workspace_id)

            job = self._find(client, workspace_id, ROLE_JOB)
            if job is None or not self._container_is_live(job):
                return StopOutcome(workspace_id=workspace_id, job_id=None, stopped=False)

            job_id = job.labels.get(LABEL_JOB_ID)
            # Phase one: the ask, and it goes in *before* the signal. The
            # still-blocked `run` classifies the instant its container settles,
            # so a marker written afterwards races that poll and loses exactly
            # when the stop was forceful — this one has to be in the volume
            # before anything can happen to the job, because it is what tells
            # `run` to wait for phase two. It says an operator asked and nothing
            # more; on its own it never makes a job `cancelled`.
            #
            # Written into the workspace *volume*, through the engine, and
            # nowhere near `~/.headspace`: the boundary above is unchanged, and
            # a failure to write it is a `False` this call proceeds past,
            # because ending the job is the need and naming it is the
            # improvement.
            #
            # The payload is the workspace's secret plus the job id, not the id
            # alone: the id is the caller's to choose, so a caller who picks a
            # predictable one and runs untrusted code would otherwise have
            # handed that code everything it needed to write this file itself.
            evidence = cancellation_evidence(anchor, job_id or "")
            if evidence is not None:
                self._write_marker(anchor, CANCELLATION_INTENT_MARKER_NAME, evidence)
            job.stop(timeout=STOP_GRACE_SECONDS)
            # The container can disappear between the signal and the follow-up:
            # `run` removes its own job container the moment the job settles, so
            # a graceful stop that works is *expected* to race that removal. A
            # `NotFound` here therefore means the stop succeeded so completely
            # that the object is already reaped — reporting it as an engine
            # failure would tell the caller their stop failed at the exact moment
            # it worked best. The escalation is what needs the object; its
            # absence is the outcome the escalation was for.
            with contextlib.suppress(NotFound):
                job.reload()
                if self._container_is_live(job):
                    job.kill()
            # Phase two: the countersignal, and it can only go in here. It is
            # the claim that the ask took effect, so it must not be written
            # until the signalling above has run its course — writing it up
            # front is precisely the fabrication the second phase exists to
            # prevent. This is the only marker `run` will classify from, and
            # the anchor is what makes it writable at all: the job's own
            # container is dead by now, and the anchor outlives every job the
            # workspace runs.
            if evidence is not None:
                self._write_marker(anchor, CANCELLATION_SIGNALLED_MARKER_NAME, evidence)

        return StopOutcome(workspace_id=workspace_id, job_id=job_id, stopped=True)

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

    @staticmethod
    def _start(
        container: Container, argv: Sequence[str], environment: str
    ) -> _RefusedCommand | None:
        """Start the job's container; report a command the image cannot exec.

        The one narrow place where an :class:`APIError` is *not* an engine
        failure. Returning ``None`` means the container started and the job is
        the caller's to wait on; returning a :class:`_RefusedCommand` means the
        engine refused to exec what it was handed, which is the caller's error
        and belongs in a :class:`~headspace.providers.base.JobOutcome`.

        Every other ``APIError`` leaves here untouched and lands in
        :data:`_ENGINE_FAILURES` exactly as it always did — so this method can
        only ever narrow exit 7, never widen it.
        """
        try:
            container.start()
        except APIError as err:
            refused = _refused_command(err, argv, environment, str(container.id or ""))
            if refused is None:
                raise
            return refused
        return None

    @staticmethod
    def _container_is_live(container: Container) -> bool:
        """Whether the engine still considers this container to be running.

        The ``or ""`` is what lets a missing or null status read as "not live"
        rather than raise: both callers are deciding whether a job has settled,
        and a state the engine declined to name is not evidence it is still
        going.
        """
        return str(container.attrs["State"].get("Status") or "") in LIVE_STATUSES

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
            if not self._container_is_live(container):
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
                except APIError as err:
                    # Two very different situations raise here, and assuming
                    # the benign one turns a broken daemon into a reported
                    # success: the container may have exited between the poll
                    # and the signal (a real exit status we must not discard),
                    # or the engine may simply have failed. Re-read rather than
                    # guess — only a container the engine now agrees is gone
                    # earns the benign reading.
                    container.reload()
                    if not self._container_is_live(container):
                        return False
                    raise ProviderError(
                        "the engine refused to stop a job that outran its wall-clock "
                        f"budget, and the job is still running: {err}"
                    ) from err
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
    def _status(cancelled: bool, timed_out: bool, oom_killed: bool, exit_status: int) -> str:
        """Name what actually stopped the job — never inferred from ``exit_status``.

        Three ways a job can be ended by something other than itself, and the
        order between them is **cancelled > timeout > resource_exhausted**. It
        is one idea applied three times: the more deliberate the act that ended
        the job, the more it outranks the ones beneath it, because that is the
        fact a caller has to act on.

        * ``cancelled`` is a human deciding this work should stop. Nothing
          outranks that — an agent told ``resource_exhausted`` raises a memory
          ceiling and re-runs a job an operator just ended on purpose, and one
          told ``timeout`` widens a budget to the same effect.
        * ``timed_out`` is headspace's own wall-clock enforcer, which also
          stops a container with ``kill()`` and yields the same 137 a kernel
          OOM kill does. Deliberate, but by a policy rather than a person, so
          it outranks the kernel and yields to the operator.
        * ``oom_killed`` is the kernel's own mark, and the least deliberate of
          the three: nobody decided this job in particular should end.

        Each one is pinned by a test with the conditions below it true at the
        same time, rather than left to branch order.

        ``cancelled`` additionally requires a non-zero exit, and that is not a
        detail of how the markers are read. ``stop`` sends ``SIGTERM`` before
        it forces anything, so a job with a handler can finish its work and
        exit 0 inside its grace window — and a job that reported success really
        did succeed, whatever anyone asked of it. The channel records that an
        operator asked and that the ask landed, never that the job was cut
        short.

        ``oom_killed`` comes from ``State.OOMKilled`` alone. Exit status 137 is
        not evidence of anything by itself — a program can call
        ``sys.exit(137)`` on its own account, and reading that as a memory-ceiling
        breach would misreport an honest computational failure as a budget one.
        The same ambiguity is exactly why ``cancelled`` is keyed off a marker
        another process wrote *after* it had signalled, rather than off the
        number: a stopped job and a ``python -c "raise SystemExit(137)"`` leave
        the engine in identical states, and only a positive signal that could
        not have been written before the fact can separate them.
        """
        if cancelled and exit_status != 0:
            return STATUS_CANCELLED
        if timed_out:
            return STATUS_TIMEOUT
        if oom_killed:
            return STATUS_RESOURCE_EXHAUSTED
        return STATUS_SUCCESS if exit_status == 0 else STATUS_FAILURE
