"""Machine route admission is bound to an owner-held policy snapshot."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import FrozenInstanceError, asdict
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.lib.fanout.container_routes import RouteRejected, read_machine_routes, select_route


def _fixture_git(root, *args):
    argv = ["git", "-C", str(root), "-c", "core.hooksPath=/dev/null",
            "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", *args]
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    print(json.dumps({"argv": argv, "returncode": result.returncode,
                      "stdout": result.stdout, "stderr": result.stderr}, sort_keys=True))
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.mark.parametrize("layout", ("normal", "linked", "bare"))
def test_manifest_admission_refuses_actual_git_ancestry(tmp_path, documents, layout):
    repository = tmp_path / "repository"
    repository.mkdir()
    _fixture_git(repository, "init", "-q", *(('--bare',) if layout == "bare" else ('-b', 'main')))
    root = repository
    if layout != "bare":
        _fixture_git(repository, "commit", "-q", "--allow-empty", "-m", "fixture")
    if layout == "linked":
        root = tmp_path / "linked"
        _fixture_git(repository, "worktree", "add", "-q", "-b", "fixture-linked", str(root))
        assert (root / ".git").is_file()
    nested = root / "owner" / "nested"
    nested.mkdir(parents=True)
    assert _fixture_git(nested, "rev-parse", "--absolute-git-dir")
    manifest, _ = _write_pair(nested, documents)
    outside = tmp_path / "outside"
    outside.mkdir()
    _, policy = _write_pair(outside, documents)
    with pytest.raises(RouteRejected, match="manifest.*repository"):
        read_machine_routes(manifest, policy)


def test_manifest_outside_git_does_not_broaden_placement_to_policy_or_path_names(
    tmp_path, documents, monkeypatch,
):
    repository = tmp_path / "repository"
    repository.mkdir()
    _fixture_git(repository, "init", "-q")
    _, policy = _write_pair(repository, documents)
    outside = tmp_path / "git" / "workspace"
    outside.mkdir(parents=True)
    manifest, _ = _write_pair(outside, documents)
    monkeypatch.setenv("GIT_DIR", str(repository / ".git"))
    admitted = read_machine_routes(manifest, policy)
    assert len(admitted.routes) == 4
    assert admitted.manifest_digest == hashlib.sha256(manifest.read_bytes()).hexdigest()


def _json(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode()


def _route(route_id, provider, auth_family, api_family, origin, project, location,
           model, credential_ref, *, max_data_classification="protected",
           execution_classes=None, wire_model=None, effective_model=None):
    return {
        "route_id": route_id, "provider": provider, "auth_family": auth_family,
        "api_family": api_family, "origin": origin, "project": project,
        "location": location, "requested_model": model,
        "wire_model": wire_model or model, "effective_model": effective_model or model,
        "credential_ref": credential_ref,
        "max_data_classification": max_data_classification,
        "execution_classes": execution_classes or ["read-only", "repo-write"],
        "transform_version": 1,
    }


@pytest.fixture
def documents():
    return [
        _route("claude-vertex", "claude", "vertex-adc", "anthropic-vertex-messages",
               "https://europe-west1-aiplatform.googleapis.com", "fictional-project",
               "europe-west1", "claude-opus-5-5", "gcloud-adc"),
        _route("agy-vertex", "agy", "vertex-adc", "google-vertex-generate-content",
               "https://europe-west1-aiplatform.googleapis.com", "fictional-project",
               "europe-west1", "gemini-3.8-flash-high", "gcloud-adc",
               wire_model="gemini-3.8-flash", effective_model="gemini-3.8-flash"),
        _route("codex-org", "codex", "openai-keychain", "openai-responses",
               "https://api.openai.com", "fictional-openai-project", None,
               "gpt-6.1-sol", "keychain:fanout-fictional-project"),
        _route("codex-plan", "codex", "codex-subscription", "codex-subscription",
               "https://api.openai.com", None, None, "gpt-6.1-sol", "adapter:codex",
               max_data_classification="non-sensitive", execution_classes=["read-only"]),
    ]


def _write_pair(tmp_path, profiles, *, policy_profiles=None):
    policy = {
        "schema": "fanout-machine-policy-v1", "machine_id": "fictional-machine",
        "routes": [{key: value for key, value in profile.items()
                    if key != "credential_ref"}
                   for profile in (profiles if policy_profiles is None else policy_profiles)],
    }
    policy_path = tmp_path / "policy.json"
    policy_path.write_bytes(_json(policy))
    policy_path.chmod(0o600)
    manifest = {
        "schema": "fanout-machine-routes-v1", "machine_id": "fictional-machine",
        "policy_digest": hashlib.sha256(policy_path.read_bytes()).hexdigest(),
        "routes": profiles,
    }
    manifest_path = tmp_path / "routes.json"
    manifest_path.write_bytes(_json(manifest))
    manifest_path.chmod(0o600)
    return manifest_path, policy_path


@pytest.fixture
def paths(tmp_path, documents):
    return _write_pair(tmp_path, documents)


@pytest.fixture
def routes(paths):
    return read_machine_routes(*paths)


def test_owner_policy_binds_exact_manifest_bytes_and_immutable_profiles(routes, paths):
    manifest_path, policy_path = paths
    assert routes.machine_id == "fictional-machine"
    assert routes.policy_digest == hashlib.sha256(policy_path.read_bytes()).hexdigest()
    assert routes.manifest_digest == hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    assert len(routes.routes) == 4
    with pytest.raises(FrozenInstanceError):
        routes.routes[0].origin = "https://other.example"


def test_subscription_cannot_take_protected_or_repo_write_route(routes):
    with pytest.raises(RouteRejected, match="data class"):
        select_route(routes, route_id="codex-plan", provider="codex",
                     requested_model="gpt-6.1-sol", execution_class="read-only",
                     data_classification="protected")
    with pytest.raises(RouteRejected, match="execution class"):
        select_route(routes, route_id="codex-plan", provider="codex",
                     requested_model="gpt-6.1-sol", execution_class="repo-write",
                     data_classification="non-sensitive")


def test_omitted_data_classification_is_protected(routes):
    with pytest.raises(RouteRejected, match="data class"):
        select_route(routes, route_id="codex-plan", provider="codex",
                     requested_model="gpt-6.1-sol", execution_class="read-only")
    with pytest.raises(RouteRejected, match="data class"):
        select_route(routes, route_id="codex-plan", provider="codex",
                     requested_model="gpt-6.1-sol", execution_class="read-only",
                     data_classification=None)
    with pytest.raises(RouteRejected, match="data class"):
        select_route(routes, route_id="codex-plan", provider="codex",
                     requested_model="gpt-6.1-sol", execution_class="read-only",
                     data_classification="unknown")


def test_missing_exact_route_never_falls_back(routes):
    with pytest.raises(RouteRejected, match="route"):
        select_route(routes, route_id="claude-direct", provider="claude",
                     requested_model="claude-opus-5-5", execution_class="repo-write",
                     data_classification="protected")
    with pytest.raises(RouteRejected, match="provider or model"):
        select_route(routes, route_id="codex-org", provider="claude",
                     requested_model="gpt-6.1-sol", execution_class="read-only",
                     data_classification="non-sensitive")


def test_route_digest_excludes_credential_but_binds_manifest_and_policy(routes, paths):
    selected = select_route(routes, route_id="codex-org", provider="codex",
                            requested_model="gpt-6.1-sol", execution_class="repo-write",
                            data_classification="protected")
    digest_fields = asdict(selected)
    digest_fields["profile"].pop("credential_ref")
    assert selected.digest == hashlib.sha256(_json(digest_fields)).hexdigest()
    assert selected.profile.credential_ref not in repr(selected)

    manifest_path, policy_path = paths
    changed = json.loads(manifest_path.read_bytes())
    changed["routes"][2]["credential_ref"] = "keychain:second-fictional-project"
    manifest_path.write_bytes(_json(changed))
    changed_route = read_machine_routes(manifest_path, policy_path)
    assert changed_route.manifest_digest != routes.manifest_digest
    assert select_route(changed_route, route_id="codex-org", provider="codex",
                        requested_model="gpt-6.1-sol", execution_class="repo-write",
                        data_classification="protected").digest != selected.digest


@pytest.mark.parametrize("target", ["manifest", "policy"])
@pytest.mark.parametrize("defect", ["symlink", "wrong-owner", "mode", "oversize",
                                    "duplicate-key", "unknown-field", "noncanonical"])
def test_unsafe_owner_file_is_refused(paths, target, defect, tmp_path, monkeypatch):
    manifest_path, policy_path = paths
    path = manifest_path if target == "manifest" else policy_path
    if defect == "symlink":
        original = tmp_path / (target + "-original")
        path.rename(original)
        path.symlink_to(original)
    elif defect == "wrong-owner":
        # An actual chown is unavailable to an unprivileged test user.
        monkeypatch.setattr(os, "getuid", lambda: os.stat(path).st_uid + 1)
    elif defect == "mode":
        path.chmod(0o640)
    elif defect == "oversize":
        path.write_bytes(b" " * (64 * 1024 + 1))
    elif defect == "duplicate-key":
        path.write_bytes(path.read_bytes().replace(b'"machine_id":',
                                                   b'"machine_id":"duplicate","machine_id":', 1))
    elif defect == "unknown-field":
        value = json.loads(path.read_bytes())
        value["extra"] = "unapproved"
        path.write_bytes(_json(value))
    else:
        path.write_bytes(b" " + path.read_bytes())
    with pytest.raises(RouteRejected):
        read_machine_routes(manifest_path, policy_path)


def test_changed_policy_hash_is_refused(paths):
    manifest_path, policy_path = paths
    policy = json.loads(policy_path.read_bytes())
    policy["machine_id"] = "another-fictional-machine"
    policy_path.write_bytes(_json(policy))
    with pytest.raises(RouteRejected, match="policy"):
        read_machine_routes(manifest_path, policy_path)


@pytest.mark.parametrize("field,value", [
    ("project", "different-project"), ("location", "europe-west2"),
    ("wire_model", "claude-opus-5-6"), ("effective_model", "claude-opus-5-6"),
])
def test_manifest_drift_with_unchanged_policy_is_refused(paths, field, value):
    manifest_path, policy_path = paths
    manifest = json.loads(manifest_path.read_bytes())
    manifest["routes"][0][field] = value
    manifest_path.write_bytes(_json(manifest))
    with pytest.raises(RouteRejected, match="policy|route"):
        read_machine_routes(manifest_path, policy_path)


def test_manifest_route_not_in_policy_is_refused(tmp_path, documents):
    paths = _write_pair(tmp_path, documents, policy_profiles=documents[:3])
    with pytest.raises(RouteRejected, match="policy|route"):
        read_machine_routes(*paths)


@pytest.mark.parametrize("field,value", [
    ("provider", "other"), ("requested_model", "unknown-model"),
    ("max_data_classification", "secret"), ("execution_classes", ["admin"]),
    ("auth_family", "unknown-auth"), ("api_family", "unknown-api"),
    ("origin", "https://evil.example"), ("credential_ref", "${SECRET}"),
    ("credential_ref", "op://Private/key/value"),
    ("transform_version", 2),
])
def test_invalid_profile_is_refused_even_when_policy_repeats_it(tmp_path, documents,
                                                                  field, value):
    documents[0][field] = value
    with pytest.raises(RouteRejected):
        read_machine_routes(*_write_pair(tmp_path, documents))


@pytest.mark.parametrize("profile_index,field,value", [
    (0, "auth_family", "openai-keychain"),
    (0, "origin", "https://api.anthropic.com"),
    (2, "auth_family", "vertex-adc"),
    (2, "api_family", "anthropic-vertex-messages"),
    (3, "execution_classes", ["read-only", "repo-write"]),
    (3, "credential_ref", "keychain:fake"),
])
def test_unsupported_family_or_subscription_authority_is_refused(
    tmp_path, documents, profile_index, field, value,
):
    documents[profile_index][field] = value
    with pytest.raises(RouteRejected):
        read_machine_routes(*_write_pair(tmp_path, documents))


def test_duplicate_route_id_is_refused(tmp_path, documents):
    documents[1]["route_id"] = documents[0]["route_id"]
    with pytest.raises(RouteRejected, match="route"):
        read_machine_routes(*_write_pair(tmp_path, documents))


@pytest.mark.parametrize("profile_index,field,value", [
    (0, "requested_model", "claude-unknown"),
    (0, "wire_model", "claude-unknown"),
    (0, "effective_model", "claude-opus-5-5/../other"),
    (1, "requested_model", "gemini-3.8-flash"),
    (1, "wire_model", "gemini-3.8-flash-high"),
    (2, "effective_model", "gpt-6.1-sol/extra"),
    (2, "requested_model", "gpt-6-sol"),
    (2, "wire_model", "gpt-6-sol"),
    (2, "effective_model", "gpt-6-sol"),
])
def test_unapproved_or_path_injecting_model_is_refused(
    tmp_path, documents, profile_index, field, value,
):
    documents[profile_index][field] = value
    with pytest.raises(RouteRejected, match="model"):
        read_machine_routes(*_write_pair(tmp_path, documents))


@pytest.mark.parametrize("target", ["manifest", "policy"])
def test_symlinked_parent_directory_is_refused(tmp_path, documents, target):
    real = tmp_path / "real"
    real.mkdir()
    manifest_path, policy_path = _write_pair(real, documents)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    if target == "manifest":
        manifest_path = alias / manifest_path.name
    else:
        policy_path = alias / policy_path.name
    with pytest.raises(RouteRejected, match="route file"):
        read_machine_routes(manifest_path, policy_path)


@pytest.mark.parametrize("profile_index", [0, 1])
def test_exact_eu_multiregion_route_is_admitted(tmp_path, documents, profile_index):
    documents[profile_index].update(location="eu", origin="https://aiplatform.eu.rep.googleapis.com")
    routes = read_machine_routes(*_write_pair(tmp_path, documents))
    profile = routes.routes[profile_index]
    assert (profile.location, profile.origin) == ("eu", "https://aiplatform.eu.rep.googleapis.com")
    assert routes.routes[1 - profile_index].location == "europe-west1"


@pytest.mark.parametrize("location,origin", [
    ("eu", "https://eu-aiplatform.googleapis.com"),
    ("eu", "https://aiplatform.googleapis.com"),
    ("eu", "https://aiplatform.us.rep.googleapis.com"),
    ("global", "https://aiplatform.googleapis.com"),
    ("us", "https://aiplatform.us.rep.googleapis.com"),
    ("europe-west1", "https://aiplatform.eu.rep.googleapis.com"),
    ("eu", "https://europe-west1-aiplatform.googleapis.com"),
    ("EU", "https://aiplatform.eu.rep.googleapis.com"),
    ("eu", "https://aiplatform.eu.rep.googleapis.com/"),
    ("global", None),
    ("unapproved", None),
])
@pytest.mark.parametrize("profile_index", [0, 1])
def test_unapproved_vertex_origin_location_tuple_is_refused(
    tmp_path, documents, profile_index, location, origin,
):
    documents[profile_index].update(location=location, origin=origin)
    with pytest.raises(RouteRejected, match="Vertex"):
        read_machine_routes(*_write_pair(tmp_path, documents))


@pytest.mark.parametrize("family,reference", [
    ("openai-keychain", "keychain:cli|fixture"),
    ("openai-1password", "op://Automation/fictional/key"),
])
def test_openai_opaque_mixed_case_project_is_admitted(tmp_path, documents, family, reference):
    documents[2].update(project="proj_FictionalA9z", auth_family=family, credential_ref=reference)
    routes = read_machine_routes(*_write_pair(tmp_path, documents))
    assert routes.routes[2].project == "proj_FictionalA9z"
    assert routes.routes[2].requested_model == "gpt-6.1-sol"


@pytest.mark.parametrize("project", [
    "", "fixture project", "fixture\rproject", "fixture\nproject", "fixture\tproject",
    "fixture/project", "fixture:project", "fixture;project", "fixture,project",
    "fixture\x00project", "fixture\x7fproject", "fixtureé", "a" * 129,
])
def test_openai_project_rejects_unsafe_or_unbounded_header_values(tmp_path, documents, project):
    documents[2]["project"] = project
    with pytest.raises(RouteRejected, match="project"):
        read_machine_routes(*_write_pair(tmp_path, documents))


def test_vertex_project_retains_existing_lowercase_validation(tmp_path, documents):
    documents[0]["project"] = "proj_FictionalA9z"
    with pytest.raises(RouteRejected, match="project"):
        read_machine_routes(*_write_pair(tmp_path, documents))


@pytest.mark.parametrize("project", ["9Fixture", "A" * 128])
def test_openai_project_admits_bounded_opaque_ids_without_google_casing(tmp_path, documents, project):
    documents[2]["project"] = project
    assert read_machine_routes(*_write_pair(tmp_path, documents)).routes[2].project == project


def test_explicit_european_regional_profiles_retain_their_identity(tmp_path, documents):
    documents[1].update(location="europe-west4", origin="https://europe-west4-aiplatform.googleapis.com")
    regional = read_machine_routes(*_write_pair(tmp_path, documents))
    assert [(route.location, route.origin) for route in regional.routes[:2]] == [
        ("europe-west1", "https://europe-west1-aiplatform.googleapis.com"),
        ("europe-west4", "https://europe-west4-aiplatform.googleapis.com"),
    ]
    selected = select_route(regional, route_id="agy-vertex", provider="agy",
                            requested_model="gemini-3.8-flash-high", execution_class="read-only")
    documents[1].update(location="eu", origin="https://aiplatform.eu.rep.googleapis.com")
    multiregion = read_machine_routes(*_write_pair(tmp_path, documents))
    assert select_route(multiregion, route_id="agy-vertex", provider="agy",
                        requested_model="gemini-3.8-flash-high", execution_class="read-only").digest != selected.digest


def test_openai_project_preserves_exact_owner_policy_equality(tmp_path, documents):
    documents[2]["project"] = "proj_FictionalA9z"
    paths = _write_pair(tmp_path, documents)
    changed = json.loads(paths[0].read_bytes())
    changed["routes"][2]["project"] = "proj_fictionala9z"
    paths[0].write_bytes(_json(changed))
    with pytest.raises(RouteRejected, match="policy"):
        read_machine_routes(*paths)


@pytest.mark.parametrize("account", ["cli|fixture", "Fixture@Example.invalid", "A0._+|:-", "A" * 128])
def test_keychain_reference_admits_bounded_account_syntax(tmp_path, documents, account):
    documents[2]["credential_ref"] = "keychain:" + account
    assert read_machine_routes(*_write_pair(tmp_path, documents)).routes[2].credential_ref == (
        "keychain:" + account)


@pytest.mark.parametrize("account", [
    "", "fixture account", "fixture\naccount", "fixture\raccount", "fixture\taccount",
    "fixture\x00account", "fixture\x7faccount", "fixture/account", "../fixture", "/fixture",
    "fixture;account", "fixture$(id)", "A" * 129,
])
def test_keychain_reference_rejects_paths_controls_and_overlong_accounts(tmp_path, documents, account):
    documents[2]["credential_ref"] = "keychain:" + account
    with pytest.raises(RouteRejected, match="Keychain"):
        read_machine_routes(*_write_pair(tmp_path, documents))
