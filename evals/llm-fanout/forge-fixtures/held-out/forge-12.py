"""Public synthetic deterministic oracle; no historical or live claim."""
import sys
from fanout_forge_fixtures import probe_observation

observed = probe_observation('forge-12', sys.argv[1])
assert observed == {'binary': '00b941ff', 'link': 'staged.bin', 'source_binary': '00b941ff'}
