"""Owned, ordinary-tool Maka Host support for explicit normal-use probes.

All paths are caller supplied. This module never attaches to the default Host
for execution and never changes workspace metadata or installed runtimes.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

import maka_smoke as smoke


@dataclass(frozen=True)
class OwnedNormalHost:
    root_path: Path
    client_data_root: Path
    root_id: str
    spec_path: Path
    attestation: dict


def verify_native_package(package_root: Path) -> None:
    manifest = json.loads(Path(__file__).with_name('maka_native_api.json').read_text())
    try:
        package_root = package_root.resolve(strict=True)
        descriptor = json.loads((package_root / 'package.json').read_text())
        if descriptor.get('name') != manifest['package'] or descriptor.get('version') != manifest['version']:
            raise ValueError('package identity differs')
        for relative, expected in manifest['files'].items():
            path = package_root / relative
            path.resolve(strict=True).relative_to(package_root)
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError('internal API hash differs: ' + relative)
    except (OSError, ValueError) as error:
        raise RuntimeError('Maka native package is not the qualified pin') from error


def normal_environment(environment=None) -> dict[str, str]:
    result = dict(environment) if environment is not None else {
        key: os.environ[key] for key in ('HOME', 'PATH', 'TMPDIR', 'LANG', 'TZ') if key in os.environ
    }
    if any(name.startswith(('NODE_', 'DYLD_', 'KHENRIX_MAKA_PRIVATE_HOST_')) or name == 'LD_PRELOAD' for name in result):
        raise RuntimeError('Normal Host must not inherit routing seals or preload controls')
    return result


@contextmanager
def owned_normal_host(*, node: Path, package_root: Path, relay_helper: Path,
                      token_file: Path, attestation_file: Path, default_root: Path,
                      evidence_dir: Path, port: int = 48173, environment=None):
    """Start a fresh owned child; stdin EOF requests zero-run settlement.

    The caller controls probe/model execution separately. Evidence is retained
    on both success and failure, without auth headers or credential values.
    """
    verify_native_package(package_root)
    environment = normal_environment(environment)
    evidence_dir = evidence_dir.resolve()
    evidence_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    root = evidence_dir / 'client/workspaces/default'
    spec = {'rootPath': str(root), 'clientDataRoot': str(evidence_dir / 'client'),
            'defaultRootPath': str(default_root.resolve()), 'packageRoot': str(package_root.resolve()),
            'relayHelper': str(relay_helper.resolve()), 'tokenFile': str(token_file.resolve()),
            'attestationFile': str(attestation_file.resolve()), 'port': port}
    spec_path = evidence_dir / 'host-spec.json'
    smoke._private_file(spec_path, json.dumps(spec).encode())
    smoke.seed_private_skill_preferences(root)
    helper = Path(__file__).with_name('maka_native_owned_host.mjs')
    with (evidence_dir / 'supervisor-stderr.log').open('wb') as stderr:
        process = subprocess.Popen([str(node), str(helper), str(spec_path)], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=stderr, env=environment)
        ready = None
        try:
            line = smoke._bounded_line(process.stdout, time.monotonic() + 100)
            smoke._private_file(evidence_dir / 'host-readiness.json', line)
            ready = json.loads(line)
            if (ready.get('schema') != 'maka-stock-normal-owned-host-v1'
                    or ready.get('rootPath') != str(root) or ready.get('processSealed') is not False
                    or ready.get('routingCertification') is not False):
                raise RuntimeError('Normal owned Host readiness mismatch')
            yield OwnedNormalHost(root, evidence_dir / 'client', ready['rootId'], spec_path, ready)
        finally:
            process.stdin.close()
            code = process.wait(timeout=40)
            final = process.stdout.read(65537)
            process.stdout.close()
            smoke._private_file(evidence_dir / 'host-settlement.json', final)
            if code or ready is None:
                raise RuntimeError('Normal owned Host failed; retained evidence at ' + str(evidence_dir))
            settled = json.loads(final)
            if (any(settled.get(key) != ready.get(key) for key in ('pid', 'rootId', 'hostEpoch'))
                    or settled.get('activeSessions') != 0 or settled.get('defaultHost') != ready.get('defaultHost')):
                raise RuntimeError('Normal owned Host settlement mismatch')
            try:
                os.kill(ready['pid'], 0)
            except ProcessLookupError:
                pass
            else:
                raise RuntimeError('Normal owned Host PID remains alive')


def run_normal_probe(host: OwnedNormalHost, *, node: Path, package_root: Path,
                     installed_helper: Path, cwd: Path, session_id: str, prompt: str,
                     output_dir: Path, create: bool = False, model: str = 'gpt-6.1-sol',
                     thinking: str = 'medium', timeout: int = 180, max_steps: int = 6,
                     environment=None):
    """Run one native bot/explore turn with ordinary tools in the owned child.

    This is normal-use evidence, not restricted-profile routing certification.
    A nonzero result and every native admission remain available to the caller.
    """
    verify_native_package(package_root)
    evidence_root = host.spec_path.parent.resolve()
    output_dir = output_dir.resolve()
    output_dir.relative_to(evidence_root)
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    request = {'packageRoot': str(package_root.resolve()), 'installedHelper': str(installed_helper.resolve()),
               'rootPath': str(host.root_path), 'clientDataRoot': str(host.client_data_root),
               'rootId': host.root_id, 'host': host.attestation, 'evidenceRoot': str(evidence_root),
               'cwd': str(cwd.resolve()), 'sessionId': session_id, 'create': create, 'prompt': prompt,
               'outputDir': str(output_dir), 'model': model, 'thinking': thinking,
               'timeout': timeout, 'maxSteps': max_steps}
    request_path = output_dir / 'request.json'
    smoke._private_file(request_path, json.dumps(request).encode())
    completed = smoke._bounded_command(
        [str(node), str(Path(__file__).with_name('maka_native_probe.mjs')), str(request_path)],
        timeout=timeout + 40, text=True, env=normal_environment(environment),
    )
    smoke._private_file(output_dir / 'stdout', completed.stdout.encode())
    smoke._private_file(output_dir / 'stderr', completed.stderr.encode())
    return completed
