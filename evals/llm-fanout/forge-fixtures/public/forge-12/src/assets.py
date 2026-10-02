"""Public synthetic binary/link copy fusion case, deliberately flawed."""
from pathlib import Path
def copy_assets(source, destination):
    destination = Path(destination)
    destination.mkdir()
    for path in Path(source).iterdir():
        (destination / path.name).write_bytes(path.read_bytes())
