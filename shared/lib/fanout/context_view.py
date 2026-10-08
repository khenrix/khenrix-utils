"""Optional bounded views over caller-approved immutable artifact evidence.

The caller supplies records in existing-reader relevance order and refreshes the
active inventory/generation after lifecycle changes. This module neither
discovers sources nor persists caches, publishes evidence, or invokes models.
Only serialized_context is the counted injection surface. Native consumers must
preserve its derived-context marker; no global capture integration is enabled.

An approved metadata artifact uses EVIDENCE_SCHEMA, explicit scope/generation,
an optional actual CheckpointIdentity, source provenance with its original
ArtifactRef, the content ArtifactRef, approved locators, and uncertainty/dissent.
source.kind preserves the upstream source kind; kind is the content format used
for locator decoding. Metadata never duplicates complete original/final text.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass

from .artifacts import (
    ArtifactRef, ArtifactStore, canonical_json, _finite_json_number, _unique_json_object,
)
from .memory import CheckpointIdentity

EVIDENCE_SCHEMA = "fanout-context-evidence-v1"
VIEW_SCHEMA = "fanout-context-view-v1"
HANDLE_SCHEMA = "fanout-context-handle-v1"
KINDS = {"code", "log", "json", "markdown", "text"}
MAX_RECORDS = 256


class ContextViewError(ValueError):
    """Unapproved, unavailable, ambiguous or malformed context evidence."""


class ContextBudgetError(ContextViewError):
    """Complete required context or expansion needs a larger allocation."""


def _identifier(value, label, *, optional=False):
    if optional and value is None:
        return
    if (not isinstance(value, str) or not value or value.strip() != value
            or len(value.encode("utf-8")) > 512
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ContextViewError(f"invalid {label}")


def _digest(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _ref(value):
    if isinstance(value, ArtifactRef):
        ref = value
    elif isinstance(value, Mapping) and set(value) == {"path", "digest", "size"}:
        ref = ArtifactRef(**value)
    else:
        raise ContextViewError("invalid artifact reference")
    if (not isinstance(ref.path, str) or not _digest(ref.digest)
            or type(ref.size) is not int or ref.size < 0):
        raise ContextViewError("invalid artifact reference")
    ArtifactStore._parts(ref.path)
    return ref


@dataclass(frozen=True, slots=True)
class ContextReader:
    """Ephemeral read policy over one existing store and current active inventory.

    active_refs contains approved metadata ArtifactRefs, selected through the
    existing source/card reader. It is a snapshot: the caller must refresh it
    and source_generation after retirement or withdrawal.
    """

    artifacts: ArtifactStore
    run_id: str
    task_id: str
    source_generation: str
    active_refs: tuple[ArtifactRef, ...]

    def __post_init__(self):
        if not isinstance(self.artifacts, ArtifactStore):
            raise ContextViewError("reader requires an existing ArtifactStore")
        _identifier(self.run_id, "run scope")
        _identifier(self.task_id, "task scope")
        if not _digest(self.source_generation):
            raise ContextViewError("invalid source generation")
        refs = tuple(_ref(ref) for ref in self.active_refs)
        if len(refs) > MAX_RECORDS or len(set(refs)) != len(refs):
            raise ContextViewError("invalid active inventory")
        object.__setattr__(self, "active_refs", refs)

    @property
    def scope(self):
        return {"run_id": self.run_id, "task_id": self.task_id}

    @property
    def store_binding(self):
        descriptor = self.artifacts._root_fd_copy()
        try:
            info = os.fstat(descriptor)
            return {"device": info.st_dev, "inode": info.st_ino}
        finally:
            os.close(descriptor)


def is_derived_context(value):
    """Recognize the optional injection surface, never count it as a new source."""
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if isinstance(value, str):
        if re.search(r"<\s*/?\s*claude-mem-context(?=[\s/>])", value, re.I):
            return True
        try:
            value = json.loads(value)
        except ValueError:
            return False
    return isinstance(value, Mapping) and (
        value.get("schema_version") == VIEW_SCHEMA
        or value.get("capture_policy") == "exclude-derived-context"
    )


def _locator(value):
    if not isinstance(value, dict):
        raise ContextViewError("invalid locator")
    kind = value.get("kind")
    if kind == "lines" and set(value) == {"kind", "start", "end"}:
        if (type(value["start"]) is int and type(value["end"]) is int
                and 1 <= value["start"] <= value["end"]):
            return
    elif kind == "json_path" and set(value) == {"kind", "path"}:
        path = value["path"]
        if (isinstance(path, list) and 1 <= len(path) <= 32
                and all(isinstance(part, str) and bool(part)
                        or type(part) is int and part >= 0 for part in path)):
            return
    elif kind == "heading" and set(value) == {"kind", "heading"}:
        if isinstance(value["heading"], str) and re.fullmatch(r"#{1,6} [^\r\n]+", value["heading"]):
            return
    raise ContextViewError("invalid or unsupported locator; full verified expansion remains available")


class _JSONNumber(str):
    """An original numeric lexeme, distinct from a JSON string value."""


def _json_integer(value):
    int(value)  # Retain the standard decoder's integer-size refusal.
    return _JSONNumber(value)


def _json_float(value):
    _finite_json_number(value)
    return _JSONNumber(value)


def _exact_json(value):
    if isinstance(value, _JSONNumber):
        return str(value)
    if isinstance(value, dict):
        return "{" + ",".join(
            json.dumps(key, ensure_ascii=False) + ":" + _exact_json(value[key])
            for key in sorted(value)
        ) + "}"
    if isinstance(value, list):
        return "[" + ",".join(_exact_json(item) for item in value) + "]"
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _extract(raw, locator, kind):
    if locator is None:
        return raw
    _locator(locator)
    if locator["kind"] == "json_path":
        if kind != "json":
            raise ContextViewError("JSON locator requires JSON evidence")
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json_object,
                           parse_int=_json_integer, parse_float=_json_float,
                           parse_constant=_finite_json_number)
        try:
            for part in locator["path"]:
                if isinstance(value, dict) and isinstance(part, str):
                    value = value[part]
                elif isinstance(value, list) and type(part) is int:
                    value = value[part]
                else:
                    raise KeyError
        except (KeyError, IndexError) as error:
            raise ContextViewError("JSON locator does not resolve") from error
        return (_exact_json(value) + "\n").encode("utf-8")
    lines = raw.decode("utf-8").splitlines(keepends=True)
    if locator["kind"] == "lines":
        if locator["end"] > len(lines):
            raise ContextViewError("line locator does not resolve")
        return "".join(lines[locator["start"] - 1:locator["end"]]).encode("utf-8")
    if kind != "markdown":
        raise ContextViewError("heading locator requires Markdown evidence")
    headings = []
    fence = None
    for index, line in enumerate(lines):
        match = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)", line)
        if fence:
            if (match and match[1][0] == fence[0] and len(match[1]) >= len(fence)
                    and not match[2].strip()):
                fence = None
            continue
        if match:
            fence = match[1]
            continue
        match = re.match(r"^ {0,3}(#{1,6}) ", line)
        if match:
            headings.append((index, len(match[1]), line.lstrip(" ").rstrip("\r\n")))
    matches = [row for row in headings if row[2] == locator["heading"]]
    if len(matches) != 1:
        raise ContextViewError("heading locator is missing or ambiguous")
    start, level, _ = matches[0]
    end = next((index for index, depth, _ in headings if index > start and depth <= level), len(lines))
    return "".join(lines[start:end]).encode("utf-8")


def _load(reader, evidence):
    if not isinstance(reader, ContextReader):
        raise ContextViewError("expansion requires a current ContextReader")
    evidence = _ref(evidence)
    if evidence not in reader.active_refs:
        raise ContextViewError("evidence is not in the current active inventory")
    document = reader.artifacts.read_json(evidence)
    fields = {"schema_version", "classification", "scope", "source_generation",
              "identity", "source", "artifact", "kind", "final", "locators",
              "preview_locator", "uncertainty", "dissent"}
    if not isinstance(document, dict) or set(document) != fields:
        raise ContextViewError("invalid evidence descriptor")
    if document["schema_version"] != EVIDENCE_SCHEMA:
        raise ContextViewError("derived or unsupported evidence schema")
    if document["classification"] not in {"curated", "source-approved"}:
        raise ContextViewError("evidence is not curated or source-approved")
    if document["scope"] != reader.scope:
        raise ContextViewError("evidence scope does not match reader")
    if document["source_generation"] != reader.source_generation:
        raise ContextViewError("evidence generation does not match current reader")
    identity = document["identity"]
    if identity is not None:
        identity = CheckpointIdentity.from_dict(identity)
        if identity.run_id != reader.run_id or identity.task_id != reader.task_id:
            raise ContextViewError("checkpoint identity does not match scope")
    if type(document["final"]) is not bool or document["final"] and identity is None:
        raise ContextViewError("final requires an actual checkpoint identity")
    if document["kind"] not in KINDS:
        raise ContextViewError("unsupported evidence kind")
    source = document["source"]
    if not isinstance(source, dict) or set(source) != {"id", "revision", "kind", "author", "artifact"}:
        raise ContextViewError("invalid source descriptor")
    _identifier(source["id"], "source identity")
    _identifier(source["revision"], "source revision", optional=True)
    _identifier(source["author"], "source author", optional=True)
    _identifier(source["kind"], "source kind")
    for field in ("uncertainty", "dissent"):
        notes = document[field]
        if (not isinstance(notes, list) or len(notes) > 16
                or any(not isinstance(note, str) or not note.strip()
                       or len(note.encode("utf-8")) > 4096 for note in notes)):
            raise ContextViewError("invalid uncertainty or dissent")
    locators = document["locators"]
    if not isinstance(locators, list) or len(locators) > 64:
        raise ContextViewError("invalid approved locators")
    for locator in locators:
        _locator(locator)
    preview = document["preview_locator"]
    if preview is not None and preview not in locators:
        raise ContextViewError("preview locator is not approved")
    artifact = _ref(document["artifact"])
    source_ref = _ref(source["artifact"])
    raw = reader.artifacts.read_bytes(artifact)
    original = raw if source_ref == artifact else reader.artifacts.read_bytes(source_ref)
    if is_derived_context(raw) or is_derived_context(original):
        raise ContextViewError("derived context cannot become source evidence")
    raw.decode("utf-8")
    if document["final"] and not raw.strip():
        raise ContextViewError("final text is empty")
    return document, raw


def _handle(reader, evidence, document):
    return {"schema_version": HANDLE_SCHEMA, "store": reader.store_binding,
            "scope": reader.scope, "source_generation": reader.source_generation,
            "evidence": asdict(evidence), "identity": document["identity"],
            "source": document["source"]}


def expand_evidence(store, handle, *, locator=None, maximum_bytes=1024 * 1024):
    """Read an exact approved artifact/locator through the current store policy."""
    if not isinstance(store, ContextReader) or not isinstance(handle, dict):
        raise ContextViewError("expansion requires a current reader and validated handle")
    if handle.get("store") != store.store_binding:
        raise ContextViewError("handle belongs to another artifact store")
    if handle.get("scope") != store.scope:
        raise ContextViewError("handle belongs to another run/task scope")
    if handle.get("source_generation") != store.source_generation:
        raise ContextViewError("handle belongs to an old source generation")
    evidence = _ref(handle.get("evidence"))
    document, raw = _load(store, evidence)
    if handle != _handle(store, evidence, document):
        raise ContextViewError("handle does not match immutable evidence")
    if locator is not None and locator not in document["locators"]:
        raise ContextViewError("locator is not approved by evidence")
    if type(maximum_bytes) is not int or maximum_bytes < 0:
        raise ContextViewError("invalid expansion byte limit")
    expanded = _extract(raw, locator, document["kind"])
    if len(expanded) > maximum_bytes:
        raise ContextBudgetError("exact expansion needs a larger byte allocation")
    return expanded


def _serialize(document):
    # Escape markup inside JSON so evidence/notes cannot close the capture tag.
    encoded = canonical_json(document).decode("utf-8").replace("<", "\\u003c").replace(">", "\\u003e")
    return "<claude-mem-context>" + encoded + "</claude-mem-context>"


def build_context_view(records, *, query, token_budget, count_tokens, required_seats=()):
    """Retain full required finals, then fit caller-ranked complete previews.

    Records contain reader/evidence and an optional acknowledged_cursor. Optional
    records with an exact acknowledged immutable cursor are delta-suppressed;
    required finals are always retained. No title/session heuristic or ranker is
    used. The caller owns query selection through its existing source reader.
    """
    if not callable(count_tokens):
        raise ContextViewError("an exact supported counter is required")
    if type(token_budget) is not int or token_budget < 0:
        raise ContextViewError("invalid token budget")
    if not isinstance(query, str) or not query.strip() or len(query.encode("utf-8")) > 2048:
        raise ContextViewError("invalid context query")
    records = tuple(records)
    required = tuple(required_seats)
    if len(records) > MAX_RECORDS or len(set(required)) != len(required):
        raise ContextViewError("context collection or required seats are invalid")
    for seat in required:
        _identifier(seat, "required seat")
    entries = []
    scope = None
    reader_bindings = {}
    source_revisions = {}
    cursors = set()
    for record in records:
        if (not isinstance(record, dict) or not {"reader", "evidence"} <= set(record)
                or set(record) - {"reader", "evidence", "acknowledged_cursor"}):
            raise ContextViewError("invalid context record")
        reader, evidence = record["reader"], _ref(record["evidence"])
        document, raw = _load(reader, evidence)
        if scope is None:
            scope = reader.scope
        elif scope != reader.scope:
            raise ContextViewError("context records mix run/task scopes")
        store_key = tuple(reader.store_binding.values())
        if store_key in reader_bindings and reader_bindings[store_key] is not reader:
            raise ContextViewError("one shared current reader is required per store scope")
        reader_bindings[store_key] = reader
        source = document["source"]
        key = (source["id"], source["revision"])
        binding = (source["author"], source["kind"], source["artifact"]["digest"])
        if key in source_revisions and source_revisions[key] != binding:
            raise ContextViewError("source revision conflicts with verified evidence")
        source_revisions[key] = binding
        handle = _handle(reader, evidence, document)
        cursor = hashlib.sha256(canonical_json(handle)).hexdigest()
        if cursor in cursors:
            raise ContextViewError("duplicate immutable evidence record")
        cursors.add(cursor)
        ack = record.get("acknowledged_cursor")
        if ack is not None and not _digest(ack):
            raise ContextViewError("invalid acknowledged cursor")
        entries.append((reader, evidence, document, raw, handle, cursor, ack == cursor))

    finals = {}
    for entry in entries:
        document = entry[2]
        if document["final"]:
            seat = document["identity"]["seat_id"]
            if seat in finals:
                raise ContextViewError("ambiguous final seat result; select explicitly")
            finals[seat] = entry
    missing = set(required) - set(finals)
    if missing:
        raise ContextViewError("missing required final seat result")

    payload = {"schema_version": VIEW_SCHEMA, "capture_policy": "exclude-derived-context",
               "scope": scope, "query": query, "seat_results": {}, "previews": [],
               "omitted": [], "uncertainties": [], "dissent": [], "cursors": {},
               "independent_source_count": 0}
    optional = []
    sources = []
    for entry in entries:
        reader, evidence, document, raw, handle, cursor, acknowledged = entry
        seat = document["identity"]["seat_id"] if document["identity"] else None
        mandatory = document["final"] and seat in required
        if acknowledged and not mandatory:
            continue
        source = document["source"]
        original = _ref(source["artifact"])
        original_key = (ArtifactStore._parts(original.path), original.digest, original.size)
        # Revisions share an origin; aliases share a verified original authority.
        group = {("source", source["id"]),
                 ("original", tuple(reader.store_binding.values()), original_key)}
        for existing in [item for item in sources if item & group]:
            group.update(existing)
            sources.remove(existing)
        sources.append(group)
        payload["cursors"][cursor] = cursor
        descriptor = {"handle": handle, "locator": document["preview_locator"]}
        for field, output in (("uncertainty", "uncertainties"), ("dissent", "dissent")):
            payload[output].extend({"text": note, "handle": handle} for note in document[field])
        if mandatory:
            payload["seat_results"][seat] = {"identity": document["identity"],
                "text": raw.decode("utf-8"), **descriptor}
        else:
            payload["omitted"].append(descriptor)
            optional.append(entry)
    payload["independent_source_count"] = len(sources)

    def measured(value):
        serialized = _serialize(value)
        count = count_tokens(serialized)
        if type(count) is not int or count < 0:
            raise ContextViewError("counter returned an invalid exact token count")
        return serialized, count

    serialized, tokens = measured(payload)
    if tokens > token_budget:
        raise ContextBudgetError("complete required context needs a larger token allocation")
    for reader, evidence, document, raw, handle, cursor, _ in optional:
        descriptor = {"handle": handle, "locator": document["preview_locator"]}
        text = _extract(raw, document["preview_locator"], document["kind"]).decode("utf-8")
        preview = {**descriptor, "identity": document["identity"], "text": text}
        position = payload["omitted"].index(descriptor)
        payload["omitted"].pop(position)
        payload["previews"].append(preview)
        candidate, count = measured(payload)
        if count <= token_budget:
            serialized, tokens = candidate, count
        else:
            payload["previews"].pop()
            payload["omitted"].insert(position, descriptor)
    return {**payload, "serialized_context": serialized, "token_count": tokens}
