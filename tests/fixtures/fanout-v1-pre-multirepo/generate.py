"""Reseal the public synthetic v1 status records, never generate live proof.

The preserved runtime/compiler hashes identify the original persisted format.
Fictional roots, timestamps, test-only material and object identities are fixed.
No provider is invoked and no originating public commit is asserted.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from shared.lib import fanout
from shared.lib.fanout import runstate

DIRECTORY = Path(__file__).resolve().parent
SYNTHETIC_ROOT = '/srv/fanout-public-synthetic-v1'
SYNTHETIC_RUN_ID = 'fanout-' + '3' * 32
TEST_ONLY_OWNER = 'public-synthetic-owner-material-00000000'
NAMES = ('descriptor.json', 'inputs.json', 'owner.json', 'events.jsonl',
         'snapshot.json', 'scheduler-meta.json', 'scheduler-record.json')


def replace(value, substitutions):
    if isinstance(value, dict):
        return {key: replace(item, substitutions) for key, item in value.items()}
    if isinstance(value, list):
        return [replace(item, substitutions) for item in value]
    if isinstance(value, str):
        for old, new in substitutions:
            value = value.replace(old, new)
    return value


def digest(value):
    return hashlib.sha256(fanout.canonical_json(value)).hexdigest()


def derive(records, format_manifest):
    envelope = records['descriptor.json']
    document = envelope['document']
    substitutions = [
        (records['owner.json']['anchor_key'], 'fanout/' + SYNTHETIC_RUN_ID + '/' + '5' * 32),
        (str(Path(document['run_root']).parent), SYNTHETIC_ROOT),
        (document['run_id'], SYNTHETIC_RUN_ID),
        (document['owner_token'], TEST_ONLY_OWNER),
        (records['owner.json']['anchor_store_id'], 'local-' + digest('public-synthetic-anchor')),
        (records['scheduler-meta.json']['backend_key'], 'scheduler/' + digest('public-synthetic-scheduler')),
    ]
    records = replace(records, substitutions)
    inputs = records['inputs.json']
    old_digest = records['owner.json']['inputs_digest']
    new_digest = fanout.RunInputs.from_dict(inputs).digest
    records = replace(records, [(old_digest, new_digest)])
    document = records['descriptor.json']['document']
    document['inputs'] = inputs
    owner = records['owner.json']
    owner.update(capability_salt='4' * 32, journal_device=1001, journal_inode=1002,
                 lock_device=1001, lock_inode=1003)
    capability = fanout.OwnerCapability.from_token(TEST_ONLY_OWNER)
    key = capability._mac_key()
    owner['capability_verifier'] = runstate._owner_verifier(owner['capability_salt'], capability)
    owner.pop('mac', None)
    owner['mac'] = runstate._mac(key, b'owner', owner)
    events = records['events.jsonl']
    head = '0' * 64
    for index, event in enumerate(events, 1):
        event.pop('mac', None)
        event['timestamp'] = f'2000-01-01T00:00:{index:02d}.000000Z'
        event['previous_checksum'] = head
        event['payload_sha256'] = digest(event['payload'])
        event['checksum'] = runstate._checksum(event)
        event['mac'] = runstate._mac(key, b'event', event)
        head = event['checksum']
    journal = b''.join(fanout.canonical_json(event) for event in events)
    snapshot = records['snapshot.json']
    snapshot.update(journal_device=1001, journal_inode=1002,
                    journal_offset=len(journal), head_checksum=head)
    snapshot['state']['head_checksum'] = head
    snapshot.pop('mac', None)
    snapshot['mac'] = runstate._mac(key, b'snapshot', snapshot)
    meta = records['scheduler-meta.json']
    meta.update(root_device=1001, root_inode=1004, lock_device=1001, lock_inode=1005,
                publication_temp='.fanout-scheduler-meta-tmp-' + '6' * 32)
    meta.pop('owner_mac', None)
    meta['owner_mac'] = hmac.digest(key, b'fanout-file-backend-v1' + fanout.canonical_json(meta), 'sha256').hex()
    scheduler = records['scheduler-record.json']
    scheduler['snapshot']['previous_commit'] = digest('public-synthetic-previous-scheduler')
    for task in scheduler['snapshot']['tasks']:
        if task.get('decision_id') is not None:
            task['decision_id'] = digest('public-synthetic-decision-' + task['task_id'])
    scheduler['snapshot_sha256'] = digest(scheduler['snapshot'])
    records['descriptor.json']['sha256'] = digest(document)
    encoded = {name: journal if name == 'events.jsonl' else fanout.canonical_json(records[name])
               for name in NAMES}
    manifest = {'schema_version': 'fanout-v1-frozen-fixture-v1',
                'runtime_sha256': format_manifest['runtime_sha256'],
                'compiler_sha256': format_manifest['compiler_sha256'],
                'files': {name: hashlib.sha256(data).hexdigest() for name, data in encoded.items()}}
    return encoded, fanout.canonical_json(manifest)


def main():
    records = {name: [json.loads(line) for line in (DIRECTORY / name).read_bytes().splitlines()]
               if name == 'events.jsonl' else json.loads((DIRECTORY / name).read_bytes())
               for name in NAMES}
    encoded, manifest = derive(records, json.loads((DIRECTORY / 'manifest.json').read_bytes()))
    for name, data in encoded.items():
        (DIRECTORY / name).write_bytes(data)
    (DIRECTORY / 'manifest.json').write_bytes(manifest)
    print('Synthetic fixture manifest SHA-256:', hashlib.sha256(manifest).hexdigest())
    print('Review the corresponding CLI fixture trust pin after any intentional change.')


if __name__ == '__main__':
    main()
