"""The bounded Council/Forge retirement policy declared in capabilities.toml."""
from __future__ import annotations

import tomllib
from pathlib import Path


def historical_only(caps: dict) -> tuple[frozenset[str], frozenset[str]]:
    declaration = caps.get("historical_only")
    if declaration is None:
        raise ValueError("historical_only declaration is required for this capability manifest")
    if not isinstance(declaration, dict) or set(declaration) != {"skills", "libraries"}:
        raise ValueError("historical_only must declare exactly skills and libraries")
    for field in ("skills", "libraries"):
        values = declaration[field]
        if (not isinstance(values, list) or len(values) != 2
                or not all(isinstance(value, str) for value in values)
                or len(set(values)) != 2):
            raise ValueError(f"historical_only.{field} must contain two unique IDs")
    skills, libraries = declaration["skills"], declaration["libraries"]
    # This is a fixed retirement contract, not a general lifecycle declaration.
    if set(skills) != {"llm-council", "llm-forge"}:
        raise ValueError("historical_only.skills contains an unknown historical ID")
    if libraries != [skill.removeprefix("llm-") for skill in skills]:
        raise ValueError("historical_only.libraries has an unknown ID or wrong skill binding")
    return frozenset(skills), frozenset(libraries)


def read_historical(root: Path) -> tuple[frozenset[str], frozenset[str]]:
    manifest = root / "capabilities.toml"
    if not manifest.is_file():
        return frozenset(), frozenset()
    with manifest.open("rb") as handle:
        return historical_only(tomllib.load(handle))


def historical_paths(caps: dict) -> tuple[Path, ...]:
    skills, libraries = historical_only(caps)
    return tuple(Path("shared/skills") / name for name in sorted(skills)) + tuple(
        Path("shared/lib") / name for name in sorted(libraries))


def contains_historical(caps: dict, relative: Path) -> bool:
    """Reject a retired subtree, a descendant, or an ancestor bundling that subtree."""
    return any(relative == path or relative in path.parents or path in relative.parents
               for path in historical_paths(caps))
