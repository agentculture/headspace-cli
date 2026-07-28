"""Tests for the context-return contract in :mod:`headspace.core.result`.

Two acceptance criteria drive this file:

1. one result structure renders to markdown and JSON carrying identical fields;
2. a multi-megabyte captured output yields a package within the configured
   byte bound, with truncation markers referencing the inspect path.

Criterion 1 is asserted by *walking* both renderings and comparing them
field-by-field, not by checking that both are non-empty. Criterion 2 builds a
genuinely multi-megabyte string rather than mocking the size.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from headspace.cli._errors import CliError
from headspace.core.policy import Policy, ResourceBudget
from headspace.core.profiles import DEFAULT_PROFILE
from headspace.core.result import (
    DEFAULT_MAX_BYTES,
    INSPECT_COMMAND,
    INSPECT_LOGS_FLAG,
    MEMORY_BASIS_SAMPLED_FLOOR,
    MEMORY_BASIS_SAMPLED_PEAK,
    MIN_RENDER_BYTES,
    SECTION_TITLES,
    STATUS_RESOURCE_EXHAUSTED,
    STATUSES,
    TRUNCATION_MARKER_PREFIX,
    Artifact,
    Evidence,
    Provenance,
    ResourceUsage,
    ResultPackage,
    bounded_dict,
    inspect_path,
    render_json,
    render_markdown,
)
from headspace.core.store import HOME_ENV_VAR, Store
from headspace.core.workspace import Orchestrator
from headspace.providers.fake import FakeProvider, JobPlan

# --- helpers --------------------------------------------------------------


def _package(**overrides: object) -> ResultPackage:
    """A representative package with every one of the nine sections populated."""
    base: dict[str, object] = {
        "outcome_summary": (
            "Factored the supplied 4096x4096 matrix and cross-checked the "
            "eigenvalues against an independent solver; the objective was met "
            "for seven of eight blocks."
        ),
        "status": "partial_success",
        "key_findings": [
            "largest eigenvalue is 12.7431",
            "the matrix is not positive definite",
        ],
        "evidence": [
            Evidence(
                label="residual check",
                kind="check",
                excerpt="max |Ax - lambda x| = 3.1e-14",
                source="job-2",
            ),
            Evidence(
                label="solver banner",
                kind="excerpt",
                excerpt="solver 4.2.1 (openblas)\nthreads=8",
                source="job-2",
            ),
        ],
        "artifacts": [
            Artifact(
                name="eigenvalues.npy",
                purpose="the computed spectrum, for the caller to reuse",
                media_type="application/octet-stream",
                digest="sha256:9f2c",
                size_bytes=131072,
                reference="artifact://ws-7/eigenvalues.npy",
            )
        ],
        "warnings": ["block 5 did not converge within the iteration budget"],
        "resource_usage": ResourceUsage(
            wall_time_seconds=12.5,
            cpu_seconds=41.2,
            max_memory_bytes=734003200,
            storage_bytes=1048576,
            output_bytes=4915200,
        ),
        "provenance": Provenance(
            workspace_id="ws-7",
            job_id="job-2",
            profile="python-3.12",
            image_digest="sha256:1d4e",
            started_at="2026-07-28T09:00:00Z",
            finished_at="2026-07-28T09:00:12Z",
            policy_summary="network=none memory=1g pids=256",
            inputs=["matrix.npy"],
            trace_id="trace-aa11",
        ),
        "attention": ["decide whether to rerun block 5 with a tighter tolerance"],
    }
    base.update(overrides)
    return ResultPackage(**base)  # type: ignore[arg-type]


def _md_scalar(value: object) -> str:
    """How the markdown renderer is contracted to spell a leaf value."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _leaves(value: object, out: list[object]) -> list[object]:
    if isinstance(value, dict):
        for item in value.values():
            _leaves(item, out)
    elif isinstance(value, list):
        for item in value:
            _leaves(item, out)
    else:
        out.append(value)
    return out


def _keys(value: object, out: list[str]) -> list[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            out.append(key)
            _keys(item, out)
    elif isinstance(value, list):
        for item in value:
            _keys(item, out)
    return out


def _headings(markdown: str) -> list[str]:
    return [line[3:].strip() for line in markdown.splitlines() if line.startswith("## ")]


def _section(markdown: str, title: str) -> str:
    """The body of one ``## <title>`` section, so an assertion cannot match elsewhere.

    Load-bearing rather than tidy: the memory ceiling a key finding must name
    also appears in the provenance section's policy summary, so a whole-document
    substring check would pass even if the finding were never written.
    """
    body: list[str] = []
    inside = False
    for line in markdown.splitlines():
        if line.startswith("## "):
            inside = line[3:].strip() == title
            continue
        if inside:
            body.append(line)
    assert body, f"no section titled {title!r} in the rendering"
    return "\n".join(body)


def _dedented(markdown: str) -> str:
    """Markdown with nesting indentation removed, so multi-line leaves match contiguously."""
    return "\n".join(line.strip() for line in markdown.splitlines())


def _huge_capture() -> str:
    """A genuinely multi-megabyte captured stdout, ending in a sentinel."""
    body = "\n".join(
        f"[{i:06d}] iterating candidate {i} residual 1.0e-0{i % 9}" for i in range(90000)
    )
    return body + "\nCAPTURE-TAIL-SENTINEL\n"


# --- schema ---------------------------------------------------------------


def test_package_carries_exactly_the_nine_contract_sections() -> None:
    expected = [
        "outcome_summary",
        "status",
        "key_findings",
        "evidence",
        "artifacts",
        "warnings",
        "resource_usage",
        "provenance",
        "attention",
    ]
    assert list(SECTION_TITLES) == expected
    assert [f.name for f in dataclasses.fields(ResultPackage)] == expected
    assert list(bounded_dict(_package())) == expected


def test_default_package_never_carries_raw_logs() -> None:
    """Raw logs are not a field: only bounded excerpts and references."""
    banned = {"logs", "log", "stdout", "stderr", "transcript", "console"}
    assert not banned & {f.name for f in dataclasses.fields(ResultPackage)}
    assert not banned & set(_keys(bounded_dict(_package()), []))


def test_status_vocabulary_is_the_shared_eight() -> None:
    """Exact equality on purpose: the vocabulary is shared across four surfaces.

    ``STATUSES`` is mirrored by ``JOB_STATUSES`` (providers/base.py),
    ``_STATUS_EXIT_CODES`` (core/workspace.py) and ``EXIT_CATEGORIES``
    (cli/_errors.py). A membership check would let a member be added here and
    nowhere else; equality makes any change to the vocabulary fail loudly, which
    is exactly the moment to go and check the other three.
    """
    assert STATUSES == (
        "success",
        "partial_success",
        "failure",
        "cancelled",
        "timeout",
        "policy_denied",
        "infrastructure_failure",
        "resource_exhausted",
    )


def test_unknown_status_fails_closed_with_a_remediation() -> None:
    with pytest.raises(CliError) as exc:
        _package(status="ok")
    assert "ok" in exc.value.message
    assert "policy_denied" in exc.value.remediation


@pytest.mark.parametrize("status", STATUSES)
def test_every_status_renders_in_both_modes(status: str) -> None:
    package = _package(status=status)
    assert status in render_markdown(package)
    assert json.loads(render_json(package))["status"] == status


# --- criterion 1: markdown and JSON carry identical fields ----------------


def test_both_renderings_derive_from_one_structure() -> None:
    """Data-first: the JSON rendering *is* the shared structure, serialised."""
    package = _package()
    assert json.loads(render_json(package)) == bounded_dict(package)


def test_markdown_and_json_carry_identical_fields() -> None:
    package = _package()
    payload = json.loads(render_json(package))
    markdown = render_markdown(package)

    # Section-level equivalence: same sections, same order, in both renderings.
    assert list(payload) == list(SECTION_TITLES)
    assert _headings(markdown) == [SECTION_TITLES[key] for key in payload]

    # Field-level equivalence, JSON -> markdown: every nested key is a label.
    nested = [key for key in _keys(payload, []) if key not in SECTION_TITLES]
    assert nested, "the fixture must exercise nested records"
    for key in nested:
        assert key in markdown, f"markdown is missing the JSON key {key!r}"

    # Field-level equivalence, values: every JSON leaf is visible in markdown.
    flat = _dedented(markdown)
    leaves = _leaves(payload, [])
    assert len(leaves) > 20, "the fixture must exercise a populated package"
    for leaf in leaves:
        assert _md_scalar(leaf) in flat, f"markdown is missing the JSON leaf {leaf!r}"


def test_markdown_exposes_no_section_absent_from_the_structure() -> None:
    """The reverse direction: markdown invents nothing the structure lacks."""
    payload = bounded_dict(_package())
    title_to_field = {title: field for field, title in SECTION_TITLES.items()}
    for heading in _headings(render_markdown(_package())):
        assert heading in title_to_field
        assert title_to_field[heading] in payload


# --- criterion 2: the byte bound ------------------------------------------


def test_multi_megabyte_capture_fits_the_byte_bound() -> None:
    huge = _huge_capture()
    assert len(huge.encode("utf-8")) > 4 * 1024 * 1024, "the capture must be multi-megabyte"

    package = _package(
        evidence=[Evidence(label="captured stdout", kind="excerpt", excerpt=huge, source="job-2")]
    )
    markdown = render_markdown(package)

    assert len(markdown.encode("utf-8")) <= DEFAULT_MAX_BYTES
    assert TRUNCATION_MARKER_PREFIX in markdown
    assert inspect_path("job-2") in markdown
    assert f"{INSPECT_COMMAND} job-2 {INSPECT_LOGS_FLAG}" in markdown
    # The marker states the true full size, so a reader knows what it is missing.
    assert str(len(huge.encode("utf-8"))) in markdown
    # The transcript itself did not come back.
    assert "CAPTURE-TAIL-SENTINEL" not in markdown
    # And the package is orders of magnitude smaller than the transcript.
    assert len(markdown.encode("utf-8")) * 100 < len(huge.encode("utf-8"))


def test_multi_megabyte_capture_json_is_valid_and_bounded() -> None:
    huge = _huge_capture()
    package = _package(
        evidence=[Evidence(label="captured stdout", kind="excerpt", excerpt=huge, source="job-2")]
    )
    raw = render_json(package)
    payload = json.loads(raw)  # still valid JSON after bounding

    assert len(raw.encode("utf-8")) <= DEFAULT_MAX_BYTES
    assert payload["evidence"][0]["truncated"] is True
    assert inspect_path("job-2") in payload["evidence"][0]["excerpt"]
    assert "CAPTURE-TAIL-SENTINEL" not in raw
    assert list(payload) == list(SECTION_TITLES)


def test_truncation_marker_falls_back_to_the_workspace_reference() -> None:
    package = _package(
        evidence=[Evidence(label="captured stdout", excerpt="q" * 200000, source="")],
        provenance=Provenance(workspace_id="ws-9"),
    )
    assert inspect_path("ws-9") in render_markdown(package)


@pytest.mark.parametrize("bound", [MIN_RENDER_BYTES, 1024, 4096, DEFAULT_MAX_BYTES, 65536])
def test_byte_bound_is_configurable(bound: int) -> None:
    package = _package(
        evidence=[Evidence(label="captured stdout", excerpt="y" * 400000, source="job-2")]
    )
    markdown = render_markdown(package, max_bytes=bound)
    assert len(markdown.encode("utf-8")) <= bound
    assert TRUNCATION_MARKER_PREFIX in markdown
    assert INSPECT_COMMAND in markdown


def _overflowing_package() -> ResultPackage:
    return _package(
        key_findings=[f"finding {i}: " + "measurement detail " * 12 for i in range(400)],
        warnings=[f"warning {i}: " + "limitation detail " * 12 for i in range(400)],
        attention=[f"attention {i}: " + "decision detail " * 12 for i in range(400)],
    )


def test_bound_holds_even_when_every_section_overflows() -> None:
    """Last-resort guarantee: the document clip still names the inspect path."""
    markdown = render_markdown(_overflowing_package(), max_bytes=MIN_RENDER_BYTES)
    assert len(markdown.encode("utf-8")) <= MIN_RENDER_BYTES
    assert TRUNCATION_MARKER_PREFIX in markdown
    assert inspect_path("job-2") in markdown


def test_last_resort_clip_leaves_json_structurally_complete() -> None:
    """The documented asymmetry: clipping JSON would emit invalid JSON, so it is not clipped.

    Markdown carries the clip marker announcing the cut; the machine rendering
    stays parseable and keeps all nine sections.
    """
    package = _overflowing_package()
    payload = json.loads(render_json(package, max_bytes=MIN_RENDER_BYTES))
    assert list(payload) == list(SECTION_TITLES)
    assert len(payload["key_findings"]) == 400


def test_bound_below_the_floor_fails_closed() -> None:
    package = _package()
    with pytest.raises(CliError) as exc:
        render_markdown(package, max_bytes=16)
    assert str(MIN_RENDER_BYTES) in exc.value.remediation


def test_small_package_renders_without_truncation_markers() -> None:
    markdown = render_markdown(_package())
    assert TRUNCATION_MARKER_PREFIX not in markdown
    assert bounded_dict(_package())["evidence"][0]["truncated"] is False  # type: ignore[index]


def test_renderers_are_pure_and_leave_the_package_untouched() -> None:
    huge = "z" * 3000000
    package = _package(evidence=[Evidence(label="captured stdout", excerpt=huge, source="job-2")])
    first_markdown = render_markdown(package)
    first_json = render_json(package)

    assert render_markdown(package) == first_markdown
    assert render_json(package) == first_json
    assert package.evidence[0].excerpt == huge


# --- what a failure is allowed to leave unsaid ----------------------------
#
# Two renderings of one honesty rule, so they live together in the file that
# owns the rendering contract even though half the assembly happens in
# :mod:`headspace.core.workspace`:
#
# * an OOM-killed job must name the ceiling it hit (the orchestrator knows the
#   policy) and must label its sampled peak a *floor* (the renderer knows the
#   status) — because the sampler polls, and the spike that triggered the kill
#   is precisely the one it is most likely to miss;
# * a command the image could not execute must name the profile and the
#   caller's own ``argv[0]``, built from what headspace knows rather than from
#   whatever sentence the engine happened to produce.
#
# The packages are built by driving a real :class:`Orchestrator` over the fake
# provider, so the wiring is under test and not only the helpers.

#: The ceiling the 2026-07-28 live test enforced (128 MiB) ...
MEMORY_CEILING = 134217728
#: ... and what the sampler actually observed under it before the kernel killed
#: the job: low by a factor of about 25. This pair is the whole reason the label
#: exists, so the fixtures use the real numbers rather than round ones.
SAMPLED_PEAK = 5320704

KILLED_COMMAND = ("allocate", "512MiB")
KILLED_JOB = JobPlan(
    status=STATUS_RESOURCE_EXHAUSTED,
    exit_status=137,
    output="Killed\n",
    max_memory_bytes=SAMPLED_PEAK,
)


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Store:
    """A throwaway store root, never the real ``~/.headspace``."""
    monkeypatch.setenv(HOME_ENV_VAR, str(tmp_path / "headspace-home"))
    return Store()


def _job_package(
    plan: JobPlan,
    command: tuple[str, ...],
    store: Store,
    *,
    memory_bytes: int = MEMORY_CEILING,
    workspace_id: str = "ws-ceiling",
) -> ResultPackage:
    """The result package one scripted job produces, through the real orchestrator."""
    orchestrator = Orchestrator(FakeProvider(script={command: plan}), store)
    orchestrator.create(
        workspace_id=workspace_id,
        policy=Policy(budget=ResourceBudget(memory_bytes=memory_bytes)),
    )
    return orchestrator.run(workspace_id, command)


def test_a_resource_exhausted_result_names_the_ceiling_and_a_remedy(store: Store) -> None:
    package = _job_package(KILLED_JOB, KILLED_COMMAND, store)
    assert package.status == STATUS_RESOURCE_EXHAUSTED

    payload = json.loads(render_json(package))
    findings = " ".join(payload["key_findings"])
    assert str(MEMORY_CEILING) in findings, "the finding must name the enforced ceiling in bytes"
    assert "memory" in findings

    assert payload["attention"], "an OOM kill must not leave the attention section empty"
    remedy = " ".join(payload["attention"])
    assert "--memory-bytes" in remedy
    assert "working set" in remedy

    markdown = render_markdown(package)
    assert str(MEMORY_CEILING) in _section(markdown, SECTION_TITLES["key_findings"])
    assert "--memory-bytes" in _section(markdown, SECTION_TITLES["attention"])


def test_an_oom_kill_labels_the_sampled_peak_a_floor_and_keeps_the_number(store: Store) -> None:
    package = _job_package(KILLED_JOB, KILLED_COMMAND, store)

    # The measured value is untouched, and the ceiling is never put in its place.
    assert package.resource_usage.max_memory_bytes == SAMPLED_PEAK
    usage = json.loads(render_json(package))["resource_usage"]
    assert usage["max_memory_bytes"] == SAMPLED_PEAK
    assert usage["max_memory_basis"] == MEMORY_BASIS_SAMPLED_FLOOR
    assert "floor" in usage["max_memory_basis"]

    rendered = _section(render_markdown(package), SECTION_TITLES["resource_usage"])
    assert f"max_memory_bytes: {SAMPLED_PEAK}" in rendered
    assert MEMORY_BASIS_SAMPLED_FLOOR in rendered
    assert str(MEMORY_CEILING) not in rendered, "the ceiling must not stand in for a measurement"


@pytest.mark.parametrize("status", STATUSES)
def test_only_an_exhausted_package_calls_its_sampled_memory_a_floor(status: str) -> None:
    """The label is derived from the status, so the two can never disagree."""
    package = _package(status=status)
    expected = (
        MEMORY_BASIS_SAMPLED_FLOOR
        if status == STATUS_RESOURCE_EXHAUSTED
        else MEMORY_BASIS_SAMPLED_PEAK
    )
    assert json.loads(render_json(package))["resource_usage"]["max_memory_basis"] == expected
    assert expected in _section(render_markdown(package), SECTION_TITLES["resource_usage"])


@pytest.mark.parametrize(
    ("exit_status", "wording"),
    [(127, "not found"), (126, "not executable")],
)
def test_a_command_the_image_cannot_run_names_the_profile_and_argv0(
    store: Store, exit_status: int, wording: str
) -> None:
    command = ("definitely-not-a-binary", "--iterations=4")
    package = _job_package(JobPlan.failing(exit_status=exit_status), command, store)

    findings = " ".join(json.loads(render_json(package))["key_findings"])
    assert DEFAULT_PROFILE in findings
    assert "definitely-not-a-binary" in findings
    assert str(exit_status) in findings
    assert wording in findings
    # argv[0], not the command line: the rest is already in provenance inputs.
    assert "--iterations=4" not in findings


def test_the_two_not_executable_statuses_do_not_share_a_finding(store: Store) -> None:
    """126 and 127 tell a caller to do different things, so they cannot read alike."""
    command = ("definitely-not-a-binary",)
    not_found = _job_package(JobPlan.failing(exit_status=127), command, store)
    not_executable = _job_package(
        JobPlan.failing(exit_status=126), command, store, workspace_id="ws-126"
    )
    # Differing in the digits alone would not count: the two must say different
    # things about what to do next, not just report different numbers.
    assert not_found.key_findings != [
        text.replace("126", "127") for text in not_executable.key_findings
    ]


def test_an_ordinary_non_zero_exit_keeps_the_plain_finding(store: Store) -> None:
    """The branch is narrow: an ordinary failing computation reads exactly as before."""
    package = _job_package(JobPlan.failing(exit_status=3), ("solver",), store)
    assert package.key_findings == ["the command completed with exit status 3"]
    assert package.attention == []
