"""Publish curated JSON or inspect a selected local WorkVault publication."""
import argparse
import json
from pathlib import Path
import sys

from curation import MAX_BATCH_BYTES, parse_curated_batch
from workvault import MANAGED, publish_curated_batch, status


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, 'workvault: invalid command arguments\n')


def main(argv=None):
    parser = _Parser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('publish', 'status'):
        command = commands.add_parser(name)
        command.add_argument('--vault', required=True, type=Path)
        command.add_argument('--publication', default=MANAGED)
        if name == 'publish':
            command.add_argument('--snapshots', required=True, type=Path)
            command.add_argument('--input', required=True, choices=('-',),
                                 help='Read a curated JSON batch from stdin.')
    args = parser.parse_args(argv)
    try:
        if args.command == 'publish':
            batch = parse_curated_batch(sys.stdin.buffer.read(MAX_BATCH_BYTES + 1))
            result = publish_curated_batch(args.vault, args.snapshots, batch,
                                           publication=args.publication)
        else:
            result = status(args.vault, publication=args.publication)
    except Exception:
        message = 'publication refused' if args.command == 'publish' else 'status refused'
        print('workvault: ' + message, file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
