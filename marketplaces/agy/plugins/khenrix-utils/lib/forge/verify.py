"""The gate runs where the builder never was, and the engine runs it (spec §6).

A check the builder could have rigged is not a check. A seat can replace `.venv/bin/pytest`,
delete an auto-discovered test file, or weaken the Makefile — so the candidate is
materialized into a CLONE BUILT FROM THE BASELINE and the gate runs there, never in the
seat. `build_verifier` is `fleet.clone_seat` plus `bundle.materialize`, so every defence the
seat clone already carries — no origin, no hardlinks, no ambient template, its own identity,
a checkout verified against B1's manifest — is reused rather than restated.

`SeatError` and `BundleError` PROPAGATE UNWRAPPED, on `harvest`'s precedent for
`SnapshotError`: both already name the thing that failed and are already RuntimeErrors, so
wrapping them would only put this module's name in front of the one that knows.

HOOKS. A hook that runs in a verifier is a builder-controlled check. Measured on git 2.53,
the two options the plan offered are NOT equivalent:

    source repo's local core.hooksPath        NOT copied by `git clone` — so there is
                                              nothing in the clone's config to unset
    clone config unset + a global hooksPath    the user's hook RAN, `git commit` exit 1
    clone config core.hooksPath=/dev/null      no hook ran, exit 0

So "unset it in the clone's config" is a no-op that leaves the global path live, and it is
the local /dev/null pin that works. It is also the only form that reaches the gate at all:
`git -c` covers one invocation (and, via the GIT_CONFIG_PARAMETERS it exports, that
invocation's git children), while a gate is `make` or `pytest` — not git — so nothing the
engine passes on a git command line is in scope by the time the gate runs its own git.

The pin doubles as a CANARY on the clone's config. `bundle.materialize` writes a sidecar
named `.git/config` without complaint — measured: its `_safe_rel` refuses an absolute or
`..` path but says nothing about `.git/…` — which would take the pin, `core.fsmonitor` and
`core.sshCommand` (both of which name a command git EXECUTES) and the clone's identity with
it. So the pin is written BEFORE the candidate is laid down and read back AFTER, and a
rewritten config is refused rather than repaired: overwriting only `core.hooksPath` would
leave every other builder-chosen key in place.

ENVIRONMENT. `run_command` gives the gate `gitcmd`'s own treatment — REDIRECTING_ENV
stripped, config discovery pinned at /dev/null — because the gate is where git actually
runs, and none of `gitcmd`'s presets are inherited by a subprocess the engine starts
directly. GIT_CONFIG_PARAMETERS is stripped IN ADDITION: it is not one of
`gitcmd.REDIRECTING_ENV`, it OUTRANKS the local pin (measured — the hook ran again), and
git exports it into every child whenever anything up the tree ran `git -c …`, so it is
ambient in ordinary use rather than exotic.

What `run_command` cannot do is scrub values that point back into the USER's checkout: it is
handed the verifier, not the repository the verifier was cloned from, and scrubbing against
the verifier's own path would remove exactly the entries that should stay. A caller holding
the repo path passes `env=fleet.forge_child_env(repo)`, which composes — this module's
strip and pin are idempotent over that result and add the name it misses.
"""
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import bundle, fleet, gitcmd

# The branch every verifier gets inside its own clone. Fixed rather than derived from
# `dest`: `clone_seat` turns this into `refs/heads/forge/<run-id>/<name>` and refuses a name
# that cannot be one, so feeding it a caller-chosen directory name would add a failure mode
# for no gain — the clones have no remotes, so two verifiers cannot collide.
VERIFIER_NAME = "verify"

# Ambient config injectors that are NOT in `gitcmd.REDIRECTING_ENV`. GIT_CONFIG_COUNT is
# (and is stripped with the rest); GIT_CONFIG_PARAMETERS is the pair that got away.
_ENV_INJECTORS = ("GIT_CONFIG_PARAMETERS",)

# Characters that mean something only to a SHELL. Applied to a step's PROGRAM NAME, never
# to its arguments: nothing here runs a shell, so `grep -E 'a|b'` and `find -name '*.py'`
# are ordinary steps whose metacharacter reaches the program literally, while a program
# name holding one can only be a command line the author expected a shell to split.
# `!` and `#` are deliberately absent — interactive-shell-only, and both occur in real
# filenames — and the whitespace test below catches the command lines they appear in.
_SHELL_META = frozenset("&;|<>$`\\\"'()[]{}*?\n\r")

# How long to wait for a killed process group to be reaped before giving up on its output.
# Bounded rather than unbounded because the second `communicate` is what drains the pipes:
# an unbounded wait there turns a step-level timeout back into the hang it exists to
# prevent, which is the failure this whole path is named after.
_REAP_GRACE = 10


class VerifyError(RuntimeError):
    """The gate could not be built, could not be run, or would not have been a gate."""


@dataclass(frozen=True)
class Step:
    """One process in a gate.

    `cwd` is RELATIVE to the verifier root and may not leave it — see `_step_cwd`. `env`
    is merged over the hardened base `run_command` builds, so a step can add a variable
    without a caller having to reconstruct the base.
    """
    argv: tuple[str, ...]
    cwd: str = ""
    env: dict = field(default_factory=dict)
    timeout: int = 600

    def __post_init__(self):
        # `Popen([])` raises IndexError from inside subprocess, which is neither this
        # module's failure type nor a message naming the step at fault. `Step` is public and
        # the brief's own timeout case constructs one directly, so the guard belongs here
        # rather than only in `parse`.
        if not self.argv:
            raise VerifyError("a verify step names no program: argv is empty")


@dataclass(frozen=True)
class Command:
    """A gate: the steps, in order, that decide whether a candidate passes.

    THE ACCEPTED INPUT SHAPE, stated once so it is unambiguous. `parse` takes a sequence of
    steps. A step IS an argv sequence — a list or tuple of strings. Its FIRST element is a
    program name, exec'd directly with no shell anywhere in the chain; the elements after it
    are arguments handed to that program verbatim.

    A bare string is NEVER a step, even one with no metacharacter in it. Accepting `"make"`
    as a one-token argv would make `"pytest -q"` — one space, nothing shell-special — a
    silent ENOENT at gate time instead of a named refusal at parse time, and a gate that
    fails for an infrastructure reason is the failure spec §4 is most insistent about.
    """
    steps: tuple[Step, ...] = ()

    @classmethod
    def parse(cls, spec) -> "Command":
        steps = []
        for i, raw in enumerate(spec):
            if isinstance(raw, (str, bytes)):
                raise VerifyError(_not_an_argv(i, raw))
            try:
                argv = tuple(raw)
            except TypeError as e:
                raise VerifyError(
                    f"verify step {i} is not a sequence of arguments: {raw!r}") from e
            if not argv:
                raise VerifyError(f"verify step {i} names no program: argv is empty")
            if not all(isinstance(t, str) for t in argv):
                raise VerifyError(f"verify step {i} has a non-string argument: {argv!r}")
            why = _shellish(argv[0])
            if why:
                raise VerifyError(
                    f"verify step {i} names a program that cannot exist: {argv[0]!r} "
                    f"{why}. Nothing here runs a shell, so it would be exec'd under that "
                    "literal name — put each command in its own argv list, "
                    'e.g. [["make", "verify"], ["./check.sh"]].')
            steps.append(Step(argv=argv))
        return cls(steps=tuple(steps))


@dataclass(frozen=True)
class Run:
    """The gate's verdict.

    `stdout`/`stderr` and `step_index` describe ONE step — the one that decided the verdict:
    the first to fail, or the last to run when none did. `duration_sec` is the whole
    command's wall time, which is the number a report wants.
    """
    exit_code: int
    stdout: str
    stderr: str
    duration_sec: float
    step_index: int


def _not_an_argv(index: int, raw) -> str:
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    found = next((c for c in text if c in _SHELL_META), "")
    if found:
        return (f"verify step {index} is a shell command line, not an argv list: {raw!r} "
                f"contains the shell metacharacter {found!r}. Nothing here runs a shell — "
                'write each command as its own argv list, e.g. [["make", "verify"], '
                '["rm", "-rf", "build"]].')
    return (f"verify step {index} must be an argv list, not a string: {raw!r}. Nothing "
            "here runs a shell, so a string would be exec'd as one program of that literal "
            f'name — write ["{text}"].')


def _shellish(token: str) -> str:
    """Why `token` cannot be a program name, or "" when it can be."""
    found = next((c for c in token if c in _SHELL_META), "")
    if found:
        return f"contains the shell metacharacter {found!r}"
    if any(c.isspace() for c in token):
        return "contains whitespace, so it is a command line rather than a program name"
    return ""


def _text(raw: bytes) -> str:
    """errors="replace", NOT `harvest`'s surrogateescape. Both exist to stop one latin-1
    byte raising UnicodeDecodeError out of a strict decode and taking the whole call with
    it; they differ in what happens next. `harvest`'s bytes are re-encoded into a patch, so
    it needs the reversible form. Gate output is read by a human and by a judge, and
    surrogates in it would raise again at the first `print` or `json.dumps`."""
    return raw.decode("utf-8", "replace")


def _gate_env(env=None) -> dict:
    base = dict(os.environ if env is None else env)
    for k in (*gitcmd.REDIRECTING_ENV, *_ENV_INJECTORS):
        base.pop(k, None)
    # LAST, and set rather than dropped: removing GIT_CONFIG_GLOBAL RESTORES ~/.gitconfig
    # and the core.hooksPath in it. Same argument `gitcmd.git` makes for the same pair.
    base.update(gitcmd.NO_USER_CONFIG)
    return base


def _step_cwd(root: Path, step: Step, index: int) -> Path:
    """Where a step runs — inside the verifier, or nowhere.

    Lexical, matching `bundle._safe_rel`: `..` in the parts and any absolute path are
    refused, and the check is on the TEXT rather than on a resolved path. A `Path(root) /
    "/etc"` is `/etc`, silently, so an unguarded absolute cwd runs the gate outside the
    clone the gate exists to be confined to.
    """
    rel = step.cwd or ""
    if not rel:
        return root
    if os.path.isabs(rel) or ".." in Path(rel).parts:
        raise VerifyError(
            f"verify step {index} asks to run in {rel!r}, which leaves the verifier; a "
            "step's cwd is relative to the clone root and must stay inside it")
    return root / rel


def _kill_group(p: subprocess.Popen) -> None:
    """SIGKILL the whole session, not the child.

    `subprocess.run`'s own timeout handling kills the DIRECT child only. A gate is `make`
    or `uvx`, so the process still holding the CPU — and still holding the stdout pipe open,
    which is what turns the next read into a second hang — is the grandchild that was doing
    the work. `start_new_session=True` at spawn is what makes the group addressable here.
    """
    try:
        pgid = os.getpgid(p.pid)
    except (ProcessLookupError, PermissionError):
        # SECOND LATCH, unpinned: `getpgid` answers for an unreaped child, so this arm is
        # not reachable from the timeout path. Kept for the reason `fleet` keeps
        # `--no-hardlinks` — it is what still holds if the group becomes unaddressable, and
        # the alternative is an OSError escaping a path whose whole contract is "a timeout
        # is a VerifyError".
        p.kill()
        return
    if pgid == os.getpgid(0):
        # NEVER killpg our own group. MEASURED, by mutating `start_new_session=True` to
        # False: the step then shares the ENGINE's process group and this line SIGKILLed
        # pytest itself — a lost timeout turning into engine suicide, silently, on every
        # gate that overruns. The guard is unpinned in unmutated code by construction
        # (the session split is what makes the ids differ), and it is the latch that keeps
        # the blast radius local if that split is ever lost.
        p.kill()
        return
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        p.kill()


def _run_step(step: Step, wd: Path, env: dict, index: int):
    try:
        p = subprocess.Popen(
            step.argv, cwd=str(wd), env=env,
            # A gate that prompts must fail, not hang. No test pins this: under pytest fd 0
            # is already a null-ish object, so removing it changes nothing HERE — it is the
            # engine's own tty, inherited by a `make` that asks a question, that this
            # closes, and reproducing that inside a test would mean rebuilding fd 0.
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True)
    except OSError as e:
        # The cwd is named as well as the argv: a missing working directory surfaces from
        # Popen as the same FileNotFoundError a missing program does, and blaming argv[0]
        # for it sends the reader to the wrong half of the step.
        raise VerifyError(
            f"verify step {index} could not be started: {list(step.argv)} in {wd}: {e}") from e
    try:
        out, err = p.communicate(timeout=step.timeout)
    except subprocess.TimeoutExpired:
        _kill_group(p)
        try:
            out, err = p.communicate(timeout=_REAP_GRACE)
        except subprocess.TimeoutExpired:
            # SECOND LATCH, unpinned: the group is already SIGKILLed, so the pipes are
            # closed and this drains at once. It exists so that a process the kernel will
            # not reap (uninterruptible I/O) costs `_REAP_GRACE` seconds rather than the
            # unbounded wait that would turn a step timeout back into the hang it names.
            out = err = b""
        raise VerifyError(
            f"verify step {index} exceeded its {step.timeout}s timeout and was killed with "
            f"its whole process group: {list(step.argv)}") from None
    return p.returncode, _text(out), _text(err)


def run_command(cwd, command: Command, *, env=None) -> Run:
    """Run `command`'s steps in `cwd`, stopping at the first that fails.

    Raises `VerifyError` when the gate could not be RUN — a step that would not start, a
    step that timed out, a cwd that leaves the tree, a command with nothing in it. A step
    that runs and exits non-zero is not an error: that is the gate answering, and it comes
    back as `Run.exit_code`.
    """
    root = Path(cwd)
    if not command.steps:
        # exit_code 0 from a command that ran nothing is this module's cardinal sin: a gate
        # that passes vacuously is indistinguishable, downstream, from a gate that passed.
        raise VerifyError(
            "this verify command has no steps; refusing to report success for a gate that "
            "would run nothing")
    base = _gate_env(env)
    started = time.monotonic()
    for i, step in enumerate(command.steps):
        wd = _step_cwd(root, step, i)
        code, out, err = _run_step(step, wd, {**base, **(step.env or {})}, i)
        if code != 0:
            break
    return Run(exit_code=code, stdout=out, stderr=err,
               duration_sec=time.monotonic() - started, step_index=i)


def _hooks_pin(path: Path) -> None:
    gitcmd.git(path, "config", "--local", "core.hooksPath", os.devnull)


def _assert_hooks_pinned(path: Path) -> None:
    # --local, so a global core.hooksPath cannot satisfy the check the local pin exists to
    # override. check=False: `--get` exits 1 for a missing key, which is a state to report
    # in this module's vocabulary, not a GitError.
    r = gitcmd.git(path, "config", "--local", "--get", "core.hooksPath",
                   env_extra=gitcmd.READONLY, check=False)
    if r.stdout.strip() != os.devnull:
        raise VerifyError(
            f"the candidate rewrote the verifier's git config: core.hooksPath is now "
            f"{r.stdout.strip()!r}, not {os.devnull!r}. A clone whose config the builder "
            "chose can run builder-supplied hooks — and core.fsmonitor and core.sshCommand "
            "name commands git executes — so it cannot be used as a verifier.")


def build_verifier(repo, baseline, candidate, dest, *, identity) -> Path:
    """A clone of the BASELINE with the candidate laid down in it, ready for the gate.

    `identity` is the verifier's own `(name, email)`, required for the same reason
    `clone_seat` requires one: §7.2 has the engine CHECKPOINT admitted generator output
    here, and a clone that cannot commit would fail that at the last moment.
    """
    seat = fleet.clone_seat(repo, baseline, dest, name=VERIFIER_NAME, identity=identity)
    # Before the candidate, so nothing between the clone and the gate runs unpinned; and
    # read back after it, because the candidate is the one thing in between that writes.
    _hooks_pin(seat.path)
    bundle.materialize(candidate, seat.path)
    _assert_hooks_pinned(seat.path)
    return seat.path
