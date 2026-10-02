"""Public synthetic deterministic oracle; no historical or live claim."""
import sys
from fanout_forge_fixtures import probe_observation

observed = probe_observation('forge-05', sys.argv[1])
assert observed['swapped'] is True, 'the pre-commit race hook was bypassed'
assert observed['escaped'] is False
