"""Standalone PreToolUse handler copied into a private legacy council agy HOME."""
from __future__ import annotations

import json
import stat
import sys
from pathlib import Path


_PATH_FIELDS = {
    "view_file": ("AbsolutePath",),
    "find_by_name": ("SearchDirectory", "SearchPath", "Path"),
    "grep_search": ("SearchPath", "DirectoryPath", "Path"),
    "list_dir": ("DirectoryPath", "AbsolutePath", "Path"),
}


def _within_workspace(raw: object, root: Path) -> bool:
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        return False
    candidate = Path(raw)
    if ".." in candidate.parts:
        return False
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return False
    current = root
    try:
        for part in relative.parts:
            current = current / part
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode):
                return False
        return candidate.resolve(strict=True) == candidate
    except OSError:
        return False


def _decision(payload: object, policy: object) -> bool:
    if not isinstance(payload, dict) or not isinstance(policy, dict):
        return False
    raw_root = policy.get("workspace")
    if not isinstance(raw_root, str) or not Path(raw_root).is_absolute():
        return False
    root = Path(raw_root)
    if not _within_workspace(raw_root, root):
        return False
    if payload.get("workspacePaths") != [raw_root]:
        return False
    call = payload.get("toolCall")
    if not isinstance(call, dict):
        return False
    name, args = call.get("name"), call.get("args")
    if name == "finish":
        return isinstance(args, dict)
    fields = _PATH_FIELDS.get(name)
    if fields is None or not isinstance(args, dict):
        return False
    present = [field for field in fields if field in args]
    if len(present) != 1:
        return False
    if not _within_workspace(args[present[0]], root):
        return False
    for key, value in args.items():
        if key != present[0] and ("Path" in key or "Directory" in key or key.endswith("File")):
            if not _within_workspace(value, root):
                return False
    return True


def main() -> None:
    allowed = False
    try:
        policy_path = Path(__file__).with_name("policy.json")
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        payload = json.loads(sys.stdin.buffer.read(128 * 1024 + 1))
        allowed = _decision(payload, policy)
    except (OSError, ValueError, UnicodeError, TypeError):
        pass
    sys.stdout.write(json.dumps({
        "decision": "allow" if allowed else "deny",
        "reason": "council read-only seat tool policy",
    }))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
