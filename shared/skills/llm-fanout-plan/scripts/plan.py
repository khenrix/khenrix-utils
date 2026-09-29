"""Read-only CLI for byte-bound fanout planning inputs."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path


def _runtime():
    skill_root = Path(__file__).resolve().parents[1]
    candidates = (skill_root / "lib/fanout", skill_root.parent.parent / "lib/fanout")
    runtime_root = next((path for path in candidates if (path / "__init__.py").is_file()), None)
    if runtime_root is None:
        raise ValueError("bundled fanout runtime is missing")
    spec = importlib.util.spec_from_file_location(
        "_fanout_plan_cli_runtime", runtime_root / "__init__.py",
        submodule_search_locations=[str(runtime_root)],
    )
    if spec is None or spec.loader is None:
        raise ValueError("bundled fanout runtime could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _read(path: str, *, limit: int) -> bytes:
    source = Path(path)
    if not source.is_file() or source.stat().st_size > limit:
        raise ValueError(f"input must be a regular file no larger than {limit} bytes: {path}")
    raw = source.read_bytes()
    if len(raw) > limit:
        raise ValueError(f"input exceeds {limit} bytes: {path}")
    return raw


def _pairs(items: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in items:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _json(raw: bytes) -> object:
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                      parse_constant=lambda value: _bad_constant(value))


def _bad_constant(value: str) -> object:
    raise ValueError(f"unsupported JSON constant: {value}")


def _resolver(runtime, roots: list[str]):
    ordered = []
    for index, root in enumerate(roots):
        path = Path(root)
        if not path.is_dir():
            raise ValueError(f"skill root does not exist: {root}")
        ordered.append(runtime.SkillRoot(f"root-{index}", path, index))
    return runtime.SkillResolver(tuple(ordered))


def _compile(runtime, source: bytes, source_path: str, draft: bytes, roots: list[str]):
    compiler = load_compiler_for_source_schema(runtime, source, source_path)
    return compiler(
        source, source_path=source_path, draft_bytes=draft,
        resolver=_resolver(runtime, roots),
    )


def load_compiler_for_source_schema(runtime, source: bytes, source_path: str):
    """Select the compiler only after a strict source parser recognizes its schema."""
    compiler_module = sys.modules[f"{runtime.__name__}.compiler"]
    try:
        runtime.parse_superpowers_plan(source, source_path=source_path)
    except runtime.CompilerError:
        compiler_module.parse_superpowers_plan_v2(source, source_path=source_path)
        return compiler_module.compile_superpowers_plan_v2
    return runtime.compile_superpowers_plan


def _question_source(question: str, tier: str) -> str:
    encoded = json.dumps(question, ensure_ascii=False)
    return "\n".join([
        "# Direct Question Implementation Plan", "",
        "**Goal:** Answer the invoking question using one read-only fanout task.", "",
        "**Architecture:** One task, with all configured CLIs as its executor seats.", "",
        "**Tech Stack:** Provider-neutral fanout runtime.", "",
        "**Spec:** `direct-question`", "", "## Global Constraints", "",
        "- The invocation authorizes model calls for this read-only question.",
        "- Repository writes and external actions are not authorized.",
        f"- Quality tier: {tier}.", "", "### Task 1: Answer question", "",
        "**Files:**", "",
        "- [ ] **Step 1: Analyze and synthesize**", "  **Depends on:** none", "",
        "Question JSON:", "```json", encoded, "```", "",
    ])


def _question_draft(source: bytes, skills: tuple[str, ...], none_reason: str | None,
                    executors: tuple[str, ...], tier: str) -> dict[str, object]:
    return {
        "schema_version": "fanout-draft-v1",
        "source_sha256": hashlib.sha256(source).hexdigest(),
        "defaults": {"executor_ids": list(executors), "rounds": 2,
                     "timeout": 900 if tier == "normal" else None,
                     "retries": 0, "minimum_success": 2,
                     "quality_tier": "standard" if tier == "normal" else "deep"},
        "tasks": [{
            "id": "answer", "kind": "work", "parent_id": None,
            "title": "Answer the question", "objective": "Produce an evidence-backed answer to the exact invoking question.",
            "source_step_ids": ["Task 1/Step 1"], "depends_on": [],
            "execution_class": "read-only", "required_skills": list(skills),
            "none_reason": none_reason,
            "owned_paths": [], "acceptance": ["A reconciled answer addresses the invoking question."],
            "checks": [], "provider_policy": None,
        }],
    }


def _emit(packet: object, runtime) -> None:
    sys.stdout.buffer.write(runtime.canonical_json(packet))


def _ingress_packet(runtime, *, kind: str, source: str, draft: dict[str, object],
                    compiled, admission: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": "fanout-plan-admission-" + compiled.plan.schema_version, "kind": kind,
        "admission": admission, "source_markdown": source,
        "draft": draft, "compiled": compiled.to_dict(),
    }


def _from_question(args: argparse.Namespace, runtime) -> None:
    if (not args.skill and not args.none_reason) or (args.skill and args.none_reason):
        raise ValueError("declare executor --skill or a reviewed --none-reason, exactly one")
    raw = _read(args.question_file, limit=1_000_000)
    question = raw.decode("utf-8")
    if not question.strip() or "\x00" in question:
        raise ValueError("question must contain nonempty UTF-8 text without NUL")
    source = _question_source(question, args.tier)
    source_bytes = source.encode("utf-8")
    executors = tuple(args.executor) if args.executor else ("claude", "codex", "agy")
    draft = _question_draft(source_bytes, tuple(args.skill), args.none_reason, executors, args.tier)
    source_path = "fanout-inputs/question-" + hashlib.sha256(raw).hexdigest()[:16] + ".md"
    compiled = _compile(runtime, source_bytes, source_path, runtime.canonical_json(draft), args.skill_root)
    _emit(_ingress_packet(
        runtime, kind="question", source=source, draft=draft, compiled=compiled,
        admission={"kind": "direct-question", "approval": args.approval, "quality_tier": args.tier},
    ), runtime)


def _from_bundle(args: argparse.Namespace, runtime) -> None:
    packet = _json(_read(args.bundle, limit=4_000_000))
    if not isinstance(packet, dict) or "quality_tier" not in packet:
        raise ValueError("bundle must declare quality_tier explicitly")
    if "owner_review" in packet:
        raise ValueError("owner review must be supplied out of band, never inside a bundle")
    if (set(packet) != {"schema_version", "source_path", "source_markdown", "draft", "quality_tier"}
            or packet["schema_version"] not in {"fanout-bundle-ingress-v1", "fanout-bundle-ingress-v2"}):
        raise ValueError("bundle must be a supported fanout-bundle-ingress object")
    if packet["quality_tier"] not in {"normal", "deep"}:
        raise ValueError("bundle quality_tier must be normal or deep")
    source, source_path, draft = packet["source_markdown"], packet["source_path"], packet["draft"]
    if not isinstance(source, str) or not isinstance(source_path, str) or not isinstance(draft, dict):
        raise ValueError("bundle source_path, source_markdown, and draft have invalid types")
    version = packet["schema_version"].removeprefix("fanout-bundle-ingress-")
    binding = hashlib.sha256(runtime.canonical_json(
        packet if version == "v1" else
        {"schema_version": "fanout-owner-review-binding-v2", "bundle": packet}
    )).hexdigest()
    review_schema = "fanout-owner-review-" + version
    review = None
    if args.owner_review_file is not None:
        if Path(args.owner_review_file).resolve() == Path(args.bundle).resolve():
            raise ValueError("owner review must be supplied separately from the bundle")
        review = _json(_read(args.owner_review_file, limit=16_384))
    tasks = draft.get("tasks", [])
    if not isinstance(tasks, list):
        raise ValueError("bundle draft tasks must be an array")
    needs_review = any(isinstance(task, dict) and task.get("kind") == "work"
                       and task.get("execution_class") in {"repo-write", "orchestrator-action"}
                       for task in tasks)
    if packet["quality_tier"] == "deep" and needs_review:
        raise ValueError("quality_tier=deep requires read-only work throughout the bundle")
    if needs_review:
        if (not isinstance(review, dict) or set(review) != {"schema_version", "reviewer", "binding_sha256"}
                or review["schema_version"] != review_schema
                or not isinstance(review["reviewer"], str) or not review["reviewer"].strip()
                or review["binding_sha256"] != binding):
            raise ValueError("owner review file must explicitly bind the exact repository-writing bundle")
    elif review is not None:
        if (not isinstance(review, dict) or set(review) != {"schema_version", "reviewer", "binding_sha256"}
                or review["schema_version"] != review_schema
                or review["binding_sha256"] != binding):
            raise ValueError("owner review binding is invalid")
    compiled = _compile(runtime, source.encode("utf-8"), source_path,
                        runtime.canonical_json(draft), args.skill_root)
    if compiled.plan.schema_version != version:
        raise ValueError("bundle schema_version differs from approved source and draft")
    selected_tier = "standard" if packet["quality_tier"] == "normal" else "deep"
    if compiled.plan.defaults.quality_tier != selected_tier:
        raise ValueError("bundle quality_tier differs from draft defaults")
    _emit(_ingress_packet(
        runtime, kind="bundle", source=source, draft=draft, compiled=compiled,
        admission={"kind": "task-bundle", "quality_tier": packet["quality_tier"], "owner_review": review},
    ), runtime)


def _source_draft(args: argparse.Namespace, runtime):
    source = _read(args.source, limit=4_000_000)
    draft = _read(args.draft, limit=4_000_000)
    return _compile(runtime, source, args.source_path, draft, args.skill_root)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    question = commands.add_parser("from-question", help="admit one direct read-only question")
    question.add_argument("--question-file", required=True)
    question.add_argument("--approval", required=True, choices=("invocation-authorized",))
    question.add_argument("--tier", required=True, choices=("normal", "deep"))
    question.add_argument("--skill", action="append", default=[])
    question.add_argument("--executor", action="append", help="registry executor ID; repeat to replace defaults")
    question.add_argument("--none-reason")
    question.add_argument("--skill-root", action="append", required=True,
                          help="ordered installed skill root; repeat for split roots and shadow checks")
    bundle = commands.add_parser("from-bundle", help="admit a reviewed task bundle")
    bundle.add_argument("--bundle", required=True)
    bundle.add_argument("--owner-review-file")
    bundle.add_argument("--skill-root", action="append", required=True,
                        help="ordered installed skill root; repeat for split roots and shadow checks")
    for name in ("compile", "validate"):
        command = commands.add_parser(name, help="compile or revalidate exact source and draft")
        command.add_argument("--source", required=True)
        command.add_argument("--source-path", required=True)
        command.add_argument("--draft", required=True)
        command.add_argument("--skill-root", action="append", required=True,
                             help="ordered installed skill root; repeat for split roots and shadow checks")
        if name == "validate":
            command.add_argument("--compiled", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        runtime = _runtime()
        if args.command == "from-question":
            _from_question(args, runtime)
        elif args.command == "from-bundle":
            _from_bundle(args, runtime)
        else:
            compiled = _source_draft(args, runtime)
            if args.command == "compile":
                sys.stdout.buffer.write(compiled.to_bytes())
            else:
                recorded = _read(args.compiled, limit=4_000_000)
                if recorded != compiled.to_bytes():
                    raise ValueError("compiled plan does not match exact source, draft, and skill closure")
                _emit({"valid": True, "compiled_sha256": hashlib.sha256(recorded).hexdigest()}, runtime)
        return 0
    except (OSError, UnicodeError, ValueError, TypeError, RuntimeError) as error:
        print(f"fanout plan: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
