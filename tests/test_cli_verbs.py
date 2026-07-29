"""Tests for the five lifecycle CLI verbs: create, run, inspect, export, destroy.

Written first, against the two acceptance criteria this task was given:

1. the five ``_commands`` modules register **without editing** ``_dispatch``,
   ``_output`` or the parser plumbing, and every verb honours ``--json``;
2. ``teken cli doctor . --strict`` still passes with the new verbs registered.

Criterion 1 is asserted mechanically rather than by inspection. The
registration-contract tests below read ``headspace/cli/__init__.py`` and prove
three things about it: the five verb names appear *only* inside
``_build_parser`` (the documented extension point), ``_dispatch``/``main``
never name a verb, and each verb's handler comes from its own module — which
is what makes the generic ``args.func(args)`` call the whole of the wiring.
``_output.py`` and ``_errors.py`` are checked for the absence of any
``_commands`` reference at all.

**No engine, ever.** Every test here drives the in-memory fake through
``--provider fake``; nothing in this file needs a Docker daemon, a socket or
a network. ``HEADSPACE_HOME`` is redirected at a ``tmp_path`` by an autouse
fixture, so no test can see or touch a developer's real store. One test spawns
a subprocess purely to prove the docker SDK is never imported on the fake path.

**The fake is memoised per process on purpose** (see
:func:`headspace.cli._commands.create.provider_for`), which is what lets a test
drive ``create`` and then ``run`` through two separate ``main()`` calls and have
the second one find the workspace the first one made. The autouse fixture
clears that memo between tests, so no workspace outlives the test that made it.
"""

from __future__ import annotations

import argparse
import inspect as _inspect
import json
import re
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import fields
from pathlib import Path
from typing import Any

import pytest

import headspace.cli as cli_module
from headspace.cli import _errors as errors_module
from headspace.cli import _output as output_module
from headspace.cli import main
from headspace.cli._commands import create as create_cmd
from headspace.cli._errors import (
    EXIT_COMPUTATION_FAILED,
    EXIT_SUCCESS,
    EXIT_TIMEOUT,
    EXIT_USER_ERROR,
)
from headspace.core.policy import ResourceBudget
from headspace.core.result import SECTION_TITLES
from headspace.core.store import HOME_ENV_VAR, Store
from headspace.explain import known_paths
from headspace.providers.fake import FakeProvider, JobPlan

#: The five verbs this task owns, in lifecycle order.
VERBS: tuple[str, ...] = ("create", "run", "inspect", "export", "destroy")


# --- fixtures ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_store_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the store at a throwaway root and hand every test a fresh fake."""
    root = tmp_path / "store"
    monkeypatch.setenv(HOME_ENV_VAR, str(root))
    create_cmd.reset_providers()
    yield root
    create_cmd.reset_providers()


@pytest.fixture
def fake() -> FakeProvider:
    """The one fake instance every ``--provider fake`` invocation will use."""
    provider = create_cmd.provider_for("fake")
    assert isinstance(provider, FakeProvider)
    return provider


def run_cli(verb: str, *argv: str) -> int:
    """Invoke the CLI the way a caller does, always against the fake.

    ``--provider`` is injected immediately after the verb rather than appended,
    because ``run`` captures its command with ``argparse.REMAINDER``: everything
    after the workspace id belongs to the job, flags included.
    """
    return main([verb, "--provider", "fake", *argv])


def make_workspace(capsys: pytest.CaptureFixture[str], *extra: str) -> str:
    """Create a workspace and return its id, discarding the create output."""
    assert run_cli("create", *extra, "--json") == EXIT_SUCCESS
    payload = json.loads(capsys.readouterr().out)
    return str(payload["provenance"]["workspace_id"])


# --- criterion 1a: the registration contract --------------------------------


def test_verbs_register_only_through_the_documented_extension_point() -> None:
    """The five verbs are wired in ``_build_parser`` and nowhere else in the CLI.

    Spec honesty condition h4 says these verbs register "without editing
    ``_dispatch``, ``_output`` or ``_errors``". This is that claim, checked
    against the source rather than trusted: every reference to a verb module
    lives inside the extension point, and the rest of the file — ``main``,
    ``_dispatch``, ``_CliArgumentParser`` — never mentions one.
    """
    module_source = Path(cli_module.__file__).read_text(encoding="utf-8")
    builder_source = _inspect.getsource(cli_module._build_parser)
    assert builder_source in module_source
    outside = module_source.replace(builder_source, "")

    for verb in VERBS:
        assert f"from headspace.cli._commands import {verb} as _{verb}_cmd" in builder_source
        assert f"_{verb}_cmd.register(sub)" in builder_source
        assert f"_{verb}_cmd" not in outside
        assert f"import {verb}" not in outside


def test_dispatch_and_main_name_no_verb() -> None:
    for source in (
        _inspect.getsource(cli_module._dispatch),
        _inspect.getsource(cli_module.main),
    ):
        for verb in VERBS:
            assert re.search(rf"\b{verb}\b", source) is None


def test_output_and_errors_know_nothing_about_commands() -> None:
    for module in (output_module, errors_module):
        text = Path(module.__file__).read_text(encoding="utf-8")
        assert "_commands" not in text
        for verb in VERBS:
            assert f"_{verb}_cmd" not in text


def subparsers() -> dict[str, argparse.ArgumentParser]:
    """The registered subparsers, by verb name."""
    parser = cli_module._build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    raise AssertionError("the top-level parser has no subparsers")


def test_every_verb_is_discoverable() -> None:
    registered = subparsers()
    for verb in VERBS:
        assert verb in registered


def test_every_verb_handler_comes_from_its_own_module() -> None:
    """Proof that generic dispatch is the whole of the wiring.

    ``_dispatch`` calls ``args.func(args)`` and nothing else; if a handler
    resolved to some shared shim inside the CLI package, that claim would be
    hollow. Each one resolves to the verb's own module.
    """
    registered = subparsers()
    for verb in VERBS:
        handler = registered[verb].get_default("func")
        assert handler is not None
        assert handler.__module__ == f"headspace.cli._commands.{verb}"


# --- criterion 1b: every verb honours --json --------------------------------


def test_every_verb_declares_json() -> None:
    registered = subparsers()
    for verb in VERBS:
        options = {opt for action in registered[verb]._actions for opt in action.option_strings}
        assert "--json" in options, verb
        assert "--provider" in options, verb


def test_every_verb_emits_json_on_demand(
    capsys: pytest.CaptureFixture[str], fake: FakeProvider, tmp_path: Path
) -> None:
    """All five verbs, end to end, in JSON. The script-and-forget smoke test."""
    fake.script_command(["write"], JobPlan.succeeding("done", writes={"out.txt": b"payload"}))
    workspace = make_workspace(capsys)

    assert run_cli("run", "--declare", "out.txt=the result", "--json", workspace, "write") == 0
    assert json.loads(capsys.readouterr().out)["status"] == "success"

    assert run_cli("inspect", workspace, "--json") == 0
    assert json.loads(capsys.readouterr().out)["status"] == "success"

    destination = tmp_path / "out.bin"
    assert run_cli("export", workspace, "out.txt", "--to", str(destination), "--json") == 0
    assert json.loads(capsys.readouterr().out)["status"] == "success"

    assert run_cli("destroy", workspace, "--json") == 0
    assert json.loads(capsys.readouterr().out)["status"] == "success"


def test_json_and_markdown_carry_the_same_content(capsys: pytest.CaptureFixture[str]) -> None:
    """One structure, two renderings — never format-then-parse (claim c19).

    The markdown an agent or a human reads and the JSON a script reads have to
    agree, so every finding in the JSON is looked for verbatim in the markdown.
    """
    workspace = make_workspace(capsys)

    assert run_cli("inspect", workspace, "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert run_cli("inspect", workspace) == 0
    markdown = capsys.readouterr().out

    assert markdown.startswith("# headspace result")
    for title in SECTION_TITLES.values():
        assert f"## {title}" in markdown
    assert payload["outcome_summary"] in markdown
    for finding in payload["key_findings"]:
        assert finding in markdown
    assert payload["provenance"]["workspace_id"] in markdown


# --- create -----------------------------------------------------------------


def test_create_reports_a_ready_workspace(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_cli("create") == EXIT_SUCCESS
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "is ready on fake" in captured.out
    assert "lifecycle state: ready" in captured.out


def test_create_honours_an_explicit_workspace_id(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_cli("create", "--workspace-id", "hs-explicit", "--json") == EXIT_SUCCESS
    payload = json.loads(capsys.readouterr().out)
    assert payload["provenance"]["workspace_id"] == "hs-explicit"


def test_create_rejects_an_unknown_profile(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_cli("create", "--profile", "cobol74") == EXIT_USER_ERROR
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err
    assert "Traceback" not in err


def test_create_rejects_a_duplicate_workspace_id(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_cli("create", "--workspace-id", "hs-twice") == EXIT_SUCCESS
    capsys.readouterr()
    assert run_cli("create", "--workspace-id", "hs-twice") == EXIT_USER_ERROR
    assert "hint:" in capsys.readouterr().err


def test_every_budget_field_is_reachable_from_the_flag_surface() -> None:
    """A new ceiling cannot ship unreachable.

    The budget flags are generated from :class:`ResourceBudget`'s own fields, so
    this is really a check that the generation covers the dataclass — the same
    shape of guard ``LIMIT_NAMES`` gets in the policy tests.
    """
    options = {opt for action in subparsers()["create"]._actions for opt in action.option_strings}
    for budget_field in fields(ResourceBudget):
        assert "--" + budget_field.name.replace("_", "-") in options


def test_budget_flags_reach_the_stored_policy(capsys: pytest.CaptureFixture[str]) -> None:
    workspace = make_workspace(
        capsys, "--wall-clock-seconds", "7", "--memory-bytes", "1048576", "--network", "enabled"
    )
    stored: dict[str, Any] = dict(Store().read_state(workspace).state)
    assert stored["policy"]["budget"]["wall_clock_seconds"] == 7
    assert stored["policy"]["budget"]["memory_bytes"] == 1048576
    assert stored["policy"]["network"] == "enabled"
    # Untouched ceilings keep the dataclass's own closed defaults.
    assert stored["policy"]["budget"]["pids_limit"] == ResourceBudget().pids_limit


def test_create_records_declared_host_paths(capsys: pytest.CaptureFixture[str]) -> None:
    workspace = make_workspace(capsys, "--allow-host-path", "/srv/data")
    stored = dict(Store().read_state(workspace).state)
    assert stored["policy"]["filesystem"]["host_paths"] == ["/srv/data"]


# --- run --------------------------------------------------------------------


def test_run_reports_a_successful_job(
    capsys: pytest.CaptureFixture[str], fake: FakeProvider
) -> None:
    fake.script_command(["echo", "hi"], JobPlan.succeeding("hi\n"))
    workspace = make_workspace(capsys)
    assert run_cli("run", workspace, "echo", "hi") == EXIT_SUCCESS
    out = capsys.readouterr().out
    assert "reported success" in out
    assert "hi" in out


def test_run_maps_a_failing_job_to_the_taxonomy(
    capsys: pytest.CaptureFixture[str], fake: FakeProvider
) -> None:
    """The exit code is the *status*'s code, not merely non-zero.

    A failed computation is exit 6, distinct from a broken engine (7) and from
    a caller who typed the wrong thing (1) — that separation is the whole point
    of the failure taxonomy, and it flows from ``exit_code_for_status``.
    """
    fake.script_command(["boom"], JobPlan.failing(exit_status=3, output="nope"))
    workspace = make_workspace(capsys)
    assert run_cli("run", workspace, "boom") == EXIT_COMPUTATION_FAILED
    assert "reported failure" in capsys.readouterr().out


def test_run_maps_a_timeout_to_the_taxonomy(
    capsys: pytest.CaptureFixture[str], fake: FakeProvider
) -> None:
    fake.script_command(["sleep"], JobPlan.timing_out())
    workspace = make_workspace(capsys)
    assert run_cli("run", workspace, "sleep") == EXIT_TIMEOUT


def test_run_passes_flag_shaped_arguments_through(
    capsys: pytest.CaptureFixture[str], fake: FakeProvider
) -> None:
    """``run WS python -c 'print(1)'`` must not have ``-c`` eaten by argparse."""
    fake.script_command(["python", "-c", "print(1)"], JobPlan.succeeding("1\n"))
    workspace = make_workspace(capsys)
    assert run_cli("run", workspace, "python", "-c", "print(1)") == EXIT_SUCCESS
    assert "1" in capsys.readouterr().out


def test_run_refuses_an_empty_command(capsys: pytest.CaptureFixture[str]) -> None:
    workspace = make_workspace(capsys)
    assert run_cli("run", workspace) == EXIT_USER_ERROR
    assert "hint:" in capsys.readouterr().err


def test_run_declaration_needs_a_purpose(capsys: pytest.CaptureFixture[str]) -> None:
    workspace = make_workspace(capsys)
    assert run_cli("run", "--declare", "out.txt", workspace, "echo") == EXIT_USER_ERROR
    err = capsys.readouterr().err
    assert "hint:" in err
    assert "purpose" in err


def test_run_declaration_surfaces_as_attention(
    capsys: pytest.CaptureFixture[str], fake: FakeProvider
) -> None:
    fake.script_command(["write"], JobPlan.succeeding("", writes={"out.txt": b"payload"}))
    workspace = make_workspace(capsys)
    assert run_cli("run", "--declare", "out.txt=the answer", workspace, "write") == EXIT_SUCCESS
    out = capsys.readouterr().out
    assert "declared but not exported" in out
    assert "the answer" in out


# --- inspect ----------------------------------------------------------------


def test_inspect_reports_both_views(capsys: pytest.CaptureFixture[str]) -> None:
    workspace = make_workspace(capsys)
    assert run_cli("inspect", workspace) == EXIT_SUCCESS
    out = capsys.readouterr().out
    assert f"workspace {workspace} is ready on fake" in out
    assert "0 job(s) recorded in this session" in out


def test_inspect_rejects_an_unknown_workspace(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_cli("inspect", "hs-nosuch") == EXIT_USER_ERROR
    assert "hint:" in capsys.readouterr().err


def test_inspect_logs_returns_the_output_the_marker_promises(
    capsys: pytest.CaptureFixture[str], fake: FakeProvider
) -> None:
    """``headspace inspect <ref> --logs`` is a real path, not a marker's fiction.

    ``headspace.core.result`` prints that command whenever it truncates. This
    asserts the promise is kept: the bounded package excerpts, and ``--logs``
    hands back every byte the store kept.
    """
    output = "line\n" * 900
    fake.script_command(["flood"], JobPlan.flooding(output))
    workspace = make_workspace(capsys)

    assert run_cli("run", "--json", workspace, "flood") == EXIT_SUCCESS
    package = json.loads(capsys.readouterr().out)
    assert package["evidence"][0]["truncated"] is True
    job_id = package["provenance"]["job_id"]

    assert run_cli("inspect", workspace, "--logs") == EXIT_SUCCESS
    by_workspace = capsys.readouterr().out
    assert output in by_workspace

    assert run_cli("inspect", job_id, "--logs", "--json") == EXIT_SUCCESS
    by_job = json.loads(capsys.readouterr().out)
    assert by_job["jobs"][0]["job_id"] == job_id
    assert by_job["jobs"][0]["output"] == output


def test_inspect_logs_rejects_an_unknown_handle(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_cli("inspect", "job-nosuch", "--logs") == EXIT_USER_ERROR
    assert "hint:" in capsys.readouterr().err


# --- export -----------------------------------------------------------------


def test_export_publishes_and_verifies(
    capsys: pytest.CaptureFixture[str], fake: FakeProvider, tmp_path: Path
) -> None:
    fake.script_command(["write"], JobPlan.succeeding("", writes={"out.txt": b"payload"}))
    workspace = make_workspace(capsys)
    assert run_cli("run", "--declare", "out.txt=the answer", workspace, "write") == EXIT_SUCCESS
    capsys.readouterr()

    destination = tmp_path / "published.bin"
    assert run_cli("export", workspace, "out.txt", "--to", str(destination)) == EXIT_SUCCESS
    out = capsys.readouterr().out
    assert destination.read_bytes() == b"payload"
    assert "is durable at" in out
    assert "sha256:" in out


def test_export_reads_an_explicit_workspace_path(
    capsys: pytest.CaptureFixture[str], fake: FakeProvider, tmp_path: Path
) -> None:
    fake.script_command(["write"], JobPlan.succeeding("", writes={"deep/result.json": b"{}"}))
    workspace = make_workspace(capsys)
    assert run_cli("run", "--declare", "report=the report", workspace, "write") == EXIT_SUCCESS
    capsys.readouterr()

    destination = tmp_path / "report.json"
    assert (
        run_cli(
            "export",
            workspace,
            "report",
            "--path",
            "deep/result.json",
            "--to",
            str(destination),
        )
        == EXIT_SUCCESS
    )
    assert destination.read_bytes() == b"{}"


def test_export_requires_a_destination(capsys: pytest.CaptureFixture[str]) -> None:
    workspace = make_workspace(capsys)
    with pytest.raises(SystemExit) as exc:
        run_cli("export", workspace, "out.txt")
    assert exc.value.code == EXIT_USER_ERROR
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err


def test_export_refuses_an_undeclared_artifact(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    workspace = make_workspace(capsys)
    rc = run_cli("export", workspace, "ghost", "--to", str(tmp_path / "ghost.bin"))
    assert rc == EXIT_USER_ERROR
    assert "never declared" in capsys.readouterr().err


def test_export_checks_an_expected_digest(
    capsys: pytest.CaptureFixture[str], fake: FakeProvider, tmp_path: Path
) -> None:
    fake.script_command(["write"], JobPlan.succeeding("", writes={"out.txt": b"payload"}))
    workspace = make_workspace(capsys)
    assert run_cli("run", "--declare", "out.txt=the answer", workspace, "write") == EXIT_SUCCESS
    capsys.readouterr()

    rc = run_cli(
        "export",
        workspace,
        "out.txt",
        "--to",
        str(tmp_path / "wrong.bin"),
        "--expect-sha256",
        "0" * 64,
    )
    assert rc != EXIT_SUCCESS
    assert "hint:" in capsys.readouterr().err


# --- destroy ----------------------------------------------------------------


def test_destroy_removes_a_clean_workspace(capsys: pytest.CaptureFixture[str]) -> None:
    workspace = make_workspace(capsys)
    assert run_cli("destroy", workspace) == EXIT_SUCCESS
    out = capsys.readouterr().out
    assert "was destroyed on fake" in out
    assert "lifecycle path:" in out
    assert not Store().exists(workspace)


def test_destroy_refuses_unexported_work_and_removes_nothing(
    capsys: pytest.CaptureFixture[str], fake: FakeProvider
) -> None:
    fake.script_command(["write"], JobPlan.succeeding("", writes={"out.txt": b"payload"}))
    workspace = make_workspace(capsys)
    assert run_cli("run", "--declare", "out.txt=the answer", workspace, "write") == EXIT_SUCCESS
    capsys.readouterr()

    assert run_cli("destroy", workspace) == EXIT_USER_ERROR
    err = capsys.readouterr().err
    assert "refusing to destroy" in err
    assert "hint:" in err
    assert Store().exists(workspace)
    assert run_cli("inspect", workspace) == EXIT_SUCCESS


def test_destroy_force_discards_and_says_so(
    capsys: pytest.CaptureFixture[str], fake: FakeProvider
) -> None:
    fake.script_command(["write"], JobPlan.succeeding("", writes={"out.txt": b"payload"}))
    workspace = make_workspace(capsys)
    assert run_cli("run", "--declare", "out.txt=the answer", workspace, "write") == EXIT_SUCCESS
    capsys.readouterr()

    assert run_cli("destroy", workspace, "--force") == EXIT_SUCCESS
    out = capsys.readouterr().out
    assert "force discarded" in out
    assert "discarded declared artifact 'out.txt'" in out
    assert not Store().exists(workspace)


# --- provider selection and the no-engine path ------------------------------


def test_unknown_provider_is_a_structured_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["create", "--provider", "podman"])
    assert exc.value.code == EXIT_USER_ERROR
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err


def test_the_fake_path_never_imports_the_docker_sdk(tmp_path: Path) -> None:
    """The docker import is lazy, so ``--help`` and the fake cost nothing.

    Run out of process because this test's own suite imports the SDK elsewhere;
    a fresh interpreter is the only place the claim can be observed honestly.
    """
    script = (
        "import sys; from headspace.cli import main;"
        "rc = main(['create', '--provider', 'fake', '--json']);"
        "assert rc == 0, rc;"
        "assert 'docker' not in sys.modules, sorted(m for m in sys.modules if 'docker' in m)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "HEADSPACE_HOME": str(tmp_path / "subprocess-store"),
            "DOCKER_HOST": "unix:///nonexistent/docker.sock",
        },
        check=False,
        cwd=str(Path(cli_module.__file__).resolve().parents[2]),
    )
    assert completed.returncode == 0, completed.stderr


# --- put --------------------------------------------------------------------


def test_put_is_registered_and_reachable() -> None:
    """The put verb is discoverable and its handler comes from its own module."""
    registered = subparsers()
    assert "put" in registered
    handler = registered["put"].get_default("func")
    assert handler is not None
    assert handler.__module__ == "headspace.cli._commands.put"


def test_put_forwards_positionals_and_overwrite(
    capsys: pytest.CaptureFixture[str], fake: FakeProvider, tmp_path: Path
) -> None:
    """put forwards its three positionals and the overwrite flag to the orchestrator."""
    workspace = make_workspace(capsys)
    capsys.readouterr()

    source = tmp_path / "source.txt"
    source.write_bytes(b"hello")

    assert run_cli("put", workspace, str(source), "in.txt") == EXIT_SUCCESS
    out = capsys.readouterr().out
    assert "in.txt" in out


def test_put_refuses_malformed_invocation(capsys: pytest.CaptureFixture[str]) -> None:
    """A missing positional is refused by argparse with a usage hint."""
    with pytest.raises(SystemExit) as exc:
        run_cli("put", "ws")
    assert exc.value.code == EXIT_USER_ERROR
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err


# --- the explain catalog ----------------------------------------------------


def test_every_verb_has_an_explain_entry(capsys: pytest.CaptureFixture[str]) -> None:
    catalog = {path[0] for path in known_paths() if len(path) == 1}
    for verb in VERBS:
        assert verb in catalog
        assert main(["explain", verb]) == EXIT_SUCCESS
        body = capsys.readouterr().out
        assert f"headspace-cli {verb}" in body
        assert "--json" in body


def test_the_root_entry_lists_the_lifecycle_verbs(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["explain", "headspace-cli"]) == EXIT_SUCCESS
    root = capsys.readouterr().out
    for verb in VERBS:
        assert f"`headspace-cli {verb}" in root
