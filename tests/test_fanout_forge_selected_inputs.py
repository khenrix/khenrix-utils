"""Selected Forge inputs reach real consumers without native/provider launches."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def fixtures():
    spec = importlib.util.spec_from_file_location("forge_selected_contract", ROOT / "scripts/fanout_forge_fixtures.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _selection(tmp_path, fixtures):
    root = tmp_path / "selected-fixtures"
    shutil.copytree(fixtures.ROOT, root)
    task = root / "public/forge-09/TASK.md"
    task.write_text(task.read_text() + "\nSelected synthetic root instruction.\n")
    (root / "held-out/probe.py").write_text("# selected synthetic candidate probe\n")
    book = json.loads(fixtures.CASEBOOK.read_bytes())
    book["forge_fixture_manifest_sha256"] = fixtures.manifest_sha256(root)
    path = tmp_path / "selected-book.json"
    path.write_bytes(fixtures._canonical(book))
    fixtures.verify_pin(path, root)
    return path, root


@pytest.mark.parametrize("consumer", ["materialize", "ingress", "declared", "executor", "probe"])
def test_explicit_selected_book_and_root_reach_existing_consumers(tmp_path, fixtures, monkeypatch, consumer):
    book, root = _selection(tmp_path, fixtures)
    workspace = tmp_path / "workspace"
    if consumer == "materialize":
        fixtures.materialize("forge-09", workspace, root, casebook=book)
        assert "Selected synthetic root instruction." in (workspace / "TASK.md").read_text()
        assert not (workspace / "held-out").exists()
    elif consumer == "ingress":
        packet = fixtures.ingress_packet("forge-09", root, casebook=book)
        assert "Selected synthetic root instruction." in packet["source_markdown"]
    elif consumer == "declared":
        shutil.copytree(root / "public/forge-01", workspace)
        result = fixtures.run_declared_check("forge-01", workspace, root, casebook=book)
        assert result.returncode == 0 and result.stderr == ""
    elif consumer == "executor":
        run = tmp_path / "campaign"
        workspace = run / "cases/forge-01/seats/claude"
        workspace.mkdir(parents=True)
        boundary = fixtures.ForgeBoundaryDescriptor(run, tmp_path / "baseline", tmp_path / "consumer", (), (), "a" * 64, "b" * 64)
        monkeypatch.setattr(fixtures, "_attest_native_boundary", lambda *_args: "synthetic-sealed-policy")
        assert fixtures.executor_argv("forge-01", workspace, ["never-launched"], root,
            casebook=book, boundary=boundary) == ["/usr/bin/sandbox-exec", "-p", "synthetic-sealed-policy", "never-launched"]
    else:
        workspace.mkdir()
        monkeypatch.setattr(fixtures, "_validate_boundary", lambda *_args, **_kwargs: "synthetic-policy")
        def no_native(argv, **kwargs):
            assert kwargs["input"] == "# selected synthetic candidate probe\n"
            return subprocess.CompletedProcess(argv, 0, stdout='{"selected_probe":true}', stderr="")
        monkeypatch.setattr(fixtures.subprocess, "run", no_native)
        assert fixtures.probe_observation("forge-01", workspace, root, casebook=book) == {"selected_probe": True}


@pytest.mark.parametrize("tamper", ["book", "root"])
def test_selected_input_tampering_refuses_before_materialization(tmp_path, fixtures, tamper):
    book, root = _selection(tmp_path, fixtures)
    if tamper == "book":
        value = json.loads(book.read_bytes())
        value["cases"][0]["prompt"] += " changed after seal"
        book.write_bytes(fixtures._canonical(value))
    else:
        (root / "held-out/probe.py").write_text("# changed after pin\n")
    workspace = tmp_path / "never-created"
    with pytest.raises(fixtures.FixtureError, match="seal|pin"):
        fixtures.materialize("forge-01", workspace, root, casebook=book)
    assert not workspace.exists()


def test_ordinary_default_materialization_preserves_public_source_bytes(tmp_path, fixtures):
    workspace = tmp_path / "ordinary-public"
    fixtures.materialize("forge-01", workspace)
    assert (workspace / "src/transfer.py").read_bytes() == (fixtures.ROOT / "public/forge-01/src/transfer.py").read_bytes()
    assert not (workspace / "held-out").exists()


def _child_selection(tmp_path, fixtures):
    book, root = _selection(tmp_path, fixtures)
    # Only this synthetic test oracle replaces the forbidden native operation.
    # The imported default-call shape is unchanged from the shipped oracles.
    oracle = root / "held-out/forge-01.py"
    oracle.write_text(
        "import json,os,pathlib,sys\n"
        "import fanout_forge_fixtures as fixtures\n"
        "fixtures._validate_boundary = lambda *_args, **_kwargs: 'test-only-policy'\n"
        "def no_native(argv, **kwargs):\n"
        "    assert argv[0] == '/usr/bin/sandbox-exec'\n"
        "    assert 'DUMMY_SELECTED_CHILD_SECRET' not in os.environ\n"
        "    return fixtures.subprocess.CompletedProcess(argv, 0, stdout=json.dumps({'source': kwargs['input']}), stderr='')\n"
        "fixtures.subprocess.run = no_native\n"
        "observed = fixtures.probe_observation('forge-01', sys.argv[1])\n"
        "assert observed['source'] == '# selected synthetic candidate probe\\n'\n"
        "pathlib.Path(sys.argv[1], 'oracle-created').write_text('fresh verifier only')\n"
        "print(json.dumps(observed))\n")
    value = json.loads(book.read_bytes())
    value["forge_fixture_manifest_sha256"] = fixtures.manifest_sha256(root)
    book.write_bytes(fixtures._canonical(value))
    return book, root


def test_real_oracle_child_default_probe_call_receives_selected_context(tmp_path, fixtures, monkeypatch):
    book, root = _child_selection(tmp_path, fixtures)
    workspace = tmp_path / "caller"
    shutil.copytree(root / "public/forge-01", workspace)
    before = {path.relative_to(root): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in root.rglob("*") if path.is_file()}
    monkeypatch.setenv("DUMMY_SELECTED_CHILD_SECRET", "synthetic-do-not-forward")
    result = fixtures.run_held_out("forge-01", workspace, root, casebook=book)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"source": "# selected synthetic candidate probe\n"}
    assert not (workspace / "oracle-created").exists()
    assert {path.relative_to(root): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("tamper", ["book-bytes", "root-bytes", "context-field", "context-digest", "context-case", "context-path", "context-bound"])
def test_real_oracle_child_revalidates_selected_bytes_and_closed_context(tmp_path, fixtures, monkeypatch, tamper):
    book, root = _child_selection(tmp_path, fixtures)
    workspace = tmp_path / "caller"
    shutil.copytree(root / "public/forge-01", workspace)
    original_run = fixtures.subprocess.run
    def tampered_launch(argv, **kwargs):
        argv = list(argv)
        if tamper == "book-bytes":
            book.write_bytes(book.read_bytes() + b" ")  # Still valid JSON and the same case seal.
        elif tamper == "root-bytes":
            (root / "held-out/probe.py").write_text("# changed after parent validation\n")
        else:
            value = json.loads(argv[-1])
            if tamper == "context-field":
                value["claimed_env"] = {"ROOT": "arbitrary"}
            elif tamper == "context-digest":
                value["fixture_manifest_sha256"] = "f" * 64
            elif tamper == "context-path":
                value["root"] += "/../selected-fixtures"
            elif tamper == "context-bound":
                value["root"] = "/" + "x" * (16 * 1024)
            else:
                value["case_id"] = "forge-02"
            argv[-1] = fixtures._canonical(value).decode()
        return original_run(argv, **kwargs)
    monkeypatch.setattr(fixtures.subprocess, "run", tampered_launch)
    result = fixtures.run_held_out("forge-01", workspace, root, casebook=book)
    assert result.returncode != 0 and ("selected input context differs" in result.stderr
        or "input context exceeds its bound" in result.stderr)
    assert result.stdout == "" and not (workspace / "oracle-created").exists()


def test_selected_book_is_denied_by_the_existing_private_probe_policy(tmp_path, fixtures, monkeypatch):
    book, root = _selection(tmp_path, fixtures)
    workspace = tmp_path / "workspace"
    shutil.copytree(root / "public/forge-01", workspace)
    monkeypatch.setattr(fixtures, "_SANDBOX_EXEC", Path(sys.executable))
    monkeypatch.setattr(fixtures, "_protected_roots", lambda _root: (root,))
    monkeypatch.setattr(fixtures, "_reject_hardlinked_git_objects", lambda *_args: None)
    monkeypatch.setattr(fixtures, "_sandbox_probe",
        lambda _policy, source, *_args: 17 if "PermissionError" in source else 0)
    policy = fixtures._validate_boundary("forge-01", workspace, root, casebook=book)
    assert f"(deny file-read* (subpath {json.dumps(str(book))}))" in policy
    assert f"(deny file-write* (subpath {json.dumps(str(book))}))" in policy
