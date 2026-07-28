"""Runtime profiles — name to digest-pinned image reference (stable-contract).

A profile is how a caller says "I need Python" without knowing anything about
containers. Digest-pinning (an explicit ``@sha256:<64 hex>`` suffix) rather
than a floating tag (``:latest``, bare ``:3.12``) is what makes a profile
reproducible: a tag can silently start pointing at a different image next
week, which would quietly destroy the provenance guarantee this whole product
sells. A digest never moves — the only way a profile's image changes is a
deliberate edit to this file.

Resolution here is PURE. :func:`resolve` looks up a name in a plain dict and
returns the pinned reference string; it does not pull the image, does not
touch the network, and this module never imports the docker SDK. A sibling
provider task owns pulling — it receives the reference this module returns
and does the I/O.

An unknown profile name is a policy failure, not a place to guess: it raises
:class:`~headspace.cli._errors.CliError` with a hint listing the valid names,
rather than silently falling back to the default profile or any other image.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from headspace.cli._errors import EXIT_USER_ERROR, CliError

# OCI content digests are the literal ``sha256:`` prefix followed by exactly
# 64 lowercase hex characters. A reference is "digest-pinned" only when it
# ends in ``@<digest>``; anything else (no ``@``, the wrong algorithm, the
# wrong length, or uppercase hex) is a floating reference that can silently
# change what it points to, so it is rejected before it can enter the
# registry.
_DIGEST_SUFFIX = re.compile(r"@sha256:[0-9a-f]{64}$")


@dataclass(frozen=True)
class Profile:
    """A named runtime environment resolved to one exact, immutable image.

    ``image`` MUST carry a trailing ``@sha256:<64 hex digest>`` pin, checked
    in :meth:`__post_init__` so a floating tag can never enter the registry —
    not even via a future typo or a well-meaning "just track latest" edit.
    """

    name: str
    image: str
    description: str

    def __post_init__(self) -> None:
        if not _DIGEST_SUFFIX.search(self.image):
            raise ValueError(
                f"profile {self.name!r} has an unpinned image reference "
                f"{self.image!r}: expected a trailing @sha256:<64 hex digest>, "
                "not a floating tag"
            )


# Digest obtained once, out-of-band, via:
#   docker pull python:3.12-slim
#   docker inspect --format='{{index .RepoDigests 0}}' python:3.12-slim
# => python@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de
# (looked up 2026-07-28; this module never re-resolves the tag on its own —
# moving this profile forward means editing the literal below.)
PYTHON_312 = Profile(
    name="python3.12",
    image="python:3.12-slim@sha256:"
    "57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de",
    description="Python 3.12 (slim Debian base) — default profile for general-purpose "
    "code execution.",
)

#: name -> Profile. :data:`DEFAULT_PROFILE` names the entry callers get when
#: they ask for "a" profile without specifying one.
REGISTRY: dict[str, Profile] = {
    PYTHON_312.name: PYTHON_312,
}

DEFAULT_PROFILE = PYTHON_312.name


def resolve(name: str) -> str:
    """Return the digest-pinned image reference registered under ``name``.

    Pure: performs no I/O and never pulls the image — a sibling provider does
    that with the reference this returns. An unknown ``name`` raises
    :class:`CliError` (exit code 1, user error) rather than silently
    substituting the default profile or any other image.
    """
    if name in REGISTRY:
        return REGISTRY[name].image
    valid = ", ".join(sorted(REGISTRY))
    raise CliError(
        code=EXIT_USER_ERROR,
        message=f"unknown profile: {name!r}",
        remediation=f"choose one of: {valid}",
    )
