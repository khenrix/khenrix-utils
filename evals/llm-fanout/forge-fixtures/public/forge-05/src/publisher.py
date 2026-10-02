"""Public synthetic path-replacement race, deliberately flawed."""
from pathlib import Path
def publish(root, relative, data, before_commit):
    destination = Path(root) / relative
    before_commit()
    destination.write_bytes(data)
