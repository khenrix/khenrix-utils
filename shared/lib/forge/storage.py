"""Run directory layout and quotas (spec §15).

Under XDG_STATE_HOME, not XDG_CACHE_HOME: the run directory holds the only copy of the
seats' work, and XDG defines the cache as data that can be deleted without loss — it is
what every cleanup tool targets first. The repo path is hashed rather than basenamed so
~/git/a/utils and ~/work/b/utils cannot collide.
"""
import hashlib
import os
import secrets
from dataclasses import dataclass
from pathlib import Path


def new_run_id() -> str:
    return secrets.token_hex(3)   # 6 hex chars; collision-checked by the caller's mkdir


def run_root(repo_path, run_id: str) -> Path:
    state = os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")
    digest = hashlib.sha256(str(Path(repo_path).resolve()).encode()).hexdigest()[:12]
    p = Path(state) / "khenrix-forge" / f"{digest}-{run_id}"
    p.mkdir(parents=True, exist_ok=True)
    p.chmod(0o700)
    return p


@dataclass(frozen=True)
class Quota:
    """Caps that FAIL CLOSED with a report line — never a silent truncation."""
    max_files: int
    max_file_bytes: int
    max_total_bytes: int

    @classmethod
    def default(cls) -> "Quota":
        return cls(max_files=5000, max_file_bytes=32 * 1024 * 1024,
                   max_total_bytes=512 * 1024 * 1024)

    def breach(self, *, files: int, file_bytes: int, total_bytes: int):
        """Return a human-readable breach description, or None when within limits."""
        if files > self.max_files:
            return f"files: {files} > {self.max_files}"
        if file_bytes > self.max_file_bytes:
            return f"file_bytes: {file_bytes} > {self.max_file_bytes}"
        if total_bytes > self.max_total_bytes:
            return f"total_bytes: {total_bytes} > {self.max_total_bytes}"
        return None
