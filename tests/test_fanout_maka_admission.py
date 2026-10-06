"""Synthetic exact installed files; selected image/boundary commands are simulated."""
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import pwd
import shutil
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'shared/lib/fanout'
SPEC = importlib.util.spec_from_file_location('_maka_admission_contracts', PACKAGE / '__init__.py',
                                           submodule_search_locations=[str(PACKAGE)])
assert SPEC and SPEC.loader
package = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = package
SPEC.loader.exec_module(package)


@pytest.fixture
def installed(tmp_path, monkeypatch, request):
    admission = importlib.import_module(f'{SPEC.name}.maka_admission')
    installer = importlib.import_module('components.maka.scripts.install_component')
    compat = importlib.import_module('components.maka.scripts.apply_gpt6_compat')
    account = SimpleNamespace(pw_dir=str(tmp_path), pw_name='synthetic')
    monkeypatch.setattr(pwd, 'getpwuid', lambda uid: account)
    layout = installer.canonical_layout()
    shutil.copytree(ROOT / 'components/maka', layout.component)
    layout.component.chmod(0o700)
    layout.state.mkdir(parents=True, mode=0o700)
    layout.wrapper.parent.mkdir(parents=True)
    mise = installer.resolve_mise(layout.home)
    layout.wrapper.write_text(installer.render_wrapper((layout.component / 'wrapper/maka.in').read_text(),
        layout=layout, account_name=account.pw_name, mise=mise, package=layout.component / 'runtime/package'))
    layout.wrapper.chmod(0o700)
    receipt = {'schema': 'khenrix-maka-install-v2', 'package': installer.PACKAGE_NAME,
        'version': installer.PACKAGE_VERSION, 'integrity': installer.PACKAGE_INTEGRITY,
        'source_commit': installer.SOURCE_COMMIT, 'platform': installer.supported_platform(),
        'component': str(layout.component), 'wrapper': str(layout.wrapper), 'backup': None,
        'source_digest': installer.source_digest(layout.component),
        'overlay_hashes': {name: {'source': compat.PATCHES[name][0], 'patched': compat.PATCHED_HASHES[name]}
                          for name in compat.PATCHES}, 'runtime_package_digest': '6' * 64,
        'boot_session_uuid': '1' * 8 + '-' + '-'.join(['1' * 4] * 3) + '-' + '1' * 12,
        'install_monotonic_ns': '1'}
    receipt_path = layout.state / 'install-receipt.json'
    receipt_path.write_text(json.dumps(receipt))
    receipt_path.chmod(0o600)
    runtime = layout.component / admission.RUNTIME_RELATIVE
    runtime.mkdir(parents=True)
    for name in ('RUNTIME_SHA256SUMS', 'RUNTIME_SYMLINKS'):
        (runtime / name).write_bytes(b'synthetic native runtime control\n')
        (runtime / name).chmod(0o400)
    socket_path = layout.home / '.colima/maka-amd64/docker.sock'
    socket_path.parent.mkdir(parents=True)
    short = tempfile.TemporaryDirectory(prefix='ma-', dir='/private/tmp')
    request.addfinalizer(short.cleanup)
    selected = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    selected.bind(str(Path(short.name) / 'docker.sock'))
    socket_path.symlink_to(Path(short.name) / 'docker.sock')
    calls = []

    def metadata(mise, component, home, tool, arguments):
        calls.append((str(mise), str(component), str(home), tool, tuple(arguments)))
        if tool == 'docker':
            assert arguments[:2] == ('image', 'inspect')
            return ('sha256:' + ('c' if 'controller' in arguments[2] else 'd') * 64 + '\n').encode()
        assert tool == 'node' and arguments[1] == '--boundary'
        return b'b' * 64 + b'\n'

    monkeypatch.setattr(admission, '_metadata', metadata)
    yield admission, layout, runtime, receipt_path, calls
    selected.close()


def test_exact_installed_constructor_uses_existing_wrapper_and_read_only_boundary(installed):
    admission, layout, runtime, receipt, calls = installed
    closure = admission.admit_installed(layout.wrapper, 'b' * 64)
    assert closure['component_root'] == str(layout.component)
    assert closure['runtime_root'] == str(runtime)
    assert closure['install_receipt_path'] == str(receipt)
    assert len(calls) == 5 and calls[2][3] == 'node'
    assert calls[2][4][1] == '--boundary'
    assert closure['launcher_sha256'] == hashlib.sha256(layout.wrapper.read_bytes()).hexdigest()


@pytest.mark.parametrize('damage', ['foreign-launcher', 'symlink-launcher', 'wrapper-drift', 'component-drift',
                                  'wrong-receipt-version', 'wrong-boundary', 'wrong-image'])
def test_unadmitted_installed_shapes_cannot_be_a_paid_constructor(installed, damage, monkeypatch):
    admission, layout, _, receipt_path, calls = installed
    launcher = layout.wrapper
    boundary = 'b' * 64
    if damage == 'foreign-launcher':
        launcher = layout.home / 'other-launcher'
        launcher.write_bytes(layout.wrapper.read_bytes())
        launcher.chmod(0o700)
    elif damage == 'symlink-launcher':
        actual = layout.wrapper.with_name('actual-wrapper')
        layout.wrapper.rename(actual)
        layout.wrapper.symlink_to(actual)
    elif damage == 'wrapper-drift':
        layout.wrapper.write_bytes(layout.wrapper.read_bytes() + b'changed\n')
    elif damage == 'component-drift':
        (layout.component / 'scripts/harbor_judge_capture.py').write_bytes(b'changed native join\n')
    elif damage == 'wrong-receipt-version':
        value = json.loads(receipt_path.read_bytes())
        value['version'] = 'unadmitted'
        receipt_path.write_text(json.dumps(value))
    elif damage == 'wrong-boundary':
        boundary = 'e' * 64
    else:
        monkeypatch.setattr(admission, '_metadata', lambda *args: b'claimed image\n')
    with pytest.raises(ValueError):
        admission.admit_installed(launcher, boundary)
    if damage not in {'wrong-boundary', 'wrong-image'}:
        assert calls == []


def test_actual_oversized_receipt_refuses_before_selected_native_metadata(installed):
    admission, layout, _, receipt_path, calls = installed
    receipt_path.write_bytes(receipt_path.read_bytes() + b' ' * 8193)
    with pytest.raises(ValueError):
        admission.admit_installed(layout.wrapper, 'b' * 64)
    assert calls == []


@pytest.mark.parametrize('damage', ['image', 'socket'])
def test_selected_image_or_socket_change_during_actual_read_side_boundary_refuses(installed, monkeypatch, damage):
    admission, layout, _, _, calls = installed
    original = admission._metadata

    def metadata(*args):
        raw = original(*args)
        if args[3] == 'node':
            if damage == 'socket':
                (layout.home / '.colima/maka-amd64/docker.sock').unlink()
            else:
                monkeypatch.setattr(admission, '_metadata', lambda *args: b'sha256:' + b'e' * 64 + b'\n')
        return raw

    monkeypatch.setattr(admission, '_metadata', metadata)
    with pytest.raises(ValueError):
        admission.admit_installed(layout.wrapper, 'b' * 64)
    assert calls[2][3] == 'node'


@pytest.mark.parametrize('tool', ['docker', 'node'])
def test_actual_mise_cannot_restore_selectors_or_node_preload_into_admission(tmp_path, monkeypatch, tool):
    admission = importlib.import_module(f'{SPEC.name}.maka_admission')
    (tmp_path / 'mise.toml').write_text('[tools]\npython = "3.12.14"\n[env]\n'
        'DOCKER_HOST = "unix:///private/tmp/wrong.sock"\nDOCKER_CONTEXT = "wrong"\n'
        'NODE_OPTIONS = "wrong"\nNODE_PATH = "wrong"\n')
    with tempfile.TemporaryDirectory(prefix='ms-', dir='/private/tmp') as short:
        home = Path(short)
        selected = home / '.colima/maka-amd64/docker.sock'
        selected.parent.mkdir(parents=True)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.bind(str(selected))
            actual = subprocess.run

            def inert(argv, **kwargs):
                argv = list(argv)
                position = argv.index(tool)
                argv[position:] = ['python', '-I', '-S', '-c',
                    'import os,json;print(json.dumps({k:os.environ.get(k) for k in '
                    '("DOCKER_HOST","DOCKER_CONTEXT","DOCKER_CONFIG","DOCKER_TLS_VERIFY",'
                    '"DOCKER_CERT_PATH","NODE_OPTIONS","NODE_PATH")}))']
                kwargs['env'] = {**kwargs['env'], 'HOME': str(Path.home())}
                return actual(argv, **kwargs)

            monkeypatch.setattr(admission.subprocess, 'run', inert)
            observed = json.loads(admission._metadata(shutil.which('mise'), tmp_path, home, tool, ('inert',)))
    assert observed == {'DOCKER_HOST': 'unix://' + str(selected), 'DOCKER_CONTEXT': None,
        'DOCKER_CONFIG': None, 'DOCKER_TLS_VERIFY': None, 'DOCKER_CERT_PATH': None,
        'NODE_OPTIONS': None, 'NODE_PATH': None}
