"""Public synthetic deterministic oracle; no historical or live claim."""
import sys
from fanout_forge_fixtures import probe_observation

observed = probe_observation('forge-01', sys.argv[1])
assert sorted(observed['orders'][0]) == ['audit', 'wallet']
assert observed['orders'][0] == observed['orders'][1]
assert observed['calls'] == [['audit', 'debit'], ['audit', 'debit']]
