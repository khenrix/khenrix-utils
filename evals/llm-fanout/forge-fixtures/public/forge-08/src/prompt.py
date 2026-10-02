"""Public synthetic evidence-role assignment, deliberately flawed."""
def compose_messages(peer, task):
    return [{'role': 'system', 'content': peer}, {'role': 'user', 'content': task}]
