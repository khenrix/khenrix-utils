"""Optional context views retain exact evidence and every expected final seat."""
from __future__ import annotations

import dataclasses
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FANOUT_ROOT = ROOT / "shared" / "lib" / "fanout"
SPEC = importlib.util.spec_from_file_location(
    "_fanout_context_contracts", FANOUT_ROOT / "__init__.py",
    submodule_search_locations=[str(FANOUT_ROOT)],
)
assert SPEC and SPEC.loader
fanout = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fanout
SPEC.loader.exec_module(fanout)
GENERATION = "a" * 64


@pytest.fixture(scope="module")
def context():
    path = FANOUT_ROOT / "context_view.py"
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location(f"{SPEC.name}.context_view", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _identity(seat="claude", **changes):
    values = dict(run_id="run-1", task_id="task-1", seat_id=seat, attempt=1,
                  round=1, provider_session_id=f"session-{seat}")
    values.update(changes)
    return fanout.CheckpointIdentity(**values)


def _publish(store, name, text, *, source_ref=None, source_id="fictional:shared-doc",
             revision="r1", source_author="fictional-human", identity=None, final=False,
             kind="markdown", preview_locator=None, locators=(), uncertainty=(), dissent=(),
             generation=GENERATION, scope=None, classification="curated", source_kind=None):
    artifact = store.write_bytes(f"evidence/{name}.txt", text.encode("utf-8"))
    source_kind = source_kind or ("markdown" if source_ref is not None else kind)
    source_ref = source_ref or artifact
    document = {
        "schema_version": "fanout-context-evidence-v1",
        "classification": classification,
        "scope": scope or {"run_id": "run-1", "task_id": "task-1"},
        "source_generation": generation,
        "identity": identity.to_dict() if identity else None,
        "source": {"id": source_id, "revision": revision, "author": source_author,
                   "kind": source_kind, "artifact": dataclasses.asdict(source_ref)},
        "artifact": dataclasses.asdict(artifact),
        "kind": kind,
        "final": final,
        "locators": list(locators),
        "preview_locator": preview_locator,
        "uncertainty": list(uncertainty),
        "dissent": list(dissent),
    }
    envelope = store.write_json(f"evidence/{name}.json", document)
    return envelope, artifact


def _reader(context, store, refs, generation=GENERATION, **changes):
    values = dict(artifacts=store, run_id="run-1", task_id="task-1",
                  source_generation=generation, active_refs=tuple(refs))
    values.update(changes)
    return context.ContextReader(**values)


def _record(reader, ref, acknowledged_cursor=None):
    return {"reader": reader, "evidence": ref, "acknowledged_cursor": acknowledged_cursor}


def _counter(text):
    # A deterministic test counter, never a claimed route-token measurement.
    return len(text.encode("utf-8"))


def test_three_equal_finals_keep_attribution_and_one_verified_original_source(tmp_path, context):
    assert context is not None, "optional context-view seam is missing"
    store = fanout.ArtifactStore(tmp_path)
    common = store.write_bytes("sources/common.md", b"Human decision: retain local reads.\n")
    refs = [_publish(store, seat, "41\n", source_ref=common, identity=_identity(seat),
                     final=True, kind="text")[0] for seat in ("claude", "codex", "agy")]
    reader = _reader(context, store, refs)
    view = context.build_context_view([_record(reader, ref) for ref in refs],
        query="result", token_budget=20000, count_tokens=_counter,
        required_seats=("claude", "codex", "agy"))
    assert set(view["seat_results"]) == {"claude", "codex", "agy"}
    assert {result["text"] for result in view["seat_results"].values()} == {"41\n"}
    assert {result["identity"]["provider_session_id"]
            for result in view["seat_results"].values()} == {
                "session-claude", "session-codex", "session-agy"}
    assert view["independent_source_count"] == 1
    assert _counter(view["serialized_context"]) <= 20000
    assert view["capture_policy"] == "exclude-derived-context"


def test_budget_counts_exact_wrapper_metadata_and_full_mandatory_finals(tmp_path, context):
    assert context is not None
    store = fanout.ArtifactStore(tmp_path)
    ref, _ = _publish(store, "required", "Do not replay uncertain attempts.\n",
                      final=True, identity=_identity(), kind="text")
    reader = _reader(context, store, [ref])
    records = [_record(reader, ref)]
    wide = context.build_context_view(records, query="result", token_budget=20000,
                                      count_tokens=_counter, required_seats=("claude",))
    exact = _counter(wide["serialized_context"])
    assert exact > len("Do not replay uncertain attempts.\n")
    assert context.build_context_view(records, query="result", token_budget=exact,
        count_tokens=_counter, required_seats=("claude",))["serialized_context"] == wide["serialized_context"]
    with pytest.raises(context.ContextBudgetError, match="larger"):
        context.build_context_view(records, query="result", token_budget=exact - 1,
                                   count_tokens=_counter, required_seats=("claude",))


def test_missing_or_ambiguous_required_final_refuses_without_latest_guess(tmp_path, context):
    assert context is not None
    store = fanout.ArtifactStore(tmp_path)
    first, _ = _publish(store, "first", "yes", final=True, identity=_identity())
    second, _ = _publish(store, "second", "no", final=True,
                         identity=_identity(round=2), source_id="fictional:second")
    reader = _reader(context, store, [first, second])
    with pytest.raises(context.ContextViewError, match="missing"):
        context.build_context_view([_record(reader, first)], query="result", token_budget=20000,
            count_tokens=_counter, required_seats=("claude", "codex"))
    with pytest.raises(context.ContextViewError, match="ambiguous"):
        context.build_context_view([_record(reader, first), _record(reader, second)],
            query="result", token_budget=20000, count_tokens=_counter, required_seats=("claude",))


def test_omitted_late_fact_expands_exactly_and_uncertainty_dissent_survive(tmp_path, context):
    assert context is not None
    store = fanout.ArtifactStore(tmp_path)
    text = "# Decision\n" + "ordinary context\n" * 100 + "Do not activate remote indexing.\n"
    line = {"kind": "lines", "start": 102, "end": 102}
    ref, artifact = _publish(store, "late", text, locators=[line],
        uncertainty=["Owner verification is pending."], dissent=["Bryn rejects activation."])
    reader = _reader(context, store, [ref])
    records = [_record(reader, ref)]
    wide = context.build_context_view(records, query="decision", token_budget=20000, count_tokens=_counter)
    budget = _counter(wide["serialized_context"]) - len(text.encode())
    small = context.build_context_view(records, query="decision", token_budget=budget, count_tokens=_counter)
    assert small["previews"] == []
    assert len(small["omitted"]) == 1
    assert small["uncertainties"][0]["text"] == "Owner verification is pending."
    assert small["dissent"][0]["text"] == "Bryn rejects activation."
    handle = small["omitted"][0]["handle"]
    assert context.expand_evidence(reader, handle, locator=line) == b"Do not activate remote indexing.\n"
    assert context.expand_evidence(reader, handle, maximum_bytes=len(text.encode())) == store.read_bytes(artifact)
    with pytest.raises(context.ContextBudgetError):
        context.expand_evidence(reader, handle, maximum_bytes=10)


@pytest.mark.parametrize(("kind", "text", "locator", "expected"), [
    ("code", "def value():\n    return 2\n", {"kind": "lines", "start": 1, "end": 2},
     b"def value():\n    return 2\n"),
    ("log", "read ok\nwrite failed\nread ok\n", {"kind": "lines", "start": 2, "end": 3},
     b"write failed\nread ok\n"),
    ("json", '{"zones":[{"limit":18},{"limit":8,"enabled":false}]}',
     {"kind": "json_path", "path": ["zones", 1]}, b'{"enabled":false,"limit":8}\n'),
    ("markdown", "# Notes\n## Decision\nUse 12 slots.\n~~~md\n## quoted\n~~~\n## Next\nIgnore.\n",
     {"kind": "heading", "heading": "## Decision"},
     b"## Decision\nUse 12 slots.\n~~~md\n## quoted\n~~~\n"),
])
def test_structure_and_locators_survive_preview_and_expansion(tmp_path, context, kind, text, locator, expected):
    assert context is not None
    store = fanout.ArtifactStore(tmp_path)
    ref, _ = _publish(store, "structured", text, kind=kind, preview_locator=locator, locators=[locator])
    reader = _reader(context, store, [ref])
    view = context.build_context_view([_record(reader, ref)], query="decision", token_budget=20000,
                                      count_tokens=_counter)
    preview = view["previews"][0]
    assert preview["locator"] == locator
    assert preview["text"].encode() == expected
    assert context.expand_evidence(reader, preview["handle"], locator=locator) == expected


def test_wrong_hash_locator_and_cross_run_store_refuse(tmp_path, context):
    assert context is not None
    store = fanout.ArtifactStore(tmp_path / "first")
    allowed = {"kind": "lines", "start": 1, "end": 1}
    ref, artifact = _publish(store, "evidence", "Do not resend.\n", locators=[allowed])
    reader = _reader(context, store, [ref])
    handle = context.build_context_view([_record(reader, ref)], query="result", token_budget=20000,
        count_tokens=_counter)["previews"][0]["handle"]
    with pytest.raises(context.ContextViewError, match="locator"):
        context.expand_evidence(reader, handle, locator={"kind": "lines", "start": 1, "end": 2})
    other = fanout.ArtifactStore(tmp_path / "other")
    other.write_bytes(artifact.path, store.read_bytes(artifact))
    other.write_bytes(ref.path, store.read_bytes(ref))
    other_reader = _reader(context, other, [ref])
    with pytest.raises(context.ContextViewError, match="store"):
        context.expand_evidence(other_reader, handle)
    tampered = json.loads(json.dumps(handle))
    tampered["evidence"]["digest"] = "f" * 64
    with pytest.raises((context.ContextViewError, fanout.ArtifactIntegrityError)):
        context.expand_evidence(reader, tampered)
    (store.root / artifact.path).write_bytes(b"Altered artifact.\n")
    with pytest.raises(fanout.ArtifactIntegrityError):
        context.expand_evidence(reader, handle)


def test_same_title_different_identity_and_revision_never_alias_or_associate(tmp_path, context):
    assert context is not None
    store = fanout.ArtifactStore(tmp_path)
    one, _ = _publish(store, "one", "# Same title\nNej.\n", source_id="fictional:one")
    two, _ = _publish(store, "two", "# Same title\nJa.\n", source_id="fictional:two",
                     source_author="fictional-Bryn")
    three, _ = _publish(store, "three", "# Same title\nSenare nej.\n",
                       source_id="fictional:one", revision="r2")
    reader = _reader(context, store, [one, two, three])
    view = context.build_context_view([_record(reader, ref) for ref in (one, two, three)],
                                     query="same", token_budget=20000, count_tokens=_counter)
    assert [preview["text"] for preview in view["previews"]] == [
        "# Same title\nNej.\n", "# Same title\nJa.\n", "# Same title\nSenare nej.\n"]
    assert len(set(view["cursors"].values())) == 3
    assert view["independent_source_count"] == 2
    wrong_reader = _reader(context, store, [one], run_id="prior-project")
    with pytest.raises(context.ContextViewError, match="scope"):
        context.build_context_view([_record(wrong_reader, one)], query="same", token_budget=20000,
                                   count_tokens=_counter)


def test_generation_withdrawal_and_exact_acknowledged_delta(tmp_path, context):
    assert context is not None
    store = fanout.ArtifactStore(tmp_path)
    ref, _ = _publish(store, "one", "Keep local reads.\n")
    reader = _reader(context, store, [ref])
    first = context.build_context_view([_record(reader, ref)], query="local", token_budget=20000,
                                      count_tokens=_counter)
    cursor = next(iter(first["cursors"].values()))
    delta = context.build_context_view([_record(reader, ref, cursor)], query="local",
                                      token_budget=20000, count_tokens=_counter)
    assert delta["previews"] == [] and delta["omitted"] == []
    assert "Keep local reads." not in delta["serialized_context"]
    unchanged = context.build_context_view([_record(reader, ref)], query="different query",
        token_budget=20000, count_tokens=lambda text: 1)
    assert unchanged["cursors"] == first["cursors"]
    handle = first["previews"][0]["handle"]
    withdrawn = _reader(context, store, [])
    with pytest.raises(context.ContextViewError, match="active"):
        context.expand_evidence(withdrawn, handle)
    changed = _reader(context, store, [ref], generation="b" * 64)
    with pytest.raises(context.ContextViewError, match="generation"):
        context.expand_evidence(changed, handle)


def test_recovery_reopens_published_artifacts_without_ack_or_new_publication(tmp_path, context):
    assert context is not None
    store = fanout.ArtifactStore(tmp_path)
    ref, _ = _publish(store, "one", "Verified source remains available.\n")
    reader = _reader(context, store, [ref])
    first = context.build_context_view([_record(reader, ref)], query="result", token_budget=20000,
                                      count_tokens=_counter)
    before = {p.relative_to(tmp_path).as_posix(): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    store.close()
    reopened = fanout.ArtifactStore.open_existing(tmp_path)
    recovered = _reader(context, reopened, [ref])
    again = context.build_context_view([_record(recovered, ref)], query="result", token_budget=20000,
                                      count_tokens=_counter)
    assert again == first
    assert context.expand_evidence(recovered, first["previews"][0]["handle"]) == b"Verified source remains available.\n"
    after = {p.relative_to(tmp_path).as_posix(): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before


def test_record_without_optional_ack_and_caller_order_fit_whole_previews(tmp_path, context):
    assert context is not None
    store = fanout.ArtifactStore(tmp_path)
    first, _ = _publish(store, "preferred", "Preferred source.\n", source_id="fictional:preferred")
    second, _ = _publish(store, "other", "Another complete source.\n", source_id="fictional:other")
    reader = _reader(context, store, [first, second])
    view = context.build_context_view([{"reader": reader, "evidence": first},
                                      {"reader": reader, "evidence": second}],
                                     query="caller-ranked", token_budget=20000, count_tokens=_counter)
    assert [row["text"] for row in view["previews"]] == [
        "Preferred source.\n", "Another complete source.\n"]


def test_native_source_kind_is_preserved_separately_from_content_format(tmp_path, context):
    assert context is not None
    store = fanout.ArtifactStore(tmp_path)
    ref, _ = _publish(store, "curated", "# Curated decision\nDo not resend.\n",
                      kind="markdown", source_kind="github", source_author=None)
    reader = _reader(context, store, [ref])
    view = context.build_context_view([_record(reader, ref)], query="decision", token_budget=20000,
                                      count_tokens=_counter)
    assert view["previews"][0]["handle"]["source"]["kind"] == "github"
    assert view["previews"][0]["handle"]["source"]["author"] is None


def test_derived_view_is_marked_and_existing_capture_strips_injection(tmp_path, context):
    assert context is not None
    store = fanout.ArtifactStore(tmp_path)
    ref, _ = _publish(store, "original", "Original curated fact.\n")
    reader = _reader(context, store, [ref])
    view = context.build_context_view([_record(reader, ref)], query="fact", token_budget=20000,
                                      count_tokens=_counter)
    assert view["schema_version"] == "fanout-context-view-v1"
    text = view["serialized_context"]
    assert text.startswith("<claude-mem-context>") and text.endswith("</claude-mem-context>")
    ingress_spec = importlib.util.spec_from_file_location(
        "_context_ingress", ROOT / "components/memory/memory_ingress.py")
    ingress = importlib.util.module_from_spec(ingress_spec)
    ingress_spec.loader.exec_module(ingress)
    hook = json.dumps({"session_id": "synthetic-session",
                       "prompt": f"Human instruction. {text}"}).encode()
    with ingress.prepare_hook(io.BytesIO(hook), "claude-code", "session-init") as cleaned:
        assert json.loads(cleaned)["prompt"] == "Human instruction. "
    derived, _ = _publish(store, "derived", text, source_id="fictional:derived")
    with pytest.raises(context.ContextViewError, match="derived"):
        context.build_context_view([_record(_reader(context, store, [derived]), derived)],
                                   query="fact", token_budget=20000, count_tokens=_counter)
    raw, _ = _publish(store, "raw", "Raw upstream body.", classification="raw-upstream",
                      source_id="fictional:raw")
    with pytest.raises(context.ContextViewError, match="approved"):
        context.build_context_view([_record(_reader(context, store, [raw]), raw)],
                                   query="fact", token_budget=20000, count_tokens=_counter)


@pytest.mark.parametrize("counter", [None, lambda text: True, lambda text: -1, lambda text: 2.5])
def test_missing_or_invalid_exact_counter_refuses(tmp_path, context, counter):
    assert context is not None
    store = fanout.ArtifactStore(tmp_path)
    ref, _ = _publish(store, "one", "Original fact.")
    reader = _reader(context, store, [ref])
    with pytest.raises(context.ContextViewError, match="counter"):
        context.build_context_view([_record(reader, ref)], query="fact", token_budget=20000,
                                   count_tokens=counter)


def test_aliases_of_one_original_count_once_without_collapsing_equal_seat_finals(tmp_path, context):
    assert context is not None
    store = fanout.ArtifactStore(tmp_path)
    original = store.write_bytes("sources/one.md", b"One original decision.\n")
    seats = ("claude", "codex", "agy")
    refs = [_publish(store, seat, "41\n", source_ref=original, source_id=f"alias:{seat}",
                     identity=_identity(seat), final=True, kind="text")[0] for seat in seats]
    reader = _reader(context, store, refs)
    view = context.build_context_view([_record(reader, ref) for ref in refs],
        query="result", token_budget=20000, count_tokens=_counter, required_seats=seats)
    assert set(view["seat_results"]) == {"claude", "codex", "agy"}
    assert [row["text"] for row in view["seat_results"].values()] == ["41\n", "41\n", "41\n"]
    assert view["independent_source_count"] == 1
    assert {row["handle"]["source"]["id"] for row in view["seat_results"].values()} == {
        "alias:claude", "alias:codex", "alias:agy"}


@pytest.mark.parametrize("alias", [
    "sources//common.txt", "sources/./common.txt", "sources/common.txt/",
])
def test_equivalent_original_paths_count_once_and_preserve_exact_handles(tmp_path, context, alias):
    assert context is not None
    store = fanout.ArtifactStore(tmp_path)
    original = store.write_bytes("sources/common.txt", b"One original decision.\n")
    aliased = dataclasses.replace(original, path=alias)
    assert store.read_bytes(aliased) == b"One original decision.\n"
    seats = ("claude", "codex", "agy")
    originals = {"claude": original, "codex": aliased, "agy": aliased}
    refs = [_publish(store, seat, "41\n", source_ref=originals[seat],
                     source_id=f"alias:{seat}", identity=_identity(seat),
                     final=True, kind="text")[0] for seat in seats]
    reader = _reader(context, store, refs)
    view = context.build_context_view([_record(reader, ref) for ref in refs],
        query="result", token_budget=20000, count_tokens=_counter, required_seats=seats)
    assert view["independent_source_count"] == 1
    assert set(view["seat_results"]) == {"claude", "codex", "agy"}
    for seat in seats:
        result = view["seat_results"][seat]
        assert result["text"] == "41\n"
        assert result["identity"] == _identity(seat).to_dict()
        assert result["handle"]["source"]["id"] == f"alias:{seat}"
        assert result["handle"]["source"]["artifact"] == dataclasses.asdict(originals[seat])
        assert context.expand_evidence(reader, result["handle"]) == b"41\n"


def test_distinct_authored_original_refs_with_equal_bytes_keep_three_sources(tmp_path, context):
    assert context is not None
    store = fanout.ArtifactStore(tmp_path)
    refs = []
    for seat in ("claude", "codex", "agy"):
        original = store.write_bytes(f"sources/{seat}.md", b"41\n")
        refs.append(_publish(store, seat, "41\n", source_ref=original,
            source_id=f"original:{seat}", source_author=f"fictional-{seat}",
            identity=_identity(seat), final=True, kind="text")[0])
    reader = _reader(context, store, refs)
    view = context.build_context_view([_record(reader, ref) for ref in refs],
        query="result", token_budget=20000, count_tokens=_counter,
        required_seats=("claude", "codex", "agy"))
    assert view["independent_source_count"] == 3
    assert set(view["seat_results"]) == {"claude", "codex", "agy"}
    assert {row["handle"]["source"]["author"] for row in view["seat_results"].values()} == {
        "fictional-claude", "fictional-codex", "fictional-agy"}


@pytest.mark.parametrize(("literal", "expected"), [
    ("1.0000000000000001", b"1.0000000000000001\n"),
    ("0.10", b"0.10\n"),
    ("1e+02", b"1e+02\n"),
    ("1e-4000", b"1e-4000\n"),
    ("-0", b"-0\n"),
    ("9007199254740993", b"9007199254740993\n"),
    ('"1.0"', b'"1.0"\n'),
])
def test_json_path_keeps_numeric_lexemes_and_distinguishes_strings(tmp_path, context, literal, expected):
    assert context is not None
    store = fanout.ArtifactStore(tmp_path)
    locator = {"kind": "json_path", "path": ["amount"]}
    raw = '{"amount":' + literal + ',"enabled":false}'
    ref, _ = _publish(store, "number", raw, kind="json", locators=[locator], preview_locator=locator)
    reader = _reader(context, store, [ref])
    view = context.build_context_view([_record(reader, ref)], query="amount",
                                      token_budget=20000, count_tokens=_counter)
    preview = view["previews"][0]
    assert preview["text"].encode() == expected
    assert context.expand_evidence(reader, preview["handle"], locator=locator) == expected
    assert context.expand_evidence(reader, preview["handle"]) == raw.encode()


def test_json_path_keeps_numbers_inside_selected_objects_and_arrays(tmp_path, context):
    assert context is not None
    store = fanout.ArtifactStore(tmp_path)
    locator = {"kind": "json_path", "path": ["selected"]}
    raw = '{"selected":{"z":[1.0000000000000001,0.10,-0],"a":"0.10","enabled":false}}'
    ref, _ = _publish(store, "nested", raw, kind="json", locators=[locator], preview_locator=locator)
    reader = _reader(context, store, [ref])
    view = context.build_context_view([_record(reader, ref)], query="selected",
                                      token_budget=20000, count_tokens=_counter)
    expected = b'{"a":"0.10","enabled":false,"z":[1.0000000000000001,0.10,-0]}\n'
    assert view["previews"][0]["text"].encode() == expected
    assert context.expand_evidence(reader, view["previews"][0]["handle"], locator=locator) == expected


@pytest.mark.parametrize("raw", [
    '{"amount":1,"amount":2}', '{"amount":NaN}', '{"amount":1e400}', '{"amount":1,}',
])
def test_targeted_json_refuses_duplicate_keys_nonfinite_overflow_and_bad_json(tmp_path, context, raw):
    assert context is not None
    store = fanout.ArtifactStore(tmp_path)
    locator = {"kind": "json_path", "path": ["amount"]}
    ref, _ = _publish(store, "bad", raw, kind="json", locators=[locator])
    reader = _reader(context, store, [ref])
    view = context.build_context_view([_record(reader, ref)], query="amount",
                                      token_budget=20000, count_tokens=_counter)
    handle = view["previews"][0]["handle"]
    assert context.expand_evidence(reader, handle) == raw.encode()
    with pytest.raises(ValueError):
        context.expand_evidence(reader, handle, locator=locator)


@pytest.mark.parametrize("indent", [" ", "  ", "   "])
def test_markdown_section_stops_before_valid_indented_next_heading(tmp_path, context, indent):
    assert context is not None
    store = fanout.ArtifactStore(tmp_path)
    locator = {"kind": "heading", "heading": "## Decision"}
    raw = "# Root\n## Decision\nDo not deploy.\n" + indent + "## Next\nUnrelated scope.\n"
    ref, _ = _publish(store, "indented", raw, kind="markdown", locators=[locator], preview_locator=locator)
    reader = _reader(context, store, [ref])
    view = context.build_context_view([_record(reader, ref)], query="decision",
                                      token_budget=20000, count_tokens=_counter)
    expected = b"## Decision\nDo not deploy.\n"
    assert view["previews"][0]["text"].encode() == expected
    assert context.expand_evidence(reader, view["previews"][0]["handle"], locator=locator) == expected


def test_indented_selected_heading_preserves_bytes_and_ignores_four_space_heading(tmp_path, context):
    assert context is not None
    store = fanout.ArtifactStore(tmp_path)
    locator = {"kind": "heading", "heading": "## Decision"}
    raw = "# Root\n  ## Decision\nKeep this section.\n    ## Literal\nKeep this line.\n   ## Next\nExclude.\n"
    ref, _ = _publish(store, "selected", raw, kind="markdown", locators=[locator], preview_locator=locator)
    reader = _reader(context, store, [ref])
    view = context.build_context_view([_record(reader, ref)], query="decision",
                                      token_budget=20000, count_tokens=_counter)
    assert view["previews"][0]["text"] == (
        "  ## Decision\nKeep this section.\n    ## Literal\nKeep this line.\n")


def test_duplicate_heading_after_indentation_normalization_refuses(tmp_path, context):
    assert context is not None
    store = fanout.ArtifactStore(tmp_path)
    locator = {"kind": "heading", "heading": "## Decision"}
    raw = "# Root\n## Decision\nFirst.\n  ## Decision\nSecond.\n"
    ref, _ = _publish(store, "duplicate", raw, kind="markdown", locators=[locator])
    reader = _reader(context, store, [ref])
    view = context.build_context_view([_record(reader, ref)], query="decision",
                                      token_budget=20000, count_tokens=_counter)
    with pytest.raises(context.ContextViewError, match="ambiguous"):
        context.expand_evidence(reader, view["previews"][0]["handle"], locator=locator)
