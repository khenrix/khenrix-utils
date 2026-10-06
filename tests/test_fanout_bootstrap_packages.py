"""Fresh-process coverage for the skill loaders' canonical package graph."""
from pathlib import Path
import os
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
HELPERS = ("plan", "execute")
PROBE = r"""
import importlib
from pathlib import Path
import sys
from types import ModuleType

root = Path(sys.argv[1])
helper_name, scenario = sys.argv[2:]
sys.path.insert(0, str(root))

def load_helper():
    path = root / "shared/skills" / ("llm-fanout-" + helper_name) / "scripts" / (helper_name + ".py")
    helper = ModuleType("bootstrap_under_test")
    helper.__file__ = str(path)
    sys.modules[helper.__name__] = helper
    exec(compile(path.read_bytes(), str(path), "exec"), helper.__dict__)
    return helper

if scenario == "smoke-first":
    importlib.import_module("scripts.fanout_smoke")
elif scenario == "import-first":
    importlib.import_module("shared.lib.fanout")
    importlib.import_module("components.maka.scripts")

helper = load_helper()
if scenario.startswith("conflict:"):
    _, name, state = scenario.split(":")
    owner_name, child_name = name.rsplit(".", 1)
    if state == "existing":
        helper._runtime()
    else:
        importlib.import_module(owner_name)
    owner = sys.modules[owner_name]
    foreign = ModuleType(name)
    foreign.__file__ = "/foreign/" + name.replace(".", "/") + "/__init__.py"
    setattr(owner, child_name, foreign)
    try:
        helper._runtime()
    except ValueError as error:
        assert "foreign" in str(error), str(error)
        assert getattr(owner, child_name) is foreign
    else:
        raise AssertionError("accepted conflicting parent attribute: " + name)
elif scenario in ("foreign-parent-root", "foreign-runtime-root"):
    if scenario == "foreign-parent-root":
        foreign = ModuleType("shared")
        foreign.__path__ = ["/foreign/shared"]
        sys.modules["shared"] = foreign
    else:
        importlib.import_module("shared.lib")
        foreign = ModuleType("shared.lib.fanout")
        foreign.__file__ = "/foreign/shared/lib/fanout/__init__.py"
        sys.modules[foreign.__name__] = foreign
    try:
        helper._runtime()
    except ValueError as error:
        assert "foreign" in str(error), str(error)
    else:
        raise AssertionError("accepted foreign root")
else:
    runtime = helper._runtime()
    if scenario == "missing-existing-attributes":
        for owner_name, child_name in (("shared", "lib"), ("shared.lib", "fanout"),
                                        ("components", "maka"), ("components.maka", "scripts")):
            owner = sys.modules[owner_name]
            if child_name in vars(owner):
                delattr(owner, child_name)
        assert helper._runtime() is runtime
    for name in ("shared.lib", "shared.lib.fanout", "components.maka", "components.maka.scripts"):
        owner_name, child_name = name.rsplit(".", 1)
        assert getattr(sys.modules[owner_name], child_name, None) is sys.modules[name], name
    assert runtime is sys.modules["shared.lib.fanout"]
    assert Path(runtime.__file__).resolve() == root / "shared/lib/fanout/__init__.py"
    assert helper._runtime() is runtime
    if scenario == "helper-first":
        importlib.import_module("scripts.fanout_smoke")
    from _pytest.monkeypatch import derive_importpath
    attribute, module = derive_importpath("shared.lib.fanout.container_upstream._connect", raising=True)
    assert attribute == "_connect"
    assert module is importlib.import_module("shared.lib.fanout.container_upstream")
print("PASS " + helper_name + " " + scenario)
"""


def run_probe(tmp_path, helper, scenario):
    environment = {"PATH": os.environ["PATH"], "HOME": str(tmp_path),
                   "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", PROBE, str(ROOT), helper, scenario],
        cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("helper", HELPERS)
@pytest.mark.parametrize("scenario", ("fresh", "smoke-first", "import-first", "helper-first",
                                      "missing-existing-attributes"))
def test_bootstrap_publishes_canonical_package_graph(tmp_path, helper, scenario):
    run_probe(tmp_path, helper, scenario)


@pytest.mark.parametrize("helper", HELPERS)
@pytest.mark.parametrize("name", ("shared.lib", "shared.lib.fanout",
                                  "components.maka", "components.maka.scripts"))
@pytest.mark.parametrize("state", ("fresh", "existing"))
def test_bootstrap_refuses_conflicting_parent_attribute(tmp_path, helper, name, state):
    run_probe(tmp_path, helper, "conflict:" + name + ":" + state)


@pytest.mark.parametrize("helper", HELPERS)
@pytest.mark.parametrize("scenario", ("foreign-parent-root", "foreign-runtime-root"))
def test_bootstrap_keeps_foreign_root_refusals(tmp_path, helper, scenario):
    run_probe(tmp_path, helper, scenario)

