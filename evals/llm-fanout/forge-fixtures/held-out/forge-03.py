"""Public synthetic deterministic oracle; no historical or live claim."""
import sys
from fanout_forge_fixtures import probe_observation

observed = probe_observation('forge-03', sys.argv[1])
assert observed == {'aborted': True, 'writes': 0}
