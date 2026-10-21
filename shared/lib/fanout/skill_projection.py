"""Reviewed prompt file sets, independent of complete source-tree admission."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from .errors import SkillAdmissionError

if TYPE_CHECKING:
    from .skills import ResolvedSkill


SCHEMA = "fanout-skill-projection-v1"
_INSTRUCTIONS = {
    ".md", ".markdown", ".mdown", ".mkd", ".mkdn", ".mdwn",
    ".txt", ".text", ".rst", ".rest", ".adoc", ".asciidoc", ".asc",
}


@dataclass(frozen=True, slots=True)
class SkillProjection:
    """Exact reviewed trees and explicit inline sets; unknown trees stay complete."""

    sha256: str
    reviews: tuple[tuple[str, frozenset[str], frozenset[str]], ...]

    @classmethod
    def from_bytes(cls, raw: bytes) -> "SkillProjection":
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise SkillAdmissionError("duplicate skill projection field")
                result[key] = value
            return result

        try:
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
        except (ValueError, UnicodeDecodeError, RecursionError) as error:
            raise SkillAdmissionError("skill projection policy is malformed") from error
        if (not isinstance(value, dict) or set(value) != {"schema_version", "reviews"}
                or value["schema_version"] != SCHEMA or not isinstance(value["reviews"], list)
                or len(value["reviews"]) > 256):
            raise SkillAdmissionError("skill projection policy schema is invalid")
        reviews = []
        for review in value["reviews"]:
            if (not isinstance(review, dict)
                    or set(review) != {"tree_hash", "inline_files", "required_resources"}
                    or not isinstance(review["tree_hash"], str)
                    or not re.fullmatch(r"sha256:[0-9a-f]{64}", review["tree_hash"])):
                raise SkillAdmissionError("skill projection review is invalid")
            inline, required = (_paths(review[key]) for key in ("inline_files", "required_resources"))
            if "SKILL.md" not in inline or not required <= inline:
                raise SkillAdmissionError("skill projection omits required resources")
            reviews.append((review["tree_hash"], inline, required))
        if len({tree for tree, _, _ in reviews}) != len(reviews):
            raise SkillAdmissionError("duplicate skill projection tree")
        return cls(hashlib.sha256(raw).hexdigest(), tuple(reviews))

    def inline_files(self, skill: "ResolvedSkill") -> frozenset[str]:
        available = frozenset(path for path, *_ in skill.manifest)
        for tree_hash, inline, required in self.reviews:
            if tree_hash != skill.tree_hash:
                continue
            if not inline <= available or not required <= available:
                raise SkillAdmissionError("skill projection names an unknown file")
            instructions = {path for path in available
                            if PurePosixPath(path).suffix.casefold() in _INSTRUCTIONS}
            if not instructions <= inline:
                raise SkillAdmissionError("skill projection omits instruction documents")
            return inline
        return available

    def reviewed(self, skill: "ResolvedSkill") -> bool:
        return any(tree == skill.tree_hash for tree, _, _ in self.reviews)


def _paths(value: object) -> frozenset[str]:
    if not isinstance(value, list) or len(value) > 256:
        raise SkillAdmissionError("skill projection file set is invalid")
    for path in value:
        if (not isinstance(path, str) or not path or "\\" in path or ":" in path
                or "\x00" in path or PurePosixPath(path).is_absolute()
                or any(part in {"", ".", ".."} for part in path.split("/"))):
            raise SkillAdmissionError("skill projection file path is unsafe")
    if len(set(value)) != len(value):
        raise SkillAdmissionError("duplicate skill projection file")
    return frozenset(value)
