"""Tests for headspace.core.profiles — digest-pinned runtime profiles.

Derived line-by-line from the t5 acceptance criteria:

1. each profile maps to a digest-pinned image reference; two resolutions of
   one profile return the same digest until the profile is explicitly
   updated.
2. an unresolvable image fails create explicitly with a policy-grade error,
   never a silent substitute.

Plus the load-bearing design constraint: a reference lacking a sha256 digest
must be rejected at ``Profile`` construction, and resolution must stay pure
(no docker SDK, no network) since a sibling provider owns pulling.
"""

from __future__ import annotations

import ast
import inspect

import pytest

from headspace.cli._errors import EXIT_USER_ERROR, CliError
from headspace.core import profiles

# --- criterion 1: digest-pinned mapping, stable across resolutions --------


def test_every_registered_profile_is_digest_pinned() -> None:
    """Every entry in the registry must carry an explicit sha256 pin.

    A future profile added with a floating tag (``:latest``, bare ``:3.12``)
    must fail this test, not slip into the registry unnoticed.
    """
    for name, profile in profiles.REGISTRY.items():
        assert profiles._DIGEST_SUFFIX.search(
            profile.image
        ), f"profile {name!r} is not digest-pinned: {profile.image!r}"


def test_resolve_returns_the_registered_image_reference() -> None:
    profile = profiles.REGISTRY[profiles.DEFAULT_PROFILE]
    assert profiles.resolve(profiles.DEFAULT_PROFILE) == profile.image


def test_resolve_is_stable_across_repeated_calls() -> None:
    """Two resolutions of one profile return the same digest.

    A profile only changes when the module is explicitly edited to re-pin
    it — never as a side effect of calling resolve() again.
    """
    first = profiles.resolve(profiles.DEFAULT_PROFILE)
    second = profiles.resolve(profiles.DEFAULT_PROFILE)
    assert first == second
    assert "@sha256:" in first


def test_default_profile_is_python_3_12() -> None:
    """The plan mandates shipping a Python 3.12 default profile."""
    default = profiles.REGISTRY[profiles.DEFAULT_PROFILE]
    assert "3.12" in default.image
    assert "python" in default.image.lower()
    assert default.description


# --- criterion 2: unknown name fails explicitly, never a silent default ---


def test_resolve_unknown_name_raises_cli_error() -> None:
    with pytest.raises(CliError) as excinfo:
        profiles.resolve("totally-bogus-profile")
    err = excinfo.value
    assert err.code == EXIT_USER_ERROR
    assert "totally-bogus-profile" in err.message
    assert err.remediation


def test_resolve_unknown_name_hint_lists_valid_names() -> None:
    with pytest.raises(CliError) as excinfo:
        profiles.resolve("does-not-exist")
    remediation = excinfo.value.remediation
    for name in profiles.REGISTRY:
        assert name in remediation


def test_resolve_unknown_name_never_substitutes_the_default() -> None:
    """'Never a silent substitute' — an unresolvable name raises, full stop.

    This proves there is no fallback branch that quietly returns the default
    profile's reference when a name is not found.
    """
    default_ref = profiles.resolve(profiles.DEFAULT_PROFILE)
    with pytest.raises(CliError):
        result = profiles.resolve("nonexistent-profile-name")
        # Unreachable if resolve() actually raises, as required; if some
        # future edit turns this into a fallback, catch it explicitly here
        # rather than letting the substitution pass silently.
        assert result != default_ref, "resolve() must not fall back to default"


def test_unknown_profile_name_uses_the_reserved_user_error_exit_code() -> None:
    """The task spec pins unknown-profile failures to exit code 1, no new codes."""
    with pytest.raises(CliError) as excinfo:
        profiles.resolve("bogus")
    assert excinfo.value.code == 1


# --- construction-time validation: the load-bearing part ------------------


@pytest.mark.parametrize(
    "bad_image",
    [
        "python:3.12-slim",  # bare floating tag, no digest at all
        "python:3.12@latest",  # "@" present but no sha256 digest
        "python@sha256:",  # empty digest
        "python@sha256:" + "a" * 63,  # one hex char short
        "python@sha256:" + "a" * 65,  # one hex char long
        "python@sha256:" + ("g" * 64),  # right length, non-hex characters
        "python@sha256:" + ("A" * 64),  # uppercase hex is not canonical
        "python@md5:" + "a" * 32,  # wrong digest algorithm
        "",  # empty reference entirely
    ],
)
def test_profile_rejects_unpinned_or_malformed_image(bad_image: str) -> None:
    with pytest.raises(ValueError):
        profiles.Profile(name="bad", image=bad_image, description="x")


def test_profile_accepts_a_properly_pinned_image() -> None:
    digest = "b" * 64
    ref = f"python:3.12-slim@sha256:{digest}"
    profile = profiles.Profile(name="ok", image=ref, description="fine")
    assert profile.image == ref
    assert profile.name == "ok"


def test_registry_entries_are_profile_instances() -> None:
    for profile in profiles.REGISTRY.values():
        assert isinstance(profile, profiles.Profile)


def test_default_profile_name_is_registered_key() -> None:
    assert profiles.DEFAULT_PROFILE in profiles.REGISTRY
    assert profiles.REGISTRY[profiles.DEFAULT_PROFILE].name == profiles.DEFAULT_PROFILE


# --- purity: no docker SDK, no network reachable from this module ---------


def test_module_never_imports_docker_sdk_or_network_libraries() -> None:
    """Resolution is pure: no pulling, no network, no docker SDK.

    A sibling provider task owns pulling; this module only maps names to
    pinned references. Statically checking the import list is a stronger
    guarantee than mocking, since it catches the dependency even if no test
    happens to exercise the code path that would use it.
    """
    source = inspect.getsource(profiles)
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    forbidden = {"docker", "socket", "requests", "urllib", "http", "ssl"}
    assert imported.isdisjoint(forbidden), f"forbidden imports found: {imported & forbidden}"


def test_resolve_does_not_perform_network_io(monkeypatch: pytest.MonkeyPatch) -> None:
    """Belt-and-suspenders: patch socket creation and confirm resolve() still works."""

    def _forbidden_connect(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("resolve() must not open a network connection")

    monkeypatch.setattr("socket.socket.connect", _forbidden_connect, raising=True)
    # resolve() must succeed untouched — no socket ever gets constructed.
    assert profiles.resolve(profiles.DEFAULT_PROFILE)
