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
    return secrets.token_hex(3)   # 6 hex chars; run_root() rejects a collision, see below


def run_root(repo_path, run_id: str, must_be_new: bool = True) -> Path:
    """Create (or reattach to) the directory holding this run's work.

    `must_be_new` makes the collision real rather than silent: a second run that draws the
    same run id for the same repo raises FileExistsError instead of sharing a directory
    with the first. Reattaching to a run that already exists — collecting results the
    engine wrote earlier — must pass False.
    """
    state = os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")
    digest = hashlib.sha256(str(Path(repo_path).resolve()).encode()).hexdigest()[:12]
    p = Path(state) / "khenrix-forge" / f"{digest}-{run_id}"
    p.mkdir(mode=0o700, parents=True, exist_ok=not must_be_new)
    p.chmod(0o700)   # mkdir's mode is masked by umask; chmod is not
    return p


@dataclass(frozen=True)
class Quota:
    """Caps that FAIL CLOSED with a report line — never a silent truncation."""
    max_files: int
    max_file_bytes: int
    max_total_bytes: int

    @classmethod
    def default(cls) -> "Quota":
        """What the PRE-LAUNCH SCREEN is willing to decode out of the user's selected
        baseline — source the user wrote, before any provider starts. Setup has not run at
        that point, so no dependency tree is in scope here."""
        return cls(max_files=5000, max_file_bytes=32 * 1024 * 1024,
                   max_total_bytes=512 * 1024 * 1024)

    @classmethod
    def for_harvest(cls) -> "Quota":
        """What an inventory of a SEAT may weigh — a different question from `default`.

        `harvest`'s design turns on setup's output (`node_modules`, `.venv`) being PRESENT
        in `Fsetup`, so that `Fsetup -> Fwork` differences it out of the artifact path set.
        Under `default` the inventory that must observe it fails closed first: measured,
        `HarvestError: files: 5001 > 5000` on a 5200-file `node_modules` — and this
        stdlib-only repository's own worktree is 5654 files with no dependencies at all.

        All three caps move because all three were measured wrong for this question, on
        this machine's uv package cache: one cached package tree is 6,071 files / 337 MB,
        and the largest single file inside one is 117.7 MiB — nearly 4x `default`'s
        per-file cap. Leaving the byte caps alone would only move the same fail-closed
        refusal one step down the same seat.

        What does NOT move is the fail-closed property: these are hard caps that still make
        `snapshot.take` return a breach and `harvest.record` raise, never truncate. They are
        sized to separate a dependency tree from a runaway, not to admit everything.
        """
        return cls(max_files=200_000, max_file_bytes=512 * 1024 * 1024,
                   max_total_bytes=8 * 1024 * 1024 * 1024)

    def breach(self, *, files: int, file_bytes: int, total_bytes: int):
        """Return a human-readable breach description, or None when within limits."""
        if files > self.max_files:
            return f"files: {files} > {self.max_files}"
        if file_bytes > self.max_file_bytes:
            return f"file_bytes: {file_bytes} > {self.max_file_bytes}"
        if total_bytes > self.max_total_bytes:
            return f"total_bytes: {total_bytes} > {self.max_total_bytes}"
        return None
