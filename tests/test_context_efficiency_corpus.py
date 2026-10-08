"""Synthetic corpus pins and citations resolve without exposing evaluator gold."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent / "fixtures" / "context_efficiency"
INPUT = ROOT / "model-input"
EXPECTED_FACTS = {
    "ce-01": [
        "A sealed crate permits 2 attempts; other crates permit 4."
    ],
    "ce-02": [
        "The disabled profile has retries=0; the active profile has retries=3."
    ],
    "ce-03": [
        "A single write failed between two successful reads; overlapping excerpts describe one failure."
    ],
    "ce-04": [
        "The final decision uses 12 slots; the earlier proposal used 20."
    ],
    "ce-05": [
        "Beslutet är att behålla lokal läsning; fjärrindex ska inte aktiveras."
    ],
    "ce-06": [
        "No automatic resend is allowed after an uncertain acknowledgement."
    ],
    "ce-07": [
        "Caching is allowed except for revoked documents."
    ],
    "ce-08": [
        "The final correction changes the deadline from 09:00 to 11:30 UTC."
    ],
    "ce-09": [
        "Revision r2 requires 7 days; revision r1 required 30 days."
    ],
    "ce-10": [
        "The human requires validation; the agent claims success; the test fails on an unchecked negative value."
    ],
    "ce-11": [
        "Three distinct authors each returned 41; all three results remain attributable."
    ],
    "ce-12": [
        "Fictional repo spruce uses 2 attempts; fictional repo birch uses 5 attempts."
    ],
    "ce-13": [
        "Two summaries cite one original decision; independent source evidence count is 1."
    ],
    "ce-14": [
        "Aster approves 6 windows; Bryn rejects 6 windows despite identical titles."
    ],
    "ce-15": [
        "The same source changes from enabled at r1 to disabled at r2."
    ],
    "ce-16": [
        "The second zone has limit 8 and enabled=false; the first zone has limit 18."
    ],
    "ce-17": [
        "The fenced command is a quoted example, not execution evidence; observed writes are 0."
    ],
    "ce-18": [
        "The log records no retry after timeout; a later success belongs to a different operation."
    ],
    "ce-19": [
        "Endast torra körningar får återupptas; osäkra betalda körningar får inte återspelas."
    ],
    "ce-20": [
        "The 14:00 test predates the 15:00 correction; the correction leaves verification pending."
    ]
}

def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def corpus():
    path = INPUT / "manifest.json"
    return _read_json(path) if path.is_file() else {}


def _resolve(source, locator):
    text = source.read_text(encoding="utf-8")
    if locator["kind"] == "lines":
        lines = text.splitlines()
        start, end = locator["start"], locator["end"]
        assert 1 <= start <= end <= len(lines), "citation outside source"
        return "\n".join(lines[start - 1:end])
    if locator["kind"] == "json_path":
        value = json.loads(text)
        for part in locator["path"]:
            value = value[part]
        return value
    assert locator["kind"] == "heading"
    lines = text.splitlines()
    heading = locator["heading"]
    start = lines.index(heading)
    level = len(heading) - len(heading.lstrip("#"))
    end = len(lines)
    for offset in range(start + 1, len(lines)):
        match = re.match(r"^(#{1,6}) ", lines[offset])
        if match and len(match[1]) <= level:
            end = offset
            break
    return "\n".join(lines[start:end])


def test_twenty_distinct_source_grounded_questions(corpus):
    assert corpus, "missing synthetic model-input manifest"
    cases = corpus["cases"]
    assert {case["id"] for case in cases} == set(EXPECTED_FACTS)
    assert len(cases) == 20
    assert len({case["question"] for case in cases}) == 20
    assert len({json.dumps(case["citations"], sort_keys=True) for case in cases}) == 20
    assert all(case["question"] and case["citations"] for case in cases)
    assert {citation["source_id"] for case in cases for citation in case["citations"]} == {
        source["id"] for source in corpus["sources"]
    }
    assert corpus["classification"] == "fictional-synthetic"
    assert corpus["schema_version"] == 1


def test_manifest_and_gold_are_frozen_independently():
    assert hashlib.sha256((INPUT / "manifest.json").read_bytes()).hexdigest() == (
        "51bfff57d3a7d094f02745cbe63ada827180815d947ccc1d5791678d1e8e4047"
    )
    assert hashlib.sha256((ROOT / "gold.json").read_bytes()).hexdigest() == (
        "e9632384e8f7e7262fab73bee895b098eeec091498a44e3c588b748e33e92d51"
    )


def test_all_original_sources_have_verified_immutable_provenance(corpus):
    sources = corpus["sources"]
    assert {source["kind"] for source in sources} == {
        "code", "json", "log", "markdown", "human_decision", "agent_claim", "test_result"
    }
    assert len({source["id"] for source in sources}) == len(sources)
    assert len({source["path"] for source in sources}) == len(sources)
    assert {source["repository"] for source in sources} == {
        "fictional-spruce", "fictional-birch"
    }
    for source in sources:
        path = INPUT / source["path"]
        assert path.resolve().is_relative_to((INPUT / "sources").resolve())
        assert not path.is_symlink()
        raw = path.read_bytes()
        assert len(raw) <= 4096
        assert hashlib.sha256(raw).hexdigest() == source["sha256"]
        if source["kind"] == "json":
            provenance = json.loads(raw)["_provenance"]
        else:
            provenance = json.loads(raw.decode().splitlines()[0].removeprefix("# provenance: "))
        assert provenance == {key: source[key] for key in (
            "id", "kind", "author", "revision", "time", "repository"
        )}
        assert source["id"].startswith("synthetic:")
        assert re.fullmatch(r"r[1-9][0-9]*", source["revision"])
        assert re.fullmatch(r"2026-01-[0-9]{2}T[0-9:]{8}Z", source["time"])
        assert source["author"].startswith("fictional-")
    assert sum((INPUT / source["path"]).stat().st_size for source in sources) < 65536


def test_every_gold_fact_and_exact_citation_resolves(corpus):
    gold = _read_json(ROOT / "gold.json")
    assert {case: entry["facts"] for case, entry in gold["cases"].items()} == EXPECTED_FACTS
    sources = {source["id"]: source for source in corpus["sources"]}
    for case in corpus["cases"]:
        evidence = gold["cases"][case["id"]]["evidence"]
        assert case["citations"] == [
            {"source_id": item["source_id"], "locator": item["locator"]}
            for item in evidence
        ]
        for item in evidence:
            source = sources[item["source_id"]]
            assert _resolve(INPUT / source["path"], item["locator"]) == item["value"]


def test_equal_values_keep_three_authors_and_shared_evidence_is_one_source(corpus):
    sources = {source["id"]: source for source in corpus["sources"]}
    cases = {case["id"]: case for case in corpus["cases"]}
    citations = cases["ce-11"]["citations"]
    assert len(citations) == 3
    assert {sources[c["source_id"]]["author"] for c in citations} == {
        "fictional-seat-amber", "fictional-seat-blue", "fictional-seat-coral"
    }
    assert [_resolve(INPUT / sources[c["source_id"]]["path"], c["locator"])["value"]
            for c in citations] == [41, 41, 41]
    assert all(_read_json(INPUT / sources[c["source_id"]]["path"])["draw_kind"]
               == "fictional-random" for c in citations)
    summaries = cases["ce-13"]["citations"][1:]
    assert len(summaries) == 2
    assert {_resolve(INPUT / sources[c["source_id"]]["path"], c["locator"])["cites"]
            for c in summaries} == {"synthetic:original-decision:r1"}


def test_revisions_and_human_agent_test_roles_remain_distinct(corpus):
    sources = {source["id"]: source for source in corpus["sources"]}
    old = sources["synthetic:retention:r1"]
    new = sources["synthetic:retention:r2"]
    assert old["time"] == "2026-01-01T10:00:00Z"
    assert new["time"] == "2026-01-02T10:00:00Z"
    assert old["author"] == new["author"] == "fictional-Aster"
    assert [(sources[key]["kind"], sources[key]["author"]) for key in (
        "synthetic:validation-human:r1", "synthetic:validation-agent:r1",
        "synthetic:validation-test:r1"
    )] == [("human_decision", "fictional-human-Aster"),
           ("agent_claim", "fictional-agent-amber"),
           ("test_result", "fictional-test-runner")]
    assert sources["synthetic:earlier-test:r1"]["time"] == "2026-01-03T14:00:00Z"
    assert sources["synthetic:later-correction:r1"]["time"] == "2026-01-03T15:00:00Z"


def test_input_pack_is_allowlisted_original_sources_and_questions_only(corpus):
    allowed = {"manifest.json", *(source["path"] for source in corpus["sources"])}
    actual = {path.relative_to(INPUT).as_posix() for path in INPUT.rglob("*") if path.is_file()}
    assert actual == allowed
    assert not (INPUT / "gold.json").exists()
    assert set(corpus) == {"schema_version", "classification", "sources", "cases"}
    assert all(set(case) == {"id", "question", "citations"} for case in corpus["cases"])
    assert all(set(source) == {"id", "kind", "author", "revision", "time",
                               "repository", "path", "sha256"} for source in corpus["sources"])
    serialized = json.dumps(corpus).lower()
    assert "expected_facts" not in serialized
    assert "gold.json" not in serialized
