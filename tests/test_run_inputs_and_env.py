"""Tests for ``run --input``, ``run --env`` and ``run --env-file``.

Written first, against the four acceptance criteria this task was given:

1. ``--env`` resolves from the caller's own environment *at parse time* and
   refuses an unset name; ``--env-file`` refuses a malformed line; an
   ``--input`` destination is put through
   :func:`~headspace.providers.base.require_workspace_path` before any engine
   is contacted;
2. what is *recorded* carries environment variable **names** (and the
   env-file's path) and never a value — and the closed default from issue #13,
   where a job sees nothing of the caller's environment unless a flag says so,
   is pinned by a regression test rather than assumed;
3. ``run --input`` drives the **same** orchestrator copy-in function ``put``
   does — one implementation site, proved three ways below;
4. ``run --help`` states the job-echo edge.

Three things about how these are proved:

**The markers are grepped, not reasoned about.** A forwarded variable's value
and a copied-in file's bytes both carry a distinctive string, and every surface
headspace composes — ``outcome_summary``, ``key_findings``, ``warnings``,
``provenance.inputs``, ``journal.jsonl`` and ``state.json``, the last two read
as raw text off disk — is searched for it. A guarantee about recording is worth
exactly as much as the search that fails to find the thing it excludes.

**The job-echo edge is asserted as a surface-class split, not waved at.** A job
that prints its own environment writes the value into its captured output, and
captured output is kept: :func:`test_a_job_that_echoes_its_env_leaks_only_into_captured_output`
asserts the value appears in the *captured-output-derived* fields and in
**none** of the fields headspace itself composes. Overstating the guarantee
would be worse than the leak.

**One implementation site is proved, not asserted.** Behaviourally, by spying on
:meth:`~headspace.core.workspace.Orchestrator._copy_in` and watching both verbs
arrive there; structurally, by reading the module source and finding exactly one
``self._provider.write(`` call in it; and by equality — the ledger a ``put``
writes and the ledger ``run --input`` writes are the same rows for the same
payload.

**No engine, ever.** Everything here drives the in-memory fake, which carries
the inbound ``write`` verb and records the env each job observed. Nothing in
this file needs a Docker daemon, and ``HEADSPACE_HOME`` is redirected at a
``tmp_path`` by an autouse fixture so no test can see a developer's real store.
"""

from __future__ import annotations

import hashlib
import inspect as _inspect
import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

import headspace.core.workspace as workspace_module
from headspace.cli import main
from headspace.cli._commands import create as create_cmd
from headspace.cli._commands.run import parse_environment, parse_inputs
from headspace.cli._errors import EXIT_POLICY_DENIED, EXIT_SUCCESS, EXIT_USER_ERROR, CliError
from headspace.core.policy import Policy, ResourceBudget
from headspace.core.store import HOME_ENV_VAR, Store
from headspace.core.workspace import (
    INTENT_PUT,
    INTENT_RUN,
    PHASE_INTENDED,
    PHASE_SETTLED,
    InputRequest,
    JobEnvironment,
    Orchestrator,
)
from headspace.providers.fake import FakeProvider, JobPlan

WS = "ws-run-inputs"
ECHO = ("echo", "hello")

#: The value of a forwarded variable. No surface headspace composes may repeat
#: it; only a job that printed it may put it in captured output.
SECRET = "ENV-VALUE-MARKER-4b21d9"
SECRET_NAME = "COLLEAGUE_API_KEY"

#: The *contents* of a copied-in file. Same rule.
PAYLOAD_MARKER = "FILE-BYTES-MARKER-77c0ea"
PAYLOAD = f"token = {PAYLOAD_MARKER}\n".encode()
PAYLOAD_DIGEST = hashlib.sha256(PAYLOAD).hexdigest()

#: A variable the caller's shell carries and never names on the command line.
#: Issue #13's probe: with no flag, a job must see nothing of it.
AMBIENT_NAME = "HEADSPACE_AMBIENT_SECRET"
AMBIENT = "AMBIENT-VALUE-MARKER-1c0de5"


# --- fixtures and helpers ---------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_store_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point every test at a throwaway store root, never the real ~/.headspace."""
    root = tmp_path / "headspace-home"
    monkeypatch.setenv(HOME_ENV_VAR, str(root))
    create_cmd.reset_providers()
    yield root
    create_cmd.reset_providers()


@pytest.fixture(autouse=True)
def ambient_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the caller's shell a secret it never names. Nothing may forward it."""
    monkeypatch.setenv(AMBIENT_NAME, AMBIENT)


@pytest.fixture
def store(isolated_store_home: Path) -> Store:
    return Store()


@pytest.fixture
def fake() -> FakeProvider:
    """The one fake instance every ``--provider fake`` invocation will use."""
    provider = create_cmd.provider_for("fake")
    assert isinstance(provider, FakeProvider)
    return provider


@pytest.fixture
def payload(tmp_path: Path) -> Path:
    """One host file whose *contents* carry a marker no surface may repeat."""
    path = tmp_path / "inputs" / "config.env"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(PAYLOAD)
    return path


def ready(provider: FakeProvider, store: Store, workspace_id: str = WS) -> Orchestrator:
    """An orchestrator over ``provider`` with ``workspace_id`` created and ready."""
    orchestrator = Orchestrator(provider, store)
    orchestrator.create(workspace_id=workspace_id)
    return orchestrator


def observed_env(provider: FakeProvider, workspace_id: str = WS) -> Mapping[str, str]:
    """The env the fake's last job actually ran with — the job's own view."""
    return dict(provider._workspaces[workspace_id].last_job_env)


def state_record(store: Store, workspace_id: str = WS) -> dict[str, Any]:
    return dict(store.read_state(workspace_id).state)


def ledger(store: Store, workspace_id: str = WS) -> list[dict[str, Any]]:
    return list(state_record(store, workspace_id).get("inputs", []))


def journal_entries(store: Store, workspace_id: str = WS) -> list[dict[str, Any]]:
    return [entry.entry for entry in store.read_journal(workspace_id)]


def raw_text(root: Path, filename: str, workspace_id: str = WS) -> str:
    """One store file as bytes-on-disk text, for grepping a recording surface."""
    path = root / "workspaces" / workspace_id / filename
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def composed_surfaces(package: Any, root: Path, workspace_id: str = WS) -> dict[str, str]:
    """Every surface headspace composes itself, as searchable text.

    Captured output is deliberately absent: it is the job's text, not
    headspace's, and the guarantee this suite states does not cover it. See
    :func:`test_a_job_that_echoes_its_env_leaks_only_into_captured_output`.
    """
    return {
        "outcome_summary": package.outcome_summary,
        "key_findings": "\n".join(package.key_findings),
        "warnings": "\n".join(package.warnings),
        "attention": "\n".join(package.attention),
        "provenance.inputs": "\n".join(package.provenance.inputs),
        "journal.jsonl": raw_text(root, "journal.jsonl", workspace_id),
    }


def assert_absent(marker: str, surfaces: Mapping[str, str]) -> None:
    for label, text in surfaces.items():
        assert marker not in text, f"{marker} leaked into {label}: {text}"


def env_file(tmp_path: Path, body: str, name: str = "secrets.env") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def run_cli(verb: str, *argv: str) -> int:
    """Invoke the CLI the way a caller does, always against the fake."""
    return main([verb, "--provider", "fake", *argv])


# --- criterion 1: parse-time resolution, and refusals before the engine ------


def test_env_forwards_the_named_variable_by_name(
    fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--env NAME`` reads the value here and hands it to the job only."""
    monkeypatch.setenv(SECRET_NAME, SECRET)
    assert run_cli("create", "--workspace-id", WS, "--json") == EXIT_SUCCESS
    assert run_cli("run", "--env", SECRET_NAME, WS, *ECHO) == EXIT_SUCCESS

    assert observed_env(fake) == {SECRET_NAME: SECRET}


def test_env_refuses_a_name_that_is_not_set(
    fake: FakeProvider, store: Store, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """An unset name is refused loudly — never forwarded as an empty string."""
    monkeypatch.delenv(SECRET_NAME, raising=False)
    assert run_cli("create", "--workspace-id", WS, "--json") == EXIT_SUCCESS
    capsys.readouterr()

    assert run_cli("run", "--env", SECRET_NAME, WS, *ECHO) == EXIT_USER_ERROR
    stderr = capsys.readouterr().err
    assert SECRET_NAME in stderr

    # And the refusal happened before anything was journalled or run.
    assert [entry for entry in journal_entries(store) if entry["intent"] == INTENT_RUN] == []
    assert state_record(store)["jobs"] == []


def test_env_refuses_a_value_typed_on_the_command_line(fake: FakeProvider, capsys: Any) -> None:
    """``--env NAME=VALUE`` is the leak this flag exists to close, so it is refused.

    And the refusal does not repeat the value: an error message is a surface
    like any other.
    """
    assert run_cli("create", "--workspace-id", WS, "--json") == EXIT_SUCCESS
    capsys.readouterr()

    assert run_cli("run", "--env", f"{SECRET_NAME}={SECRET}", WS, *ECHO) == EXIT_USER_ERROR
    captured = capsys.readouterr()
    assert SECRET_NAME in captured.err
    assert SECRET not in captured.err
    assert SECRET not in captured.out


def test_env_refuses_a_name_that_is_not_a_name() -> None:
    with pytest.raises(CliError) as refused:
        parse_environment(["9lives"], None)
    assert refused.value.code == EXIT_USER_ERROR
    assert "9lives" in refused.value.message


def test_env_file_forwards_every_assignment_it_holds(fake: FakeProvider, tmp_path: Path) -> None:
    """Blank lines and comments are skipped; a value keeps its spaces verbatim."""
    path = env_file(
        tmp_path,
        f"# a comment\n\n{SECRET_NAME}={SECRET}\nSPACED=two words \n",
    )
    assert run_cli("create", "--workspace-id", WS, "--json") == EXIT_SUCCESS
    assert run_cli("run", "--env-file", str(path), WS, *ECHO) == EXIT_SUCCESS

    assert observed_env(fake) == {SECRET_NAME: SECRET, "SPACED": "two words "}


def test_env_file_refuses_a_malformed_line_without_quoting_it(tmp_path: Path) -> None:
    """A line with no ``=`` is refused by path and line number, never by content."""
    path = env_file(tmp_path, f"GOOD=fine\nthis-line-holds-{SECRET}\n")
    path_str = str(path)

    with pytest.raises(CliError) as refused:
        parse_environment(None, [path_str])
    assert refused.value.code == EXIT_USER_ERROR
    assert path_str in refused.value.message
    assert "line 2" in refused.value.message
    assert SECRET not in refused.value.message
    assert SECRET not in refused.value.remediation


@pytest.mark.parametrize("body", ["9LIVES=x\n", " SPACED =x\n", "=x\n", "export A=x\n"])
def test_env_file_refuses_a_line_whose_name_is_not_a_name(tmp_path: Path, body: str) -> None:
    """No shell grammar is guessed: no quote stripping, no ``export`` prefix."""
    path_str = str(env_file(tmp_path, body))
    with pytest.raises(CliError) as refused:
        parse_environment(None, [path_str])
    assert refused.value.code == EXIT_USER_ERROR


def test_env_file_refuses_a_path_that_does_not_exist(tmp_path: Path) -> None:
    missing = tmp_path / "nope.env"
    missing_str = str(missing)
    with pytest.raises(CliError) as refused:
        parse_environment(None, [missing_str])
    assert refused.value.code == EXIT_USER_ERROR
    assert missing_str in refused.value.message


def test_a_name_given_twice_is_refused_rather_than_resolved(tmp_path: Path) -> None:
    """Two definitions may hold different secrets; headspace picks neither."""
    path = env_file(tmp_path, f"{SECRET_NAME}=from-the-file\n")
    path_str = str(path)
    with pytest.raises(CliError) as refused:
        parse_environment([SECRET_NAME], [path_str])
    assert refused.value.code == EXIT_USER_ERROR
    assert SECRET_NAME in refused.value.message
    assert "from-the-file" not in refused.value.message


def test_input_destination_is_bounded_at_construction(payload: Path) -> None:
    """``require_workspace_path`` runs when the request is built — before any engine.

    :class:`~headspace.core.workspace.InputRequest` normalises its destination in
    ``__post_init__``, so a CLI that parses flags before it constructs a provider
    has satisfied "validated before any engine contact" structurally.
    """
    host_path = str(payload)
    with pytest.raises(CliError) as refused:
        InputRequest(host_path=host_path, destination="../escape.env")
    assert refused.value.code == EXIT_USER_ERROR
    assert "leave the workspace" in refused.value.message

    normalised = InputRequest(host_path=host_path, destination="./data//config.env")
    assert normalised.destination == "data/config.env"


def test_parse_inputs_refuses_a_spec_with_no_host_path() -> None:
    with pytest.raises(CliError) as refused:
        parse_inputs(["config.env"])
    assert refused.value.code == EXIT_USER_ERROR


def test_run_refuses_an_escaping_input_before_it_journals_or_runs(
    fake: FakeProvider, store: Store, payload: Path, capsys: Any
) -> None:
    assert run_cli("create", "--workspace-id", WS, "--json") == EXIT_SUCCESS
    capsys.readouterr()

    code = run_cli("run", "--input", f"../escape.env={payload}", WS, *ECHO)
    assert code == EXIT_USER_ERROR

    record = state_record(store)
    assert record["jobs"] == []
    assert record["inputs"] == []
    assert [entry for entry in journal_entries(store) if entry["intent"] != "create"] == []


def test_run_refuses_an_over_budget_input_before_the_job_runs(
    fake: FakeProvider, store: Store, payload: Path
) -> None:
    orchestrator = Orchestrator(fake, store)
    orchestrator.create(
        workspace_id=WS, policy=Policy(budget=ResourceBudget(storage_bytes=len(PAYLOAD) - 1))
    )

    input_request = InputRequest(host_path=str(payload), destination="config.env")
    with pytest.raises(CliError) as refused:
        orchestrator.run(WS, ECHO, inputs=[input_request])
    assert refused.value.code == EXIT_POLICY_DENIED
    assert state_record(store)["jobs"] == []
    assert ledger(store) == []


# --- criterion 2: name-only recording, and the closed default ---------------


def test_no_flag_run_forwards_nothing_of_the_callers_environment(
    fake: FakeProvider, store: Store
) -> None:
    """Issue #13's probe, pinned: with no flag the job's env is empty.

    The caller's shell carries ``HEADSPACE_AMBIENT_SECRET`` for the whole of
    this module (see the autouse fixture). A job must not see it, and must not
    see anything else of the caller's either — the closed default is the
    product's whole posture and a regression here would be silent.
    """
    orchestrator = ready(fake, store)
    orchestrator.run(WS, ECHO)

    assert observed_env(fake) == {}
    assert AMBIENT not in json.dumps(state_record(store))


def test_env_values_reach_no_surface_headspace_composes(
    fake: FakeProvider, store: Store, isolated_store_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SECRET_NAME, SECRET)
    orchestrator = ready(fake, store)
    package = orchestrator.run(WS, ECHO, environment=parse_environment([SECRET_NAME], None))

    surfaces = composed_surfaces(package, isolated_store_home)
    surfaces["state.json"] = raw_text(isolated_store_home, "state.json")
    assert_absent(SECRET, surfaces)

    # The name is present on every one of them — that is the whole record.
    assert f"env: [{SECRET_NAME}]" in package.outcome_summary
    assert any(SECRET_NAME in line for line in package.provenance.inputs)
    assert SECRET_NAME in surfaces["journal.jsonl"]
    assert SECRET_NAME in surfaces["state.json"]
    assert SECRET_NAME in surfaces["key_findings"]


def test_the_run_intent_and_the_job_record_carry_names_only(
    fake: FakeProvider, store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SECRET_NAME, SECRET)
    path = env_file(tmp_path, "FROM_FILE=file-only-value\n")
    orchestrator = ready(fake, store)
    orchestrator.run(
        WS, ECHO, environment=parse_environment([SECRET_NAME], [str(path)]), job_id="job-env"
    )

    intents = [entry for entry in journal_entries(store) if entry["intent"] == INTENT_RUN]
    detail = intents[0]["detail"]
    assert detail["env"] == [SECRET_NAME, "FROM_FILE"]
    assert detail["env_files"] == [str(path)]

    job = state_record(store)["jobs"][-1]
    assert job["env"] == [SECRET_NAME, "FROM_FILE"]
    assert job["env_files"] == [str(path)]
    assert "file-only-value" not in json.dumps(state_record(store))


def test_env_file_path_is_recorded_and_its_values_are_not(
    fake: FakeProvider, store: Store, isolated_store_home: Path, tmp_path: Path
) -> None:
    path = env_file(tmp_path, f"{SECRET_NAME}={SECRET}\n")
    orchestrator = ready(fake, store)
    package = orchestrator.run(WS, ECHO, environment=parse_environment(None, [str(path)]))

    surfaces = composed_surfaces(package, isolated_store_home)
    surfaces["state.json"] = raw_text(isolated_store_home, "state.json")
    assert_absent(SECRET, surfaces)
    assert str(path) in surfaces["provenance.inputs"]
    assert str(path) in surfaces["journal.jsonl"]


def test_copied_in_file_contents_reach_no_surface(
    fake: FakeProvider, store: Store, isolated_store_home: Path, payload: Path
) -> None:
    """Issue #14's half: the file's bytes are recorded as a digest, never as text."""
    orchestrator = ready(fake, store)
    package = orchestrator.run(
        WS, ECHO, inputs=[InputRequest(host_path=str(payload), destination="config.env")]
    )

    surfaces = composed_surfaces(package, isolated_store_home)
    surfaces["state.json"] = raw_text(isolated_store_home, "state.json")
    assert_absent(PAYLOAD_MARKER, surfaces)
    assert PAYLOAD_DIGEST in surfaces["key_findings"]
    assert PAYLOAD_DIGEST in surfaces["provenance.inputs"]
    assert PAYLOAD_DIGEST in surfaces["state.json"]


def test_a_job_that_echoes_its_env_leaks_only_into_captured_output(
    store: Store, isolated_store_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The honesty edge, asserted as a surface-class split.

    A job that prints its own environment writes the value into its captured
    output, and captured output persists in the job record. That is the job's
    doing; every field headspace itself composes must still be clean, and the
    difference between those two classes is the whole of the claim.
    """
    monkeypatch.setenv(SECRET_NAME, SECRET)
    printenv = ("printenv", SECRET_NAME)
    provider = FakeProvider(script={printenv: JobPlan.succeeding(output=SECRET)})
    orchestrator = Orchestrator(provider, store)
    orchestrator.create(workspace_id=WS)

    package = orchestrator.run(
        WS, printenv, environment=parse_environment([SECRET_NAME], None), job_id="job-echo"
    )

    # Captured-output-derived: allowed to carry it, and does.
    assert package.evidence[0].excerpt == SECRET
    assert state_record(store)["jobs"][-1]["output"] == SECRET

    # Everything headspace composed itself: clean.
    assert_absent(SECRET, composed_surfaces(package, isolated_store_home))

    # And in state.json the value appears under the job's captured output and
    # nowhere else at all.
    record = state_record(store)
    job = record["jobs"].pop()
    assert job.pop("output") == SECRET
    assert SECRET not in json.dumps(record)
    assert SECRET not in json.dumps(job)


def test_run_help_states_the_job_echo_edge(capsys: Any) -> None:
    with pytest.raises(SystemExit) as exited:
        main(["run", "--help"])
    assert exited.value.code == 0

    text = " ".join(capsys.readouterr().out.split())
    assert "--env NAME" in text
    assert "--env-file" in text
    assert "--input NAME=HOST_PATH" in text
    # Criterion 4: the edge is stated where the flags are, in the caller's terms.
    assert "printenv" in text
    assert "captured output" in text


# --- criterion 3: one copy-in implementation site ---------------------------


def test_run_input_and_put_both_arrive_at_the_one_copy_in(
    fake: FakeProvider, store: Store, payload: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Behavioural proof: both verbs pass through ``Orchestrator._copy_in``."""
    seen: list[tuple[str, tuple[str, ...]]] = []
    original = Orchestrator._copy_in

    def spy(
        self: Orchestrator,
        workspace_id: str,
        record: dict[str, Any],
        entries: Any,
        *,
        overwrite: bool,
    ) -> list[dict[str, Any]]:
        entries = list(entries)
        seen.append((workspace_id, tuple(entry.destination for entry in entries)))
        return original(self, workspace_id, record, entries, overwrite=overwrite)

    monkeypatch.setattr(Orchestrator, "_copy_in", spy)

    orchestrator = ready(fake, store)
    orchestrator.create(workspace_id="ws-put-twin")
    orchestrator.put("ws-put-twin", str(payload), "config.env")
    orchestrator.run(
        WS, ECHO, inputs=[InputRequest(host_path=str(payload), destination="config.env")]
    )

    assert seen == [("ws-put-twin", ("config.env",)), (WS, ("config.env",))]


def test_the_module_holds_exactly_one_engine_write_call() -> None:
    """Structural proof: there is one place in the orchestrator that writes bytes in.

    ``put`` reaches it directly; ``run`` reaches it through ``_copy_inputs``,
    which is the host-side expansion and budget check and nothing else. Neither
    verb writes to the engine itself.
    """
    source = Path(workspace_module.__file__).read_text(encoding="utf-8")
    assert source.count("self._provider.write(") == 1
    assert "self._copy_in(" in _inspect.getsource(Orchestrator.put)
    assert "self._copy_inputs(" in _inspect.getsource(Orchestrator.run)
    assert "self._copy_in(" in _inspect.getsource(Orchestrator._copy_inputs)
    assert "self._provider.write(" not in _inspect.getsource(Orchestrator.put)
    assert "self._provider.write(" not in _inspect.getsource(Orchestrator.run)


def test_run_input_writes_the_same_ledger_put_does(
    fake: FakeProvider, store: Store, payload: Path
) -> None:
    """Equality proof: same payload, same rows — the recorded shape cannot diverge."""
    orchestrator = ready(fake, store)
    orchestrator.create(workspace_id="ws-put-twin")
    orchestrator.put("ws-put-twin", str(payload), "config.env")
    orchestrator.run(
        WS, ECHO, inputs=[InputRequest(host_path=str(payload), destination="config.env")]
    )

    def rows(workspace_id: str) -> list[dict[str, Any]]:
        return [
            {key: value for key, value in row.items() if key != "recorded_at"}
            for row in ledger(store, workspace_id)
        ]

    assert rows(WS) == rows("ws-put-twin")
    assert rows(WS)[0]["sha256"] == PAYLOAD_DIGEST


def test_run_input_journals_a_put_intent_before_the_run_intent(
    fake: FakeProvider, store: Store, payload: Path
) -> None:
    """The copy-in is settled before the job is even intended.

    Ordering matters for recovery: a CLI killed mid-copy must leave an open
    ``put`` intent and a workspace still in ``ready``, which is exactly the
    shape reconciliation already knows how to settle.
    """
    orchestrator = ready(fake, store)
    orchestrator.run(
        WS,
        ECHO,
        inputs=[InputRequest(host_path=str(payload), destination="config.env")],
        job_id="job-with-input",
    )

    phases = [
        (entry["intent"], entry["phase"])
        for entry in journal_entries(store)
        if entry["intent"] in (INTENT_PUT, INTENT_RUN)
    ]
    assert phases == [
        (INTENT_PUT, PHASE_INTENDED),
        (INTENT_PUT, PHASE_SETTLED),
        (INTENT_RUN, PHASE_INTENDED),
        (INTENT_RUN, PHASE_SETTLED),
    ]


def test_run_input_lands_the_bytes_the_job_can_read(
    fake: FakeProvider, store: Store, payload: Path
) -> None:
    orchestrator = ready(fake, store)
    orchestrator.run(
        WS, ECHO, inputs=[InputRequest(host_path=str(payload), destination="lib/config.env")]
    )
    assert fake._workspaces[WS].files["lib/config.env"] == PAYLOAD


def test_run_input_accepts_a_host_directory(
    fake: FakeProvider, store: Store, tmp_path: Path
) -> None:
    """A harness plus its dependencies is one ``--input``, not one per file."""
    harness = tmp_path / "harness"
    (harness / "lib").mkdir(parents=True)
    (harness / "main.py").write_bytes(b"print(1)\n")
    (harness / "lib" / "helper.py").write_bytes(b"HELP = 1\n")

    orchestrator = ready(fake, store)
    orchestrator.run(WS, ECHO, inputs=[InputRequest(host_path=str(harness), destination="work")])

    assert sorted(fake._workspaces[WS].files) == ["work/lib/helper.py", "work/main.py"]


def test_two_inputs_landing_at_one_destination_are_refused(
    fake: FakeProvider, store: Store, payload: Path, tmp_path: Path
) -> None:
    other = tmp_path / "other.env"
    other.write_bytes(b"different\n")
    orchestrator = ready(fake, store)

    first_input = InputRequest(host_path=str(payload), destination="config.env")
    second_input = InputRequest(host_path=str(other), destination="config.env")
    with pytest.raises(CliError) as refused:
        orchestrator.run(WS, ECHO, inputs=[first_input, second_input])
    assert refused.value.code == EXIT_USER_ERROR
    assert "config.env" in refused.value.message
    assert ledger(store) == []


def test_copied_in_files_are_inputs_and_do_not_gate_a_destroy(
    fake: FakeProvider, store: Store, payload: Path
) -> None:
    orchestrator = ready(fake, store)
    orchestrator.run(
        WS, ECHO, inputs=[InputRequest(host_path=str(payload), destination="config.env")]
    )
    assert state_record(store)["artifacts"] == []
    orchestrator.destroy(WS)


# --- the CLI surface, end to end --------------------------------------------


def test_cli_run_carries_input_and_env_together(
    fake: FakeProvider, store: Store, payload: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shape issue #13 and issue #14 asked for, in one invocation."""
    monkeypatch.setenv(SECRET_NAME, SECRET)
    assert run_cli("create", "--workspace-id", WS, "--json") == EXIT_SUCCESS
    code = run_cli(
        "run",
        "--input",
        f"config.env={payload}",
        "--env",
        SECRET_NAME,
        "--json",
        WS,
        *ECHO,
    )
    assert code == EXIT_SUCCESS

    assert observed_env(fake) == {SECRET_NAME: SECRET}
    assert fake._workspaces[WS].files["config.env"] == PAYLOAD
    assert [row["destination"] for row in ledger(store)] == ["config.env"]


def test_flags_after_the_workspace_id_belong_to_the_job(fake: FakeProvider) -> None:
    """The REMAINDER bargain holds for the new flags exactly as for ``--declare``."""
    assert run_cli("create", "--workspace-id", WS, "--json") == EXIT_SUCCESS
    assert run_cli("run", WS, "echo", "--env", "NOT_A_HEADSPACE_FLAG") == EXIT_SUCCESS
    assert observed_env(fake) == {}


def test_job_environment_repr_never_shows_a_value() -> None:
    """Even an accidental interpolation of the object cannot leak a value."""
    environment = JobEnvironment(values={SECRET_NAME: SECRET}, sources=("/tmp/x.env",))
    assert SECRET not in repr(environment)
    assert SECRET_NAME in repr(environment)
    assert environment.names == (SECRET_NAME,)
