"""Public synthetic deterministic oracle; no historical or live claim."""
import sys
from fanout_forge_fixtures import probe_observation

observed = probe_observation('forge-10', sys.argv[1])
assert observed == {'label': 'amber exhibit', 'binary': '00a937ff', 'note': 'Public synthetic caller note: exhibit 37.\n'}
