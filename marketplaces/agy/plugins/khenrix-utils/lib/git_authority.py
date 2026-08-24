#!/usr/bin/env python3
"""Single fail-closed authority for production Git subprocesses.

Every caller supplies argv as a sequence.  This module owns the executable prefix and
environment so an exported Git repository, index, object store, config injection, pathspec
mode, diff program, attribute source, pager, prompt, or trace sink cannot silently change
what the caller observes (or execute code while it observes it).

The repository's own tracked ``.gitattributes`` and local config remain inputs where Git
normally needs them.  Dangerous behavior-bearing local settings are overridden explicitly:
hooks, fsmonitor, global excludes/attributes, external diff, credential helpers, and recursive
submodule traversal.  Review callers must still pass ``--no-ext-diff`` and ``--no-textconv``
because those flags express the stronger claim that the patch itself is native Git output.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Mapping, Sequence


# Remove the complete Git namespace, not a hand-maintained subset.  Git keeps adding
# behavior-bearing variables (GIT_ATTR_SOURCE and Trace2 are recent examples); a deny-list
# inevitably leaves the newest one live.  Controlled values are restored below.
_CONTROLLED_GIT_ENV = {
    "GIT_ATTR_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_NO_REPLACE_OBJECTS": "1",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_PAGER": "cat",
    "GIT_TERMINAL_PROMPT": "0",
}

# These non-GIT variables can independently re-enable an interactive helper/pager.  Keep
# unrelated process state intact: callers may deliberately pass HOME, locale, proxy, or test
# variables to the child, but none of those may regain Git config authority because the
# explicit GIT_CONFIG_* values above win.
_DROP_NON_GIT_ENV = frozenset({
    "GCM_INTERACTIVE", "SSH_ASKPASS", "SSH_ASKPASS_REQUIRE",
})

_FIXED_CONFIG = (
    f"core.hooksPath={os.devnull}",
    "core.fsmonitor=false",
    "core.untrackedCache=false",
    f"core.attributesFile={os.devnull}",
    f"core.excludesFile={os.devnull}",
    "core.askPass=",
    "core.editor=false",
    "credential.helper=",
    "credential.interactive=false",
    "diff.external=",
    "color.ui=false",
    "submodule.recurse=false",
    "fetch.recurseSubmodules=false",
    "protocol.ext.allow=never",
)


def sanitized_environment(
        base: Mapping[str, str] | None = None, *, literal_pathspecs: bool = True,
) -> dict[str, str]:
    """Return process state in which only this module controls Git behavior.

    ``literal_pathspecs=False`` is an explicit capability for the few call sites that build
    trusted ``:(exclude)`` pathspecs.  Ambient glob/noglob/icase settings are still removed;
    the caller merely permits the literal magic present in its own argv.
    """
    env = dict(os.environ if base is None else base)
    for key in list(env):
        if key.startswith("GIT_") or key in _DROP_NON_GIT_ENV:
            env.pop(key, None)
    env.update(_CONTROLLED_GIT_ENV)
    if literal_pathspecs:
        env["GIT_LITERAL_PATHSPECS"] = "1"
    return env


def command(
        args: Sequence[str | bytes | os.PathLike], *, repo: Path | str | None = None,
        literal_pathspecs: bool = True, config: Sequence[str] = (),
) -> list[str | bytes]:
    """Build the complete Git argv, keeping every authority option before the subcommand."""
    argv = ["git", "--no-pager", "--no-replace-objects"]
    if literal_pathspecs:
        argv.append("--literal-pathspecs")
    for item in (*_FIXED_CONFIG, *config):
        if not isinstance(item, str) or "\0" in item or "\n" in item or "\r" in item:
            raise ValueError(f"unsafe Git config atom {item!r}")
        argv.extend(("-c", item))
    if repo is not None:
        argv.extend(("-C", str(repo)))
    for arg in args:
        atom = os.fspath(arg) if isinstance(arg, os.PathLike) else arg
        if not isinstance(atom, (str, bytes)):
            raise TypeError(f"Git argv atoms must be str, bytes, or path-like, got {arg!r}")
        forbidden = ("\0",) if isinstance(atom, str) else (b"\0",)
        if any(item in atom for item in forbidden):
            raise ValueError(f"Git argv atom contains NUL: {arg!r}")
        argv.append(atom)
    return argv


def run(
        args: Sequence[str | bytes | os.PathLike], *, repo: Path | str | None = None,
        cwd: Path | str | None = None, literal_pathspecs: bool = True,
        config: Sequence[str] = (), env: Mapping[str, str] | None = None,
        runner=None, **kwargs,
) -> subprocess.CompletedProcess:
    """Run Git through the authority boundary.

    ``env`` is a BASE environment, never a bypass: it is sanitized again here even when a
    caller already obtained it from :func:`sanitized_environment`.
    """
    if "shell" in kwargs:
        raise ValueError("Git authority never accepts shell=; pass a literal argv sequence")
    execute = subprocess.run if runner is None else runner
    return execute(
        command(args, repo=repo, literal_pathspecs=literal_pathspecs, config=config),
        cwd=cwd, env=sanitized_environment(env, literal_pathspecs=literal_pathspecs),
        **kwargs,
    )


__all__ = ["command", "run", "sanitized_environment"]
