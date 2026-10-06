"""Private directory contracts only; no native process or model execution."""
import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.lib.fanout import native_boundary
from shared.lib.fanout.errors import ProviderRequestError
from shared.lib.fanout.eval_provider import AGY_AGENT_TEXT


def test_quality_agy_agent_creates_each_private_parent_under_ordinary_umask(tmp_path):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    original = os.umask(0o022)
    try:
        native_boundary._quality_agy_agent(home)
    finally:
        os.umask(original)
    for path in (home / ".gemini", home / ".gemini/config", home / ".gemini/config/agents"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    agent = home / ".gemini/config/agents/eval-no-tools.md"
    assert agent.read_bytes() == AGY_AGENT_TEXT.encode()
    assert stat.S_IMODE(agent.stat().st_mode) == 0o600
    native_boundary._quality_agy_agent(home)


@pytest.mark.parametrize("unsafe", ["mode", "symlink"])
def test_quality_agy_agent_refuses_unsafe_existing_parent_without_repair(tmp_path, unsafe):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    (home / ".gemini").mkdir(mode=0o700)
    directory = home / ".gemini/config"
    if unsafe == "mode":
        directory.mkdir(mode=0o755)
        directory.chmod(0o755)
    else:
        other = tmp_path / "other"
        other.mkdir(mode=0o700)
        directory.symlink_to(other, target_is_directory=True)
    with pytest.raises(ProviderRequestError):
        native_boundary._quality_agy_agent(home)
    assert not (directory / "agents").exists()
