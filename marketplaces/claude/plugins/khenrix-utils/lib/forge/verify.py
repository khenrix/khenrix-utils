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

The pin doubles as a CANARY on the clone's config: it is written BEFORE the candidate is
laid down and read back AFTER, because the candidate is the one thing in between that
writes. What the readback MEASURES is one key in two scopes — `core.hooksPath` is still
`/dev/null` both in the clone's LOCAL config and in the value git would obey there, so a
scope inside the clone that outranks the local file cannot move the pin unseen. What it
does NOT measure is any other key: a rewrite that keeps the pin and ADDS `core.fsmonitor`
(a program git executes on an ordinary `git status`) or its own `[user]` section passes the
canary, and that was measured, not reasoned about.

The route that made it a live defence is closed at its source: `bundle._safe_rel` now
refuses a `.git` COMPONENT, so `materialize` cannot write the clone's config at all. What
is left here is defence in depth against a bundle assembled some other way, and it is
deliberately NOT widened into a whole-config comparison — the honest baseline for that would
have to be captured after `clone_seat` and would then have to encode which keys a legitimate
materialization may move, which is a policy this module has no way to state truthfully.
It refuses rather than repairs for the same reason: overwriting just `core.hooksPath` would
leave every other builder-chosen key in place.

ENVIRONMENT. `run_command` gives the gate `gitcmd`'s own treatment — `HOSTILE_ENV` stripped,
config discovery pinned at /dev/null — because the gate is where git actually runs, and none
of `gitcmd`'s presets are inherited by a subprocess the engine starts directly. The list is
read from `gitcmd` and never extended here: a gate-only strip is how GIT_CONFIG_PARAMETERS
came to be closed for the gate while every seat still inherited it.

What `run_command` cannot do is scrub values that point back into the USER's checkout: it is
handed the verifier, not the repository the verifier was cloned from, and scrubbing against
the verifier's own path would remove exactly the entries that should stay. A caller holding
the repo path passes `env=fleet.forge_child_env(repo)`, which composes: that function drops
the same `HOSTILE_ENV` and pins the same /dev/null pair, so this module's strip and pin are
idempotent over its result and all it contributes is the checkout scrub.
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

# Characters that mean something only to a SHELL. Applied to a step's PROGRAM NAME, never
# to its arguments: nothing here runs a shell, so `grep -E 'a|b'` and `find -name '*.py'`
# are ordinary steps whose metacharacter reaches the program literally, while a program
# name holding one can only be a command line the author expected a shell to split.
# `!` and `#` are deliberately absent — interactive-shell-only, and both occur in real
# filenames — and the whitespace test below catches the command lines they appear in.
# `~` is absent for a different reason: it is not a membership question at all. It expands
# only at the START of a token, and it is an ordinary character everywhere else — `foo.py~`
# is a real filename and a legal program name — so it is tested positionally in `_shellish`
# rather than added here, where it would refuse the backup file along with the tilde path.
_SHELL_META = frozenset("&;|<>$`\\\"'()[]{}*?\n\r")

# How long to wait for a killed process group to be reaped before giving up on its output.
# Bounded rather than unbounded because the second `communicate` is what drains the pipes:
# an unbounded wait there turns a step-level timeout back into the hang it exists to
# prevent, which is the failure this whole path is named after.
_REAP_GRACE = 10

# How much of a killed step's output the timeout message carries. Enough for a pytest or
# `make` tail to name the test that hung, and bounded because the message travels into a
# report and a judge prompt.
_TAIL_CHARS = 2000


class VerifyError(RuntimeError):
    """The gate could not be built, could not be run, or would not have been a gate."""


@dataclass(frozen=True)
class Step:
    """One process in a gate.

    `cwd` is RELATIVE to the verifier root and may not leave it — see `_step_cwd`. `env`
    is merged over the hardened base `run_command` builds, so a step can add a variable
    without a caller having to reconstruct the base; the merged result is re-hardened, so
    a step cannot re-admit a git redirector the base had dropped.
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
        # The SAME argument, applied to the same rule `Command.parse` enforces on argv[0]:
        # a `Step` built directly used to accept `("make verify",)` and fail at gate time
        # with an ENOENT naming a program nobody meant to run. Both of parse's preconditions
        # come with it — `_shellish` iterates its argument, so a non-string argv[0] would
        # raise a raw TypeError out of the one path whose whole job is a named refusal.
        if not all(isinstance(t, str) for t in self.argv):
            raise VerifyError(f"a verify step has a non-string argument: {self.argv!r}")
        why = _shellish(self.argv[0])
        if why:
            raise VerifyError(
                f"a verify step names a program that cannot exist: {self.argv[0]!r} {why}. "
                "Nothing here runs a shell, so it would be exec'd under that literal name.")


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
        if isinstance(spec, (str, bytes)):
            # A whole spec that is one string, not a LIST of them. Guarded here because
            # iterating it yields CHARACTERS: without this, `parse("make")` refused with
            # "verify step 0 must be an argv list, not a string: 'm'" — a refusal, so
            # fail-closed, but one that names a step and a value the caller never wrote.
            raise VerifyError(
                f"a verify command is a LIST of steps, not one string: {spec!r}. Nothing "
                'here runs a shell — write [["make", "verify"]].')
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
    if token.startswith("~"):
        # LEADING only. `~/bin/tool` parses clean and then ENOENTs at gate time — exactly
        # the outcome the bare-string refusal exists to prevent — because the home directory
        # is expanded by the shell, not by execve. A trailing `~` is an ordinary filename
        # (`./build.sh~`) and stays legal.
        return ("begins with '~', which only a shell expands; write the path out, since "
                "execve takes the name literally")
    return ""


def _text(raw: bytes) -> str:
    """errors="replace", NOT `harvest`'s surrogateescape. Both exist to stop one latin-1
    byte raising UnicodeDecodeError out of a strict decode and taking the whole call with
    it; they differ in what happens next. `harvest`'s bytes are re-encoded into a patch, so
    it needs the reversible form. Gate output is read by a human and by a judge, and
    surrogates in it would raise again at the first `print` or `json.dumps`."""
    return raw.decode("utf-8", "replace")


def _tail(out: bytes, err: bytes) -> str:
    """What a killed step had already printed, for the timeout message.

    A timeout that names only the argv sends the reader back to reproduce a run that takes
    `timeout` seconds to fail again, when the answer — WHICH test hung — was already sitting
    in the pipe this function drains. stderr first because that is where a test runner puts
    its progress; stdout too, because `make` puts everything there.

    Bounded, and from the END: a step that hangs after printing a hundred megabytes would
    otherwise put all of it in an exception message that a report and a judge both carry.
    """
    parts = [f"\n--- last {_TAIL_CHARS} chars of {name} ---\n{_text(raw)[-_TAIL_CHARS:]}"
             for name, raw in (("stderr", err), ("stdout", out)) if raw.strip()]
    return "".join(parts) or " (it printed nothing before it was killed)"


def _gate_env(env=None) -> dict:
    base = dict(os.environ if env is None else env)
    for k in gitcmd.HOSTILE_ENV:
        base.pop(k, None)
    # LAST, and set rather than dropped: removing GIT_CONFIG_GLOBAL RESTORES ~/.gitconfig
    # and the core.hooksPath in it. Same argument `gitcmd.git` makes for the same pair.
    base.update(gitcmd.NO_USER_CONFIG)
    return base


def _step_cwd(root: Path, step: Step, index: int) -> Path:
    """Where a step runs — inside the verifier, or nowhere.

    Lexical, matching `bundle._assert_contained`: `..` in the parts and any absolute path
    are refused, and the check is on the TEXT rather than on a resolved path. A `Path(root)
    / "/etc"` is `/etc`, silently, so an unguarded absolute cwd runs the gate outside the
    clone the gate exists to be confined to. `bundle._safe_rel` is the wider rule — it also
    refuses a `.git` component, which has no analogue for a working directory.
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
        drained = True
        try:
            out, err = p.communicate(timeout=_REAP_GRACE)
        except subprocess.TimeoutExpired:
            # SECOND LATCH, unpinned — and so is the `drained` message below, which only
            # this arm can produce: the group is already SIGKILLed, so the pipes are closed
            # and this drains at once. It exists so that a process the kernel will not reap
            # (uninterruptible I/O) costs `_REAP_GRACE` seconds rather than the unbounded
            # wait that would turn a step timeout back into the hang it names. The flag is
            # what stops "nothing arrived" being reported as "the step printed nothing".
            out, err, drained = b"", b"", False
        raise VerifyError(
            f"verify step {index} exceeded its {step.timeout}s timeout and was killed with "
            f"its whole process group: {list(step.argv)}"
            + (_tail(out, err) if drained else
               f"; its output could not be drained within {_REAP_GRACE}s, so nothing it "
               "printed is available")) from None
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
        # Through `_gate_env` AGAIN, not merged onto its result: `{**base, **step.env}`
        # let a step's own env re-admit exactly the names the base had just dropped, so
        # `run_command(env=…)` was hardened and `Step.env` was not — one rule with two
        # answers. Idempotent over `base`, which is already stripped and pinned.
        code, out, err = _run_step(step, wd, _gate_env({**base, **(step.env or {})}), i)
        if code != 0:
            break
    return Run(exit_code=code, stdout=out, stderr=err,
               duration_sec=time.monotonic() - started, step_index=i)


def _hooks_pin(path: Path) -> None:
    gitcmd.git(path, "config", "--local", "core.hooksPath", os.devnull)


def _assert_hooks_pinned(path: Path) -> None:
    # BOTH scopes, because either one alone is blind in one direction, and the scope that
    # makes them differ — `.git/config.worktree`, enabled by `extensions.worktreeConfig` —
    # sits INSIDE the clone, writable by exactly the party under suspicion. Measured, git
    # 2.53, both ways round: with the pin moved to that file and the LOCAL key naming a
    # hooks directory a bare `--get` answers /dev/null, so `--local` is what asks the
    # clone's own file; with the local pin untouched and the WORKTREE file naming a hooks
    # directory `--local --get` answers /dev/null while the hook runs, so the effective
    # value is what says the pin is still the one git obeys. The scopes above the clone are
    # closed elsewhere — global and system are pinned at /dev/null and `gitcmd.git` strips
    # GIT_CONFIG_PARAMETERS — so a bare `--get` here cannot answer from outside it.
    # check=False: `--get` exits 1 for a missing key, which is a state to report in this
    # module's vocabulary, not a GitError.
    for where, scope in (("its local config", ("--local",)),
                         ("the value git would obey", ())):
        r = gitcmd.git(path, "config", *scope, "--get", "core.hooksPath",
                       env_extra=gitcmd.READONLY, check=False)
        if r.stdout.strip() != os.devnull:
            raise VerifyError(
                f"the candidate rewrote the verifier's git config: core.hooksPath in "
                f"{where} is now {r.stdout.strip()!r}, not {os.devnull!r}. A clone whose "
                "config the builder chose can run builder-supplied hooks — and "
                "core.fsmonitor and core.sshCommand name commands git executes — so it "
                "cannot be used as a verifier.")


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
