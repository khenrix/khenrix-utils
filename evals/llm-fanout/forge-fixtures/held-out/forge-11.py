"""Public synthetic deterministic oracle; no historical or live claim."""
import sys
from fanout_forge_fixtures import probe_observation

observed = probe_observation('forge-11', sys.argv[1])
assert observed['result'] == 'museum-delivered'
assert len(observed['transient']) == len(observed['permanent']) == 2
assert observed['failure'] == 'TimeoutError'
for attempts in (observed['transient'], observed['permanent']):
    assert all(len(call) == 2 and call[0] == attempts[0][0] and call[1] == {'exhibit': 37} for call in attempts)
