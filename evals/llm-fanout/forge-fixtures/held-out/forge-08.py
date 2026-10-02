"""Public synthetic deterministic oracle; no historical or live claim."""
import sys
from fanout_forge_fixtures import probe_observation

observed = probe_observation('forge-08', sys.argv[1])
assert all(item['role'] != 'system' or observed['peer'] not in item['content'] for item in observed['messages'])
assert any(item['role'] == 'user' and item['content'] == observed['task'] for item in observed['messages'])
assert any(observed['peer'] in item['content'] for item in observed['messages'])
lower = [item['content'] for item in observed['messages'] if item['role'] in {'user', 'tool'}]
assert any(observed['peer'] in content and content != observed['peer'] for content in lower), 'peer evidence not delimited'
assert all(item['content'] != observed['peer'] for item in observed['messages']), 'raw peer instruction'
