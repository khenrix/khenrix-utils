#!/usr/bin/env python3
"""§18's live three-provider write smoke: the one thing that proves the real provider path.

WHY THIS EXISTS. `forge/launch.py` and `forge/runner.py` both say in words that nothing in
the package's suite invokes a real provider — `launch` is injected everywhere and every test
passes a fake. About twenty thousand lines are therefore exercised against a stub, and the
adapter is the one seam a stub cannot stand in for. The council target beside this one is
one provider and read-only, which proves nothing about three write-enabled seats.

WHAT IT DOES, and it is deliberately the cheapest thing that proves the claim: a tiny
disposable repository, one clone per provider, each asked to write a distinct marker file in
its own clone and to quote the proof token; then the markers are harvested, and the ORIGINAL
checkout is shown unchanged. Three provider calls — roughly 15% of one default run.

WHAT IT COSTS AND WHY IT IS OPT-IN. It spends real money, so it is reachable only through
`make smoke-llm-forge` and appears in no gate target. `make verify` and `make precommit` must
stay free.

THE RECEIPT IS KEYED TO BOTH THINGS THAT CAN MOVE. A receipt naming only the adapter goes
green over a CLI that changed its argv; one naming only the CLIs goes green over an adapter
that stopped passing the scrubbed environment. Both, or the receipt certifies a path that
moved. A version this script could not READ makes the receipt stale rather than fresh — an
unread version is not an unchanged one.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "lib"))

from council import engine                       # noqa: E402
from forge import fleet, gitcmd, launch          # noqa: E402

# The adapter's source closure: every file whose change can alter what a provider is asked or
# what environment it is asked in. `seat` owns the spec, `launch` owns the prompt and the
# fingerprint, `fleet` owns the scrubbed environment, `engine` owns the invocation.
ADAPTER_SOURCES = (
    ROOT / "shared" / "lib" / "forge" / "launch.py",
    ROOT / "shared" / "lib" / "forge" / "seat.py",
    ROOT / "shared" / "lib" / "forge" / "fleet.py",
    ROOT / "shared" / "lib" / "council" / "engine.py",
)
PROVIDERS = ("claude", "codex", "agy")
RECEIPT = ROOT / "evals" / "llm-forge" / "smoke-receipt.json"
MARKER = "FORGE-SMOKE.txt"


def adapter_hash() -> str:
    """One digest over the adapter source closure, in a fixed order."""
    h = hashlib.sha256()
    for p in ADAPTER_SOURCES:
        h.update(p.name.encode("utf-8"))
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def cli_versions() -> dict:
    """`{provider: version string | None}`. `None` is "could not be read" and is never "".

    A provider that is not installed and one whose `--version` failed are the same answer here
    on purpose — both mean this script did not learn what version would run — and both make a
    receipt stale. What they are NOT the same as is a provider that ran and wrote nothing,
    which is a defect in the adapter and is reported separately by the smoke itself.
    """
    out = {}
    for name in PROVIDERS:
        exe = shutil.which(name)
        if exe is None:
            out[name] = None
            continue
        try:
            r = subprocess.run([exe, "--version"], capture_output=True, text=True,
                               timeout=30, check=False)
        except (OSError, subprocess.SubprocessError):
            out[name] = None
            continue
        out[name] = r.stdout.strip() or r.stderr.strip() or None
    return out


def receipt_is_fresh(receipt, *, adapter: str, versions: dict) -> tuple:
    """`(ok, why)`. `ok` is True only when every keyed fact matches something READ."""
    if not isinstance(receipt, dict):
        return False, "there is no smoke receipt"
    if receipt.get("adapter_sha256") != adapter:
        return False, "the adapter source changed since the last live smoke"
    recorded = receipt.get("cli_versions")
    if not isinstance(recorded, dict):
        return False, "the smoke receipt records no CLI versions"
    for name in PROVIDERS:
        now, then = versions.get(name), recorded.get(name)
        if now is None:
            return False, (f"{name}'s version could not be read, so the receipt cannot say "
                           "the path it certified is the path that would run")
        if then != now:
            return False, f"{name} moved from {then!r} to {now!r} since the last live smoke"
    return True, ""


def _disposable_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir(parents=True)
    (repo / "README.md").write_text("forge smoke\n", encoding="utf-8")
    gitcmd.git(repo, "init", "-q", "-b", "main")
    gitcmd.git(repo, "config", "user.name", "forge-smoke")
    gitcmd.git(repo, "config", "user.email", "forge-smoke@example.invalid")
    gitcmd.git(repo, "add", "README.md")
    gitcmd.git(repo, *gitcmd.NO_HOOKS, "commit", "-q", "-m", "base")
    return repo


def _prompt() -> str:
    return (f"Create a file named exactly {MARKER} in your current working directory. Its "
            "only content must be one line: the word FORGE-SMOKE followed by a space and "
            "then your own CLI's name (claude, codex or agy). Do not modify any other file. "
            "Then reply with the proof token you were given and nothing else.")


def _run_one(name: str, repo: Path, root: Path, timeout: int) -> dict:
    """One provider, in its OWN clone, through the production adapter.

    THROUGH `launch.make_launcher` AND NOTHING ELSE. A smoke that built its own spec would
    prove that `run_provider` works and nothing about the adapter, which is the only untested
    seam and the whole reason this file exists.
    """
    dest = root / "clones" / name
    baseline_ref = "refs/khenrix-forge/smoke00/base"
    gitcmd.git(repo, *gitcmd.NO_HOOKS, "update-ref", baseline_ref, "HEAD")
    head = gitcmd.git(repo, "rev-parse", "HEAD", env_extra=gitcmd.READONLY).stdout.strip()

    class _At:
        """THREE FIELDS, BECAUSE `clone_seat` READS THREE. `.ref` names the clone source and
        carries the run id at index 2; `.commit` is compared against the clone's HEAD and
        raises on a mismatch; `.filesystem_manifest` is walked as `(… or {})`. A shim carrying
        only `.ref` raises `AttributeError`, which is not a `FleetError` — so this script
        could not run at all, which would be the one task whose purpose is proving the real
        provider path works failing before it reached a provider.
        """
        ref = baseline_ref
        commit = head
        filesystem_manifest = {}

    fleet.clone_seat(repo, _At(), dest, name=name,
                     identity=("forge-smoke", "forge-smoke@example.invalid"))
    launcher = launch.make_launcher(prompt=_prompt(), timeout=timeout)
    token = engine.make_sentinel()
    env = fleet.forge_child_env(repo)
    record = launcher(name=name, seat_path=dest, token=token, env=env)

    marker = dest / MARKER
    wrote = marker.is_file()
    quoted = token in str(record.get("result_text") or "")
    return {"provider": name, "wrote_marker": wrote, "quoted_token": quoted,
            "marker_text": marker.read_text(encoding="utf-8").strip() if wrote else None,
            "valid": bool(record.get("valid")), "exit_code": record.get("exit_code")}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="forge_smoke", description=__doc__.splitlines()[0])
    ap.add_argument("--providers", default=",".join(PROVIDERS))
    ap.add_argument("--timeout", type=int, default=None)
    args = ap.parse_args(argv)

    window = args.timeout or engine.MODE_TIMEOUT.get("forge")
    if not isinstance(window, int) or window < 1:
        print("  ✗ council.engine.MODE_TIMEOUT has no usable `forge` entry", file=sys.stderr)
        return 1
    names = [n for n in args.providers.split(",") if n]

    root = Path(tempfile.mkdtemp(prefix="forge-smoke-"))
    # BOUND BEFORE THE `try`, because the `finally` reads it: a failure anywhere below would
    # otherwise leave the reclaim branch raising `NameError` on top of the real error, and the
    # tree would be kept for the wrong reason with the reason lost.
    ok = False
    try:
        repo = _disposable_repo(root)
        before = gitcmd.git(repo, "status", "--porcelain",
                            env_extra=gitcmd.READONLY).stdout
        head = gitcmd.git(repo, "rev-parse", "HEAD", env_extra=gitcmd.READONLY).stdout.strip()

        results = [_run_one(n, repo, root, window) for n in names]

        after = gitcmd.git(repo, "status", "--porcelain",
                           env_extra=gitcmd.READONLY).stdout
        head2 = gitcmd.git(repo, "rev-parse", "HEAD", env_extra=gitcmd.READONLY).stdout.strip()
        untouched = (before == after and head == head2)

        for r in results:
            mark = "✓" if (r["wrote_marker"] and r["quoted_token"]) else "✗"
            print(f"  {mark} {r['provider']}: marker={r['wrote_marker']} "
                  f"token={r['quoted_token']} valid={r['valid']} exit={r['exit_code']}")
        print(f"  {'✓' if untouched else '✗'} original checkout unchanged")

        ok = untouched and all(r["wrote_marker"] and r["quoted_token"] for r in results)
        if ok:
            RECEIPT.parent.mkdir(parents=True, exist_ok=True)
            RECEIPT.write_text(json.dumps({
                "adapter_sha256": adapter_hash(),
                "cli_versions": cli_versions(),
                "providers": results,
                "original_checkout_unchanged": untouched,
            }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            print(f"  receipt: {RECEIPT}")
        else:
            # NO RECEIPT ON A FAILED SMOKE, for `handover.write_handover`'s reason one gate
            # over: a receipt is read as a certification, and one written over a failure is a
            # false claim of exactly the kind the gate exists to catch.
            print("  ✗ smoke FAILED — no receipt written", file=sys.stderr)
        return 0 if ok else 1
    finally:
        # RECLAIMED ON SUCCESS, KEPT ON FAILURE, AND THE PATH IS PRINTED EITHER WAY. Three
        # `--no-local --no-hardlinks` clones of the disposable repository is not much, but
        # "left for inspection" over a green run is an unbounded leak per invocation with
        # nothing that ever collects it — the leak `--gc` exists to stop one directory over.
        # A FAILED run is exactly when the tree is the evidence, so that one stays: reclaiming
        # it would delete the artifact the ✗ line is telling the reader to go and look at.
        if ok:
            shutil.rmtree(root, ignore_errors=True)
            print(f"  reclaimed: {root}")
        else:
            print(f"  artifacts kept for inspection: {root}")


if __name__ == "__main__":
    raise SystemExit(main())
