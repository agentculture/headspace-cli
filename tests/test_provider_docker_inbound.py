"""Copying a file *in*: the staging path, the boundary, and the honest refusals.

WHY this module exists
----------------------
``read`` closed one half of the durability gap; ``write`` closes the other, and
it is the half with a commit point to get wrong. The engine's archive endpoint
has none — ``put_archive`` lands bytes and returns ``True``, and there is no
moment at which the daemon will tell you "these bytes are now the file, and
they are the bytes you sent". So the provider builds a commit point out of
pieces the engine does give it: a staging directory it created itself, a
re-hash *inside* the container, and a rename within one filesystem. This module
is where each of those pieces is proven to be load-bearing rather than
decorative.

Real bytes, a real shell, no engine
-----------------------------------
:class:`HostBackedAnchor` is a stand-in for the workspace container that does
not pretend: its ``exec_run`` really runs the host's ``/bin/sh`` against a real
directory standing in for the workspace volume, and its ``put_archive`` really
untars what the provider streamed at it. Only the *daemon* is absent. That
matters more here than anywhere else in the suite, because the interesting half
of this verb is a shell script — a scripted ``(exit_code, output)`` stub would
assert that the provider classifies answers it was handed, and say nothing at
all about whether ``realpath`` actually catches a planted symlink or whether
``sha256sum`` actually catches swapped bytes. Those are the claims worth
testing, so the shell runs.

The mapping is one substitution in each direction: the container's
:data:`~headspace.providers.docker.WORKSPACE_MOUNT_PATH` is the fixture's
directory on the way in, and the fixture's directory is
:data:`~headspace.providers.docker.WORKSPACE_MOUNT_PATH` again on the way out,
so the provider sees container-shaped paths in every string it parses or
reports — exactly as it would from a daemon.

Wordings recorded, not invented
-------------------------------
:data:`EXEC_NO_SUCH_SHELL` and :data:`NOT_RUNNING_EXPLANATION` are what a live
engine said (Docker 29.1.3 / API 1.52, probed 2026-07-29), not a plausible
paraphrase. Two facts from that probe shape this whole module:

* an exec whose *binary* is missing does **not** raise — it comes back as exit
  127 with the OCI diagnostic as the exec's output, which is why the provider
  inspects an exec's output for it rather than only catching :class:`APIError`;
* an exec against a stopped container raises a 409 whose explanation carries
  the container's full id, which is why the stopped-anchor path is asserted to
  leak neither the id nor the engine's URL.

The same probe recorded a third fact that this module turns into a test rather
than a comment: ``put_archive`` succeeds against a *stopped* container. Copy-in
is therefore not refused on a stopped anchor because the transfer would fail —
it would not — but because nothing could verify what landed.

The live end-to-end counterpart of these assertions belongs to the integration
lane; everything here runs on a host with no daemon.
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import stat
import subprocess  # nosec B404 - the fixture runs the host's own shell on purpose
import tarfile
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import IO, Any

import pytest
from docker.errors import APIError, NotFound

from headspace.cli._errors import (
    EXIT_ENV_ERROR,
    EXIT_INFRASTRUCTURE_FAILURE,
    EXIT_USER_ERROR,
    CliError,
)
from headspace.core import profiles
from headspace.providers.base import ProviderError
from headspace.providers.docker import (
    DISCARD_STAGING_SCRIPT,
    EXEC_START_MARKER,
    FINALIZE_WRITE_SCRIPT,
    LABEL_ENVIRONMENT,
    LABEL_PROVIDER,
    LABEL_ROLE,
    LABEL_WORKSPACE_ID,
    NOT_RUNNING_MARKER,
    OBJECT_PREFIX,
    PREPARE_STAGING_SCRIPT,
    REAP_STAGING_SCRIPT,
    REQUIRED_REAP_TOOLS,
    REQUIRED_WRITE_TOOLS,
    ROLE_WORKSPACE,
    SCRIPT_ARGV0,
    SCRIPT_DETAIL_MARKER,
    STAGED_FILE_NAME,
    STAGING_DIR_NAME,
    WORKSPACE_MOUNT_PATH,
    WRITE_DESTINATION_EXISTS,
    WRITE_DESTINATION_IS_DIRECTORY,
    WRITE_DIGEST_MISMATCH,
    WRITE_ESCAPES_VOLUME,
    WRITE_MISSING_TOOL,
    WRITE_SHELL,
    WRITE_STAGED_FILE_MISSING,
    WRITE_STAGING_UNUSABLE,
    DockerProvider,
)

WORKSPACE = "inbound-ws"

#: The environment the fixture workspace was created from — the default
#: profile's own digest-pinned reference, so a refusal that names "the profile"
#: has a real profile to name.
ENVIRONMENT = profiles.resolve(profiles.DEFAULT_PROFILE)

#: A container id of the shape the engine mints, and the endpoint it leaks
#: from. Recorded from the same probe as the wordings below.
CONTAINER_ID = "c5a84b321204aaff9b04512e4d50b62e41f4b8f8c4b6ee510e91ffd584770314"
ENDPOINT = f"http+docker://localhost/v1.52/containers/{CONTAINER_ID}/exec"

#: What the engine actually answers when an exec names a binary the image does
#: not have. Recorded live: this arrives as the exec's **output** at exit 127,
#: not as an exception — which is why a distroless image produces an honest
#: refusal here rather than an unhandled :class:`APIError`.
EXEC_NO_SUCH_SHELL = (
    "OCI runtime exec failed: exec failed: unable to start container process: "
    f'exec: "{WRITE_SHELL}": stat {WRITE_SHELL}: no such file or directory: unknown\r\n'
)

#: What the engine actually answers when an exec is asked of a container that
#: has stopped. Recorded live, container id and all.
NOT_RUNNING_EXPLANATION = f"container {CONTAINER_ID} is not running"

#: The three substrings a caller must never receive from the stopped-anchor path.
FORBIDDEN = (CONTAINER_ID, "http+docker", "409 Client Error")

PAYLOAD = b"headspace copy-in\n" * 64
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()

#: The host tools the fixture's shell needs to stand in for the container's.
#: Absent any of them the fixture would be testing its own gaps, so it skips.
_HOST_TOOLS = ("sh", *REQUIRED_WRITE_TOOLS)


def _missing_host_tools() -> list[str]:
    return [tool for tool in _HOST_TOOLS if shutil.which(tool) is None]


pytestmark = pytest.mark.skipif(
    bool(_missing_host_tools()),
    reason=f"the host shell lacks {', '.join(_missing_host_tools())}",
)


# --- a workspace container that is really a directory and really a shell -----


class HostBackedAnchor:
    """The workspace container, with the daemon replaced and nothing else.

    ``exec_run`` and ``put_archive`` are the entire surface :meth:`DockerProvider.write`
    touches, and both are honoured for real here: the first runs the provider's
    own script text through the host's ``/bin/sh``, the second untars the
    provider's own transfer stream onto the host's filesystem. What is faked is
    the transport, not the semantics — so a test that says "a planted symlink is
    refused" is a statement about ``realpath``, not about a stub's ``if``.
    """

    def __init__(self, root: Path, labels: Mapping[str, str]) -> None:
        # Resolved once: the script compares ``realpath`` output against this
        # prefix, so a fixture root reached through a symlinked ``/tmp`` would
        # otherwise fail the volume-boundary check for the wrong reason.
        self.root = Path(root).resolve()
        self.id = CONTAINER_ID
        self.labels = dict(labels)
        self.attrs: dict[str, Any] = {
            "Config": {"Image": ENVIRONMENT},
            "HostConfig": {"NetworkMode": "none"},
            "State": {"Status": "running", "ExitCode": None},
        }
        #: Ordered record of what the provider did, so ordering is asserted
        #: rather than inferred from side effects.
        self.calls: list[str] = []
        #: Every member the provider's transfer archive carried, so "exactly
        #: one regular file" is checked against the bytes on the wire.
        self.members: list[tarfile.TarInfo] = []
        #: The container path each ``put_archive`` targeted.
        self.put_targets: list[str] = []
        #: Snapshot of the volume taken at ``put_archive`` time, which is how
        #: "the destination did not exist yet" is proven rather than assumed.
        self.volume_at_put: list[str] = []
        #: Armed by a test: runs immediately after the archive is extracted,
        #: standing in for a job container that shares the volume.
        self.after_put: Any = None
        #: Armed by a test: the ``PATH`` the scripts run under, which is how a
        #: profile missing one tool is modelled without a distroless image.
        self.path_override: str | None = None
        #: Armed by a test: the exec answers with the recorded OCI diagnostic
        #: instead of running, which is what an image with no shell does.
        self.shell_missing = False
        #: Armed by a test: the discard exec is recorded and then not run.
        #: This is how a *killed* copy-in is modelled. A ``SIGKILL`` means no
        #: further code runs at all — but no in-process test can stop a
        #: ``finally`` from executing, so what is suppressed instead is the one
        #: thing that ``finally`` actually does to the volume: the cleanup exec.
        #: The resulting volume state is identical to the one a killed process
        #: leaves, which is the state the reaper exists for.
        self.skip_discard = False

    # -- path translation, one substitution each way --------------------------

    def _to_host(self, value: str) -> str:
        if value == WORKSPACE_MOUNT_PATH:
            return str(self.root)
        if value.startswith(f"{WORKSPACE_MOUNT_PATH}/"):
            return f"{self.root}{value[len(WORKSPACE_MOUNT_PATH):]}"
        return value

    def _to_container(self, text: str) -> str:
        return text.replace(str(self.root), WORKSPACE_MOUNT_PATH)

    # -- the surface the provider actually uses -------------------------------

    def exec_run(self, cmd: Sequence[str], **_: Any) -> SimpleNamespace:
        step = _step_of(cmd)
        self.calls.append(step)
        if step == "discard" and self.skip_discard:
            return SimpleNamespace(exit_code=0, output=b"")
        if self.attrs["State"]["Status"] != "running":
            raise _not_running_error()
        if self.shell_missing:
            return SimpleNamespace(exit_code=127, output=EXEC_NO_SUCH_SHELL.encode())
        shell, dash_c, script, argv0, *rest = cmd
        assert (shell, dash_c, argv0) == (WRITE_SHELL, "-c", SCRIPT_ARGV0)
        environment = dict(os.environ)
        if self.path_override is not None:
            environment["PATH"] = self.path_override
        completed = subprocess.run(  # nosec B603 - the fixture's own shell, by design
            [
                shutil.which("sh") or "/bin/sh",
                "-c",
                script,
                argv0,
                *(self._to_host(a) for a in rest),
            ],
            capture_output=True,
            env=environment,
            check=False,
        )
        output = completed.stdout + completed.stderr
        return SimpleNamespace(
            exit_code=completed.returncode,
            output=self._to_container(output.decode("utf-8", "ignore")).encode(),
        )

    def put_archive(self, path: str, data: IO[bytes]) -> bool:
        self.calls.append("put_archive")
        self.put_targets.append(path)
        target = Path(self._to_host(path))
        if not target.is_dir():
            # The daemon's own answer, recorded live: an archive PUT to a path
            # that is not there is a 404. Nothing the provider staged can land
            # anywhere it did not first create.
            raise NotFound(f"404 Client Error for {ENDPOINT}: Not Found")
        archive = tarfile.open(mode="r|", fileobj=data)  # noqa: SIM115 - closed below
        try:
            for member in archive:
                self.members.append(member)
                extracted = archive.extractfile(member)
                assert extracted is not None, "the transfer archive held no readable member"
                (target / member.name).write_bytes(extracted.read())
                os.chmod(target / member.name, member.mode)  # nosec B103 - the tar's own mode
        finally:
            archive.close()
        self.volume_at_put = _listing(self.root)
        if self.after_put is not None:
            self.after_put(self.root)
        return True


def _step_of(cmd: Sequence[str]) -> str:
    """Name the provider's step from the script it is about to run.

    Comparing against the module's own script constants rather than sniffing
    the text: it pins that ``write`` runs *these* three scripts and no fourth,
    so a refactor that quietly inlines a different one fails here.
    """
    script = cmd[2] if len(cmd) > 2 else ""
    return {
        PREPARE_STAGING_SCRIPT: "prepare",
        FINALIZE_WRITE_SCRIPT: "finalize",
        DISCARD_STAGING_SCRIPT: "discard",
        REAP_STAGING_SCRIPT: "reap",
    }.get(script, "unknown-script")


def _not_running_error() -> APIError:
    """The 409 a live engine raises for an exec against a stopped container."""
    response = SimpleNamespace(status_code=409, url=ENDPOINT, reason="Conflict")
    return APIError("409 Client Error", response=response, explanation=NOT_RUNNING_EXPLANATION)


def _listing(root: Path) -> list[str]:
    """Every path under ``root``, relative and sorted — links never followed."""
    found: list[str] = []
    for directory, subdirectories, files in os.walk(root, followlinks=False):
        for name in list(subdirectories) + files:
            found.append(str(Path(directory, name).relative_to(root)))
    return sorted(found)


class StubEngine:
    """A ``docker.DockerClient`` stand-in holding exactly one workspace container."""

    def __init__(self, anchor: HostBackedAnchor) -> None:
        self.anchor = anchor
        self.containers = _StubContainers(self)
        self.volumes = _StubVolumes()
        #: Anything ``write`` created. The verb's strongest guarantee is that
        #: this stays empty, so it is recorded rather than trusted.
        self.created: list[str] = []
        self.closed = 0

    def objects(self) -> tuple[int, int]:
        return len(self.containers.registry), len(self.volumes.registry)

    def close(self) -> None:
        self.closed += 1


class _StubVolumes:
    def __init__(self) -> None:
        self.registry = [SimpleNamespace(name=f"{OBJECT_PREFIX}{WORKSPACE}")]

    def list(self, **_: Any) -> list[Any]:
        return list(self.registry)


class _StubContainers:
    def __init__(self, engine: StubEngine) -> None:
        self._engine = engine
        self.registry = [engine.anchor]

    def create(self, image: str, **kwargs: Any) -> Any:
        # Never reached by a correct ``write``; recorded so the test can say so.
        self._engine.created.append(str(kwargs.get("name") or image))
        raise AssertionError("write must not create an engine object")

    def list(self, all: bool = False, filters: Mapping[str, Any] | None = None) -> list[Any]:
        del all
        wanted = dict(
            str(pair).split("=", 1) for pair in (filters or {}).get("label", []) if "=" in str(pair)
        )
        return [
            container
            for container in self.registry
            if all_labels_match(container.labels, wanted)  # noqa: F821 - defined below
        ]


def all_labels_match(labels: Mapping[str, str], wanted: Mapping[str, str]) -> bool:
    return all(labels.get(key) == value for key, value in wanted.items())


# --- fixtures ----------------------------------------------------------------


@pytest.fixture
def volume(tmp_path: Path) -> Path:
    """The directory standing in for the workspace volume."""
    root = tmp_path / "volume"
    root.mkdir()
    return root


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    """A directory the workspace volume does not contain — the escape target."""
    escape = tmp_path / "outside"
    escape.mkdir()
    return escape


@pytest.fixture
def anchor(volume: Path) -> HostBackedAnchor:
    return HostBackedAnchor(
        volume,
        {
            LABEL_WORKSPACE_ID: WORKSPACE,
            LABEL_PROVIDER: "docker",
            LABEL_ROLE: ROLE_WORKSPACE,
            LABEL_ENVIRONMENT: ENVIRONMENT,
        },
    )


@pytest.fixture
def engine(anchor: HostBackedAnchor) -> StubEngine:
    return StubEngine(anchor)


@pytest.fixture
def provider(engine: StubEngine) -> DockerProvider:
    return DockerProvider(connect=lambda: engine)


def write(provider: DockerProvider, path: str, payload: bytes = PAYLOAD, **kwargs: Any) -> None:
    kwargs.setdefault("expected_sha256", hashlib.sha256(payload).hexdigest())
    provider.write(WORKSPACE, path, io.BytesIO(payload), **kwargs)


def _staging_root(anchor: HostBackedAnchor) -> Path:
    return anchor.root / STAGING_DIR_NAME


# --- the recorded wordings are the engine's own -----------------------------


def test_the_recorded_engine_wordings_carry_the_markers_the_provider_matches_on() -> None:
    """Without this, every classification assertion below could pass vacuously."""
    assert EXEC_START_MARKER in EXEC_NO_SUCH_SHELL
    assert NOT_RUNNING_MARKER in NOT_RUNNING_EXPLANATION
    assert NOT_RUNNING_MARKER in str(_not_running_error())
    for forbidden in FORBIDDEN:
        assert forbidden in str(_not_running_error())


# --- the shell and the provider share one vocabulary, or neither works ------


class TestTheScriptsAndTheConstantsCannotDrift:
    """Two languages, one contract, and nothing but agreement holding it together.

    A shell script cannot import a Python constant, so the marker and the exit
    statuses are written out longhand inside the script text. That is the one
    place this design can rot silently: renumber :data:`WRITE_DIGEST_MISMATCH`
    and the provider starts reading a verified refusal as an unexplained engine
    failure — a change that no behavioural test would catch, because the shell
    would still exit 13 and the provider would still raise. So the agreement is
    asserted directly rather than left to reviewers to notice.
    """

    def test_every_script_reports_its_evidence_through_the_marker_the_provider_reads(
        self,
    ) -> None:
        for script in (PREPARE_STAGING_SCRIPT, FINALIZE_WRITE_SCRIPT):
            assert f'echo "{SCRIPT_DETAIL_MARKER}' in script

    @pytest.mark.parametrize(
        ("status", "script"),
        [
            (WRITE_MISSING_TOOL, PREPARE_STAGING_SCRIPT),
            (WRITE_STAGING_UNUSABLE, PREPARE_STAGING_SCRIPT),
            (WRITE_DIGEST_MISMATCH, FINALIZE_WRITE_SCRIPT),
            (WRITE_ESCAPES_VOLUME, FINALIZE_WRITE_SCRIPT),
            (WRITE_DESTINATION_EXISTS, FINALIZE_WRITE_SCRIPT),
            (WRITE_DESTINATION_IS_DIRECTORY, FINALIZE_WRITE_SCRIPT),
            (WRITE_STAGED_FILE_MISSING, FINALIZE_WRITE_SCRIPT),
        ],
    )
    def test_each_named_status_is_a_status_its_script_can_actually_exit_with(
        self, status: int, script: str
    ) -> None:
        assert f"exit {status}" in script

    def test_the_named_statuses_stay_clear_of_the_shells_own_conventions(self) -> None:
        """1, 2, 126, 127 and 128+n all mean something to a shell already.

        A refusal that collided with one of them would be indistinguishable
        from the shell's own report of a usage error or an unrunnable command
        — and the collision would surface as a copy-in that quietly reported
        the wrong reason, not as a crash.
        """
        named = {
            WRITE_MISSING_TOOL,
            WRITE_STAGING_UNUSABLE,
            WRITE_DIGEST_MISMATCH,
            WRITE_ESCAPES_VOLUME,
            WRITE_DESTINATION_EXISTS,
            WRITE_DESTINATION_IS_DIRECTORY,
            WRITE_STAGED_FILE_MISSING,
        }
        assert len(named) == 7, "two conditions sharing a status cannot be told apart"
        assert named.isdisjoint({0, 1, 2, 126, 127})
        assert all(2 < status < 126 for status in named)

    def test_the_preflight_checks_exactly_the_tools_the_finalize_step_uses(self) -> None:
        """The preflight is only worth running if it covers what comes after it.

        The tools are passed into the prepare script as arguments precisely so
        the list cannot be stated twice — but that only helps if the list is
        the right one, so every tool is checked to be a word the finalize
        script actually invokes.
        """
        for tool in REQUIRED_WRITE_TOOLS:
            assert f"{tool} " in FINALIZE_WRITE_SCRIPT or f"{tool}(" in FINALIZE_WRITE_SCRIPT
        assert "rm -rf" in DISCARD_STAGING_SCRIPT


# --- acceptance criterion 1: bytes land in staging, and are renamed from it --


class TestBytesLandOnlyInStaging:
    """The commit point the engine does not provide, built and then proven."""

    def test_the_transfer_targets_a_prepared_staging_path_and_the_rename_comes_after(
        self, provider: DockerProvider, anchor: HostBackedAnchor
    ) -> None:
        """Three facts in one flow: order, target, and "not yet the destination".

        The ordering assertion alone would tolerate a provider that streamed
        straight at ``/workspace/out.bin`` and *then* ran a verifying script
        over it — which is precisely the shape this design refuses, because it
        publishes unverified bytes under the caller's own name first. So the
        transfer's target is checked to be under the reserved staging prefix,
        and the volume is listed at the moment the archive lands: the caller's
        destination must not be in it.
        """
        write(provider, "out.bin")

        assert anchor.calls == ["prepare", "put_archive", "finalize"]
        (target,) = anchor.put_targets
        assert target.startswith(f"{WORKSPACE_MOUNT_PATH}/{STAGING_DIR_NAME}/")
        assert "out.bin" not in anchor.volume_at_put
        assert (anchor.root / "out.bin").read_bytes() == PAYLOAD

    def test_the_transfer_archive_carries_exactly_one_regular_member(
        self, provider: DockerProvider, anchor: HostBackedAnchor
    ) -> None:
        """A tar is a container format; a copy-in is one file. The two must agree.

        A second member would be a second write nobody asked for, landing at a
        name the caller never named and outside everything the finalize step
        checks — so the archive's shape is asserted on the bytes the provider
        actually put on the wire, not on the intent behind them.
        """
        write(provider, "out.bin")

        (member,) = anchor.members
        assert member.name == STAGED_FILE_NAME
        assert member.isfile()
        assert member.size == len(PAYLOAD)
        # Readable by whatever user the image runs as: the finalize step has to
        # hash the staged file, and a mode only root could read would make a
        # non-root profile fail for reasons that have nothing to do with bytes.
        assert member.mode & stat.S_IRGRP and member.mode & stat.S_IROTH

    def test_a_completed_write_leaves_no_staging_residue(
        self, provider: DockerProvider, anchor: HostBackedAnchor
    ) -> None:
        write(provider, "out.bin")
        assert _listing(_staging_root(anchor)) == []

    def test_a_nested_destination_gets_its_parents_inside_the_volume(
        self, provider: DockerProvider, anchor: HostBackedAnchor
    ) -> None:
        write(provider, "results/deep/final.csv")
        assert (anchor.root / "results/deep/final.csv").read_bytes() == PAYLOAD

    def test_the_source_may_be_an_iterable_of_chunks_as_well_as_a_reader(
        self, provider: DockerProvider, anchor: HostBackedAnchor
    ) -> None:
        """``ByteSource`` is one type with two shapes; a copy-in honours both."""
        provider.write(
            WORKSPACE,
            "chunked.bin",
            [PAYLOAD[:10], PAYLOAD[10:]],
            expected_sha256=DIGEST,
        )
        assert (anchor.root / "chunked.bin").read_bytes() == PAYLOAD


# --- the engine-side re-hash, which is the whole point ----------------------


class TestTheEngineSideRehashIsLoadBearing:
    """The host's digest certifies bytes it sent; only the container's certifies
    bytes that are *there*. This is the difference, made visible."""

    def test_bytes_rewritten_after_the_transfer_are_caught_before_the_rename(
        self, provider: DockerProvider, anchor: HostBackedAnchor
    ) -> None:
        """The race the design exists for, driven rather than described.

        A job container shares the workspace volume and is not a headspace
        verb, so no lock excludes it from rewriting the staged file between the
        archive landing and the rename. ``after_put`` is that job. Without the
        in-container re-hash the provider would rename bytes it never saw,
        under a digest that certified different ones — so this test is the
        difference between a verified copy-in and a hopeful one.
        """

        def a_job_rewrites_the_staged_file(root: Path) -> None:
            for staged in (root / STAGING_DIR_NAME).glob(f"*/{STAGED_FILE_NAME}"):
                staged.write_bytes(b"swapped by a job that shares the volume\n")

        anchor.after_put = a_job_rewrites_the_staged_file

        with pytest.raises(ProviderError) as caught:
            write(provider, "out.bin")

        assert caught.value.code == EXIT_INFRASTRUCTURE_FAILURE
        assert caught.value.remediation
        assert not (anchor.root / "out.bin").exists()
        assert _listing(_staging_root(anchor)) == []

    def test_a_digest_that_does_not_describe_the_source_never_reaches_the_engine(
        self, provider: DockerProvider, anchor: HostBackedAnchor
    ) -> None:
        """A stale digest is the caller's, and it is refused before any bytes move.

        Distinct from the case above on purpose. This one is knowable on the
        host, so it is answered on the host — and answering it there is what
        lets the in-container refusal mean the narrower, sharper thing it
        means: the bytes changed after they left.
        """
        source = io.BytesIO(PAYLOAD)
        with pytest.raises(CliError) as caught:
            provider.write(WORKSPACE, "out.bin", source, expected_sha256="0" * 64)

        assert caught.value.code == EXIT_USER_ERROR
        assert not isinstance(caught.value, ProviderError)
        assert anchor.put_targets == []
        assert _listing(_staging_root(anchor)) == []


# --- the boundary: a copy-in never leaves the volume, never clobbers by default


class TestTheDestinationBoundary:
    def test_an_existing_destination_is_refused_without_overwrite(
        self, provider: DockerProvider, anchor: HostBackedAnchor
    ) -> None:
        (anchor.root / "out.bin").write_bytes(b"a job produced this\n")

        with pytest.raises(CliError) as caught:
            write(provider, "out.bin")

        assert caught.value.code == EXIT_USER_ERROR
        assert not isinstance(caught.value, ProviderError)
        assert "overwrite" in caught.value.remediation
        assert (anchor.root / "out.bin").read_bytes() == b"a job produced this\n"

    def test_overwrite_replaces_the_destination_when_it_is_asked_for(
        self, provider: DockerProvider, anchor: HostBackedAnchor
    ) -> None:
        (anchor.root / "out.bin").write_bytes(b"a job produced this\n")
        write(provider, "out.bin", overwrite=True)
        assert (anchor.root / "out.bin").read_bytes() == PAYLOAD

    def test_a_directory_at_the_destination_is_refused_even_with_overwrite(
        self, provider: DockerProvider, anchor: HostBackedAnchor
    ) -> None:
        """``mv`` onto a directory moves *into* it; that is not a copy-in.

        The refusal is not politeness about types: a rename cannot atomically
        replace a directory with a file, so the one guarantee this verb sells
        is unavailable for that destination and it says so instead of doing
        something adjacent.
        """
        (anchor.root / "results").mkdir()

        with pytest.raises(CliError) as caught:
            write(provider, "results", overwrite=True)

        assert caught.value.code == EXIT_USER_ERROR
        assert (anchor.root / "results").is_dir()
        assert _listing(anchor.root / "results") == []

    def test_a_parent_that_resolves_outside_the_volume_is_refused(
        self, provider: DockerProvider, anchor: HostBackedAnchor, outside: Path
    ) -> None:
        """Issue #10's write-path twin: a job plants a link, a copy-in follows it.

        The engine resolves link targets within the container's filesystem
        perfectly happily, so nothing about ``put_archive`` or a rename would
        notice. ``realpath`` inside the container is what notices, and the
        assertion that matters is the negative one: the escape target is still
        empty afterwards.
        """
        (anchor.root / "sub").symlink_to(outside)

        with pytest.raises(CliError) as caught:
            write(provider, "sub/planted.bin")

        assert caught.value.code == EXIT_USER_ERROR
        assert not isinstance(caught.value, ProviderError)
        assert _listing(outside) == []
        assert _listing(_staging_root(anchor)) == []

    def test_a_symlinked_destination_is_replaced_rather_than_written_through(
        self, provider: DockerProvider, anchor: HostBackedAnchor, outside: Path
    ) -> None:
        """``rename`` acts on the link, never on its target — asserted, not assumed.

        This is the one case where following the link would be silent and
        catastrophic: the caller names a path inside the workspace, and the
        bytes land wherever a job pointed it. The rename replaces the link
        itself, so the target keeps the bytes it had.
        """
        target = outside / "target.bin"
        target.write_bytes(b"outside the workspace\n")
        (anchor.root / "link").symlink_to(target)

        write(provider, "link", overwrite=True)

        assert target.read_bytes() == b"outside the workspace\n"
        assert not (anchor.root / "link").is_symlink()
        assert (anchor.root / "link").read_bytes() == PAYLOAD

    def test_a_planted_staging_root_is_refused_before_any_bytes_are_sent(
        self, provider: DockerProvider, anchor: HostBackedAnchor, outside: Path
    ) -> None:
        """The reserved prefix is checked too, because reserving it does not enforce it.

        A job shares the volume and can create ``.headspace-staging`` as a link
        out of it. If the prepare step trusted the name it reserved, the very
        first thing a copy-in did would be to stream bytes outside the volume.
        """
        (anchor.root / STAGING_DIR_NAME).symlink_to(outside)

        with pytest.raises(CliError) as caught:
            write(provider, "out.bin")

        assert caught.value.code == EXIT_USER_ERROR
        assert anchor.put_targets == []
        assert _listing(outside) == []

    def test_a_path_that_would_leave_the_workspace_is_refused_at_the_seam(
        self, provider: DockerProvider, anchor: HostBackedAnchor
    ) -> None:
        with pytest.raises(CliError) as caught:
            write(provider, "../escape.bin")

        assert caught.value.code == EXIT_USER_ERROR
        assert anchor.calls == []

    @pytest.mark.parametrize("digest", ["", "not-hex", "0" * 63, "A" * 64, f"sha256:{'0' * 64}"])
    def test_a_malformed_digest_is_refused_before_the_engine_is_touched(
        self, provider: DockerProvider, anchor: HostBackedAnchor, digest: str
    ) -> None:
        """Bare 64-char lowercase hex, the shape ``core.inputs`` produces.

        A prefixed or uppercase digest would compare unequal to what
        ``sha256sum`` prints and be reported as a *mismatch* — an engine
        failure, retried forever — when the truth is that the caller passed the
        wrong shape. Refusing the shape up front keeps the mismatch honest.
        """
        source = io.BytesIO(PAYLOAD)
        with pytest.raises(CliError) as caught:
            provider.write(WORKSPACE, "out.bin", source, expected_sha256=digest)

        assert caught.value.code == EXIT_USER_ERROR
        assert anchor.calls == []


# --- acceptance criterion 2: honest taxonomy for the anchor and the image ----


class TestHonestRefusals:
    """A stopped anchor and a toolless image are real, nameable conditions."""

    def test_a_stopped_anchor_is_an_infrastructure_failure_that_names_the_anchor(
        self, provider: DockerProvider, anchor: HostBackedAnchor
    ) -> None:
        """Not the caller's fault, not a traceback, and not silently worked around.

        The asymmetry with ``read`` is the whole point and the remediation has
        to carry it: a stopped workspace can still hand its artifacts back, so a
        caller told "stopped" must not conclude the workspace is worthless.
        """
        anchor.attrs["State"] = {"Status": "exited", "ExitCode": 0}

        with pytest.raises(ProviderError) as caught:
            write(provider, "out.bin")

        assert caught.value.code == EXIT_INFRASTRUCTURE_FAILURE
        assert caught.value.category == "infrastructure_failure"
        assert WORKSPACE in caught.value.message
        assert "workspace container" in caught.value.message
        assert "inspect" in caught.value.remediation
        assert anchor.put_targets == []

    def test_an_anchor_that_stops_between_the_check_and_the_exec_is_classified_alike(
        self, provider: DockerProvider, anchor: HostBackedAnchor
    ) -> None:
        """The engine's own 409, and the same answer — with its id stripped.

        The liveness check and the exec are two round trips, so the container
        can stop in between; the engine then answers 409 with the container's
        full id and its internal endpoint in the string. Both readings must
        reach the caller as the same honest condition, and neither may carry
        the handle.
        """

        def stop_the_anchor_after_preparing(root: Path) -> None:
            del root
            anchor.attrs["State"] = {"Status": "exited", "ExitCode": 0}

        anchor.after_put = stop_the_anchor_after_preparing

        with pytest.raises(ProviderError) as caught:
            write(provider, "out.bin")

        assert caught.value.code == EXIT_INFRASTRUCTURE_FAILURE
        assert "workspace container" in caught.value.message
        rendered = f"{caught.value.message} {caught.value.remediation}"
        for forbidden in FORBIDDEN:
            assert forbidden not in rendered

    def test_a_missing_tool_names_the_tool_and_the_profile_that_lacks_it(
        self, provider: DockerProvider, anchor: HostBackedAnchor, tmp_path: Path
    ) -> None:
        """A distroless image must produce a sentence, not a hang or a traceback.

        Modelled by a ``PATH`` with one tool removed rather than by a second
        image, because the claim under test is the provider's preflight and its
        wording — that it says *which* tool and *which* profile — not the
        engine's ability to run a smaller image.
        """
        crippled = tmp_path / "bin"
        crippled.mkdir()
        for tool in REQUIRED_WRITE_TOOLS:
            if tool != "realpath":
                (crippled / tool).symlink_to(shutil.which(tool) or f"/usr/bin/{tool}")
        anchor.path_override = str(crippled)

        with pytest.raises(CliError) as caught:
            write(provider, "out.bin")

        assert caught.value.code == EXIT_ENV_ERROR
        assert not isinstance(caught.value, ProviderError)
        assert "realpath" in caught.value.message
        assert profiles.DEFAULT_PROFILE in caught.value.message
        assert anchor.put_targets == []

    def test_an_image_with_no_shell_is_refused_by_name_rather_than_by_traceback(
        self, provider: DockerProvider, anchor: HostBackedAnchor
    ) -> None:
        """The recorded engine answer: exit 127 with an OCI diagnostic, not an exception.

        This is the case a scripted stub would have got wrong. Because the
        engine reports a missing exec binary through the exec's *output*, a
        provider that only caught :class:`APIError` would read the OCI
        diagnostic as the script's own failure and report a broken engine.
        """
        anchor.shell_missing = True

        with pytest.raises(CliError) as caught:
            write(provider, "out.bin")

        assert caught.value.code == EXIT_ENV_ERROR
        assert WRITE_SHELL in caught.value.message
        assert profiles.DEFAULT_PROFILE in caught.value.message

    def test_an_unknown_workspace_is_the_callers_error(
        self, provider: DockerProvider, anchor: HostBackedAnchor
    ) -> None:
        source = io.BytesIO(PAYLOAD)
        with pytest.raises(CliError) as caught:
            provider.write("no-such-ws", "out.bin", source, expected_sha256=DIGEST)

        assert caught.value.code == EXIT_USER_ERROR
        assert anchor.calls == []

    def test_an_unreachable_engine_is_an_infrastructure_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DOCKER_HOST", "unix:///nonexistent/headspace-no-such-engine.sock")
        monkeypatch.delenv("DOCKER_TLS_VERIFY", raising=False)
        monkeypatch.delenv("DOCKER_CERT_PATH", raising=False)

        unreachable_provider = DockerProvider()
        source = io.BytesIO(PAYLOAD)
        with pytest.raises(ProviderError) as caught:
            unreachable_provider.write(WORKSPACE, "out.bin", source, expected_sha256=DIGEST)

        assert caught.value.code == EXIT_INFRASTRUCTURE_FAILURE
        assert caught.value.remediation


# --- acceptance criterion 3: nothing is created ------------------------------


class TestNothingIsCreated:
    """ "The engine's object count does not move" is a guarantee no reaper matches."""

    def test_a_write_creates_no_engine_object_of_its_own(
        self, provider: DockerProvider, engine: StubEngine, anchor: HostBackedAnchor
    ) -> None:
        """A helper container mounting the same volume was the obvious design.

        It was rejected for the reason the read path already records: a second
        engine object has to be reaped on every failure path, including the
        ones nobody thought of. Copy-in has *more* failure paths than read — a
        digest that disagrees, a destination that exists, a parent that escapes
        — so the argument is stronger here, not weaker. The count is asserted
        across a success and across every refusal.
        """
        before = engine.objects()

        write(provider, "out.bin")
        assert engine.objects() == before

        (anchor.root / "taken.bin").write_bytes(b"already here\n")
        (anchor.root / "adir").mkdir()
        (anchor.root / "escape").symlink_to(anchor.root.parent)
        for attempt in (
            lambda: write(provider, "taken.bin"),
            lambda: write(provider, "adir", overwrite=True),
            lambda: write(provider, "escape/planted.bin"),
            lambda: provider.write(
                WORKSPACE, "stale.bin", io.BytesIO(PAYLOAD), expected_sha256="1" * 64
            ),
        ):
            with pytest.raises(CliError):
                attempt()
            assert engine.objects() == before

        assert engine.created == []

    def test_every_exec_is_run_against_the_workspace_container_itself(
        self, provider: DockerProvider, engine: StubEngine, anchor: HostBackedAnchor
    ) -> None:
        """There is exactly one container in the engine, and it is the anchor.

        The count assertion above would also pass for a provider that created
        and reaped a helper perfectly. This one would not: every exec is
        recorded on the anchor object, so a helper would have to appear in the
        registry to receive one.
        """
        write(provider, "out.bin")
        assert engine.containers.registry == [anchor]
        assert anchor.calls.count("prepare") == 1
        assert anchor.calls.count("finalize") == 1


# --- cleanup is a teardown, not a trailing call ------------------------------


class TestStagingResidue:
    """Every failure path leaves the volume as it found it."""

    def _staged_names(self, anchor: HostBackedAnchor) -> list[str]:
        return _listing(_staging_root(anchor))

    def test_a_refused_destination_leaves_no_staged_bytes_behind(
        self, provider: DockerProvider, anchor: HostBackedAnchor
    ) -> None:
        (anchor.root / "out.bin").write_bytes(b"a job produced this\n")
        with pytest.raises(CliError):
            write(provider, "out.bin")
        assert self._staged_names(anchor) == []

    def test_a_source_that_fails_mid_stream_leaves_no_staged_bytes_behind(
        self, provider: DockerProvider, anchor: HostBackedAnchor
    ) -> None:
        """The staging directory exists before the payload is framed, so a source
        that dies half way is exactly the case a trailing ``rm`` would miss."""

        def failing_source() -> Iterator[bytes]:
            yield PAYLOAD[:10]
            raise OSError("the source went away")

        source = failing_source()
        with pytest.raises(ProviderError) as caught:
            provider.write(WORKSPACE, "out.bin", source, expected_sha256=DIGEST)

        assert caught.value.code == EXIT_INFRASTRUCTURE_FAILURE
        assert self._staged_names(anchor) == []
        assert "discard" in anchor.calls

    def test_a_source_yielding_something_other_than_bytes_is_refused(
        self, provider: DockerProvider, anchor: HostBackedAnchor
    ) -> None:
        with pytest.raises(CliError) as caught:
            provider.write(WORKSPACE, "out.bin", ["not bytes"], expected_sha256=DIGEST)

        assert caught.value.code == EXIT_USER_ERROR
        assert self._staged_names(anchor) == []


# --- reconciliation: residue nobody holds a handle to any more ---------------


class TestReapingStagingResidue:
    """The case ``write``'s own cleanup structurally cannot reach.

    Every failure ``write`` survives, it tidies up after. The one it cannot is
    the one where it does not survive: a process killed between the transfer
    and the rename leaves a staging directory whose nonce died with it. Nothing
    above the seam can name that directory — ``write``'s signature never handed
    one out — so reconciliation can only ask the backend to clear the prefix it
    reserved. That is what makes the prefix reserved rather than conventional,
    and this is where it is proven to work.
    """

    def _plant(self, anchor: HostBackedAnchor, *names: str) -> None:
        """Stand in for copy-ins that died between put_archive and the rename."""
        for name in names:
            corpse = anchor.root / STAGING_DIR_NAME / name
            corpse.mkdir(parents=True)
            (corpse / STAGED_FILE_NAME).write_bytes(PAYLOAD)

    def test_residue_is_removed_and_every_removal_is_reported(
        self, provider: DockerProvider, anchor: HostBackedAnchor
    ) -> None:
        """Both halves matter: the volume is clean, and the caller is told what went.

        A reaper that removed without reporting would leave a reconciler unable
        to say anything true about what it did; one that reported without
        removing would let it say something false. So the return value and the
        filesystem are asserted against each other.
        """
        self._plant(anchor, "deadbeef" * 4, "cafef00d" * 4)

        reaped = provider.reap_staging(WORKSPACE)

        assert sorted(reaped) == sorted(
            f"{STAGING_DIR_NAME}/{name}" for name in ("cafef00d" * 4, "deadbeef" * 4)
        )
        assert _listing(_staging_root(anchor)) == []
        assert anchor.calls == ["reap"]

    def test_the_reaped_paths_are_workspace_relative_like_every_other_path_here(
        self, provider: DockerProvider, anchor: HostBackedAnchor
    ) -> None:
        """The container's absolute layout is not a promise this seam makes.

        ``read`` and ``write`` both speak workspace-relative paths, and a
        reconciler rendering ``/workspace/.headspace-staging/…`` into a report
        would be publishing an engine-side detail as though it were part of the
        contract.
        """
        self._plant(anchor, "0" * 32)
        (reaped,) = provider.reap_staging(WORKSPACE)
        assert not reaped.startswith("/")
        assert not reaped.startswith(WORKSPACE_MOUNT_PATH)
        assert reaped == f"{STAGING_DIR_NAME}/{'0' * 32}"

    def test_nothing_to_reap_is_an_empty_result_and_never_a_failure(
        self, provider: DockerProvider, anchor: HostBackedAnchor
    ) -> None:
        """Two flavours of "nothing", and neither is an error.

        A workspace that has never been written to has no prefix at all; one
        that has, and settled cleanly, has an empty prefix. Reconciliation runs
        on every workspace it walks, so the ordinary case has to be the quiet
        one — a reaper that raised on a clean workspace would make the honest
        answer indistinguishable from a broken backend.
        """
        assert provider.reap_staging(WORKSPACE) == ()

        write(provider, "out.bin")
        assert _staging_root(anchor).is_dir()
        assert provider.reap_staging(WORKSPACE) == ()
        assert (anchor.root / "out.bin").read_bytes() == PAYLOAD

    def test_a_reap_settles_the_residue_a_killed_copy_in_would_have_left(
        self, provider: DockerProvider, anchor: HostBackedAnchor
    ) -> None:
        """End to end on the real failure: the transfer lands, the process dies.

        ``after_put`` raises where a ``SIGKILL`` would land — after the archive
        is in the staging directory and before the finalize exec — and
        ``skip_discard`` withholds the cleanup exec the way a killed process
        withholds everything. The staged bytes are therefore genuinely orphaned
        rather than planted to look orphaned, and the reaper is the only thing
        left that can clear them.
        """

        def killed_after_the_transfer(root: Path) -> None:
            del root
            raise KeyboardInterrupt("the copy-in process was killed")

        anchor.after_put = killed_after_the_transfer
        anchor.skip_discard = True
        with pytest.raises(KeyboardInterrupt):
            write(provider, "out.bin")

        anchor.after_put = None
        anchor.skip_discard = False
        orphaned = _listing(_staging_root(anchor))
        assert any(entry.endswith(STAGED_FILE_NAME) for entry in orphaned), orphaned

        reaped = provider.reap_staging(WORKSPACE)

        assert len(reaped) == 1
        assert _listing(_staging_root(anchor)) == []
        assert not (anchor.root / "out.bin").exists()

    def test_only_the_reserved_prefix_is_touched(
        self, provider: DockerProvider, anchor: HostBackedAnchor
    ) -> None:
        """A reaper with a wider reach than its knowledge would eat a job's work.

        The prefix is the whole of this verb's authority: everything inside it
        is headspace's own scratch space and nothing outside it is, so a job's
        artifacts — including one that has not been exported yet — must survive
        a reconciliation pass untouched.
        """
        self._plant(anchor, "f" * 32)
        (anchor.root / "results").mkdir()
        (anchor.root / "results/final.csv").write_bytes(b"a job produced this\n")
        (anchor.root / "top.bin").write_bytes(PAYLOAD)

        provider.reap_staging(WORKSPACE)

        assert (anchor.root / "results/final.csv").read_bytes() == b"a job produced this\n"
        assert (anchor.root / "top.bin").read_bytes() == PAYLOAD

    def test_a_stopped_anchor_refuses_on_the_same_terms_write_does(
        self, provider: DockerProvider, anchor: HostBackedAnchor
    ) -> None:
        """Residue left in place is the honest report; a silent success is not.

        The reap runs as an exec — there is no way to delete a path inside a
        volume through the archive endpoint — so a stopped anchor genuinely
        cannot be cleaned. Returning ``()`` here would tell a reconciler the
        workspace was clean when its residue is still sitting there.
        """
        self._plant(anchor, "e" * 32)
        anchor.attrs["State"] = {"Status": "exited", "ExitCode": 0}

        with pytest.raises(ProviderError) as caught:
            provider.reap_staging(WORKSPACE)

        assert caught.value.code == EXIT_INFRASTRUCTURE_FAILURE
        assert "workspace container" in caught.value.message
        assert _listing(_staging_root(anchor)) != []

    def test_a_planted_staging_root_is_refused_rather_than_reaped_through(
        self, provider: DockerProvider, anchor: HostBackedAnchor, outside: Path
    ) -> None:
        """``rm -rf`` through a link out of the volume is the worst bug in this file.

        The reaper deletes recursively, so the boundary check it shares with the
        prepare step is doing more work here than anywhere else: a
        ``.headspace-staging -> /etc`` that the reaper trusted would clear the
        container's ``/etc`` rather than headspace's scratch space.
        """
        (outside / "keep.txt").write_bytes(b"not headspace's to delete\n")
        (anchor.root / STAGING_DIR_NAME).symlink_to(outside)

        with pytest.raises(CliError) as caught:
            provider.reap_staging(WORKSPACE)

        assert caught.value.code == EXIT_USER_ERROR
        assert (outside / "keep.txt").read_bytes() == b"not headspace's to delete\n"

    def test_an_unknown_workspace_is_the_callers_error(self, provider: DockerProvider) -> None:
        with pytest.raises(CliError) as caught:
            provider.reap_staging("no-such-ws")
        assert caught.value.code == EXIT_USER_ERROR

    def test_reaping_creates_no_engine_object_of_its_own(
        self, provider: DockerProvider, engine: StubEngine, anchor: HostBackedAnchor
    ) -> None:
        before = engine.objects()
        self._plant(anchor, "a" * 32)
        provider.reap_staging(WORKSPACE)
        assert engine.objects() == before
        assert engine.created == []

    def test_the_reaper_asks_for_less_of_the_image_than_a_write_does(self) -> None:
        """Its tool list is a strict subset, and that is deliberate.

        An image that has lost the ability to *write* — no ``sha256sum``, say —
        is exactly the image most likely to be holding residue from a copy-in
        that failed. Requiring the wider set would refuse to clean up precisely
        where cleaning up matters most.
        """
        assert set(REQUIRED_REAP_TOOLS) < set(REQUIRED_WRITE_TOOLS)
        for tool in REQUIRED_REAP_TOOLS:
            assert f"{tool} " in REAP_STAGING_SCRIPT
