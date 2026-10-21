"""Consumer-only AGY installed-read hook. Stdlib; copied into private controls."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import stat
import sys


def regular_bytes(path):
    """Open each component without following symlinks, including parent directories."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('absolute regular path required')
    directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(descriptor, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError('regular file required')
            return stream.read()
    finally:
        os.close(directory)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def config_text(command):
    return json.dumps({'installed-read-evaluation': {'enabled': True, 'PreToolUse': [
        {'matcher': '*', 'hooks': [{'type': 'command', 'command': command, 'timeout': 10}]}]}}, indent=2) + '\n'


def validate_args(args):
    if not isinstance(args, dict) or not set(args) <= {'Path', 'StartLine', 'EndLine'}:
        return False
    path = args.get('Path')
    if not isinstance(path, str) or not path or '\x00' in path or not Path(path).is_absolute():
        return False
    if '..' in Path(path).parts or str(Path(path)) != path:
        return False
    for key in ('StartLine', 'EndLine'):
        if key in args and (type(args[key]) is not int or not 1 <= args[key] <= 2147483647):
            return False
    return args.get('EndLine', 2147483647) >= args.get('StartLine', 1)


def decide(payload, policy):
    tool = payload.get('toolCall', {})
    args = tool.get('args') if isinstance(tool, dict) else None
    if not isinstance(tool, dict) or tool.get('name') != 'read_file' or not validate_args(args):
        return 'deny', None
    # Membership precedes any operation on the requested source.
    expected = policy['files'].get(args['Path'])
    if expected is None:
        return 'deny', None
    raw = regular_bytes(args['Path'])
    if sha(raw) != expected['sha256']:
        return 'deny', None
    raw.decode('utf-8')
    return 'allow', expected['sha256']


def minimal_input(payload):
    if not isinstance(payload, dict):
        payload = {}
    tool = payload.get('toolCall', {})
    args = tool.get('args') if isinstance(tool, dict) else None
    return {'conversationId': payload.get('conversationId'), 'stepIdx': payload.get('stepIdx'),
            'toolCall': {'name': tool.get('name') if isinstance(tool, dict) else None,
                         'arguments_sha256': sha(json.dumps(args, sort_keys=True, separators=(',', ':')).encode()),
                         'args': args if isinstance(tool, dict) and tool.get('name') == 'read_file' and validate_args(args) else None}}


def main():
    policy_path, expected_policy, expected_handler = sys.argv[1:]
    reason = 'AGY installed-read evaluation ' + os.urandom(16).hex()
    decision, source_sha = 'deny', None
    policy, payload = None, {}
    try:
        raw = sys.stdin.buffer.read(1048577)
        if len(raw) > 1048576:
            raise ValueError('oversized hook input')
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError('object required')
        policy_raw = regular_bytes(policy_path)
        if sha(policy_raw) != expected_policy or sha(regular_bytes(__file__)) != expected_handler:
            raise ValueError('control drift')
        policy = json.loads(policy_raw)
        command = shlex.join([sys.executable, '-I', str(Path(__file__)), policy_path, expected_policy, expected_handler])
        if regular_bytes(Path(policy['cwd']) / '.agents/hooks.json') != config_text(command).encode():
            raise ValueError('config drift')
        if not isinstance(payload.get('conversationId'), str) or type(payload.get('stepIdx')) is not int:
            raise ValueError('missing native join fields')
        decision, source_sha = decide(payload, policy)
    except (OSError, ValueError, KeyError, TypeError):
        decision = 'deny'
    try:
        # If controls drift, still retain the denial when the hash-bound log path is available.
        if policy is None:
            candidate = regular_bytes(policy_path)
            if sha(candidate) == expected_policy:
                policy = json.loads(candidate)
        if policy is not None:
            record = {'input': minimal_input(payload), 'decision': decision, 'reason': reason,
                      'source_sha256': source_sha, 'policy_sha256': expected_policy}
            descriptor = os.open(policy['hook_log'], os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
            with os.fdopen(descriptor, 'w') as stream:
                stream.write(json.dumps(record) + '\n')
        else:
            decision = 'deny'
    except (OSError, ValueError, KeyError, TypeError):
        decision = 'deny'
    print(json.dumps({'decision': decision, 'reason': reason}), flush=True)


if __name__ == '__main__':
    main()
