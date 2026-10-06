"""Fixed installed Harbor consumer admission; no supplied proof or route callback."""
from __future__ import annotations

import os
from pathlib import Path
import pwd
import stat
import subprocess

from components.maka.scripts import harbor_judge_capture as native
from components.maka.scripts import install_component as installer
from components.maka.scripts import component_doctor as doctor
from .container_source import _SOURCE_ROOT, _read_source, runtime_source_digest


RUNTIME_RELATIVE = Path('runtime/linux-amd64/maka-0.2.0-dev.62.20260928-node-24.16.0-'
    'relay-root-virtiofs-v2-eval-openai-onboarding-v2-harbor-judge-sol61-v1-undici-h2-close-v2')
IMAGE_TAGS = ('maka-lab-controller:0.2.0-dev.62.20260928', 'maka-eval-egress-proxy:12.2.3')


def _docker_socket(home):
    path = (home / '.colima/maka-amd64/docker.sock').resolve(strict=True)
    info = path.lstat()
    native._require(stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid())
    return str(path), native._identity(info)


def _metadata(mise, component, home, tool, arguments):
    host = 'unix://' + _docker_socket(home)[0]
    environment = {'HOME': str(home), 'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C',
                   'MISE_AUTO_INSTALL': '0'}
    result = subprocess.run((str(mise), '-C', str(component), 'exec', '--', '/usr/bin/env',
        '-u', 'DOCKER_CONTEXT', '-u', 'DOCKER_CONFIG', '-u', 'DOCKER_TLS_VERIFY', '-u', 'DOCKER_CERT_PATH',
        '-u', 'NODE_OPTIONS', '-u', 'NODE_PATH', 'DOCKER_HOST=' + host, tool, *arguments),
        env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        timeout=120 if tool == 'node' else 10, check=False)
    native._require(result.returncode == 0 and len(result.stdout) <= 4096)
    return result.stdout


def _images(mise, component, home):
    images = []
    for tag in IMAGE_TAGS:
        raw = _metadata(mise, component, home, 'docker', ('image', 'inspect', tag, '--format', '{{.Id}}'))
        native._require(raw.endswith(b'\n') and raw.count(b'\n') == 1)
        image = raw[:-1].decode('ascii')
        native._require(native.IMAGE.fullmatch(image) is not None)
        images.append(image)
    return images


def admit_installed(launcher, boundary):
    """Measure the canonical current consumer using original read-only validators."""
    try:
        native._require(isinstance(boundary, str) and native.DIGEST.fullmatch(boundary) is not None)
        account = pwd.getpwuid(os.getuid())
        layout = installer.canonical_layout()
        native._require(Path(launcher) == layout.wrapper)
        component = layout.component
        parent = native._directory(component, 0o700)
        try:
            manifest_mode = stat.S_IMODE((component / 'install-files.txt').lstat().st_mode)
            manifest = native._read_at(parent, 'install-files.txt', mode=manifest_mode)
        finally:
            os.close(parent)
        portable = _SOURCE_ROOT / 'components/maka'
        native._require(manifest == _read_source(_SOURCE_ROOT, 'components/maka/install-files.txt', 65536))
        names = installer.read_manifest(portable)
        native._require(len(names) <= 256)
        for name in names:
            path = component / name
            mode = stat.S_IMODE(path.lstat().st_mode)
            native._require(mode & 0o022 == 0)
            native._require(native._read(path, mode=mode, limit=1048576)
                            == _read_source(_SOURCE_ROOT, 'components/maka/' + name.as_posix(), 1048576))
        receipt = doctor.inspect_install_receipt(layout)
        mise = installer.resolve_mise(layout.home)
        expected = installer.render_wrapper(_read_source(_SOURCE_ROOT, 'components/maka/wrapper/maka.in',
            1048576).decode('utf-8'),
            layout=layout, account_name=account.pw_name, mise=mise, package=component / 'runtime/package').encode()
        mode = stat.S_IMODE(layout.wrapper.lstat().st_mode)
        native._require(mode & 0o022 == 0 and os.access(layout.wrapper, os.X_OK)
                        and native._read(layout.wrapper, mode=mode) == expected)
        public = runtime_source_digest()
        socket = _docker_socket(layout.home)
        images = _images(mise, component, layout.home)
        runtime = component / RUNTIME_RELATIVE
        receipt_path = layout.state / 'install-receipt.json'
        before = native.observe_closure(component, runtime, receipt_path, layout.wrapper,
            public_source_sha256=public, boundary_sha256=boundary,
            controller_image_id=images[0], egress_image_id=images[1])
        observed = _metadata(mise, component, layout.home, 'node',
            (str(component / 'controller/harbor_judge.mjs'), '--boundary', '--runtime-root', str(runtime),
             '--install-receipt', str(receipt_path), '--controller-image-id', images[0], '--egress-image-id', images[1]))
        native._require(observed == boundary.encode() + b'\n')
        native._require(_docker_socket(layout.home) == socket and _images(mise, component, layout.home) == images
                        and _docker_socket(layout.home) == socket)
        after = native.observe_closure(component, runtime, receipt_path, layout.wrapper,
            public_source_sha256=runtime_source_digest(), boundary_sha256=boundary,
            controller_image_id=images[0], egress_image_id=images[1])
        native._require(before == after and receipt['source_digest'] == after['installed_source_sha256'])
        return after
    except (OSError, ValueError, UnicodeError, RuntimeError, subprocess.SubprocessError):
        raise native.CaptureRejected() from None
