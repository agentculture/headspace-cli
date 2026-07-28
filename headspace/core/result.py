"""The context-return contract: one result package, two renderings, one byte bound.

headspace exists to keep computational noise *out* of a model's context. That
promise lives or dies in this module: it defines the compact, evidence-bearing
package a caller receives instead of an execution transcript, and the only two
ways that package is allowed to be rendered. If the default rendering is not
dramatically smaller than the raw transcript, the product has no reason to
exist — hence the bound is enforced here, not left to callers.

The nine sections
-----------------
Fixed by the requirements doc's context-return contract (section 8). The field
names below are the stable surface every other layer wires itself to, and
:data:`SECTION_TITLES` is the single ordered map from field name to heading:

==================== ==========================================================
field                contract element
==================== ==========================================================
``outcome_summary``  what was attempted, concluded, and whether the objective
                     was met
``status``           normalized terminal state, from :data:`STATUSES`
``key_findings``     only facts that change the caller's next reasoning step
``evidence``         checks, bounded excerpts, measurements, references
``artifacts``        named exports with purpose, integrity, type, retrieval ref
``warnings``         warnings and limitations that weaken confidence
``resource_usage``   time, compute, memory, storage, output volume
``provenance``       environment, inputs, policy, timestamps, trace identity
``attention``        suggested attention: what needs a human or agent decision
==================== ==========================================================

Data-first, never format-then-parse
-----------------------------------
:meth:`ResultPackage.to_dict` produces the one normalized structure.
:func:`render_json` serializes it; :func:`render_markdown` *walks* it
generically — every heading comes from :data:`SECTION_TITLES`, every label from
a dict key, every value from :func:`_md_scalar`. Neither renderer knows a field
the other does not, and neither ever reads the other's output. Adding a field
to a dataclass makes it appear in both renderings automatically, so the two
cannot drift apart field-wise.

Raw logs are not a field
------------------------
There is deliberately no ``logs`` / ``stdout`` / ``stderr`` field anywhere in
this schema. Captured output enters only as a bounded :class:`Evidence`
excerpt; whatever does not fit is replaced by a marker naming the retrieval
path (:func:`inspect_path`), so a reader is never left guessing where the rest
went. That marker is the contract's honesty clause: compression is allowed to
hide volume, never to hide its own existence.

The byte bound
--------------
:data:`DEFAULT_MAX_BYTES` is 8 KiB — roughly two thousand tokens, about one
percent of a 200k context window, and three orders of magnitude below the
multi-megabyte transcripts this package stands in for. It is a keyword argument
on every renderer so a caller with a tighter budget can shrink it, floored at
:data:`MIN_RENDER_BYTES` so the bound can never be set below the room a
truncation marker itself needs (a bound that silently drops the marker would
defeat the honesty clause).

Enforcement is two-stage, both stages driven off the structure:

1. *Field budgeting.* :func:`_fit` binary-searches the largest per-text-field
   byte allowance whose rendering still fits, so all nine sections survive and
   only oversized text is excerpted. Both renderings share the resulting
   structure, so JSON and markdown carry the same bounded content.
2. *Document clip.* If even the floor allowance does not fit (a caller that
   handed us thousands of findings), the markdown document is clipped to the
   bound and a package-level marker appended. This is the unconditional
   guarantee. It applies to markdown only — clipping JSON would emit invalid
   JSON — so in that last-resort case the JSON rendering stays structurally
   complete while markdown says, in the marker, that it was cut.

List sections are not item-capped: by contract they are curated by the caller
("only the facts that materially affect the next step"), so the only field that
grows without bound is captured text, and that is what stage 1 targets.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from headspace.cli._errors import EXIT_USER_ERROR, CliError

# --- status vocabulary ----------------------------------------------------
# Shared verbatim with the exit-code mapping in headspace.cli._errors; renaming
# a member here silently breaks that mapping, so treat this tuple as frozen.
# Appending a new member is the sanctioned exception — the exit-code band is
# additive by the same policy (see cli._errors: "extend downward-compatibly,
# never renumber an existing code") — so a new status is added at the end,
# never inserted or reordered.
STATUS_SUCCESS = "success"
STATUS_PARTIAL_SUCCESS = "partial_success"
STATUS_FAILURE = "failure"
STATUS_CANCELLED = "cancelled"
STATUS_TIMEOUT = "timeout"
STATUS_POLICY_DENIED = "policy_denied"
STATUS_INFRASTRUCTURE_FAILURE = "infrastructure_failure"
STATUS_RESOURCE_EXHAUSTED = "resource_exhausted"

STATUSES: tuple[str, ...] = (
    STATUS_SUCCESS,
    STATUS_PARTIAL_SUCCESS,
    STATUS_FAILURE,
    STATUS_CANCELLED,
    STATUS_TIMEOUT,
    STATUS_POLICY_DENIED,
    STATUS_INFRASTRUCTURE_FAILURE,
    STATUS_RESOURCE_EXHAUSTED,
)

# --- byte bound -----------------------------------------------------------
DEFAULT_MAX_BYTES = 8192
MIN_RENDER_BYTES = 512
MIN_EXCERPT_BYTES = 80

# --- the inspect path a truncation marker points at -----------------------
INSPECT_COMMAND = "headspace inspect"
INSPECT_LOGS_FLAG = "--logs"
TRUNCATION_MARKER_PREFIX = "[truncated:"

SECTION_TITLES: dict[str, str] = {
    "outcome_summary": "Outcome summary",
    "status": "Status",
    "key_findings": "Key findings",
    "evidence": "Evidence",
    "artifacts": "Artifacts",
    "warnings": "Warnings and limitations",
    "resource_usage": "Resource usage",
    "provenance": "Provenance",
    "attention": "Suggested attention",
}


def inspect_path(ref: str) -> str:
    """The command that retrieves the full data a truncation marker stands for.

    ``ref`` is the most specific handle available (job id, else workspace id).
    It is sanitised and length-capped so a hostile or malformed id can neither
    break out of the marker's backticks nor blow past the byte bound.
    """
    handle = (ref or "").strip().replace("`", "").replace("\n", " ")[:64] or "<workspace>"
    return f"{INSPECT_COMMAND} {handle} {INSPECT_LOGS_FLAG}"


def _excerpt_marker(ref: str, shown: int, total: int) -> str:
    """Marker for a single over-long text field. States the true full size."""
    return (
        f"\n...{TRUNCATION_MARKER_PREFIX} {shown} of {total} bytes shown"
        f" -- full text: `{inspect_path(ref)}`]"
    )


def _package_marker(ref: str, max_bytes: int) -> str:
    """Marker for the last-resort document clip."""
    return (
        f"\n\n...{TRUNCATION_MARKER_PREFIX} result package clipped to {max_bytes} bytes"
        f" -- full result: `{inspect_path(ref)}`]\n"
    )


def _fit_text(text: str, budget: int | None, ref: str) -> tuple[str, bool]:
    """Cut ``text`` to ``budget`` bytes, appending a marker when anything is lost.

    Cuts on a byte boundary but decodes leniently, so a multi-byte codepoint
    straddling the cut is dropped rather than emitted as mojibake.
    """
    if budget is None or not text:
        return text, False
    raw = text.encode("utf-8")
    if len(raw) <= budget:
        return text, False
    head = raw[:budget].decode("utf-8", "ignore")
    return head + _excerpt_marker(ref, len(head.encode("utf-8")), len(raw)), True


# --- schema ---------------------------------------------------------------


@dataclass(frozen=True)
class Evidence:
    """One selected check, excerpt, measurement, or reference behind a finding.

    ``excerpt`` is the only place captured output is allowed to enter the
    package, and it is budgeted at render time. ``source`` names where the full
    text lives so the truncation marker can point a reader at it.
    """

    label: str
    kind: str = "excerpt"
    excerpt: str = ""
    source: str = ""

    def to_dict(self, *, text_budget: int | None, fallback_ref: str) -> dict[str, Any]:
        ref = self.source or fallback_ref
        excerpt, truncated = _fit_text(self.excerpt, text_budget, ref)
        return {
            "label": self.label,
            "kind": self.kind,
            "source": self.source,
            "truncated": truncated,
            "excerpt": excerpt,
        }


@dataclass(frozen=True)
class Artifact:
    """A named export that crossed the workspace boundary intentionally.

    Carries purpose, type, integrity (``digest``), and a retrieval reference —
    the four things a caller needs to trust and recover it without the caller
    ever seeing the workspace.
    """

    name: str
    purpose: str = ""
    media_type: str = ""
    digest: str = ""
    size_bytes: int = 0
    reference: str = ""

    def to_dict(self, *, text_budget: int | None, fallback_ref: str) -> dict[str, Any]:
        purpose, _ = _fit_text(self.purpose, text_budget, self.reference or fallback_ref)
        return {
            "name": self.name,
            "purpose": purpose,
            "media_type": self.media_type,
            "digest": self.digest,
            "size_bytes": self.size_bytes,
            "reference": self.reference,
        }


@dataclass(frozen=True)
class ResourceUsage:
    """What the work cost, for governance and for optimising the next delegation.

    ``output_bytes`` is the volume actually captured — the number that explains
    to a reader why the evidence excerpts are truncated.
    """

    wall_time_seconds: float = 0.0
    cpu_seconds: float = 0.0
    max_memory_bytes: int = 0
    storage_bytes: int = 0
    output_bytes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "wall_time_seconds": self.wall_time_seconds,
            "cpu_seconds": self.cpu_seconds,
            "max_memory_bytes": self.max_memory_bytes,
            "storage_bytes": self.storage_bytes,
            "output_bytes": self.output_bytes,
        }


@dataclass(frozen=True)
class Provenance:
    """Enough identity to audit or reproduce the work.

    Deliberately backend-neutral: ``profile`` plus ``image_digest`` pin the
    environment without naming an engine, so swapping the execution provider
    changes values here, never fields.
    """

    workspace_id: str = ""
    job_id: str = ""
    profile: str = ""
    image_digest: str = ""
    started_at: str = ""
    finished_at: str = ""
    policy_summary: str = ""
    inputs: list[str] = field(default_factory=list)
    trace_id: str = ""

    @property
    def reference(self) -> str:
        """The most specific handle for the inspect path: job id, else workspace."""
        return self.job_id or self.workspace_id or ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "job_id": self.job_id,
            "profile": self.profile,
            "image_digest": self.image_digest,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "policy_summary": self.policy_summary,
            "inputs": list(self.inputs),
            "trace_id": self.trace_id,
        }


@dataclass(frozen=True)
class ResultPackage:
    """The nine-section context-return package. The product's defining contract."""

    outcome_summary: str = ""
    status: str = STATUS_SUCCESS
    key_findings: list[str] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    artifacts: list[Artifact] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    resource_usage: ResourceUsage = field(default_factory=ResourceUsage)
    provenance: Provenance = field(default_factory=Provenance)
    attention: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Fail closed on an unknown status: a status outside the vocabulary has
        # no exit code, so letting it through would surface as a mysterious
        # process exit rather than a named error.
        if self.status not in STATUSES:
            raise CliError(
                code=EXIT_USER_ERROR,
                message=f"unknown result status {self.status!r}",
                remediation="use one of: " + ", ".join(STATUSES),
            )

    def to_dict(self, *, text_budget: int | None = None) -> dict[str, Any]:
        """The single normalized structure both renderers consume.

        ``text_budget`` is the per-text-field byte allowance; ``None`` renders
        every field in full. Key order is :data:`SECTION_TITLES` order, which is
        what makes the markdown section order and the JSON key order identical.
        """
        ref = self.provenance.reference
        return {
            "outcome_summary": _fit_text(self.outcome_summary, text_budget, ref)[0],
            "status": self.status,
            "key_findings": [_fit_text(t, text_budget, ref)[0] for t in self.key_findings],
            "evidence": [
                item.to_dict(text_budget=text_budget, fallback_ref=ref) for item in self.evidence
            ],
            "artifacts": [
                item.to_dict(text_budget=text_budget, fallback_ref=ref) for item in self.artifacts
            ],
            "warnings": [_fit_text(t, text_budget, ref)[0] for t in self.warnings],
            "resource_usage": self.resource_usage.to_dict(),
            "provenance": self.provenance.to_dict(),
            "attention": [_fit_text(t, text_budget, ref)[0] for t in self.attention],
        }


# --- markdown rendering (a generic walk, so it cannot omit a field) --------

# One level of markdown nesting. Two spaces, because a list continuation has to
# line up under its bullet's text for the renderer to read it as a child.
_INDENT = "  "


def _md_scalar(value: Any) -> str:
    # bool before the str() fallback: JSON spells them lowercase, and the
    # markdown rendering must match so the two stay comparable.
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    return text if text else "(none)"


def _fenced(text: str, pad: str) -> list[str]:
    """Fence ``text`` with a backtick run longer than any inside it.

    Captured output can contain code fences; a fixed three-backtick fence would
    let it break out of its own block and corrupt the surrounding document.
    """
    longest = run = 0
    for char in text:
        run = run + 1 if char == "`" else 0
        longest = max(longest, run)
    fence = pad + "`" * max(3, longest + 1)
    return [fence, *[pad + line for line in text.splitlines() or [""]], fence]


def _md_lines(value: Any, depth: int) -> list[str]:
    """Dispatch on the JSON shape — the three cases the payload can hold."""
    if isinstance(value, dict):
        return _md_mapping(value, depth)
    if isinstance(value, list):
        return _md_sequence(value, depth)
    return [f"{_INDENT * depth}{_md_scalar(value)}"]


def _md_mapping(mapping: dict[str, Any], depth: int) -> list[str]:
    """Every key, in payload order, nesting whatever is not a leaf."""
    pad = _INDENT * depth
    lines: list[str] = []
    for key, item in mapping.items():
        if isinstance(item, (dict, list)):
            lines.append(f"{pad}- {key}:")
            lines.extend(_md_lines(item, depth + 1))
        elif key == "excerpt" and item:
            lines.append(f"{pad}- {key}:")
            lines.extend(_fenced(str(item), _INDENT * (depth + 1)))
        else:
            lines.append(f"{pad}- {key}: {_md_scalar(item)}")
    return lines


def _md_sequence(items: list[Any], depth: int) -> list[str]:
    """Every element, with an empty list saying so rather than rendering blank."""
    pad = _INDENT * depth
    if not items:
        return [f"{pad}- (none)"]
    lines: list[str] = []
    for item in items:
        if isinstance(item, dict):
            # A record renders as one bullet headed by its first field,
            # with the rest nested. Generic: no key names are hardcoded.
            entries = list(item.items())
            first_key, first_value = entries[0]
            lines.append(f"{pad}- {first_key}: {_md_scalar(first_value)}")
            lines.extend(_md_lines(dict(entries[1:]), depth + 1))
        else:
            lines.append(f"{pad}- {_md_scalar(item)}")
    return lines


def _document(payload: dict[str, Any]) -> str:
    lines: list[str] = ["# headspace result", ""]
    for key, title in SECTION_TITLES.items():
        lines.append(f"## {title}")
        lines.append("")
        lines.extend(_md_lines(payload[key], 0))
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def _json_text(payload: dict[str, Any]) -> str:
    # Separators match headspace.cli._output.emit_result, so the size measured
    # here is the size the CLI actually writes.
    return json.dumps(payload, ensure_ascii=False)


# --- bounding -------------------------------------------------------------


def _nbytes(text: str) -> int:
    return len(text.encode("utf-8"))


def _check_bound(max_bytes: int) -> None:
    if max_bytes < MIN_RENDER_BYTES:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"result byte bound {max_bytes} is below the {MIN_RENDER_BYTES}-byte floor",
            remediation=(
                f"pass max_bytes >= {MIN_RENDER_BYTES}; below that there is no room for the "
                f"truncation marker that names the `{INSPECT_COMMAND}` retrieval path"
            ),
        )


def _clip(document: str, max_bytes: int, ref: str) -> str:
    marker = _package_marker(ref, max_bytes)
    room = max(max_bytes - _nbytes(marker), 0)
    return document.encode("utf-8")[:room].decode("utf-8", "ignore") + marker


def _fit(package: ResultPackage, max_bytes: int) -> tuple[dict[str, Any], str]:
    """Return the bounded structure and its markdown rendering.

    Both renderings are measured, and the search fits the larger of the two, so
    asking for JSON never sneaks past a bound calibrated on markdown.
    """
    _check_bound(max_bytes)

    payload = package.to_dict()
    document = _document(payload)
    if max(_nbytes(document), _nbytes(_json_text(payload))) <= max_bytes:
        return payload, document

    # Largest per-field allowance that still fits: better a full package with
    # short excerpts than a clipped document missing provenance and attention.
    best: tuple[dict[str, Any], str] | None = None
    low, high = MIN_EXCERPT_BYTES, max_bytes
    while low <= high:
        mid = (low + high) // 2
        candidate = package.to_dict(text_budget=mid)
        rendered = _document(candidate)
        if max(_nbytes(rendered), _nbytes(_json_text(candidate))) <= max_bytes:
            best = (candidate, rendered)
            low = mid + 1
        else:
            high = mid - 1
    if best is not None:
        return best

    # Last resort: even the floor allowance overflows. Clip markdown and say so.
    payload = package.to_dict(text_budget=MIN_EXCERPT_BYTES)
    return payload, _clip(_document(payload), max_bytes, package.provenance.reference)


# --- public renderers -----------------------------------------------------


def bounded_dict(package: ResultPackage, *, max_bytes: int = DEFAULT_MAX_BYTES) -> dict[str, Any]:
    """The bounded structure both renderers are derived from.

    Hand this to :func:`headspace.cli._output.emit_result` in JSON mode.
    """
    return _fit(package, max_bytes)[0]


def render_markdown(package: ResultPackage, *, max_bytes: int = DEFAULT_MAX_BYTES) -> str:
    """Render the default (markdown) view. Never exceeds ``max_bytes``."""
    return _fit(package, max_bytes)[1]


def render_json(package: ResultPackage, *, max_bytes: int = DEFAULT_MAX_BYTES) -> str:
    """Render the machine view: the same structure, serialised. Always valid JSON."""
    return _json_text(_fit(package, max_bytes)[0])
