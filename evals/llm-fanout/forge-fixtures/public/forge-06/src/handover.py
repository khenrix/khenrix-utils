"""Public synthetic failed-check rollback, deliberately flawed."""
def apply_change(state, candidate, check):
    state['body'] = candidate
    state['index'] = 'changed-index'
    return bool(check())
