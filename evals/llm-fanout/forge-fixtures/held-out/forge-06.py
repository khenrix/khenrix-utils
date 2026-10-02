"""Public synthetic deterministic oracle; no historical or live claim."""
import sys
from fanout_forge_fixtures import probe_observation

observed = probe_observation('forge-06', sys.argv[1])
assert observed['accepted'] is False
assert observed['after'] == observed['before']
