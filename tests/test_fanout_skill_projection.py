"""Optional prompt presentation never changes complete skill admission."""
import base64
import dataclasses
import importlib
import json
import os

import pytest

import test_fanout_collaboration as cases


def _fixture(tmp_path):
    root = tmp_path / "source"
    skill = root / "reviewed"
    (skill / "references").mkdir(parents=True)
    (skill / "scripts").mkdir()
    (skill / "SKILL.md").write_text("---\nname: reviewed\nmetadata:\n  version: 1.1.1\n---\nRead `references/RULES.TXT` and run `scripts/check.py`.\n")
    (skill / "references/RULES.TXT").write_text("Keep exact arithmetic.\n")
    (skill / "scripts/check.py").write_text("print('required')\n")
    (skill / "helper.json").write_text('{"packaging":"HELPER_BYTES"}\n')
    resolver = cases.fanout.SkillResolver((cases.fanout.SkillRoot("source", root, 0),))
    admission = resolver.admit(("reviewed",), task_id="work", seat_id="seat-codex",
                               provider="codex", session_id="session")
    admission = resolver.stage(admission, tmp_path / "stage")
    admission = resolver.verify_engine_delivery(admission, tuple(
        cases.fanout.SkillLoadEvidence.engine(skill, admission) for skill in admission.skills))
    bundle = cases.canonical_json({"schema_version": "fanout-seat-skill-bundle-v1",
                                    "skills": admission.manifest_dict()["skills"]})
    with cases.ArtifactStore(tmp_path / "artifacts") as store:
        seat = cases.SeatAssignment.from_admission(admission, skill_bundle=bundle, artifacts=store)
    return resolver, seat


def _policy(seat, **changes):
    module = importlib.import_module(cases.fanout.__name__ + ".skill_projection")
    review = {"tree_hash": seat.admission.skills[0].tree_hash,
              "inline_files": ["SKILL.md", "references/RULES.TXT", "scripts/check.py"],
              "required_resources": ["scripts/check.py"]}
    review.update(changes)
    return module.SkillProjection.from_bytes(cases.canonical_json({
        "schema_version": "fanout-skill-projection-v1", "reviews": [review]}))


def test_default_full_delivery_is_exact_and_opt_in_keeps_required_native_resources(tmp_path):
    _, seat = _fixture(tmp_path)
    original = cases.CollaborationCoordinator._staged_skill_context(seat)
    full = json.loads(original)
    assert set(full) == {"delivery_evidence_sha256", "engine_evidence", "files",
                         "schema_version", "staged_manifest_sha256"}
    assert [item["path"] for item in full["files"]] == [
        "reviewed/SKILL.md", "reviewed/helper.json", "reviewed/references/RULES.TXT", "reviewed/scripts/check.py"]
    assert base64.b64decode(full["files"][1]["content_base64"]) == b'{"packaging":"HELPER_BYTES"}\n'
    policy = _policy(seat)
    projected = dataclasses.replace(seat, admission=dataclasses.replace(seat.admission, projection=policy))
    context = json.loads(cases.CollaborationCoordinator._staged_skill_context(projected))
    assert [item["path"] for item in context["files"]] == [
        "reviewed/SKILL.md", "reviewed/references/RULES.TXT", "reviewed/scripts/check.py"]
    assert b"HELPER_BYTES" not in base64.b64decode(context["files"][0]["content_base64"])
    available = {item["path"]: item for item in context["available_files"]}
    assert set(available) == {"reviewed/SKILL.md", "reviewed/helper.json",
                              "reviewed/references/RULES.TXT", "reviewed/scripts/check.py"}
    assert available["reviewed/helper.json"]["native_path"] == str(seat.staged_root / "reviewed/helper.json")
    assert context["projection"]["policy_sha256"] == policy.sha256
    assert projected.admission.manifest_dict() == seat.admission.manifest_dict()
    assert cases.CollaborationCoordinator._staged_skill_context(seat) == original


def test_projected_helper_is_still_readable_through_authorized_native_path(tmp_path):
    _, seat = _fixture(tmp_path)
    policy = _policy(seat)
    seat = dataclasses.replace(seat, admission=dataclasses.replace(seat.admission, projection=policy))
    context = json.loads(cases.CollaborationCoordinator._staged_skill_context(seat))
    native = next(item["native_path"] for item in context["available_files"]
                  if item["path"] == "reviewed/helper.json")
    native_trace = importlib.import_module(cases.fanout.__name__ + ".native_trace")
    trace = {"calls": [{"id": "read-helper", "arguments": {"file_path": native}}]}
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    assert native_trace.read_boundary_violation(trace, cwd=workspace, forbidden=(tmp_path,),
        allowed_trees=(workspace, seat.staged_root), allowed_files=()) is None
    assert cases.subprocess.run(["/bin/cat", native], check=True, capture_output=True).stdout == (
        b'{"packaging":"HELPER_BYTES"}\n')


def test_changed_omitted_file_invalidates_projection_before_turn(tmp_path):
    _, seat = _fixture(tmp_path)
    seat = dataclasses.replace(seat, admission=dataclasses.replace(seat.admission, projection=_policy(seat)))
    omitted = seat.staged_root / "reviewed/helper.json"
    os.chmod(omitted, 0o600)
    omitted.write_text("changed")
    os.chmod(omitted, 0o400)
    with pytest.raises(cases.CollaborationValidationError, match="skill.*(unavailable|changed)"):
        cases.CollaborationCoordinator._staged_skill_context(seat)


def test_changed_omitted_source_cannot_replace_original_admitted_bundle(tmp_path):
    resolver, seat = _fixture(tmp_path)
    admission = dataclasses.replace(seat.admission, projection=_policy(seat))
    (tmp_path / "source/reviewed/helper.json").write_text("changed source helper")
    with pytest.raises(cases.fanout.SkillAdmissionError, match="drift"):
        resolver.stage(admission, tmp_path / "second-stage")


def test_unknown_tree_including_new_skill_version_uses_full_legacy_delivery(tmp_path):
    _, seat = _fixture(tmp_path)
    original = json.loads(cases.CollaborationCoordinator._staged_skill_context(seat))
    policy = _policy(seat, tree_hash="sha256:" + "1" * 64)
    seat = dataclasses.replace(seat, admission=dataclasses.replace(seat.admission, projection=policy))
    context = json.loads(cases.CollaborationCoordinator._staged_skill_context(seat))
    assert context["files"] == original["files"]
    assert context["projection"]["skills"][0]["mode"] == "full-unreviewed"


@pytest.mark.parametrize("suffix", [
    ".markdown", ".MarKDown", ".mdown", ".mkd", ".mkdn", ".mdwn",
    ".text", ".rest", ".asciidoc", ".AsciiDoc", ".asc",
])
def test_instruction_document_aliases_cannot_be_omitted_at_admission(tmp_path, suffix):
    root = tmp_path / "skills"
    skill = root / "reviewed"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(f"Read `instructions{suffix}`.\n")
    (skill / f"instructions{suffix}").write_text("Preserve these required instructions.\n")
    roots = (cases.fanout.SkillRoot("source", root, 0),)
    resolved = cases.fanout.SkillResolver(roots).resolve(("reviewed",))[0]
    module = importlib.import_module(cases.fanout.__name__ + ".skill_projection")
    policy = module.SkillProjection.from_bytes(cases.canonical_json({
        "schema_version": "fanout-skill-projection-v1", "reviews": [{
            "tree_hash": resolved.tree_hash, "inline_files": ["SKILL.md"],
            "required_resources": []}]}))
    resolver = cases.fanout.SkillResolver(roots, projection=policy)
    with pytest.raises(cases.fanout.SkillAdmissionError, match="omits instruction documents"):
        resolver.admit(("reviewed",), task_id="work", seat_id="seat-codex",
                       provider="codex", session_id="session")


@pytest.mark.parametrize("changes", [
    {"inline_files": ["SKILL.md", "scripts/check.py"]},
    {"inline_files": ["SKILL.md", "references/RULES.TXT"]},
    {"inline_files": ["SKILL.md", "references/RULES.TXT", "missing.py"]},
    {"inline_files": ["SKILL.md", "../escape"]},
])
def test_projection_rejects_missing_instruction_required_resource_unknown_or_unsafe_file(tmp_path, changes):
    _, seat = _fixture(tmp_path)
    with pytest.raises(cases.fanout.SkillAdmissionError):
        policy = _policy(seat, **changes)
        policy.inline_files(seat.admission.skills[0])
