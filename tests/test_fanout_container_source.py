"""The private probe and later gates must attest the same bounded source set."""
from __future__ import annotations

import importlib
import importlib.util
import hashlib
import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


REQUIRED = (
    "capabilities.toml",
    "mise.toml",
    "mise.lock",
    "shared/lib/fanout/broker.py",
    "shared/lib/fanout/container_broker.py",
    "shared/lib/fanout/container_boundary.py",
    "shared/lib/fanout/container_certification.py",
    "shared/lib/fanout/container_credentials.py",
    "shared/lib/fanout/container_routes.py",
    "shared/lib/fanout/container_source.py",
    "shared/lib/fanout/container_transfer.py",
    "shared/lib/fanout/container_transfer_support.py",
    "shared/lib/fanout/container_transfer_transport.py",
    "shared/lib/fanout/container_upstream.py",
    "components/fanout-container/Dockerfile",
    "components/fanout-container/mise.toml",
    "components/fanout-container/mise.lock",
    "components/fanout-container/relay.py",
    "shared/skills/llm-fanout-plan/SKILL.md",
    "shared/skills/llm-fanout-plan/agents/openai.yaml",
    "shared/skills/llm-fanout-plan/scripts/plan.py",
    "shared/skills/llm-fanout-execute/SKILL.md",
    "shared/skills/llm-fanout-execute/agents/openai.yaml",
    "shared/skills/llm-fanout-execute/scripts/execute.py",
    "scripts/fanout_container_certify.py",
    "scripts/fanout_route_doctor.py",
    "scripts/lib/checks.py",
)


@pytest.fixture
def source_module():
    name = "shared.lib.fanout.container_source"
    if importlib.util.find_spec(name) is None:
        class MissingSource:
            def runtime_source_digest(self, _root):
                pytest.fail("container source-closure helper is missing")
        return MissingSource()
    return importlib.import_module(name)


@pytest.fixture
def source_root(tmp_path):
    for name in REQUIRED:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((name + "\n").encode())
    return tmp_path


@pytest.mark.parametrize("name", REQUIRED)
def test_required_source_byte_changes_digest(source_module, source_root, name):
    before = source_module.runtime_source_digest(source_root)
    path = source_root / name
    path.write_bytes(path.read_bytes() + b"changed\n")
    assert source_module.runtime_source_digest(source_root) != before


@pytest.mark.parametrize("name", (
    "shared/lib/fanout/new_module.py",
    "scripts/fanout_release_certificate.py",
    "shared/skills/llm-fanout-plan/scripts/extra.py",
))
def test_new_selected_source_changes_or_refuses_closure(source_module, source_root, name):
    before = source_module.runtime_source_digest(source_root)
    path = source_root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"new source\n")
    if name.endswith("/extra.py"):
        with pytest.raises(ValueError, match="script"):
            source_module.runtime_source_digest(source_root)
    else:
        assert source_module.runtime_source_digest(source_root) != before


def test_source_digest_is_independent_of_creation_order_and_excludes_evidence(
    source_module, source_root, tmp_path,
):
    before = source_module.runtime_source_digest(source_root)
    other = tmp_path / "other"
    for name in reversed(REQUIRED):
        path = other / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((name + "\n").encode())
    assert source_module.runtime_source_digest(other) == before
    for name in ("scripts/nested/fanout_future.py", "scripts/receipt.json",
                 "shared/lib/fanout/__pycache__/broker.py", "private/receipt.json"):
        path = source_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"untrusted evidence")
    assert source_module.runtime_source_digest(source_root) == before


@pytest.mark.parametrize("name", (
    "mise.toml",
    "mise.lock",
    "shared/lib/fanout/broker.py",
    "shared/lib/fanout/container_boundary.py",
    "shared/lib/fanout/container_credentials.py",
    "shared/lib/fanout/container_routes.py",
    "shared/lib/fanout/container_source.py",
    "shared/lib/fanout/container_transfer.py",
    "shared/lib/fanout/container_upstream.py",
    "components/fanout-container/mise.lock",
    "shared/skills/llm-fanout-plan/agents/openai.yaml",
    "shared/skills/llm-fanout-execute/scripts/execute.py",
    "scripts/fanout_container_certify.py",
    "scripts/fanout_route_doctor.py",
))
def test_absent_required_source_refuses_digest(source_module, source_root, name):
    (source_root / name).unlink()
    with pytest.raises(ValueError, match="required|missing"):
        source_module.runtime_source_digest(source_root)


def test_selected_symlink_refuses_digest(source_module, source_root, tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_bytes(b"untrusted source")
    selected = source_root / "scripts" / "fanout_future.py"
    selected.symlink_to(outside)
    with pytest.raises(ValueError, match="symlink|regular"):
        source_module.runtime_source_digest(source_root)


@pytest.mark.parametrize("name", (
    "mise.toml", "mise.lock", "shared/lib/fanout/container_upstream.py",
))
def test_required_symlink_refuses_digest(source_module, source_root, tmp_path, name):
    outside = tmp_path / "outside-source"
    outside.write_bytes(b"outside")
    selected = source_root / name
    selected.unlink()
    selected.symlink_to(outside)
    with pytest.raises(ValueError, match="symlink|regular"):
        source_module.runtime_source_digest(source_root)


def test_host_runtime_identity_detects_executable_and_version_drift(
    source_module, source_root, tmp_path, monkeypatch,
):
    executable = tmp_path / "python"
    executable.write_bytes(b"python-one")
    executable.chmod(0o700)
    monkeypatch.setattr(sys, "executable", str(executable))
    source_digest = source_module.runtime_source_digest(source_root)
    first = source_module.host_runtime_identity()
    assert first.executable_sha256 == "17ef05ba51d0faf82518e18c459d35af909533a47e8ca1a211a80c87385d5900"
    assert first.python_version == sys.version
    assert first.openssl_version == source_module.ssl.OPENSSL_VERSION
    executable.write_bytes(b"python-two")
    assert source_module.host_runtime_identity().executable_sha256 != first.executable_sha256
    assert source_module.runtime_source_digest(source_root) == source_digest
    monkeypatch.setattr(sys, "version", "different Python version")
    assert source_module.host_runtime_identity().python_version != first.python_version
    monkeypatch.setattr(source_module.ssl, "OPENSSL_VERSION", "different SSL library")
    assert source_module.host_runtime_identity().openssl_version != first.openssl_version


def test_host_runtime_identity_refuses_nonregular_or_oversized_executable(
    source_module, tmp_path, monkeypatch,
):
    executable = tmp_path / "python"
    executable.mkdir()
    monkeypatch.setattr(sys, "executable", str(executable))
    with pytest.raises(ValueError, match="regular|executable"):
        source_module.host_runtime_identity()
    executable.rmdir()
    executable.write_bytes(b"oversized!")
    executable.chmod(0o700)
    monkeypatch.setattr(source_module, "_MAX_EXECUTABLE_BYTES", 8)
    with pytest.raises(ValueError, match="budget|large|size"):
        source_module.host_runtime_identity()


def test_host_runtime_identity_refuses_missing_or_nonexecutable_file(
    source_module, tmp_path, monkeypatch,
):
    executable = tmp_path / "python"
    monkeypatch.setattr(sys, "executable", str(executable))
    with pytest.raises(ValueError, match="executable|attested"):
        source_module.host_runtime_identity()
    executable.write_bytes(b"python")
    executable.chmod(0o600)
    with pytest.raises(ValueError, match="regular|executable"):
        source_module.host_runtime_identity()


def test_selected_source_through_symlinked_directory_refuses_digest(
    source_module, source_root,
):
    selected_directory = source_root / "shared/lib/fanout"
    real_directory = source_root / "fanout-source"
    selected_directory.rename(real_directory)
    selected_directory.symlink_to(real_directory, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink|directory"):
        source_module.runtime_source_digest(source_root)


def test_selected_fifo_refuses_digest_without_opening_it(source_module, source_root):
    os.mkfifo(source_root / "shared/lib/fanout/new_module.py")
    with pytest.raises(ValueError, match="regular"):
        source_module.runtime_source_digest(source_root)


def test_oversized_selected_source_refuses_digest(source_module, source_root):
    (source_root / "shared/lib/fanout/new_module.py").write_bytes(b"x" * (2 * 1024 * 1024))
    with pytest.raises(ValueError, match="budget|size|large"):
        source_module.runtime_source_digest(source_root)


@pytest.fixture
def release_root(source_root):
    memory = source_root / "components/memory"
    memory.mkdir()
    files = ("memoryctl.py", "memory_ingress.py", "mise.toml", "mise.lock",
             "provenance.json")
    for name in files:
        (memory / name).write_bytes((name + "\n").encode())
    (memory / "memoryctl.py").write_text(f"CONTROLLER_FILES = {files!r}\n")
    (memory / "upstreams.toml").write_bytes(b"pinned provenance\n")
    return source_root


def _release(source_module, root):
    api = getattr(source_module, "release_source_binding", None)
    assert callable(api), "canonical release-source binding is missing"
    return api(root)


@pytest.mark.parametrize("component", ("shared/lib/fanout/broker.py", "components/memory/memory_ingress.py"))
def test_current_release_names_portable_modes_without_changing_legacy_bytes(source_module, release_root, component):
    before = _release(source_module, release_root)
    path = release_root / component
    path.chmod(0o755)
    after = _release(source_module, release_root)
    assert after.runtime_sha256 == before.runtime_sha256
    assert after.memory_sha256 == before.memory_sha256
    assert after.digest != before.digest
    value = after.to_dict()
    assert value["schema_version"] == "fanout-release-source-v2"
    assert set(value["components"]) == {"runtime", "memory", "modes"}
    path.chmod(0o500)
    assert _release(source_module, release_root) == after


@pytest.mark.parametrize("relative", ("shared/lib/fanout/helpers/extra.py", "components/fanout-container/assets/relay.conf",
    "shared/skills/llm-fanout-execute/references/authority.md"))
def test_nested_owned_source_is_in_current_closure(source_module, release_root, relative):
    before = _release(source_module, release_root)
    path = release_root / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(b"new owned source\n")
    assert _release(source_module, release_root).runtime_sha256 != before.runtime_sha256


def test_current_release_requires_certificate_implementation(source_module, release_root):
    (release_root / "shared/lib/fanout/container_certification.py").unlink()
    with pytest.raises(ValueError, match="missing|required"):
        _release(source_module, release_root)


@pytest.mark.parametrize("rebind", ("del CONTROLLER_FILES", "def CONTROLLER_FILES(): pass",
    "class CONTROLLER_FILES: pass", "import os as CONTROLLER_FILES",
    "from os import path as CONTROLLER_FILES", "try:\n  pass\nexcept Exception as CONTROLLER_FILES:\n  pass",
    "match None:\n  case CONTROLLER_FILES:\n    pass", "match {}:\n  case {**CONTROLLER_FILES}:\n    pass"))
def test_memory_manifest_rejects_non_name_store_rebindings(source_module, release_root, rebind):
    controller = release_root / "components/memory/memoryctl.py"
    controller.write_text(controller.read_text() + rebind + "\n")
    with pytest.raises(ValueError, match="manifest|controller"):
        _release(source_module, release_root)


@pytest.mark.parametrize("change", ("bytes", "mode", "membership"))
def test_current_release_refuses_earlier_runtime_drift_during_memory_reads(source_module, release_root, monkeypatch, change):
    reader = source_module._read_source
    changed = False
    def drift(root, name, budget):
        nonlocal changed
        data = reader(root, name, budget)
        if name == "components/memory/memory_ingress.py" and not changed:
            changed = True
            earlier = root / "shared/lib/fanout/broker.py"
            if change == "bytes":
                earlier.write_bytes(earlier.read_bytes() + b"later drift\n")
            elif change == "mode":
                earlier.chmod(0o755)
            else:
                nested = root / "shared/lib/fanout/new-helper.txt"
                nested.write_bytes(b"new member\n")
        return data
    monkeypatch.setattr(source_module, "_read_source", drift)
    with pytest.raises(ValueError, match="changed|drift|inventory"):
        _release(source_module, release_root)


@pytest.mark.parametrize("damage", ("missing-modes", "extra-component", "null-modes", "bad-modes", "extra-top",
    "v1-with-modes", "bad-schema", "bad-digest", "non-object-components"))
def test_current_release_parser_requires_exact_named_mode_component(source_module, release_root, damage):
    value = _release(source_module, release_root).to_dict()
    if damage == "missing-modes":
        value["components"].pop("modes")
    elif damage == "extra-component":
        value["components"]["permissions"] = "a" * 64
    elif damage == "null-modes":
        value["components"]["modes"] = None
    elif damage == "bad-modes":
        value["components"]["modes"] = "not a digest"
    elif damage == "extra-top":
        value["extra"] = True
    elif damage == "v1-with-modes":
        value["schema_version"] = "fanout-release-source-v1"
    elif damage == "bad-schema":
        value["schema_version"] = []
    elif damage == "bad-digest":
        value["digest"] = "a" * 64
    else:
        value["components"] = []
    with pytest.raises(ValueError, match="binding|component|digest"):
        source_module.ReleaseSourceBinding.from_dict(value)


def test_legacy_release_encoding_round_trips_but_is_not_current(source_module, release_root):
    import json
    current = _release(source_module, release_root)
    legacy = source_module.ReleaseSourceBinding(current.runtime_sha256, current.memory_sha256)
    value = {"schema_version": "fanout-release-source-v1",
             "components": {"runtime": current.runtime_sha256, "memory": current.memory_sha256}}
    digest = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    value["digest"] = digest
    assert legacy.to_dict() == value
    assert source_module.ReleaseSourceBinding.from_dict(value) == legacy
    assert source_module.ReleaseSourceBinding.from_dict(current.to_dict()) == current
    assert legacy != current and legacy.digest != current.digest


def test_required_skill_entry_cannot_be_replaced_by_another_python_file(source_module, release_root):
    entry = release_root / "shared/skills/llm-fanout-execute/scripts/execute.py"
    entry.rename(entry.with_name("unrelated.py"))
    with pytest.raises(ValueError, match="entry|required"):
        _release(source_module, release_root)


def test_active_entry_membership_change_during_component_read_refuses(source_module, hidden_release_root, monkeypatch):
    reader = source_module._read_source
    def add_entry(root, name, budget):
        data = reader(root, name, budget)
        if name == "components/memory/memory_ingress.py":
            (hidden_release_root / "scripts/plan.py").write_bytes(b"unexpected active entry\n")
        return data
    monkeypatch.setattr(source_module, "_read_source", add_entry)
    with pytest.raises(ValueError, match="changed|inventory"):
        _release(source_module, hidden_release_root)


@pytest.mark.parametrize("name", ("memoryctl.py", "memory_ingress.py", "mise.toml",
                                 "mise.lock", "provenance.json", "upstreams.toml"))
def test_memory_component_drift_changes_release_without_changing_runtime(
    source_module, release_root, name,
):
    before = _release(source_module, release_root)
    path = release_root / "components/memory" / name
    path.write_bytes(path.read_bytes() + b"\n# changed\n")
    after = _release(source_module, release_root)
    assert after.runtime_sha256 == before.runtime_sha256
    assert after.memory_sha256 != before.memory_sha256
    assert after.digest != before.digest
    document = after.to_dict()
    assert document["schema_version"] == "fanout-release-source-v2"
    assert document["components"] == {"runtime": after.runtime_sha256,
                                      "memory": after.memory_sha256,
                                      "modes": after.modes_sha256}


def test_release_runtime_component_preserves_the_old_framed_encoding(source_module, release_root):
    expected = hashlib.sha256()
    for name in sorted(REQUIRED):
        path_bytes, content = name.encode(), (name + "\n").encode()
        expected.update(len(path_bytes).to_bytes(4, "big") + path_bytes)
        expected.update(len(content).to_bytes(8, "big") + content)
    binding = _release(source_module, release_root)
    assert binding.runtime_sha256 == expected.hexdigest()
    assert source_module.runtime_source_digest(release_root) == expected.hexdigest()


@pytest.fixture
def hidden_release_root(release_root):
    installed = release_root / "installed"
    hidden = installed / ".runtime"
    hidden.mkdir(parents=True)
    for path in list(release_root.iterdir()):
        if path != installed:
            path.rename(hidden / path.name)
    canonical = hidden / "shared/skills/llm-fanout-execute"
    for relative in ("SKILL.md", "agents/openai.yaml", "scripts/execute.py"):
        target = installed / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(canonical / relative, target)
    return installed


def test_hidden_source_attests_outer_explicit_hidden_and_default_roots(
    source_module, hidden_release_root, monkeypatch,
):
    installed = hidden_release_root
    monkeypatch.setattr(source_module, "_SOURCE_ROOT", installed / ".runtime")
    expected = source_module.release_source_binding(installed / ".runtime")
    assert source_module.release_source_binding(installed) == expected
    assert source_module.release_source_binding() == expected
    assert source_module.runtime_source_digest(installed) == expected.runtime_sha256


@pytest.mark.parametrize("root", ("outer", "hidden", "default"))
@pytest.mark.parametrize("relative", ("SKILL.md", "agents/openai.yaml", "scripts/execute.py"))
def test_hidden_source_refuses_changed_outer_bytes(
    source_module, hidden_release_root, monkeypatch, root, relative,
):
    installed = hidden_release_root
    monkeypatch.setattr(source_module, "_SOURCE_ROOT", installed / ".runtime")
    active = installed / relative
    active.write_bytes(active.read_bytes() + b"changed\n")
    selected = {"outer": installed, "hidden": installed / ".runtime", "default": None}[root]
    with pytest.raises(ValueError, match="active"):
        source_module.release_source_binding(selected)


@pytest.mark.parametrize("root", ("outer", "hidden", "default"))
@pytest.mark.parametrize("malformed", ("missing", "empty", "file", "symlink"))
def test_hidden_source_refuses_missing_or_unsafe_runtime(
    source_module, hidden_release_root, monkeypatch, root, malformed,
):
    installed = hidden_release_root
    hidden = installed / ".runtime"
    original = installed / "saved-runtime"
    hidden.rename(original)
    if malformed == "empty":
        hidden.mkdir()
    elif malformed == "file":
        hidden.write_text("unsafe runtime\n")
    elif malformed == "symlink":
        hidden.symlink_to(original, target_is_directory=True)
    monkeypatch.setattr(source_module, "_SOURCE_ROOT", hidden)
    selected = {"outer": installed, "hidden": hidden, "default": None}[root]
    with pytest.raises(ValueError, match="missing|unsafe|required"):
        source_module.release_source_binding(selected)


@pytest.mark.parametrize("change", ("missing-entry", "ambiguous-entry", "missing-metadata", "linked-entry"))
def test_hidden_source_cannot_skip_active_validation(source_module, hidden_release_root, change):
    installed = hidden_release_root
    entry = installed / "scripts/execute.py"
    if change == "ambiguous-entry":
        (installed / "scripts/plan.py").write_bytes(b"other entry\n")
    elif change == "missing-metadata":
        (installed / "SKILL.md").unlink()
    else:
        entry.unlink()
        if change == "linked-entry":
            entry.symlink_to(installed / ".runtime/shared/skills/llm-fanout-execute/scripts/execute.py")
    with pytest.raises(ValueError, match="active|missing|regular|symlink"):
        source_module.release_source_binding(installed / ".runtime")


def test_memory_closure_derives_added_controller_without_executing_source(source_module, release_root):
    before = _release(source_module, release_root)
    memory = release_root / "components/memory"
    controller = memory / "memoryctl.py"
    controller.write_text(controller.read_text().replace("'memory_ingress.py',", "'extra.py', 'memory_ingress.py',")
                          + "raise RuntimeError('must never import this source')\n")
    (memory / "extra.py").write_bytes(b"new managed helper\n")
    after = _release(source_module, release_root)
    assert after.memory_sha256 != before.memory_sha256
    (memory / "extra.py").write_bytes(b"changed managed helper\n")
    assert _release(source_module, release_root).memory_sha256 != after.memory_sha256


@pytest.mark.parametrize("declaration", ("CONTROLLER_FILES = ('../outside',)",
                                       "CONTROLLER_FILES = tuple(['memoryctl.py'])",
                                       "CONTROLLER_FILES = ('memoryctl.py',)\nCONTROLLER_FILES = ()"))
def test_memory_closure_refuses_unsafe_or_nonliteral_manifest(source_module, release_root, declaration):
    _release(source_module, release_root)
    (release_root / "components/memory/memoryctl.py").write_text(declaration)
    with pytest.raises(ValueError, match="controller|manifest"):
        _release(source_module, release_root)


def test_memory_closure_refuses_symlink_and_missing_pinned_source(source_module, release_root):
    _release(source_module, release_root)
    path = release_root / "components/memory/provenance.json"
    path.unlink()
    with pytest.raises(ValueError, match="missing"):
        _release(source_module, release_root)
    path.symlink_to(release_root / "mise.toml")
    with pytest.raises(ValueError, match="symlink|regular"):
        _release(source_module, release_root)


@pytest.mark.parametrize("assignment", ("CONTROLLER_FILES += ('unbound.py',)",
                                       "CONTROLLER_FILES: tuple = ('unbound.py',)"))
def test_memory_closure_refuses_a_later_manifest_write(source_module, release_root, assignment):
    controller = release_root / "components/memory/memoryctl.py"
    controller.write_text(controller.read_text() + assignment + "\n")
    with pytest.raises(ValueError, match="manifest"):
        _release(source_module, release_root)


def test_memory_manifest_change_between_selection_and_hash_is_refused(source_module, release_root, monkeypatch):
    reader = source_module._read_source
    changed = False

    def change_after_manifest_read(root, name, budget):
        nonlocal changed
        data = reader(root, name, budget)
        if name == "components/memory/memoryctl.py" and not changed:
            changed = True
            controller = root / name
            controller.write_bytes(data.replace(b"'memory_ingress.py',", b"'extra.py', 'memory_ingress.py',"))
            (root / "components/memory/extra.py").write_bytes(b"new helper")
        return data

    monkeypatch.setattr(source_module, "_read_source", change_after_manifest_read)
    with pytest.raises(ValueError, match="changed|manifest"):
        _release(source_module, release_root)
