"""Owner-only certificate issuance; never launches a provider or creates keys."""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    bootstrap_path = Path(__file__).resolve().parents[1] / "shared/skills/llm-fanout-execute/scripts/execute.py"
    bootstrap_spec = importlib.util.spec_from_file_location("_fanout_checkout_bootstrap", bootstrap_path)
    bootstrap = importlib.util.module_from_spec(bootstrap_spec)
    sys.modules[bootstrap_spec.name] = bootstrap
    bootstrap_spec.loader.exec_module(bootstrap)
    bootstrap._runtime()

from shared.lib.fanout.artifacts import canonical_json
from shared.lib.fanout.container_certification import CertificateAuthority, _owner_paths
from shared.lib.fanout.container_routes import _open_parent
from scripts.fanout_container_certify import _write_private


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--issue-trial", action="store_true")
    mode.add_argument("--issue-release", action="store_true")
    parser.add_argument("--probe-root", type=Path, required=True)
    parser.add_argument("--synthetic-run-root", type=Path)
    parser.add_argument("--caller-root", type=Path, action="append", default=[])
    parser.add_argument("--trial-root", type=Path)
    parser.add_argument("--evaluation-root", type=Path)
    parser.add_argument("--output", type=Path)
    options = parser.parse_args(argv)
    try:
        trial_key, release_key, default_release = _owner_paths()
        authority = CertificateAuthority(trial_key, release_key)
        if options.issue_trial:
            if options.synthetic_run_root is None or options.caller_root or options.trial_root or options.evaluation_root:
                raise ValueError("trial prerequisites")
            roots = tuple(options.synthetic_run_root / "targets" / name for name in ("repo-alpha", "repo-beta"))
            certificate = authority.issue_trial(options.probe_root, roots)
            output = options.output or options.synthetic_run_root / "trial-certificate.json"
        else:
            if not options.caller_root or options.trial_root is None or options.evaluation_root is None or options.synthetic_run_root:
                raise ValueError("release prerequisites")
            certificate = authority.issue_release(options.probe_root, tuple(options.caller_root),
                trial_root=options.trial_root, evaluation_root=options.evaluation_root)
            output = options.output or default_release
        parent = _open_parent(output)
        os.close(parent)
        _write_private(output, canonical_json(certificate.to_dict()))
        return 0
    except Exception:
        print("certificate issuance prerequisites are unavailable", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
