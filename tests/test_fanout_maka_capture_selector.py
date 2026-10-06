"""An inert pinned Python child measures mise selectors; Docker is never run."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'shared/lib/fanout'
SPEC = importlib.util.spec_from_file_location('_maka_selector_contracts', PACKAGE / '__init__.py',
                                           submodule_search_locations=[str(PACKAGE)])
assert SPEC and SPEC.loader
package = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = package
SPEC.loader.exec_module(package)
import importlib
capture = importlib.import_module(f'{SPEC.name}.maka_capture')


def test_metadata_selector_is_fixed_after_actual_mise_environment_restoration(tmp_path, monkeypatch):
    config = tmp_path / 'mise.toml'
    config.write_text('[tools]\npython = "3.12.14"\n[env]\n'
                      'DOCKER_HOST = "unix:///private/tmp/wrong-declared.sock"\n'
                      'DOCKER_CONTEXT = "wrong-declared"\n')
    fixed = tmp_path / 'fixed.sock'
    adapter = object.__new__(capture.NativeHarborCapture)
    adapter.closure = {'component_root': str(tmp_path)}
    adapter.calls = []
    adapter.deadline = capture.time.monotonic() + 30
    monkeypatch.setattr(adapter, '_mise_bin', lambda: shutil.which('mise'))
    monkeypatch.setattr(adapter, '_socket_path', lambda: fixed)
    actual_run = subprocess.run
    vectors = []

    def inert_selected_command(argv, **kwargs):
        assert kwargs['env']['MISE_AUTO_INSTALL'] == '0'
        argv = list(argv)
        vectors.append(tuple(argv))
        position = argv.index('docker')
        argv[position:position + 1] = ['python', '-I', '-S', '-c',
            'import os,json;print(json.dumps({k:os.environ.get(k) for k in '
            '("DOCKER_HOST","DOCKER_CONTEXT","DOCKER_CONFIG","DOCKER_TLS_VERIFY","DOCKER_CERT_PATH","MISE_AUTO_INSTALL")}))']
        return actual_run(argv, **kwargs)

    monkeypatch.setattr(capture.subprocess, 'run', inert_selected_command)
    observed = json.loads(adapter._docker(('container', 'inspect', '1' * 64)))
    assert observed == {'DOCKER_HOST': 'unix://' + str(fixed), 'DOCKER_CONTEXT': None,
                        'DOCKER_CONFIG': None, 'DOCKER_TLS_VERIFY': None, 'DOCKER_CERT_PATH': None,
                        'MISE_AUTO_INSTALL': '0'}
    assert vectors[0][:5] == (shutil.which('mise'), '-C', str(tmp_path), 'exec', '--')
