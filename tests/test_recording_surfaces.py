"""The dedicated four-surface no-value-recording evidence (issues #13/#14, task t11).

Two issues were filed by a real consumer of this product: there was no way to
pass a *secret* into a workspace (#13) and no way to get a *file* in (#14),
which together forced callers to smuggle payloads through argv — and argv is
recorded verbatim in four places: ``outcome_summary``, ``provenance.inputs``,
``journal.jsonl`` and ``state.json``. Two of those outlive the job, and the
journal is by design a durable crash trace. ``run --env``, ``run --env-file``,
``run --input`` and ``headspace put`` are the channels that replace argv for
this purpose, and the guarantee they carry is: **names, paths and digests are
recorded; values and contents never are.**

The filer of #13 asked for this file specifically:

    "a documented statement that the value is excluded from outcome_summary,
    provenance.inputs, journal.jsonl and state.json, and a test that plants a
    marker and greps all four — that assertion is the part that makes the
    feature trustworthy, more than the flag itself."

This module is that test. It is deliberately self-contained: every marker is
planted here, every one of the four named surfaces is read back here, and both
the negative half (the marker is absent) and the positive half (the path, the
digest, or the name really is present) are asserted — a suite that only proved
absence would pass against a feature that recorded nothing at all, which is not
the guarantee anyone asked for.

Two sibling files already exercise large parts of this same ground as part of
proving their own tasks' acceptance criteria, and this module does not repeat
their coverage:

* :mod:`tests.test_workspace_put` —
  ``test_no_recording_surface_carries_a_byte_of_the_files_contents`` already
  plants a marker in a copied-in file's bytes and greps
  ``outcome_summary``, ``key_findings``, ``provenance.inputs``, ``journal.jsonl``
  and ``state.json`` (as raw on-disk text) for it, and for the path and digest
  that should replace it. It also covers the full recovery story this module does
  not touch at all: a copy-in killed mid-flight, staging residue, reconciliation.
* :mod:`tests.test_run_inputs_and_env` — ``test_env_values_reach_no_surface_headspace_composes``,
  ``test_env_file_path_is_recorded_and_its_values_are_not`` and
  ``test_a_job_that_echoes_its_env_leaks_only_into_captured_output`` already
  plant an env-variable marker and prove the same four-surface absence, the
  name's presence, and the surface-class split for a job that prints its own
  environment. It also covers flag parsing, refusals and the "one copy-in
  implementation site" proof, none of which this module repeats.

What this module adds instead:

1. **The literal four named surfaces, satisfied to the letter, in one place.**
   ``run --input``'s ``outcome_summary`` deliberately summarises a copy-in by
   count and total bytes rather than by per-file path and digest (see
   :func:`headspace.core.workspace._run_summary` — a multi-file copy-in
   listing every digest in one sentence would be the transcript the whole
   result package exists not to be), so it is not the composed field that
   proves ``outcome_summary`` carries a digest. ``headspace put``'s
   single-file ``outcome_summary`` is (see
   :func:`headspace.core.workspace._copy_in_summary`), so
   :func:`test_put_records_the_files_path_and_digest_never_its_bytes` uses
   ``put`` for that half of the claim, and states the ``run --input`` design
   fact explicitly rather than silently switching verbs.
2. **The CLI's own flags, end to end.** Every sibling test above drives the
   :class:`~headspace.core.workspace.Orchestrator` directly. This module adds
   one test that instead calls ``headspace run --input ... --env ... --env-file
   ...`` and ``headspace put`` through :func:`headspace.cli.main`, so the
   guarantee is proved for the actual channels a caller types, not only for the
   orchestrator method underneath them.
3. **A single dedicated location an auditor can point at.** The filer asked for
   *a* test, not an assertion spread across two other tasks' suites — so this
   module restates the whole four-surface claim, for both halves of the feature,
   with its own fresh markers, fixtures and helpers, and does not lean on the
   sibling modules at import time or at runtime.

The markers are unmistakable
-----------------------------
Every planted value is a fixed, high-entropy nonce that cannot occur
incidentally in any surface headspace renders on its own (a workspace id, a
digest, a path, a status word). A guarantee about recording is worth exactly as
much as the search that fails to find the thing it excludes, so the search has
to be for something nothing else in the system would ever produce by accident.

The surface-class split
------------------------
The no-value-recording guarantee covers what headspace itself *composes*. It
does not, and cannot, cover a job that prints its own environment: that value
lands in the job's *captured output*, which is kept — in the result package's
``evidence`` and in the job's own row of ``state.json``. Asserting a blanket
absence across the whole of ``state.json`` would therefore be untestable
against exactly the case that matters (an echoed value legitimately appearing
under ``jobs[-1]["output"]``), so
:func:`test_a_job_that_prints_its_own_env_leaks_only_into_captured_output`
reads ``state.json`` back as parsed JSON and checks the ``output`` field
separately from the rest of the record — never as one whole-file grep.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from headspace.cli import main
from headspace.cli._commands import create as create_cmd
from headspace.cli._commands.run import parse_environment
from headspace.cli._errors import EXIT_SUCCESS
from headspace.core.store import HOME_ENV_VAR, Store
from headspace.core.workspace import InputRequest, Orchestrator
from headspace.providers.fake import FakeProvider, JobPlan

WS = "ws-recording-surfaces"
ECHO = ("echo", "hello")

#: The copied-in file's *contents* — issue #14's half of the guarantee. Nothing
#: headspace composes may repeat this string; only a job that actually read and
#: printed the file's bytes could, and no job in this module does that.
FILE_MARKER = "MARKER-7f3a9c2e-DO-NOT-RECORD-file-contents"
FILE_PAYLOAD = f"api_key = {FILE_MARKER}\n".encode()
FILE_DIGEST = hashlib.sha256(FILE_PAYLOAD).hexdigest()
FILE_DESTINATION = "secrets/config.env"

#: A second, smaller payload for the CLI end-to-end test, so its digest and
#: this module's other digests can never be mistaken for one another in a
#: failing assertion's diff.
CLI_FILE_MARKER = "MARKER-1e4a6f08-DO-NOT-RECORD-cli-put"
CLI_FILE_PAYLOAD = f"cli-input = {CLI_FILE_MARKER}\n".encode()
CLI_FILE_DIGEST = hashlib.sha256(CLI_FILE_PAYLOAD).hexdigest()

#: A forwarded variable's *value* — issue #13's half. Its *name* is exactly what
#: every recording surface is supposed to carry instead.
ENV_NAME = "RECORDING_SURFACES_SECRET"
ENV_VALUE = "MARKER-3c9e51ab-DO-NOT-RECORD-env-value"

#: A second env marker for the CLI end-to-end test and the ``--env-file`` path,
#: kept distinct so a leak from one channel cannot hide behind an assertion
#: written for the other.
ENV_FILE_NAME = "RECORDING_SURFACES_FROM_FILE"
ENV_FILE_VALUE = "MARKER-9b2d7e14-DO-NOT-RECORD-env-file-value"

#: A variable the caller's shell carries and never names on any flag. Issue
#: #13's no-flag probe: with nothing asked for, a job must see nothing of it.
AMBIENT_NAME = "RECORDING_SURFACES_AMBIENT"
AMBIENT_VALUE = "MARKER-c001d00d-DO-NOT-RECORD-ambient-value"


# --- fixtures and small helpers ---------------------------------------------


@pytest.fixture(autouse=True)
def isolated_store_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point every test at a throwaway store root, never the real ``~/.headspace``."""
    root = tmp_path / "headspace-home"
    monkeypatch.setenv(HOME_ENV_VAR, str(root))
    create_cmd.reset_providers()
    yield root
    create_cmd.reset_providers()


@pytest.fixture
def store(isolated_store_home: Path) -> Store:
    return Store()


@pytest.fixture
def fake() -> FakeProvider:
    """The one fake instance every ``--provider fake`` CLI invocation will use."""
    provider = create_cmd.provider_for("fake")
    assert isinstance(provider, FakeProvider)
    return provider


@pytest.fixture
def payload(tmp_path: Path) -> Path:
    """One host file whose *contents* carry a marker no surface may repeat."""
    path = tmp_path / "inputs" / "config.env"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(FILE_PAYLOAD)
    return path


def ready(provider: FakeProvider, store: Store, workspace_id: str = WS) -> Orchestrator:
    """An orchestrator over ``provider`` with ``workspace_id`` created and ready."""
    orchestrator = Orchestrator(provider, store)
    orchestrator.create(workspace_id=workspace_id)
    return orchestrator


def state_record(store: Store, workspace_id: str = WS) -> dict[str, Any]:
    return dict(store.read_state(workspace_id).state)


def journal_entries(store: Store, workspace_id: str = WS) -> list[dict[str, Any]]:
    return [entry.entry for entry in store.read_journal(workspace_id)]


def raw_text(root: Path, filename: str, workspace_id: str = WS) -> str:
    """One store file as bytes-on-disk text — the literal surface a crash leaves behind."""
    path = root / "workspaces" / workspace_id / filename
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def composed_surfaces(package: Any, root: Path, workspace_id: str = WS) -> dict[str, str]:
    """Every field headspace itself composes, as searchable text.

    ``state.json`` is included here as raw, whole-file text. That is safe for
    every test below *except* the surface-class-split one, because none of the
    other scenarios in this module run a job whose captured output could
    legitimately contain a planted marker — the split test builds its own,
    field-wise view of ``state.json`` instead of using this helper for it. See
    the module docstring's "surface-class split" section.
    """
    return {
        "outcome_summary": package.outcome_summary,
        "key_findings": "\n".join(package.key_findings),
        "warnings": "\n".join(package.warnings),
        "attention": "\n".join(package.attention),
        "provenance.inputs": "\n".join(package.provenance.inputs),
        "journal.jsonl": raw_text(root, "journal.jsonl", workspace_id),
        "state.json": raw_text(root, "state.json", workspace_id),
    }


def assert_absent(marker: str, surfaces: Mapping[str, str]) -> None:
    """The negative half: the marker is grepped, not reasoned about."""
    for label, text in surfaces.items():
        assert marker not in text, f"{marker!r} leaked into {label}: {text!r}"


def assert_present(needle: str, surfaces: Mapping[str, str], labels: Iterator[str]) -> None:
    """The positive half: a clean grep is not, by itself, evidence of anything.

    A suite that only proved absence would pass against a feature that recorded
    nothing at all — so every "absent" assertion in this module is paired with
    a "present" one naming exactly what should have replaced the marker.
    """
    for label in labels:
        assert needle in surfaces[label], f"{needle!r} missing from {label}: {surfaces[label]!r}"


def job_output_removed(record: Mapping[str, Any]) -> dict[str, Any]:
    """``record`` with the last job's captured output field stripped — field-wise.

    Used only by the surface-class-split test, where the marker is expected to
    appear *legitimately* under ``jobs[-1]["output"]``. Stripping that one field
    and then grepping what remains is what makes "the rest of state.json is
    clean" a testable claim instead of one a whole-file grep would falsify by
    construction.
    """
    jobs = [dict(job) for job in record.get("jobs", [])]
    if jobs:
        jobs[-1] = {key: value for key, value in jobs[-1].items() if key != "output"}
    return {**record, "jobs": jobs}


# --- criterion 1: a copied-in file — path and sha256 in, contents never -----


def test_put_records_the_files_path_and_digest_never_its_bytes(
    store: Store, isolated_store_home: Path, payload: Path
) -> None:
    """Issue #14's evidence, to the letter: the four named surfaces, one file.

    ``headspace put``'s outcome_summary for a single-file copy-in states the
    digest directly (see ``workspace._copy_in_summary``), which is what makes
    ``put`` — rather than ``run --input`` — the verb that proves
    "outcome_summary ... contains the path and sha256" literally rather than by
    a looser reading of the claim.
    """
    orchestrator = ready(FakeProvider(), store)

    package = orchestrator.put(WS, payload, FILE_DESTINATION)

    surfaces = composed_surfaces(package, isolated_store_home)

    # Negative: the file's contents are nowhere headspace composed.
    assert_absent(FILE_MARKER, surfaces)

    # Positive: the path and the digest really are present, in every one of
    # the four surfaces the filer named.
    for surface in ("outcome_summary", "provenance.inputs", "journal.jsonl", "state.json"):
        assert FILE_DESTINATION in surfaces[surface], f"{surface} lost the path"
        assert FILE_DIGEST in surfaces[surface], f"{surface} lost the digest"

    # And the ledger row itself — the structural reason none of the above can
    # drift: an InputEntry has no content field to leak in the first place.
    (row,) = state_record(store)["inputs"]
    assert row["destination"] == FILE_DESTINATION
    assert row["sha256"] == FILE_DIGEST
    assert row["size_bytes"] == len(FILE_PAYLOAD)


def test_run_input_records_the_files_path_and_digest_never_its_bytes(
    store: Store, isolated_store_home: Path, payload: Path
) -> None:
    """The same claim through ``run --input``, with its one documented difference.

    ``run``'s outcome_summary deliberately names a copy-in by count and total
    bytes, not by per-file path and digest (a multi-file copy-in listing every
    digest in one sentence would be the transcript the result package exists
    not to be — see ``workspace._run_summary``). So this test asserts the path
    and digest land in ``provenance.inputs``, ``journal.jsonl`` and
    ``state.json`` — and asserts, rather than silently ignores, that
    ``outcome_summary`` for this verb carries neither the path nor the marker.
    """
    orchestrator = ready(FakeProvider(), store)

    package = orchestrator.run(
        WS, ECHO, inputs=[InputRequest(host_path=str(payload), destination=FILE_DESTINATION)]
    )

    surfaces = composed_surfaces(package, isolated_store_home)
    assert_absent(FILE_MARKER, surfaces)

    for surface in ("provenance.inputs", "journal.jsonl", "state.json"):
        assert FILE_DESTINATION in surfaces[surface], f"{surface} lost the path"
        assert FILE_DIGEST in surfaces[surface], f"{surface} lost the digest"

    # Design fact, pinned rather than assumed: run's outcome_summary summarises
    # a copy-in by count and volume, not by naming this file or its digest.
    assert FILE_DESTINATION not in surfaces["outcome_summary"]
    assert FILE_DIGEST not in surfaces["outcome_summary"]
    assert "1 file(s) totalling" in surfaces["outcome_summary"]

    (row,) = state_record(store)["inputs"]
    assert row["sha256"] == FILE_DIGEST


# --- criterion 2: a marker-valued env variable — name in, value never ------


def test_env_forwards_the_value_and_records_only_the_name(
    store: Store, isolated_store_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #13's evidence, to the letter: the four named surfaces, one secret.

    The value really is forwarded — checked first, so a later "the value is
    absent from every recorded surface" cannot be trivially true because the
    value was never handed to the job in the first place.
    """
    monkeypatch.setenv(ENV_NAME, ENV_VALUE)
    fake_provider = FakeProvider()
    orchestrator = ready(fake_provider, store)

    package = orchestrator.run(
        WS, ECHO, environment=parse_environment([ENV_NAME], None), job_id="job-env-marker"
    )

    # The job really did receive the value — the channel works.
    assert dict(fake_provider._workspaces[WS].last_job_env) == {ENV_NAME: ENV_VALUE}

    surfaces = composed_surfaces(package, isolated_store_home)

    # Negative: the value is nowhere headspace composed.
    assert_absent(ENV_VALUE, surfaces)

    # Positive: the name is present in every one of the four named surfaces.
    for surface in ("outcome_summary", "provenance.inputs", "journal.jsonl", "state.json"):
        assert ENV_NAME in surfaces[surface], f"{surface} lost the name"


def test_no_flag_run_forwards_nothing_of_the_callers_environment(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #13's no-flag probe: with nothing asked for, a job sees nothing.

    The caller's shell carries ``RECORDING_SURFACES_AMBIENT`` for this test.
    With no ``--env`` naming it, the job must not see it, and the state record
    must not mention it either — the closed default is the product's whole
    posture, and a regression here would otherwise be silent.
    """
    monkeypatch.setenv(AMBIENT_NAME, AMBIENT_VALUE)
    fake_provider = FakeProvider()
    orchestrator = ready(fake_provider, store)

    orchestrator.run(WS, ECHO)

    assert dict(fake_provider._workspaces[WS].last_job_env) == {}
    assert AMBIENT_VALUE not in json.dumps(state_record(store))


def test_a_job_that_prints_its_own_env_leaks_only_into_captured_output(
    store: Store, isolated_store_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The surface-class split, asserted field-wise rather than by whole-file grep.

    A job that prints its own environment writes the value into its captured
    output, and captured output legitimately persists — in the result
    package's ``evidence`` and in the job's own row of ``state.json``. That is
    the job's doing, not headspace's, and ``run --help`` says so. Every field
    headspace itself composes must still be clean, which is the split this test
    exists to prove rather than assume.
    """
    monkeypatch.setenv(ENV_NAME, ENV_VALUE)
    printenv = ("printenv", ENV_NAME)
    provider = FakeProvider(script={printenv: JobPlan.succeeding(output=ENV_VALUE)})
    orchestrator = ready(provider, store)

    package = orchestrator.run(
        WS, printenv, environment=parse_environment([ENV_NAME], None), job_id="job-echo-marker"
    )

    # Captured-output-derived fields: allowed to carry it, and do.
    assert package.evidence[0].excerpt == ENV_VALUE
    record = state_record(store)
    assert record["jobs"][-1]["output"] == ENV_VALUE

    # Everything headspace composed itself: clean. outcome_summary,
    # key_findings, warnings, attention, provenance.inputs and journal.jsonl
    # never carry captured output at all, so the ordinary whole-text helper is
    # safe for them.
    text_surfaces = composed_surfaces(package, isolated_store_home)
    del text_surfaces["state.json"]  # checked field-wise below instead
    assert_absent(ENV_VALUE, text_surfaces)

    # state.json specifically: read as parsed JSON, the captured-output field
    # stripped out field-wise, and only then grepped. A whole-file grep here
    # would fail against a passing implementation, because the value
    # legitimately appears under jobs[-1]["output"] — that is the point this
    # test exists to make testable.
    assert ENV_VALUE not in json.dumps(job_output_removed(record))


# --- both channels, through the CLI's actual flags --------------------------


def composed_surfaces_from_json(
    data: Mapping[str, Any], root: Path, workspace_id: str = WS
) -> dict[str, str]:
    """The same composed-surface view as :func:`composed_surfaces`, built from a
    ``headspace --json`` response instead of a
    :class:`~headspace.core.result.ResultPackage` object.

    Used only by the CLI end-to-end test below, which drives
    :func:`headspace.cli.main` and gets back a process exit code, not a package
    — ``outcome_summary``, ``key_findings``, ``warnings``, ``attention`` and
    ``provenance.inputs`` are read from the same JSON document a real caller
    would parse, which is what makes this test prove the CLI's actual output
    rather than an internal object a caller never sees.
    """
    provenance = data.get("provenance", {})
    return {
        "outcome_summary": str(data.get("outcome_summary", "")),
        "key_findings": "\n".join(data.get("key_findings", [])),
        "warnings": "\n".join(data.get("warnings", [])),
        "attention": "\n".join(data.get("attention", [])),
        "provenance.inputs": "\n".join(provenance.get("inputs", [])),
        "journal.jsonl": raw_text(root, "journal.jsonl", workspace_id),
        "state.json": raw_text(root, "state.json", workspace_id),
    }


def test_cli_flags_record_names_and_digests_and_never_the_values_they_stand_for(
    isolated_store_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """The same guarantee, proved through ``headspace run`` and ``headspace put``
    as a caller actually types them — ``--input``, ``--env``, ``--env-file`` and
    the ``put`` verb — rather than through the orchestrator methods underneath.

    Every sibling test in this area drives :class:`~headspace.core.workspace.Orchestrator`
    directly. Nothing else in the suite closes the loop from the CLI's own flag
    names, through :func:`headspace.cli.main`, back to the four recording
    surfaces for both channels at once — which is the gap this test fills. The
    JSON responses are read with ``capsys``, exactly as an agent piping
    ``headspace ... --json`` into a parser would see them.
    """
    monkeypatch.setenv(ENV_NAME, ENV_VALUE)
    env_file_path = tmp_path / "secrets.env"
    env_file_path.write_text(f"{ENV_FILE_NAME}={ENV_FILE_VALUE}\n", encoding="utf-8")
    cli_input_path = tmp_path / "cli-input.env"
    cli_input_path.write_bytes(CLI_FILE_PAYLOAD)

    assert main(["create", "--provider", "fake", "--workspace-id", WS, "--json"]) == EXIT_SUCCESS
    capsys.readouterr()  # discard create's own JSON; this test is about run and put

    code = main(
        [
            "run",
            "--provider",
            "fake",
            "--input",
            f"cli-config.env={cli_input_path}",
            "--env",
            ENV_NAME,
            "--env-file",
            str(env_file_path),
            "--json",
            WS,
            *ECHO,
        ]
    )
    assert code == EXIT_SUCCESS
    run_response = json.loads(capsys.readouterr().out.strip())

    surfaces = composed_surfaces_from_json(run_response, isolated_store_home)
    assert_absent(CLI_FILE_MARKER, surfaces)
    assert_absent(ENV_VALUE, surfaces)
    assert_absent(ENV_FILE_VALUE, surfaces)
    assert CLI_FILE_DIGEST in surfaces["provenance.inputs"]
    assert CLI_FILE_DIGEST in surfaces["journal.jsonl"]
    assert CLI_FILE_DIGEST in surfaces["state.json"]
    assert ENV_NAME in surfaces["outcome_summary"]
    assert ENV_NAME in surfaces["journal.jsonl"]
    assert ENV_NAME in surfaces["state.json"]
    assert str(env_file_path) in surfaces["provenance.inputs"]
    assert str(env_file_path) in surfaces["journal.jsonl"]

    put_code = main(
        [
            "put",
            "--provider",
            "fake",
            "--json",
            WS,
            str(cli_input_path),
            "cli-put-destination.env",
        ]
    )
    assert put_code == EXIT_SUCCESS
    put_response = json.loads(capsys.readouterr().out.strip())

    surfaces = composed_surfaces_from_json(put_response, isolated_store_home)
    assert_absent(CLI_FILE_MARKER, surfaces)
    assert "cli-put-destination.env" in surfaces["outcome_summary"]
    assert CLI_FILE_DIGEST in surfaces["outcome_summary"]
    assert CLI_FILE_DIGEST in surfaces["journal.jsonl"]
    assert CLI_FILE_DIGEST in surfaces["state.json"]
