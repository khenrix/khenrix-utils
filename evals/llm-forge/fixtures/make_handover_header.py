#!/usr/bin/env python3
"""Regenerate `handover-header.txt` BY RUNNING THE RENDERER.

    python3 evals/llm-forge/fixtures/make_handover_header.py

A hand-written header drifts from `handover.text` the first time either moves, and the eval
beside it would then grade an artifact this engine does not emit. So every line of the fixture
comes out of the engine: the mergeability sentence is `handover.mergeability`'s own, computed
against a throwaway git repository built to match `repo-state/` (dirty tracked file, one
ignored artifact, one selected untracked path), and the header is `handover.text`.

The throwaway repository is created under a temp directory and removed. Nothing here touches
the repository this script lives in.

The run it describes: three seats, two completed, one passing verify; PATCH_ONLY because the
baseline was dirty AND one out-of-band artifact exists; `strongest = (None, why)`;
`agreement = "differently-prompted"`; `synthesis_measured = False`.
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

from forge import deepreview, handover  # noqa: E402

RUN_ID = "a3f9c1"
SYNTH = "/home/you/.local/state/khenrix-forge/9d41f0a2-a3f9c1/synthesis"


def _git(cwd, *argv):
    # PINNED DATES, NOT JUST IDENTITY. Commit OIDs hash the timestamps too, so identity alone
    # left the seed commit's hash tracking wall-clock time — and `eval_set_hash` covers this
    # fixtures tree, so an innocent regeneration staled the receipt with nothing semantic
    # changed. Fixed dates make the header byte-reproducible.
    stamp = "2026-01-01T00:00:00 +0000"
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
           "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
           "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
           "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
    return subprocess.run(["git", *argv], cwd=str(cwd), env=env, check=True,
                          capture_output=True, text=True).stdout.strip()


class _Manifest:
    """The three fields `handover.mergeability` reads. A real `runstate.Manifest` would need a
    whole run directory to exist; what the function actually uses is these."""

    def __init__(self, repo_path, run_id, base_commit, tracked_tree_oid):
        self.repo_path = repo_path
        self.run_id = run_id
        self.base_commit = base_commit
        self.tracked_tree_oid = tracked_tree_oid


class _Sidecar:
    def __init__(self, path, payload):
        self.path = path
        self.payload = payload


def build() -> str:
    tmp = Path(tempfile.mkdtemp(prefix="forge-header-fixture-"))
    try:
        repo = tmp / "repo"
        (repo / "src").mkdir(parents=True)
        (repo / "tests").mkdir()
        (repo / ".gitignore").write_text("dist/\n.venv/\n")
        (repo / "Makefile").write_text("verify:\n\tpython3 -m compileall -q src\n")
        (repo / "src" / "app.py").write_text("VERSION = 1\n")
        (repo / "tests" / "test_app.py").write_text("def test_it():\n    assert True\n")
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "seed")
        base_commit = _git(repo, "rev-parse", "HEAD")

        # B1: the dirty edit to a TRACKED file plus the --select'ed untracked path, committed
        # as the baseline. Its tracked tree is therefore not base^{tree} — which is the first
        # of the two PATCH_ONLY reasons.
        (repo / "src" / "app.py").write_text("VERSION = 2\n")
        (repo / "scratch").mkdir()
        (repo / "scratch" / "notes.md").write_text("scratch\n")
        _git(repo, "add", "-A", "--", ".")
        b1 = _git(repo, "commit-tree", "-p", base_commit, "-m", "B1",
                  _git(repo, "write-tree"))
        tracked_tree = _git(repo, "rev-parse", f"{b1}^{{tree}}")

        # The synthesis tree: anything that is not B1's tracked tree. `mergeability` refuses an
        # equal one, which is a worktree nobody fused into.
        (repo / "src" / "app.py").write_text("VERSION = 3\n")
        _git(repo, "add", "-A", "--", ".")
        synthesis_tree = _git(repo, "write-tree")

        payload = b"console.log('built');\n"
        sidecars = (_Sidecar("dist/bundle.js", payload),)
        manifest = _Manifest(repo, RUN_ID, base_commit, tracked_tree)
        merge = handover.mergeability(manifest, synthesis_tree_oid=synthesis_tree,
                                      sidecars=sidecars)
        oob = handover.out_of_band(sidecars, synthesis_path=SYNTH)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    h = handover.Handover(
        run_id=RUN_ID, branch=handover.branch(RUN_ID, handover.SYNTHESIS), kind=merge.kind,
        handover_target=None, accepted=True, out_of_band=oob,
        baseline_owned=("scratch/notes.md",),
        b1_files=(".gitignore", "Makefile", "scratch/notes.md", "src/app.py",
                  "tests/test_app.py"),
        why=merge.why)
    p = handover.Provenance(
        seats=(handover.SeatLine("claude", "completed", "usable", "PASS"),
               handover.SeatLine("codex", "completed", "usable", "FAIL"),
               handover.SeatLine("agy", "partial", "unusable", None)),
        synthesis_outcome="PASS",
        # False, and the CLI writes it as a constant: nothing measures the fusion.
        synthesis_measured=False,
        verify_command="make verify",
        # None, because an unmeasured verdict carries no duration.
        verify_seconds=None,
        # `strategy.STRATEGIES`, which is what the fusion FOLLOWED — a different vocabulary
        # from the answer sheet's strategy RULE (`size-gated`/`fusion`/`base-and-port`).
        strategy="from_scratch",
        strongest=(None, "no strongest seat can be named: this run recorded a claim ledger "
                         "and no coverage report over it, and the order is taken over the "
                         "coverage, the gate outcome, the review risk and the measured size "
                         "together — a rank over the one input that exists would order the "
                         "fleet on a quarter of the rubric"),
        agreement="differently-prompted",
        review_terminal=None, review_rounds=0, unresolved_findings=0,
        # A review that RAN and found nothing, from a full panel — the seats tuple is
        # what makes a degraded (1-of-3) review distinguishable from a complete one,
        # so a fixture that omitted it would pin a header shape the engine never emits.
        deep_review=deepreview.DeepReview(
            deepreview.RAN, None, (), ("claude", "codex", "agy"), True,
            "3 of 3 answering seat(s) produced a payload; 0 finding(s) over 1 file(s)",
            reporting=("claude", "codex", "agy")))
    return handover.text(h, p)


if __name__ == "__main__":
    out = Path(__file__).resolve().parent / "handover-header.txt"
    out.write_text(build())
    print(f"wrote {out}")
