"""Qualification-only Claude fixture turn using the shared native evaluator bridge."""
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import eval_harness as harness
import eval_claude_scratch_policy as policy


def run_text(prompt, cfg, workdir, *, claude_scratch_policy, timeout,
             base_env=None, real_home=harness.REAL_HOME, tool_canary=None, registry=None):
    """Run a fresh executor turn; controls fence only the dedicated MCP tool."""
    context = claude_scratch_policy
    cwd = Path(context["cwd"])
    child_env = dict(os.environ if base_env is None else base_env)
    control_environment = dict(child_env)
    prompt = ("Use your installed read tools and skills when relevant. Do not write "
              "repository or service data, send messages, or change configuration.\n\n" + prompt)
    spec = harness.eval_runtime.build_native_spec(
        "claude", prompt, cfg, cwd=cwd, role="executor", registry=registry,
    )
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    preexisting = {path.relative_to(workdir).as_posix()
                   for path in harness._artifact_files(workdir)}
    try:
        spec = policy.bind(spec, context, child_env)
        record = harness.eval_runtime.run(
            spec, workdir, retries=0, timeout=timeout,
            environment=child_env, real_home=real_home,
        )
        policy.check(context, environment=control_environment)
        record["claude_scratch_policy"] = {**context, "controls_unchanged": True}
        (workdir / "manifest.json").write_text(json.dumps({"providers": [record]}, indent=2) + "\n")
    finally:
        harness.guard_provider_artifacts(workdir, child_env, tool_canary or "", preexisting)
    answer = Path(record["result_file"]).read_text() if record.get("valid") else ""
    return answer, record
