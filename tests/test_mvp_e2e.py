"""The MVP success test, scripted literally, through the CLI.

The requirements doc (section 11) closes its acceptance criteria with one
sentence, and this module is that sentence turned into an executable script::

    A reasoning model can delegate a non-trivial computation, keep the noisy
    working process outside its context, receive a compact evidence-bearing
    result, and recover the intended artifacts without understanding the
    underlying isolation technology.

Five clauses, five things to prove, and the last one dictates the shape of
everything else.

WHY this drives the CLI and not the Python API
----------------------------------------------
"without understanding the underlying isolation technology" is a claim about
the *surface a caller touches*. A test that imported
:class:`~headspace.core.workspace.Orchestrator` and handed it a
:class:`~headspace.providers.docker.DockerProvider` would have already
understood the isolation technology — it would have *named* it in Python — and
whatever it then proved about the result package would be true of a caller who
never existed. So every step below goes through :func:`headspace.cli.main` with
stdout captured: the same entry point the console script calls, the same argv a
model would emit, the same bytes a model would read back. The only place this
module touches the docker SDK is to *audit* the CLI's output against the engine
objects the caller was never told about (see
:func:`test_no_default_output_requires_knowing_about_containers`) and to
guarantee cleanup — never to make the product work.

WHY the computation is what it is
---------------------------------
"Delegate a computation and trust the result" is only under test if the answer
is not obvious by inspection. So the job walks a seeded linear-congruential
sequence, takes the Collatz stopping time of each of its
:data:`RECORDS` values, and reduces them to a total, an arg-max, and an
order-sensitive rolling checksum. Nobody can eyeball that number; nobody can
guess it from the source; and it is *deterministic*, which is what lets
:func:`expected_answer` — deliberately a second, independent implementation
written against the same specification rather than a copy of the program text —
recompute it on the host and compare. If the delegation returned a plausible
lie, this test fails.

The same job is also genuinely noisy: it prints one trace line per record,
roughly 1.7 MB, and writes two real files into the workspace.

WHY the compression is measured and not asserted qualitatively
--------------------------------------------------------------
"Compact" is the product's entire reason to exist, so it is a number here, not
an adjective. :data:`Delegation.emitted_bytes` is what the job really produced
(headspace's own capture measurement, read back through ``inspect --logs
--json``); :data:`Delegation.returned_bytes` is what the default rendering of
``run`` actually wrote to stdout. Every assertion message carries both, so a
regression reports *how far* it regressed rather than merely that it did.

Running without an engine, and leaving nothing behind
-----------------------------------------------------
Same discipline as ``tests/test_integration_docker.py``:
:func:`_skip_without_engine` probes once and the delegation fixture skips
inside itself, so ``DOCKER_HOST=unix:///nonexistent/docker.sock uv run pytest
-q`` passes with skips rather than failing or hanging. The fixture's teardown
force-removes the workspace's engine objects whether the delegation succeeded,
failed, or died halfway — cleanup never depends on ``destroy`` having run — and
a module-scoped reaper sweeps anything still wearing :data:`WORKSPACE_PREFIX`
afterwards. ``HEADSPACE_HOME`` points at a ``tmp_path`` for the whole module,
so the operator's real store is never opened.

One delegation, many assertions
-------------------------------
The whole lifecycle runs once, in a module-scoped fixture, and the test
functions below assert over what it recorded. That keeps the module inside its
runtime budget (one workspace, three containers) while still failing one clause
at a time: a broken digest round-trip does not also report the compression as
broken.
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import io
import json
import os
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import docker
import pytest

from headspace.cli import main
from headspace.cli._commands import create as create_cmd
from headspace.cli._errors import EXIT_SUCCESS
from headspace.core.result import DEFAULT_MAX_BYTES, TRUNCATION_MARKER_PREFIX, inspect_path
from headspace.core.store import HOME_ENV_VAR
from headspace.providers.docker import LABEL_WORKSPACE_ID, engine_unavailable

#: This process's slice of the engine's namespace, disjoint between
#: ``pytest -n auto`` workers — the convention the two Docker suites already use.
WORKER = os.environ.get("PYTEST_XDIST_WORKER", "solo")

#: Every engine object this module makes is reachable from a workspace id
#: starting with this, which is what the reaper and the teardown key off.
WORKSPACE_PREFIX = f"hs-mvp-{WORKER}"

#: Real containers, a real 1.7 MB flood, a real export: slow by unit-test
#: standards, and marked so a caller can select or exclude the whole class.
pytestmark = pytest.mark.integration


# --- the delegated computation ----------------------------------------------

#: How many values the job reduces. Sized so the transcript is comfortably
#: multi-megabyte (the thing that must stay out of the caller's context) while
#: the arithmetic itself stays well under a second on both sides.
RECORDS = 20000

#: Fixed seed: the delegation must be reproducible, or the host could not check
#: the answer without re-running the container.
SEED = 20260728

#: glibc's ``rand`` constants. Chosen because they are unremarkable — the point
#: is a stream nobody can shortcut, not a good generator.
LCG_MULTIPLIER = 1103515245
LCG_INCREMENT = 12345
LCG_MODULUS = 2**31

#: Values are reduced into this range before the Collatz walk, so stopping
#: times are large enough (hundreds of steps) to make the totals non-trivial.
VALUE_RANGE = 1000000

#: Rolling-checksum parameters. Order-sensitive on purpose: a job that computed
#: the right multiset of stopping times in the wrong order still fails.
CHECKSUM_BASE = 131
CHECKSUM_MODULUS = 1000000007

#: The names the job promises to produce, and what each is for. These are the
#: strings ``--declare`` carries and a refused ``destroy`` quotes back.
ANSWER_NAME = "collatz.json"
ANSWER_PURPOSE = "the reduced answer: totals, arg-max and checksum over the generated series"
DIGEST_NAME = "collatz.sha256"
DIGEST_PURPOSE = "the sha256 the job computed for the answer, inside the workspace"

#: The job, as the caller writes it: plain Python, no headspace vocabulary, no
#: awareness that it is inside anything. The two placeholders are substituted
#: from the constants above so the program and :func:`expected_answer` cannot
#: drift apart in their inputs — only in their implementation, which is the
#: whole point of having two.
_PROGRAM_TEMPLATE = r"""
import hashlib, json
records, seed = __RECORDS__, __SEED__
x, total, best_n, best_steps, checksum = seed, 0, 0, -1, 0
for i in range(records):
    x = (1103515245 * x + 12345) % (2 ** 31)
    n = x % 1000000 + 1
    m, steps = n, 0
    while m != 1:
        m = m // 2 if m % 2 == 0 else 3 * m + 1
        steps += 1
    total += steps
    checksum = (checksum * 131 + steps) % 1000000007
    if steps > best_steps:
        best_n, best_steps = n, steps
    print("trace i=%05d lcg=%010d n=%07d steps=%04d total=%09d checksum=%010d"
          % (i, x, n, steps, total, checksum))
answer = {"argmax_n": best_n, "checksum": checksum, "max_steps": best_steps,
          "records": records, "seed": seed, "total_steps": total}
blob = json.dumps(answer, sort_keys=True) + "\n"
open("collatz.json", "w").write(blob)
open("collatz.sha256", "w").write(hashlib.sha256(blob.encode("utf-8")).hexdigest() + "\n")
"""

JOB_PROGRAM = _PROGRAM_TEMPLATE.replace("__RECORDS__", str(RECORDS)).replace("__SEED__", str(SEED))

#: The prefix of one trace line. Counted in the default result to show how
#: little of the transcript came back.
TRACE_PREFIX = "trace i="


def expected_answer() -> dict[str, int]:
    """Recompute the delegated answer on the host, independently.

    Deliberately *not* an ``exec`` of :data:`JOB_PROGRAM`: running the job's own
    code and comparing it to itself would prove only that Docker is
    deterministic. This is a second implementation of the same specification —
    same seed, same generator, same reduction — so agreement between the two is
    evidence about the *answer*, which is what a caller who delegated the work
    is being asked to trust.
    """
    x, total, best_n, best_steps, checksum = SEED, 0, 0, -1, 0
    for _ in range(RECORDS):
        x = (LCG_MULTIPLIER * x + LCG_INCREMENT) % LCG_MODULUS
        n = x % VALUE_RANGE + 1
        steps = 0
        m = n
        while m != 1:
            m = m // 2 if m % 2 == 0 else 3 * m + 1
            steps += 1
        total += steps
        checksum = (checksum * CHECKSUM_BASE + steps) % CHECKSUM_MODULUS
        if steps > best_steps:
            best_n, best_steps = n, steps
    return {
        "argmax_n": best_n,
        "checksum": checksum,
        "max_steps": best_steps,
        "records": RECORDS,
        "seed": SEED,
        "total_steps": total,
    }


# --- engine reachability, probed once ---------------------------------------


@functools.cache
def _engine_reason() -> str:
    """Why this host has no engine, probed once; ``""`` when one answered."""
    return engine_unavailable()


def _skip_without_engine() -> None:
    reason = _engine_reason()
    if reason:
        pytest.skip(f"no reachable docker engine: {reason}")


def _force_remove(workspace_id: str) -> None:
    """Remove a workspace's engine objects, whatever state anything is in.

    Independent of ``destroy`` and of the store on purpose: a test that fails
    mid-delegation must still leave the engine clean, and it cannot rely on the
    very verb whose failure it may be reporting.
    """
    if _engine_reason():
        return
    client = docker.from_env()
    try:
        selector = {"label": f"{LABEL_WORKSPACE_ID}={workspace_id}"}
        for container in client.containers.list(all=True, filters=selector):
            with contextlib.suppress(Exception):
                container.remove(force=True)
        for volume in client.volumes.list(filters=selector):
            with contextlib.suppress(Exception):
                volume.remove(force=True)
    finally:
        client.close()


@pytest.fixture(scope="module", autouse=True)
def _reap_engine_strays() -> Iterator[None]:
    """Sweep anything still wearing :data:`WORKSPACE_PREFIX` when the module ends."""
    yield
    if _engine_reason():
        return
    client = docker.from_env()
    try:
        for container in client.containers.list(
            all=True, filters={"label": LABEL_WORKSPACE_ID}, ignore_removed=True
        ):
            if container.labels.get(LABEL_WORKSPACE_ID, "").startswith(f"{WORKSPACE_PREFIX}-"):
                container.remove(force=True)
        for volume in client.volumes.list(filters={"label": LABEL_WORKSPACE_ID}):
            workspace_id = (volume.attrs.get("Labels") or {}).get(LABEL_WORKSPACE_ID, "")
            if workspace_id.startswith(f"{WORKSPACE_PREFIX}-"):
                volume.remove(force=True)
    finally:
        client.close()


# --- driving the CLI the way a caller does ----------------------------------


def cli(*argv: str) -> tuple[int, str, str]:
    """Invoke the real CLI entry point and capture both streams.

    ``main`` is what ``headspace``'s console script calls, so this is the
    caller's surface and not a shortcut behind it. stderr comes back too
    because a non-zero exit is only diagnosable with the ``error:``/``hint:``
    lines the CLI wrote there.
    """
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


def cli_ok(*argv: str) -> str:
    """:func:`cli`, insisting on exit 0 and returning stdout."""
    code, out, err = cli(*argv)
    assert code == EXIT_SUCCESS, f"`headspace {' '.join(argv)}` exited {code}\nstderr:\n{err}"
    return out


# --- the delegation, run once -----------------------------------------------


@dataclass(frozen=True)
class Delegation:
    """Everything the scripted delegation observed, for the assertions below."""

    workspace_id: str
    job_id: str
    read_back_job_id: str

    #: Default (markdown) stdout of each verb, in the order they were called.
    create_out: str = ""
    run_out: str = ""
    read_back_out: str = ""
    export_digest_out: str = ""
    export_answer_out: str = ""
    inspect_out: str = ""
    destroy_out: str = ""

    #: The full transcript, recovered through the documented separate path.
    transcript: str = ""
    #: The same retrieval in its human rendering, taken before the teardown.
    transcript_rendered: str = ""
    #: Exit code and stderr of the same retrieval attempted *after* destroy.
    logs_after_destroy: tuple[int, str] = (0, "")
    #: Bytes the job really produced, per headspace's own capture measurement.
    emitted_bytes: int = 0
    #: Bytes the default ``run`` rendering actually wrote to stdout.
    returned_bytes: int = 0

    #: Digest the job computed inside the workspace, recovered as an artifact.
    job_digest: str = ""
    #: Where the answer artifact landed on the host.
    answer_path: Path = field(default_factory=Path)

    #: Engine facts the caller was never shown — the audit's needles.
    container_ids: tuple[str, ...] = ()
    image_ids: tuple[str, ...] = ()
    volume_names: tuple[str, ...] = ()

    @property
    def default_outputs(self) -> dict[str, str]:
        """Every default rendering the caller saw, keyed by the verb that wrote it."""
        return {
            "create": self.create_out,
            "run": self.run_out,
            "run (read-back)": self.read_back_out,
            f"export {DIGEST_NAME}": self.export_digest_out,
            f"export {ANSWER_NAME}": self.export_answer_out,
            "inspect": self.inspect_out,
            "destroy": self.destroy_out,
        }


def _engine_facts(workspace_id: str) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """The engine objects behind a workspace: container ids, image ids, volume names.

    Read with the raw SDK, *outside* the product, precisely because none of
    these strings may appear in what the CLI told the caller.
    """
    client = docker.from_env()
    try:
        selector = {"label": f"{LABEL_WORKSPACE_ID}={workspace_id}"}
        containers = client.containers.list(all=True, filters=selector)
        return (
            tuple(container.id for container in containers),
            tuple(str(container.image.id) for container in containers),
            tuple(volume.name for volume in client.volumes.list(filters=selector)),
        )
    finally:
        client.close()


def _delegate(workspace_id: str, root: Path) -> Delegation:
    """The MVP success test, as a caller writes it. Nine CLI invocations."""
    exports = root / "exports"
    exports.mkdir(parents=True, exist_ok=True)
    job_id = f"job-{uuid.uuid4().hex[:10]}"
    read_back_job_id = f"job-{uuid.uuid4().hex[:10]}"

    # 1. Ask for a workspace. No image, no engine, no mount is named.
    create_out = cli_ok("create", "--workspace-id", workspace_id, "--profile", "python3.12")
    container_ids, image_ids, volume_names = _engine_facts(workspace_id)

    # 2. Delegate the noisy computation, declaring what it will leave behind.
    run_out = cli_ok(
        "run",
        "--job-id",
        job_id,
        "--declare",
        f"{ANSWER_NAME}={ANSWER_PURPOSE}",
        "--declare",
        f"{DIGEST_NAME}={DIGEST_PURPOSE}",
        workspace_id,
        "python",
        "-c",
        JOB_PROGRAM,
    )

    # 3. Recover the transcript through the separate path the contract names.
    logs = json.loads(cli_ok("inspect", job_id, "--logs", "--json"))
    record = next(job for job in logs["jobs"] if job["job_id"] == job_id)
    transcript_rendered = cli_ok("inspect", job_id, "--logs")

    # 4. A second job in the same session, reading what the first one wrote.
    read_back_out = cli_ok("run", "--job-id", read_back_job_id, workspace_id, "cat", ANSWER_NAME)

    # 5. Recover the digest the job computed for itself, then use it as the
    #    precondition of the answer's own export: if the bytes that left the
    #    workspace are not the bytes the job hashed, nothing is published.
    digest_path = exports / DIGEST_NAME
    export_digest_out = cli_ok("export", workspace_id, DIGEST_NAME, "--to", str(digest_path))
    job_digest = digest_path.read_text(encoding="utf-8").strip()

    answer_path = exports / ANSWER_NAME
    export_answer_out = cli_ok(
        "export",
        workspace_id,
        ANSWER_NAME,
        "--to",
        str(answer_path),
        "--expect-sha256",
        job_digest,
    )

    # 6. Take stock before tearing down: state, job history, artifact
    #    inventory, effective policy, retained evidence — one verb, one view.
    inspect_out = cli_ok("inspect", workspace_id)

    # 7. Tear it down. The exports are already elsewhere.
    destroy_out = cli_ok("destroy", workspace_id)
    after_code, _, after_err = cli("inspect", job_id, "--logs")

    return Delegation(
        workspace_id=workspace_id,
        job_id=job_id,
        read_back_job_id=read_back_job_id,
        create_out=create_out,
        run_out=run_out,
        read_back_out=read_back_out,
        export_digest_out=export_digest_out,
        export_answer_out=export_answer_out,
        inspect_out=inspect_out,
        destroy_out=destroy_out,
        transcript=str(record["output"]),
        transcript_rendered=transcript_rendered,
        logs_after_destroy=(after_code, after_err),
        emitted_bytes=int(record["output_bytes"]),
        returned_bytes=len(run_out.encode("utf-8")),
        job_digest=job_digest,
        answer_path=answer_path,
        container_ids=container_ids,
        image_ids=image_ids,
        volume_names=volume_names,
    )


@pytest.fixture(scope="module")
def delegation(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Delegation]:
    """Run the whole scripted delegation once, and clean up unconditionally."""
    _skip_without_engine()
    root = tmp_path_factory.mktemp("mvp-e2e")
    workspace_id = f"{WORKSPACE_PREFIX}-{uuid.uuid4().hex[:10]}"

    patch = pytest.MonkeyPatch()
    patch.setenv(HOME_ENV_VAR, str(root / "headspace-home"))
    create_cmd.reset_providers()
    try:
        yield _delegate(workspace_id, root)
    finally:
        _force_remove(workspace_id)
        create_cmd.reset_providers()
        patch.undo()


# --- clause 1: a workspace exists, with an identity and a state -------------


def test_the_caller_gets_an_identity_and_a_lifecycle_state(delegation: Delegation) -> None:
    """``create`` answers with a stable handle and an explicit state (criteria 1-2)."""
    assert f"workspace_id: {delegation.workspace_id}" in delegation.create_out
    assert "lifecycle state: ready" in delegation.create_out
    assert "profile: python3.12" in delegation.create_out
    # Every later verb answered about the same handle, so the identity is stable
    # across invocations and not a per-call token.
    for verb, output in delegation.default_outputs.items():
        assert delegation.workspace_id in output, f"{verb} lost the workspace identity"


# --- clause 2: the computation was really delegated, and the answer is right -


def test_the_delegated_answer_matches_an_independent_recomputation(
    delegation: Delegation,
) -> None:
    """The artifact the workspace produced is the answer the host computes itself.

    This is the clause that makes the delegation worth anything. The caller
    never watched the work; it gets a file back and has to trust it. Here the
    host does the arithmetic a second way and insists the two agree.
    """
    recovered = json.loads(delegation.answer_path.read_text(encoding="utf-8"))
    assert recovered == expected_answer(), (
        "the delegated answer disagrees with the host's independent recomputation:\n"
        f"  delegated: {recovered}\n"
        f"  expected:  {expected_answer()}"
    )
    # And the answer is genuinely not guessable by inspection: a six-figure
    # arg-max and a nine-figure checksum over 20k stopping times.
    assert recovered["records"] == RECORDS
    assert recovered["max_steps"] > 100
    assert recovered["total_steps"] > 100 * RECORDS


# --- clause 3: the result that came back is compact, and measurably so ------


def test_the_default_result_is_compact_against_what_the_job_emitted(
    delegation: Delegation,
) -> None:
    """Measure the compression the product exists to deliver (criteria 7-8).

    Both numbers are real measurements: ``emitted`` is headspace's own capture
    count for the job, ``returned`` is the byte length of what ``run`` wrote to
    stdout in its default rendering. The ratio is the product's claim.
    """
    emitted, returned = delegation.emitted_bytes, delegation.returned_bytes
    ratio = emitted / max(returned, 1)

    assert emitted > 1_000_000, (
        f"the job must really flood: it emitted only {emitted} bytes, "
        "which is not a working process worth keeping out of a context window"
    )
    assert returned <= DEFAULT_MAX_BYTES, (
        f"the default result returned {returned} bytes, over the "
        f"{DEFAULT_MAX_BYTES}-byte contract bound"
    )
    assert ratio >= 100, (
        f"the default result is not compact: the job emitted {emitted} bytes and "
        f"{returned} bytes came back, a ratio of {ratio:.1f}x (want >= 100x)"
    )


def test_the_raw_transcript_did_not_enter_the_default_result(delegation: Delegation) -> None:
    """The noise stayed outside the caller's context (criterion 8).

    Not merely "the result is small" — the *tail* of the transcript is checked
    for by name, because a result that happened to fit while still carrying the
    end of the run would mean the bound, not the selection, was doing the work.
    """
    lines = delegation.transcript.splitlines()
    assert len(lines) >= RECORDS, f"the transcript kept only {len(lines)} of {RECORDS} lines"

    last = lines[-1]
    middle = lines[len(lines) // 2]
    assert last.startswith(TRACE_PREFIX) and middle.startswith(TRACE_PREFIX)
    assert last not in delegation.run_out, "the tail of the transcript came back in the result"
    assert middle not in delegation.run_out, "the middle of the transcript came back"

    returned_traces = delegation.run_out.count(TRACE_PREFIX)
    assert returned_traces * 100 < RECORDS, (
        f"{returned_traces} of {RECORDS} trace lines came back — more than one percent "
        "of the working process entered the result"
    )
    # ...and the result says so, rather than pretending it was everything.
    assert TRUNCATION_MARKER_PREFIX in delegation.run_out
    assert str(delegation.emitted_bytes) in delegation.run_out, (
        "the truncation marker must state the true volume, so the caller knows "
        "what it is not seeing"
    )


def test_the_transcript_stays_retrievable_through_the_separate_path(
    delegation: Delegation,
) -> None:
    """What was left out is still reachable, by the handle the result carries.

    Retrieval is bounded by retention, not by the result package: while the
    workspace lives, ``inspect <job> --logs`` returns the whole transcript in
    both renderings; once it is destroyed the store's record goes with it, and
    the CLI says so by name rather than answering with an empty list.
    """
    assert delegation.transcript.count(TRACE_PREFIX) >= RECORDS
    assert len(delegation.transcript.encode("utf-8")) > 1_000_000
    assert delegation.transcript.splitlines()[-1] in delegation.transcript_rendered
    assert str(delegation.emitted_bytes) in delegation.transcript_rendered

    code, message = delegation.logs_after_destroy
    assert code != EXIT_SUCCESS
    assert delegation.job_id in message and "destroyed workspace" in message


def test_the_truncation_marker_names_a_command_a_caller_can_run(
    delegation: Delegation,
) -> None:
    """The honesty clause must point somewhere real, not merely somewhere.

    A marker naming a command that cannot be typed is worse than no marker: it
    tells a caller its context was trimmed and then sends it nowhere.
    """
    assert inspect_path(delegation.job_id) in delegation.run_out
    assert "inspect headspace inspect" not in delegation.run_out


# --- clause 4: a second job shares the session's state ----------------------


def test_a_second_job_reads_the_first_job_s_state_and_answers_compactly(
    delegation: Delegation,
) -> None:
    """Two jobs, one session, shared workspace state (criterion 4).

    The read-back job is also the cheap demonstration of the whole bargain: a
    command whose output is small comes back *whole*, in the same result
    package shape as the flood, with no flag changed.
    """
    blob = json.dumps(expected_answer(), sort_keys=True)
    assert (
        blob in delegation.read_back_out
    ), "the second job did not see the answer the first job wrote into the workspace"
    assert TRUNCATION_MARKER_PREFIX not in delegation.read_back_out
    assert len(delegation.read_back_out.encode("utf-8")) <= DEFAULT_MAX_BYTES


def test_one_inspect_shows_state_history_inventory_policy_and_evidence(
    delegation: Delegation,
) -> None:
    """The five things criterion 11 asks a caller to be able to see, in one verb."""
    out = delegation.inspect_out
    assert "lifecycle state: ready" in out, "current state"
    assert "2 job(s) recorded in this session" in out, "job history"
    for name in (ANSWER_NAME, DIGEST_NAME):
        assert f"name: {name}" in out, f"artifact inventory is missing {name}"
    assert f"digest: {delegation.job_digest}" in out, "inventory carries integrity"
    assert "policy_summary: network=disabled" in out, "effective policy"
    assert f"captured output of job {delegation.read_back_job_id}" in out, "retained evidence"


def test_the_result_names_the_inputs_that_produced_it(delegation: Delegation) -> None:
    """Criterion 6's second half: which inputs contributed to this result.

    The MVP has exactly one kind of input — the job's own argv, since nothing
    can be staged into a workspace from outside — so provenance records the
    command verbatim, and that is what a caller correlates a result against.
    """
    assert "\n- inputs:\n  - python\n  - -c\n" in delegation.run_out
    assert "\n- inputs:\n  - cat\n  - collatz.json\n" in delegation.read_back_out


def test_every_declared_limit_is_visible_to_the_caller(delegation: Delegation) -> None:
    """Criterion 5, and the backend-imposed half of criterion 14.

    The doc names six limit classes. Each is looked for by its own name in the
    effective-policy line, and the one the host cannot actually enforce is
    required to say so — a limit reported without its enforcement status is the
    silent weakening the policy module exists to prevent.
    """
    for output in (delegation.create_out, delegation.run_out, delegation.inspect_out):
        for limit in (
            "network=",
            "filesystem=",
            "wall_clock=",
            "memory=",
            "storage=",
            "output_bytes=",
        ):
            assert limit in output, f"the effective policy never mentions {limit!r}"
        assert "storage=1073741824 (measured)" in output, (
            "storage is reported but not capped by the default volume driver, and the "
            "result must label it 'measured' rather than implying a hard cap"
        )
        assert "the storage limit is measured, not enforced" in output


# --- clause 5: the artifacts come back, verifiably --------------------------


def test_the_artifact_digest_survives_the_round_trip(delegation: Delegation) -> None:
    """The bytes that left are the bytes the job hashed (criterion 9).

    Four independent facts are made to agree: the digest the job computed
    *inside* the workspace, the digest ``export`` computed as the bytes crossed
    the boundary, the digest the host computes over the published file, and the
    ``--expect-sha256`` precondition that would have refused to publish had any
    of them differed.
    """
    published = delegation.answer_path.read_bytes()
    host_digest = hashlib.sha256(published).hexdigest()

    assert len(delegation.job_digest) == 64
    assert host_digest == delegation.job_digest, (
        "the published artifact does not hash to what the job computed inside "
        f"the workspace: host {host_digest} vs job {delegation.job_digest}"
    )
    # The export reported the same digest back to the caller, with its size and
    # its purpose, so a caller that never touched the file still knows all four.
    assert f"sha256:{delegation.job_digest}" in delegation.export_answer_out
    assert f"{len(published)} bytes verified" in delegation.export_answer_out
    assert ANSWER_PURPOSE in delegation.export_answer_out
    assert f"name: {ANSWER_NAME}" in delegation.export_answer_out
    # The four facts criterion 9 names, plus where the bytes now live.
    assert "media_type: application/octet-stream" in delegation.export_answer_out
    assert f"reference: {delegation.answer_path}" in delegation.export_answer_out


def test_destroy_reports_what_it_removed_and_the_export_outlives_it(
    delegation: Delegation,
) -> None:
    """Destruction is reported in neutral nouns, and the artifacts remain (criteria 12-13)."""
    assert "removed: runtime, storage" in delegation.destroy_out
    assert "retained: (none)" in delegation.destroy_out
    assert "unverified: (none)" in delegation.destroy_out
    assert "lifecycle path:" in delegation.destroy_out

    # The point of exporting first: the workspace is gone and the answer is not.
    assert delegation.answer_path.exists()
    assert json.loads(delegation.answer_path.read_text(encoding="utf-8")) == expected_answer()
    # And the engine agrees the workspace is gone.
    containers, _, volumes = _engine_facts(delegation.workspace_id)
    assert containers == () and volumes == ()


def test_destroy_names_the_exported_artifacts_that_remain_elsewhere(
    delegation: Delegation,
) -> None:
    """The other half of criterion 13: what survived, and where it went."""
    assert ANSWER_NAME in delegation.destroy_out
    assert str(delegation.answer_path) in delegation.destroy_out


# --- clause 6: none of it required knowing about containers ------------------

#: Mechanics a caller would have to understand the backend to make sense of.
#: The backend's *name* is deliberately not on this list: the result contract
#: reports which provider ran the work (``destroyed on docker``) because
#: "at least one isolated execution backend" has to be nameable for a caller to
#: choose it, and because reconciliation refuses to reap another backend's
#: workspaces. What must not leak is how that backend *works*.
BACKEND_MECHANICS: tuple[str, ...] = (
    "HostConfig",
    "NetworkMode",
    "Mounts",
    "Binds",
    "docker.sock",
    "/var/run/docker",
    "cgroup",
    "Cgroup",
    "OCI",
    "containerd",
    "Entrypoint",
    "RepoTags",
    "RootFS",
    "layer",
)

#: A default result must never instruct the caller to drive the engine itself.
_ENGINE_COMMAND = re.compile(r"\bdocker\s+(run|exec|ps|rm|cp|logs|pull|build|volume|inspect)\b")


def test_no_default_output_requires_knowing_about_containers(delegation: Delegation) -> None:
    """The isolation technology never surfaced (criterion 3).

    The needles are not a guessed vocabulary: the container ids, image ids and
    volume names asserted absent here were read off the live engine during the
    delegation, so this is a comparison against the actual objects rather than
    against a list of words someone hoped were the right ones. The vocabulary
    list is the second half, catching the shapes an id-only check would miss.
    """
    needles = delegation.container_ids + delegation.image_ids + delegation.volume_names
    assert needles, "the audit needs the engine's own identifiers to be meaningful"

    for verb, output in delegation.default_outputs.items():
        for needle in needles:
            assert needle not in output, f"{verb} leaked the engine identifier {needle!r}"
        for token in BACKEND_MECHANICS:
            assert token not in output, f"{verb} leaked backend mechanics: {token!r}"
        assert _ENGINE_COMMAND.search(output) is None, f"{verb} told the caller to drive the engine"


def test_the_result_names_the_environment_without_naming_the_engine(
    delegation: Delegation,
) -> None:
    """Provenance pins the environment by profile and digest, not by image id.

    The digest a caller does see is the profile registry's own pinned reference
    — the reproducibility fact — and it is deliberately *not* any identifier the
    engine minted locally, which is what the id comparison above proves.
    """
    assert "profile: python3.12" in delegation.run_out
    assert "image_digest: sha256:" in delegation.run_out
    for image_id in delegation.image_ids:
        assert image_id not in delegation.run_out
