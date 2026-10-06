"""Normal fanout skill bundle contains only current portable Maka source."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tomllib

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_both_normal_fanout_bundles_map_exactly_owned_portable_maka_files():
    capabilities = tomllib.loads((ROOT / 'capabilities.toml').read_text())
    names = {line for line in (ROOT / 'components/maka/install-files.txt').read_text().splitlines()
             if line and not line.startswith('#')}
    expected = {'.runtime/components/maka/' + name: 'components/maka/' + name for name in names}
    for skill in ('llm-fanout-plan', 'llm-fanout-execute'):
        selected = {name: value for name, value in capabilities['skill_delivery']['skill_bundles'][skill].items()
                    if name.startswith('.runtime/components/maka')}
        assert selected == expected
        assert all((ROOT / value).is_file() and not (ROOT / value).is_symlink() for value in selected.values())


def test_real_staged_installed_skill_imports_current_native_consumer_without_runtime_cache(tmp_path):
    spec = importlib.util.spec_from_file_location('_maka_skill_delivery', ROOT / 'components/skills/skillctl.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    configuration = module.load_configuration(ROOT, tmp_path, None)
    from shared.lib.fanout.container_source import runtime_source_digest
    public = runtime_source_digest()
    with module.staged_skill_sources(configuration) as sources:
        for skill in ('llm-fanout-plan', 'llm-fanout-execute'):
            hidden = sources[skill] / '.runtime'
            portable = hidden / 'components/maka'
            names = {line for line in (ROOT / 'components/maka/install-files.txt').read_text().splitlines()
                     if line and not line.startswith('#')}
            actual = {path.relative_to(portable).as_posix() for path in portable.rglob('*') if path.is_file()}
            assert actual == names
            for name in names:
                assert (portable / name).read_bytes() == (ROOT / 'components/maka' / name).read_bytes()
            child = subprocess.run([sys.executable, '-B', '-I', '-S', '-c',
                'import sys,importlib.util;'
                'spec=importlib.util.spec_from_file_location("cold_bootstrap",sys.argv[1]);'
                'bootstrap=importlib.util.module_from_spec(spec);sys.modules[spec.name]=bootstrap;'
                'spec.loader.exec_module(bootstrap);bootstrap._runtime();'
                'from shared.lib.fanout import maka_admission,maka_capture,maka_quality;'
                'from shared.lib.fanout.container_source import runtime_source_digest;'
                'print(runtime_source_digest())', str(sources[skill] / 'scripts' /
                    ('plan.py' if skill.endswith('plan') else 'execute.py'))],
                cwd=tmp_path, capture_output=True, check=False, timeout=30)
            assert child.returncode == 0, child.stderr.decode()
            assert child.stdout.decode().strip() == public


@pytest.mark.parametrize('damage', ['components', 'components.maka', 'components.maka.scripts', 'helper-symlink'])
def test_real_staged_bootstrap_refuses_foreign_native_namespace_and_unsafe_helper(tmp_path, damage):
    spec = importlib.util.spec_from_file_location('_maka_refusing_delivery', ROOT / 'components/skills/skillctl.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    configuration = module.load_configuration(ROOT, tmp_path, None)
    with module.staged_skill_sources(configuration) as sources:
        for skill in ('llm-fanout-plan', 'llm-fanout-execute'):
            source = sources[skill]
            if damage == 'helper-symlink':
                path = source / '.runtime/components/maka/scripts/harbor_judge_capture.py'
                path.unlink()
                path.symlink_to(ROOT / 'components/maka/scripts/harbor_judge_capture.py')
            child = subprocess.run([sys.executable, '-B', '-I', '-S', '-c',
                'import sys,importlib.util,types;'
                'spec=importlib.util.spec_from_file_location("refusing_bootstrap",sys.argv[1]);'
                'bootstrap=importlib.util.module_from_spec(spec);sys.modules[spec.name]=bootstrap;'
                'spec.loader.exec_module(bootstrap);'
                'foreign=types.ModuleType(sys.argv[2]);foreign.__path__=[sys.argv[3]];'
                'sys.modules.update({sys.argv[2]:foreign} if sys.argv[2]!="helper-symlink" else {});'
                '\ntry: bootstrap._runtime()'
                '\nexcept ValueError as error: print(str(error))'
                '\nelse: raise AssertionError("unsafe native package was accepted")',
                str(source / 'scripts' / ('plan.py' if skill.endswith('plan') else 'execute.py')),
                damage, str(tmp_path / 'foreign')], cwd=tmp_path, capture_output=True,
                check=False, timeout=30)
            assert child.returncode == 0, child.stderr.decode()
            assert ('source is unsafe' if damage == 'helper-symlink' else 'foreign parent package') in child.stdout.decode()
