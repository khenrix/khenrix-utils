"""Public synthetic deterministic oracle; no historical or live claim."""
import sys
from fanout_forge_fixtures import probe_observation

observed = probe_observation('forge-04', sys.argv[1])
assert observed['safe']['exit'] == 0
flags = observed['safe']['arguments'][:-2]
assert observed['safe']['arguments'][-2:] == ['--', observed['target']]
assert set(''.join(flag[1:] for flag in flags)) >= {'r', 'f'}
assert all(item['exit'] != 0 and not item['arguments'] for item in observed['unsafe'])
