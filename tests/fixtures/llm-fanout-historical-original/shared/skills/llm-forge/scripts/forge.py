#!/usr/bin/env python3
"""Executable entry point for the llm-forge skill.

The engine is `forge.cli`, bundled at the plugin's `lib/forge/`. This resolves that directory
whether it is being run from the repository source tree or from an installed plugin copy, then
delegates. It contains no logic of its own, on `fanout.py`'s precedent: a façade that decides
anything is a second implementation.

ONE sys.path ENTRY COVERS BOTH PACKAGES, which is why no `PYTHONPATH` export accompanies this
in SKILL.md. `forge.cli` imports `council.engine` for the seat timeout window, and `council/`
sits beside `forge/` under the same `lib/` in both layouts — so exporting the variable would
be dead configuration for a resolution this file has already made.
"""
import sys
from pathlib import Path

_here = Path(__file__).resolve()

# ONE EXPRESSION, BOTH LIVE LAYOUTS — measured on both, not assumed. This script sits at
# `<root>/<skills-dir>/llm-forge/scripts/forge.py` in each of them, so `parents[3]` is the root
# and the engine is at `<root>/lib/forge/`:
#
#   installed plugin  .../plugins/khenrix-utils/skills/llm-forge/scripts/forge.py
#                     -> parents[3] = .../plugins/khenrix-utils   -> lib/forge/cli.py      ✓
#   repo source tree  shared/skills/llm-forge/scripts/forge.py
#                     -> parents[3] = shared                      -> shared/lib/forge/cli.py ✓
#
# A second candidate would be worse than none here rather than merely redundant. The two
# obvious ones do not reach anything: `parents[3]/"shared"/"lib"` resolves to `shared/shared/lib`
# in the repo and `.../khenrix-utils/shared/lib` in a plugin, neither of which exists; and
# `parents[4]/"shared"/"lib"` does reach the repo's `shared/lib`, but only from the repo, where
# the line below has already matched. A dead candidate reads as coverage of a case nobody has.
_lib = _here.parents[3] / "lib"
if not (_lib / "forge" / "cli.py").is_file():
    sys.exit(f"could not locate the forge engine: no forge/cli.py under {_lib}")
sys.path.insert(0, str(_lib))

from forge import cli  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(cli.main())
