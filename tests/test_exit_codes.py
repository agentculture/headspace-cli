"""Tests for the failure-taxonomy exit codes (the reserved 3+ band).

Derived line-by-line from the task's acceptance criteria:

1. Policy denial, timeout, cancellation, computational failure, and
   infrastructure failure map to distinct documented exit codes; user error
   stays 1, environment error stays 2.
2. Each category is distinguishable in text and --json error output and
   listed in ``learn``.
"""

from __future__ import annotations

import io
import json

import pytest

from headspace.cli import main
from headspace.cli._errors import (
    EXIT_CANCELLED,
    EXIT_CATEGORIES,
    EXIT_COMPUTATION_FAILED,
    EXIT_ENV_ERROR,
    EXIT_INFRASTRUCTURE_FAILURE,
    EXIT_POLICY_DENIED,
    EXIT_RESOURCE_EXHAUSTED,
    EXIT_SUCCESS,
    EXIT_TIMEOUT,
    EXIT_USER_ERROR,
    TAXONOMY_CODES,
    CliError,
    category_for_code,
)
from headspace.cli._output import emit_error

_NEW_CATEGORIES = [
    (EXIT_POLICY_DENIED, "policy_denied"),
    (EXIT_TIMEOUT, "timeout"),
    (EXIT_CANCELLED, "cancelled"),
    (EXIT_COMPUTATION_FAILED, "computation_failed"),
    (EXIT_INFRASTRUCTURE_FAILURE, "infrastructure_failure"),
]


# --- criterion 1: distinct, documented, and 0/1/2 pinned -------------------


def test_legacy_codes_pinned() -> None:
    # A future renumber of 0/1/2 must fail this suite.
    assert EXIT_SUCCESS == 0
    assert EXIT_USER_ERROR == 1
    assert EXIT_ENV_ERROR == 2


def test_new_band_starts_above_env_error_in_declared_order() -> None:
    # "Use exactly these names and this order" — policy_denied, timeout,
    # cancelled, computation_failed, infrastructure_failure — starting where
    # the reserved band begins (3).
    codes = [code for code, _name in _NEW_CATEGORIES]
    assert codes == [3, 4, 5, 6, 7]
    assert all(code > EXIT_ENV_ERROR for code in codes)


def test_all_exit_codes_are_pairwise_distinct() -> None:
    all_codes = [EXIT_SUCCESS, EXIT_USER_ERROR, EXIT_ENV_ERROR] + [
        code for code, _name in _NEW_CATEGORIES
    ]
    assert len(all_codes) == len(set(all_codes)), "two categories share an exit code"


def test_category_names_match_declared_vocabulary() -> None:
    # Names must match the sibling status vocabulary exactly (so the two
    # agree), and each must resolve through the code -> category mapping.
    for code, expected_name in _NEW_CATEGORIES:
        assert category_for_code(code) == expected_name
        assert EXIT_CATEGORIES[code] == expected_name


def test_legacy_categories_also_named() -> None:
    assert category_for_code(EXIT_SUCCESS) == "success"
    assert category_for_code(EXIT_USER_ERROR) == "user_error"
    assert category_for_code(EXIT_ENV_ERROR) == "environment_error"


def test_unmapped_code_reports_unknown_category() -> None:
    assert category_for_code(999) == "unknown"


# --- criterion 2: recoverable from text output, JSON output, and learn -----


@pytest.mark.parametrize("code,name", _NEW_CATEGORIES)
def test_category_recoverable_from_to_dict(code: int, name: str) -> None:
    err = CliError(code=code, message="the job did not finish", remediation="retry it")
    payload = err.to_dict()
    # Existing keys stay intact for existing consumers...
    assert payload["code"] == code
    assert payload["remediation"] == "retry it"
    assert "message" in payload
    # ...and the category is additive, not a replacement.
    assert payload["category"] == name


def test_to_dict_keeps_existing_keys_for_legacy_codes() -> None:
    err = CliError(code=EXIT_USER_ERROR, message="bad flag", remediation="see --help")
    payload = err.to_dict()
    assert payload["code"] == EXIT_USER_ERROR
    assert payload["message"] == "bad flag"
    assert payload["remediation"] == "see --help"
    assert payload["category"] == "user_error"


def test_legacy_message_text_is_not_mutated() -> None:
    # 0/1/2 keep their exact existing message shape — only the reserved band
    # gets the self-documenting category tag.
    err = CliError(code=EXIT_ENV_ERROR, message="tool not installed")
    assert err.message == "tool not installed"


@pytest.mark.parametrize("code,name", _NEW_CATEGORIES)
def test_category_recoverable_from_json_render(code: int, name: str) -> None:
    err = CliError(code=code, message="failure detail", remediation="hint text")
    buf = io.StringIO()
    emit_error(err, json_mode=True, stream=buf)
    payload = json.loads(buf.getvalue())
    assert payload["category"] == name
    assert payload["code"] == code


@pytest.mark.parametrize("code,name", _NEW_CATEGORIES)
def test_category_recoverable_from_text_render(code: int, name: str) -> None:
    err = CliError(code=code, message="failure detail", remediation="hint text")
    buf = io.StringIO()
    emit_error(err, json_mode=False, stream=buf)
    text = buf.getvalue()
    assert text.startswith("error:")
    assert "hint:" in text
    assert name in text


def test_categories_are_distinguishable_from_each_other_in_text() -> None:
    # Rendering each category must not leak another category's name.
    rendered = {}
    for code, _name in _NEW_CATEGORIES:
        err = CliError(code=code, message="failure detail")
        buf = io.StringIO()
        emit_error(err, json_mode=False, stream=buf)
        rendered[code] = buf.getvalue()
    for code, name in _NEW_CATEGORIES:
        for other_code, other_name in _NEW_CATEGORIES:
            if other_code == code:
                continue
            assert other_name not in rendered[code], f"category {name!r} text leaked {other_name!r}"


# --- criterion 2: listed in learn -------------------------------------------


def test_learn_text_lists_every_category(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["learn"])
    assert rc == 0
    out = capsys.readouterr().out
    for _code, name in _NEW_CATEGORIES:
        assert name in out, f"learn text output does not mention {name!r}"
    # user error / environment error stay documented too.
    assert "1 " in out
    assert "2 " in out


def test_learn_json_lists_every_category(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["learn", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    exit_codes = payload["exit_codes"]
    for code, name in _NEW_CATEGORIES:
        assert str(code) in exit_codes, f"learn --json is missing exit code {code}"
        assert name in exit_codes[str(code)], f"learn --json entry for {code} missing {name!r}"


# --- resource_exhausted: exit code 8, the taxonomy's newest member ---------
#
# Deliberately a *separate* list from `_NEW_CATEGORIES` above, not folded into
# it: `learn`'s self-teaching prompt (headspace/cli/_commands/learn.py) is a
# hand-written surface that a different task keeps in sync with the taxonomy,
# and this task's contract is the four vocabulary surfaces named in its brief
# (STATUSES, JOB_STATUSES, _STATUS_EXIT_CODES, EXIT_CATEGORIES/TAXONOMY_CODES)
# — not `learn`'s prose. Folding code 8 into `_NEW_CATEGORIES` would make
# `test_learn_text_lists_every_category` / `test_learn_json_lists_every_category`
# above fail for a reason outside that contract.


def test_resource_exhausted_is_exit_code_eight() -> None:
    assert EXIT_RESOURCE_EXHAUSTED == 8


def test_resource_exhausted_extends_the_band_downward_compatibly() -> None:
    # Additive extension of the reserved 3+ band: one past the previous
    # highest code (7), never a renumbering or reuse of an existing one.
    assert EXIT_RESOURCE_EXHAUSTED == EXIT_INFRASTRUCTURE_FAILURE + 1
    existing_codes = {EXIT_SUCCESS, EXIT_USER_ERROR, EXIT_ENV_ERROR} | {
        code for code, _name in _NEW_CATEGORIES
    }
    assert EXIT_RESOURCE_EXHAUSTED not in existing_codes


def test_resource_exhausted_is_named_in_exit_categories_and_taxonomy_codes() -> None:
    assert EXIT_CATEGORIES[EXIT_RESOURCE_EXHAUSTED] == "resource_exhausted"
    assert category_for_code(EXIT_RESOURCE_EXHAUSTED) == "resource_exhausted"
    assert EXIT_RESOURCE_EXHAUSTED in TAXONOMY_CODES


def test_resource_exhausted_recoverable_from_to_dict() -> None:
    err = CliError(
        code=EXIT_RESOURCE_EXHAUSTED,
        message="the job was killed for exceeding its memory ceiling",
        remediation="raise the memory budget or reduce the working set",
    )
    payload = err.to_dict()
    assert payload["code"] == EXIT_RESOURCE_EXHAUSTED
    assert payload["category"] == "resource_exhausted"


def test_resource_exhausted_recoverable_from_json_render() -> None:
    err = CliError(code=EXIT_RESOURCE_EXHAUSTED, message="failure detail", remediation="hint text")
    buf = io.StringIO()
    emit_error(err, json_mode=True, stream=buf)
    payload = json.loads(buf.getvalue())
    assert payload["category"] == "resource_exhausted"
    assert payload["code"] == EXIT_RESOURCE_EXHAUSTED


def test_resource_exhausted_recoverable_from_text_render() -> None:
    err = CliError(code=EXIT_RESOURCE_EXHAUSTED, message="failure detail", remediation="hint text")
    buf = io.StringIO()
    emit_error(err, json_mode=False, stream=buf)
    text = buf.getvalue()
    assert text.startswith("error:")
    assert "hint:" in text
    assert "resource_exhausted" in text


def test_resource_exhausted_is_distinguishable_from_the_existing_band_in_text() -> None:
    err = CliError(code=EXIT_RESOURCE_EXHAUSTED, message="failure detail")
    buf = io.StringIO()
    emit_error(err, json_mode=False, stream=buf)
    rendered = buf.getvalue()
    for _code, name in _NEW_CATEGORIES:
        assert name not in rendered, f"resource_exhausted text leaked {name!r}"
