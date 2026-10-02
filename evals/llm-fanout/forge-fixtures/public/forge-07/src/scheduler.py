"""Public synthetic stale-revision claim, deliberately flawed."""
def claim(authority, expected, item):
    authority.update(revision=expected + 1, item=item)
    return True
