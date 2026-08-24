"""Git process authority: hostile ambient state cannot steer production observations."""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
import git_authority  # noqa: E402


def _git(args: list[str], *, repo: Path | None = None, cwd: Path | None = None,
         config: tuple[str, ...] = ()) -> subprocess.CompletedProcess:
    return git_authority.run(
        args, repo=repo, cwd=cwd, config=config, check=True,
        capture_output=True, text=True)


def _repo(path: Path, tracked: dict[str, str]) -> None:
    path.mkdir()
    _git(["init", "-q", str(path)], cwd=path.parent)
    for rel, content in tracked.items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    _git(["add", "-A"], repo=path)
    _git(
        ["commit", "-qm", "fixture"], repo=path,
        config=("user.name=test", "user.email=test@example.invalid"),
    )


def test_hostile_git_environment_cannot_redirect_or_execute(tmp_path, monkeypatch):
    good = tmp_path / "good"
    decoy = tmp_path / "decoy"
    _repo(good, {"kept.txt": "good\n", ":(exclude)kept.txt": "literal\n"})
    _repo(decoy, {"decoy.txt": "wrong repository\n"})
    (good / "kept.txt").write_text("changed\n")
    (good / "hidden.txt").write_text("must remain visible\n")

    marker = tmp_path / "executed"
    attacker = tmp_path / "attacker.sh"
    attacker.write_text(f"#!/bin/sh\ntouch {marker}\nexit 97\n")
    attacker.chmod(0o755)
    excludes = tmp_path / "excludes"
    excludes.write_text("hidden.txt\n")
    attributes = tmp_path / "attributes"
    attributes.write_text("*.txt diff=attacker\n")
    config = tmp_path / "hostile.gitconfig"
    config.write_text(
        "[core]\n"
        f"  excludesFile = {excludes}\n"
        f"  attributesFile = {attributes}\n"
        f"  hooksPath = {tmp_path}\n"
        "[diff]\n"
        f"  external = {attacker}\n"
        "[credential]\n"
        f"  helper = {attacker}\n"
    )
    hook = good / ".git" / "hooks" / "post-index-change"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)

    hostile = {
        "GIT_DIR": str(decoy / ".git"),
        "GIT_WORK_TREE": str(decoy),
        "GIT_COMMON_DIR": str(decoy / ".git"),
        "GIT_INDEX_FILE": str(decoy / ".git" / "index"),
        "GIT_OBJECT_DIRECTORY": str(decoy / ".git" / "objects"),
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(decoy / ".git" / "objects"),
        "GIT_CONFIG_GLOBAL": str(config),
        "GIT_CONFIG_SYSTEM": str(config),
        "GIT_CONFIG_PARAMETERS": "'diff.external=attacker'",
        "GIT_CONFIG_COUNT": "3",
        "GIT_CONFIG_KEY_0": "core.excludesFile",
        "GIT_CONFIG_VALUE_0": str(excludes),
        "GIT_CONFIG_KEY_1": "core.attributesFile",
        "GIT_CONFIG_VALUE_1": str(attributes),
        "GIT_CONFIG_KEY_2": "diff.external",
        "GIT_CONFIG_VALUE_2": str(attacker),
        "GIT_ATTR_SOURCE": "HEAD",
        "GIT_EXTERNAL_DIFF": str(attacker),
        "GIT_DIFF_OPTS": "--stat",
        "GIT_GLOB_PATHSPECS": "1",
        "GIT_ICASE_PATHSPECS": "1",
        "GIT_NOGLOB_PATHSPECS": "1",
        "GIT_PAGER": str(attacker),
        "GIT_ASKPASS": str(attacker),
        "GIT_SSH_COMMAND": str(attacker),
        "GIT_TRACE": str(tmp_path / "trace"),
        "GIT_TRACE2": str(tmp_path / "trace2"),
        "GCM_INTERACTIVE": "Always",
        "SSH_ASKPASS": str(attacker),
    }
    for key, value in hostile.items():
        monkeypatch.setenv(key, value)

    env = git_authority.sanitized_environment()
    assert all(key not in env for key in hostile if key not in {
        "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_PAGER",
    })
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_CONFIG_SYSTEM"] == os.devnull
    assert env["GIT_LITERAL_PATHSPECS"] == "1"

    roster = set(filter(None, _git(
        ["ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        repo=good).stdout.split("\0")))
    assert roster == {"kept.txt", ":(exclude)kept.txt", "hidden.txt"}
    assert _git(
        ["ls-files", "--", ":(exclude)kept.txt"], repo=good,
    ).stdout.splitlines() == [":(exclude)kept.txt"]
    assert _git(
        ["check-attr", "diff", "--", "kept.txt"], repo=good,
    ).stdout.strip() == "kept.txt: diff: unspecified"

    patch = _git([
        "diff", "--no-ext-diff", "--no-textconv", "--no-color", "--no-renames",
        "--default-prefix", "HEAD", "--", "kept.txt",
    ], repo=good).stdout
    assert "-good" in patch and "+changed" in patch

    (good / "added.txt").write_text("added\n")
    _git(["add", "added.txt"], repo=good)
    assert not marker.exists()
    assert not (tmp_path / "trace").exists()
    assert not (tmp_path / "trace2").exists()


def _production_direct_git_calls(path: Path) -> list[int]:
    """AST locations where production code bypasses git_authority with literal Git argv."""
    tree = ast.parse(path.read_text(), filename=str(path))
    bad: list[int] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.functions: list[str] = []

        def visit_FunctionDef(self, node: ast.FunctionDef):
            self.functions.append(node.name)
            self.generic_visit(node)
            self.functions.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node: ast.Call):
            if any(name in {"_self_test", "self_test"} for name in self.functions):
                return
            if node.args and isinstance(node.args[0], (ast.List, ast.Tuple)):
                values = node.args[0].elts
                if values and isinstance(values[0], ast.Constant) and values[0].value == "git":
                    bad.append(node.lineno)
            self.generic_visit(node)

    Visitor().visit(tree)
    return bad


def test_production_git_subprocesses_share_one_authority():
    paths = (
        ROOT / "shared" / "skills" / "skill-tuneup" / "scripts" / "tuneup.py",
        ROOT / "scripts" / "lib" / "checks.py",
        ROOT / "scripts" / "eval_harness.py",
        ROOT / "scripts" / "cli_sources.py",
    )
    assert {str(path.relative_to(ROOT)): _production_direct_git_calls(path)
            for path in paths if _production_direct_git_calls(path)} == {}


def test_renderer_bundles_the_authority_beside_checks():
    import importlib.util

    spec = importlib.util.spec_from_file_location("_git_authority_render", ROOT / "scripts" / "render.py")
    render = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(render)
    assert {path.name for path in render.SHARED_LIB_FILES} >= {
        "checks.py", "git_authority.py",
    }
