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

THE GATE IS RUN TO A FIXED POINT, not once. A repository whose verify command regenerates
tracked files — this one, whose `make verify` runs `render` — has no clean single-pass tree,
so `fixed_point` re-runs it and stages the outputs a `GeneratorContract` declares. What the
contract does NOT declare is never staged and never silently dropped either; see
`FixedPoint`, whose third field is the whole of that argument.

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
import configparser
import fnmatch
import os
import signal
import subprocess
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from . import bundle, fleet, gitcmd, snapshot
from .storage import Quota

# The branch every verifier gets inside its own clone. Fixed rather than derived from
# `dest`: `clone_seat` turns this into `refs/heads/forge/<run-id>/<name>` and refuses a name
# that cannot be one, so feeding it a caller-chosen directory name would add a failure mode
# for no gain — the clones have no remotes, so two verifiers cannot collide.
VERIFIER_NAME = "verify"

# The outcome vocabulary: §6.2's five table rows, plus `GATE_CHANGED` for §6.1's
# `gate_changed`, which §6.2's table does not list and §7.2 also routes through.
# Plain `str` constants, not an Enum: these values travel into a
# manifest, a report and a judge prompt as text, and a `str`-mixin Enum gives one value two
# spellings in exactly those places — measured on 3.14, `f"{X.PASS}"` is "X.PASS" while
# `json.dumps(X.PASS)` is "PASS". `enum.StrEnum` agrees with itself, but nothing else in
# this package is an enum, and a bare `str` is what a manifest round-trips unchanged.
PASS = "PASS"
FAIL = "FAIL"
FLAKY = "FLAKY"
BASELINE_RED_NO_NEW_IDENTIFIED_FAILURE = "BASELINE_RED_NO_NEW_IDENTIFIED_FAILURE"
HARVEST_INCOMPLETE = "HARVEST_INCOMPLETE"
GATE_CHANGED = "GATE_CHANGED"

# Every value `classify` can return, for a consumer validating one read back out of a
# manifest. §6.2's table order with §6.1's outcome last, which is NOT the precedence — see
# `classify` for that.
OUTCOMES = (PASS, BASELINE_RED_NO_NEW_IDENTIFIED_FAILURE, FAIL, FLAKY, HARVEST_INCOMPLETE,
            GATE_CHANGED)

# How many paths a reason names before it counts the rest. A reason is carried into a report
# and a judge prompt, and `omitted` and a gate delta are each as large as the candidate.
_REASON_PATHS = 5

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


# How many admitted paths go into one `git add`. The admitted set is as large as whatever
# the generator rewrote, which nothing bounds, and a path set long enough to exceed ARG_MAX
# raises OSError(E2BIG) out of subprocess — a class `harvest` documents as UNCLOSED for
# `git diff`, which has no way to take its pathspec in chunks. `git add` does, so here it is
# closed instead of documented, batched the way `inspect._ATTR_BATCH` batches check-attr.
_CHECKPOINT_BATCH = 500


class VerifyError(RuntimeError):
    """The gate could not be built, could not be run, or would not have been a gate."""


class GeneratorUnstable(VerifyError):
    """A declared generator never settled, so this command has no fixed point to reach.

    INFRASTRUCTURE-CLASS (spec §7.2): it says the verify command keeps rewriting its own
    declared outputs, which is a property of the command and the tree rather than a verdict
    on the candidate — a consumer that folds it into FAIL blames a builder for a
    nondeterministic generator it did not write.
    """


class ContractMismatch(VerifyError):
    """A bundle and a verifier disagree about which generator contract the run declared.

    Fatal rather than resolved in either direction. Taking the id the bundle happens to
    carry would let a stale record decide what the gate admits; taking the verifier's would
    write a manifest whose contract is not the one the gate ran under. Both are the failure
    §7.2 names — a success criterion the run did not confirm.
    """


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


@dataclass(frozen=True)
class FixedPoint:
    """What running the gate to a fixed point measured.

    THREE facts, not the `(Run, admitted)` pair the plan sketched, and the third is why.
    `run.exit_code == 0` with `admitted == ()` is what BOTH a gate that changed nothing and
    a gate that rewrote a tracked file no relation covers hand back — and §6.2 makes only
    the first a PASS. Measured: under the pair those two runs return equal values, so the
    unexplained rewrite is unrecoverable FROM THE RETURNED VALUES (see
    `test_an_unexplained_tracked_rewrite_is_not_hidden_behind_exit_zero`).

    `unexplained` is what the gate did to git's own view of the tree that no relation
    declares: CONTENT that moved under a tracked path, or TRACKED-NESS that moved in either
    direction — a `git add` of a sidecar the candidate carried, a `git rm --cached`, both of
    which leave the bytes on disk exactly as they were. Untracked churn — `__pycache__`, a
    build directory, a test runner's cache — is what every real gate produces and is
    excluded; a path the gate STAGES has stopped being churn.

    It is measured only for a pass whose gate exited 0. A nonzero exit returns at once,
    because §6.2 already has its verdict, so on a failing run this field holds whatever
    EARLIER passes measured and nothing about the pass that failed.
    """
    run: Run
    # What the ENGINE staged, not everything under the contract that moved. A declared path
    # whose index membership the gate moved without touching its bytes is in neither this
    # field nor `unexplained` — the contract explains it, so it is not unexplained, and the
    # engine did not stage it, so it is not admitted. A consumer that needs "the index moved
    # at all" is asking for a different field and has to name it.
    admitted: tuple[str, ...] = ()
    unexplained: tuple[str, ...] = ()


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


def _contained(rel: str) -> str | None:
    """`rel` as the tree-relative path it names, or None when that could leave the tree.

    Returns the VALUE, not a verdict about it, because every caller has to join something
    onto the root afterwards and the defect this replaces was checking one spelling and
    joining another: `.//etc` passed a containment predicate, correctly — as a POSIX path
    it means `etc` — and a leading-`./` strip applied after the check rewrote it to `/etc`.
    A predicate two callers share is only half a rule; the normalization is the other half,
    and it belongs here rather than at each caller for the reason that defect demonstrates.

    Normalization is `PurePosixPath`, which collapses `.` components and repeated slashes
    as the kernel does for every path it resolves. A textual edit cannot
    stand in for it: `.//x` is a RELATIVE path meaning `x`, so a rule that strips the `./`
    off it produces an absolute one. `step.cwd` and the argv tokens are the only inputs,
    and both are POSIX strings handed to a POSIX gate; this module is POSIX-only regardless
    (`_kill_group` needs process groups), so the platform `Path` a caller joins the result
    with re-applies this same parse rather than a second one.

    The refusal is lexical, matching `bundle._assert_contained`: `..` in the parts, and any
    absolute path. `Path(root) / "/etc"` is `/etc`, silently, so an unguarded absolute
    component leaves the clone with no error anywhere. `..` is REFUSED rather than
    collapsed — which is what rules out `os.path.normpath` as the normalization — because
    collapsing `a/../b` to `b` disagrees with the kernel whenever `a` is a symlink: it
    follows the link and then leaves the tree, and no text this function ever sees says so.
    `bundle._safe_rel` is the wider rule — it also refuses a `.git` component.

    "" is the root itself and is not an escape; None is.
    """
    if not rel:
        return ""
    norm = PurePosixPath(rel)
    if norm.is_absolute() or ".." in norm.parts:
        return None
    text = str(norm)
    return "" if text == "." else text


def _step_cwd(root: Path, step: Step, index: int) -> Path:
    """Where a step runs — inside the verifier, or nowhere."""
    rel = _contained(step.cwd or "")
    if rel is None:
        raise VerifyError(
            f"verify step {index} asks to run in {step.cwd!r}, which leaves the verifier; a "
            "step's cwd is relative to the clone root and must stay inside it")
    return root / rel if rel else root


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


def build_verifier(repo, baseline, candidate, dest, *, identity, contract) -> Path:
    """A clone of the BASELINE with the candidate laid down in it, ready for the gate.

    `identity` is the verifier's own `(name, email)`, required for the same reason
    `clone_seat` requires one. It used to be justified here by §7.2's checkpoint; that
    justification is gone, because `fixed_point` only STAGES admitted output and `git add`
    needs no identity. What still needs one is the GATE: a verify command that commits is
    ordinary — this suite's own hook cases are exactly that — and in a clone without an
    identity it fails for an infrastructure reason.

    `contract` is required rather than defaulted, because a default is a policy and the
    policy this field encodes is confirmed by a human at the §5 gate. A caller with no
    contract passes the empty one, which is a statement that the run declared none.

    It is checked BEFORE the clone: a mismatch is a fact about two values the caller
    already holds, so paying for a checkout to discover it would leave a verifier tree
    behind that no gate may run in.
    """
    if candidate.generator_contract_id != contract.id:
        raise ContractMismatch(
            f"the candidate was built under generator contract "
            f"{candidate.generator_contract_id!r} and this verifier was handed "
            f"{contract.id!r}; a run has one contract, confirmed once at the §5 gate")
    seat = fleet.clone_seat(repo, baseline, dest, name=VERIFIER_NAME, identity=identity)
    # Before the candidate, so nothing between the clone and the gate runs unpinned; and
    # read back after it, because the candidate is the one thing in between that writes.
    _hooks_pin(seat.path)
    bundle.materialize(candidate, seat.path)
    _assert_hooks_pinned(seat.path)
    return seat.path


def _inventory(root: Path) -> dict:
    """One content-keyed inventory of the verifier, or a refusal.

    `Quota.for_harvest`, not `Quota.default`, on `harvest.record`'s measurement: the default
    fails closed at 5000 files, and a verifier holds a checkout plus whatever setup and the
    gate itself installed — this stdlib-only repository's own worktree is already past it.

    A breach RAISES for a reason sharper than "fail closed": `snapshot.take` answers a
    breach with an empty dict, and `snapshot.diff({}, {})` is empty, which this function's
    caller reads as CONVERGED. Swallowing the line would not lose a warning, it would
    manufacture a fixed point. `SnapshotError` propagates unwrapped, on this module's stated
    precedent for `SeatError` and `BundleError`.
    """
    entries, breaches = snapshot.take(root, quota=Quota.for_harvest())
    if breaches:
        raise VerifyError(
            f"the verifier tree could not be inventoried, so no fixed point can be "
            f"measured in it: {'; '.join(breaches)}")
    return entries


def _tracked(root: Path) -> frozenset:
    """Every path in the verifier's index, as `snapshot` would key it.

    binary=True + surrogateescape, not `gitcmd`'s text mode: these strings are compared
    against `snapshot`'s keys, which come from `os.walk` and therefore already carry
    surrogates for a non-UTF-8 filename. A strict decode would raise on that filename, and
    `errors="replace"` would produce a key that silently never matches — a tracked path
    reported as untracked, which is the fail-OPEN direction here.
    """
    out = gitcmd.git(root, "ls-files", "-z", env_extra=gitcmd.READONLY, binary=True).stdout
    return frozenset(p for p in out.decode("utf-8", "surrogateescape").split("\0") if p)


def _declared(contract, path: str) -> bool:
    """True when `path` is an output some relation in `contract` declares.

    `*` CROSSES `/` here, measured: `gen/*` matches `gen/deep/a.txt`, and `marketplaces/**`
    and `marketplaces/*` are the same pattern. A relation therefore cannot be narrowed by
    depth, which a contract author writing `gen/*` for one directory needs to know.

    `fnmatchcase` is an UNPINNED latch: `fnmatch` normalizes both sides through
    `os.path.normcase`, which is the identity function on this platform, so no test here
    can tell the two apart. It is the form that cannot widen a contract by case-folding a
    path onto a glob it does not literally match.
    """
    return any(fnmatch.fnmatchcase(path, output) for _source, output in contract.relations)


def _checkpoint(root: Path, paths, tracked) -> None:
    """Stage the admitted outputs. Staged, and deliberately not committed.

    §7.2 requires this to be stage-or-commit rather than a record, and this repository is
    the proof: `make precommit`'s render-drift check exits nonzero on regenerated-but-
    UNSTAGED output, so a record-only checkpoint makes the most likely confirmed verify
    command structurally un-passable. Stopping at the index is what keeps the engine's
    bookkeeping out of the history a later stage diffs.

    `-f` because a declared generator output may be gitignored (§7.4's `.chunkmap/map.md`
    is the spec's own example) and git's ignore rules must not veto a relation the §5 gate
    confirmed — the same flag `baseline.materialize` spends on selected untracked paths.
    `:(literal)` for `harvest._literal`'s reason: a pathspec is a glob with magic, and these
    names came off a filesystem.

    A path that no longer exists is staged only when the index still knows it — that is a
    deletion to record. An untracked file that appeared and vanished within one pass has no
    index state to write, and naming it would fail the whole call with "did not match any
    files".
    """
    stageable = [p for p in paths if os.path.lexists(root / p) or p in tracked]
    for start in range(0, len(stageable), _CHECKPOINT_BATCH):
        batch = stageable[start:start + _CHECKPOINT_BATCH]
        # check=False: a checkpoint that cannot be written is this module's failure to
        # report, not a raw GitError — the §6.2 caller has no name for that class and would
        # read it as an engine crash.
        r = gitcmd.git(root, "add", "-f", "--", *(f":(literal){p}" for p in batch),
                       check=False)
        if r.returncode != 0:
            raise VerifyError(
                "the admitted generator output could not be checkpointed into the "
                f"verifier's index: git add -> {r.returncode}: {r.stderr.strip()}")


def fixed_point(verifier_path, command, contract, *, max_passes=2, env=None) -> FixedPoint:
    """Run the gate until running it again changes nothing, admitting only declared output.

    Why a loop exists at all: a repository whose verify command REGENERATES tracked files
    can never show a clean tree on a single pass — this repository is that shape, its
    `make verify` runs `render` — so without a fixed point every candidate fails for an
    infrastructure reason, which is the outcome §4 is most insistent about.

    ADMISSION IS DECIDED BY THE CONTRACT'S OUTPUT GLOBS ALONE. Not by what changed, which is
    what "a seat cannot widen the contract" means concretely; and not by whether the path is
    already tracked, because a generator that creates a NEW output file must have it staged
    too or the drift check sees an untracked required file.

    The delta is measured PER PASS, against the state the previous pass left, because that
    is the fixed-point question — does running it again change anything. `unexplained`
    accumulates across passes instead: a rewrite outside the contract that is idempotent
    shows up on the first pass and never again, so a set read off the last pass would be
    empty exactly when the loop converged.

    Three terminations, and only the last is a raise:
      * the gate answered nonzero — there is a verdict, and checkpointing after it would
        stage output from a failed run. The delta is not measured there either, and that
        is a choice with a measurement behind it: reading it costs a post-run `_inventory`,
        which FAILS CLOSED, so a gate that failed in a tree over quota came back as a
        `VerifyError` instead of the FAIL it had already earned — an infrastructure failure
        manufactured out of a measurement that §6.2 needs only to call a run a PASS;
      * nothing in the delta was admissible — staging nothing leaves the next pass the same
        tree, so it can only produce the same answer;
      * `max_passes` exhausted with declared output still moving — `GeneratorUnstable`.

    `env` composes the way `run_command`'s does: a caller holding the repo path passes
    `env=fleet.forge_child_env(repo)`. Without it this function would be strictly less
    usable than the `run_command` it wraps, on the one composition this module documents.
    """
    root = Path(verifier_path)
    if max_passes < 1:
        # `range(0)` would fall straight through to the raise below, reporting a generator
        # unstable on the evidence of zero runs. Same sin as a gate with no steps.
        raise VerifyError(
            f"max_passes={max_passes} would run the gate zero times; a fixed point nobody "
            "measured is not a fixed point")
    admitted: set = set()
    unexplained: set = set()
    before = _inventory(root)
    for _pass in range(max_passes):
        # Re-read each pass rather than once before the loop, so that `moved` below means
        # only what it says: what the GATE moved during THIS pass. The other thing that
        # writes this index is `_checkpoint`, between passes. UNPINNED, measured: hoisting
        # this read out of the loop fails no test, because everything the checkpoint stages
        # is by construction declared and the subtraction below drops it either way. It is
        # the latch that keeps that from depending on the subtraction.
        tracked_before = _tracked(root)
        run = run_command(root, command, env=env)
        if run.exit_code != 0:
            return FixedPoint(run, tuple(sorted(admitted)), tuple(sorted(unexplained)))
        after = _inventory(root)
        changed = snapshot.diff(before, after)
        declared = [p for p in sorted(changed) if _declared(contract, p)]
        tracked = _tracked(root)
        # TWO ways the gate can author a tracked change, and a content diff sees only the
        # first: it can move CONTENT under a path git tracks, or it can move TRACKED-NESS
        # itself — `git add` of a sidecar the candidate carried, `git rm --cached` of a
        # tracked file — with the bytes on disk untouched. Removals count as much as
        # additions: `git rm` leaves a changed path that is no longer tracked when the run
        # ends, which the first half alone reads as untracked build noise. WHICH side of the
        # run the first half asks about is not itself a defence — a path the two tracked sets
        # disagree about is in the second half by definition, so the union is the same either
        # way, and it is the transition set that makes an index the gate moved visible.
        moved = {p for p in changed if p in tracked} | (tracked_before ^ tracked)
        # Subtracted by the contract's OWN GLOBS, not by the set the content delta admitted:
        # "admission is decided by the output glob alone, not by what changed" has to hold in
        # the index channel too, or a declared path is explained when its bytes move and
        # unexplained when only its index entry does. One predicate across both halves also
        # leaves no gap between them for a path that is regenerated AND newly tracked in the
        # same pass to fall into.
        unexplained |= {p for p in moved if not _declared(contract, p)}
        if not declared:
            return FixedPoint(run, tuple(sorted(admitted)), tuple(sorted(unexplained)))
        admitted |= set(declared)
        _checkpoint(root, declared, tracked)
        before = after
    raise GeneratorUnstable(
        f"the verify command was still rewriting {len(declared)} declared generator "
        f"output(s) on the last of its {max_passes} passes, so it has no fixed point: "
        f"{', '.join(declared[:5])}{' …' if len(declared) > 5 else ''}. This is an "
        "infrastructure failure of the command, never the candidate's verdict.")


def _as_run(value, what: str) -> Run:
    """The `Run` inside `value`, which may be a `FixedPoint` wrapping one.

    Both shapes are accepted because §6.2's PASS is "exit 0 AND no unexplained tracked
    delta" and only the `FixedPoint` carries the second half. Anything else is refused
    rather than duck-typed, so a wrong shape is a named refusal here instead of an
    AttributeError from somewhere inside a verdict.

    THE TYPE IS ALL THIS ESTABLISHES. `Run` is a plain frozen dataclass any caller can
    build, so passing this check is no evidence that the run came from `run_command`, or
    from a verifier, or from anywhere in particular. Nothing downstream may say otherwise.
    """
    run = value.run if isinstance(value, FixedPoint) else value
    if not isinstance(run, Run):
        raise VerifyError(
            f"classify's {what} must be a Run or a FixedPoint, not "
            f"{type(value).__name__}; nothing here reads .exit_code off another shape")
    return run


def _paths_phrase(paths) -> str:
    shown = ", ".join(paths[:_REASON_PATHS])
    rest = len(paths) - _REASON_PATHS
    return f"{shown} (+{rest} more)" if rest > 0 else shown


def _harvest_reason(cand: Run, omitted) -> str:
    """Why a bundle that omitted something is not a verdict on the candidate.

    The strongest form is §6.2's own mechanical check — a failing command whose output
    names one of the missing paths — so it is stated separately when it holds. It does not
    hold on a PASS, and a PASS is exactly where the field is load-bearing: the gate can
    exit 0 BECAUSE an input is missing, and `bundle.CandidateBundle.omitted` records the
    measurement (a nested repo's gitlink omitted, `git -C sub rev-parse HEAD` answering the
    verifier's own HEAD) where that is invisible at the gate itself.
    """
    named = [p for p in omitted
             if cand.exit_code != 0 and (p in cand.stdout or p in cand.stderr)]
    if named:
        return (f"the gate exited {cand.exit_code} and its output names {len(named)} path(s) "
                f"the bundle could not carry: {_paths_phrase(named)}. §6.2 calls that a "
                "harvesting gap, not a candidate defect")
    return (f"the bundle could not carry {len(omitted)} of the candidate's paths, so the "
            f"verifier never held the whole candidate: {_paths_phrase(omitted)}. The gate "
            f"exited {cand.exit_code}, which settles nothing either way — a gate can pass "
            "BECAUSE an input is missing")


def _gate_taints(candidate_run, cand: Run, cb) -> list:
    """Every reason this run's gate cannot be treated as the baseline's.

    Two facts, and the first has three states rather than two. `gate_delta is None` is
    "nobody looked", which `bundle.CandidateBundle` names as the fail-OPEN reading of an
    empty tuple and requires a consumer to treat as UNKNOWN; `()` is a measured, clean
    surface; a non-empty tuple is §6.1's `gate_changed`.

    The second is §7.2's: a gate that rewrote tracked files no generator relation declares
    is partly a generator nobody declared, and §7.2 routes exactly that through §6.1's
    `gate_changed` rather than inventing a seventh outcome. It is read ONLY from a
    `FixedPoint` whose run exited 0, because `FixedPoint.unexplained` is measured only for
    a pass that exited 0 — on a failing run it holds whatever earlier passes measured and
    nothing about the pass that failed, so reading it there would attribute a gate change
    to evidence that is not about this verdict.
    """
    taints = []
    if cb.gate_delta is None:
        taints.append("nobody measured the gate surface (gate_delta is None), so this run "
                      "cannot claim it ran the baseline's gate")
    elif cb.gate_delta:
        taints.append(f"the candidate changed {len(cb.gate_delta)} path(s) that define "
                      f"the gate: {_paths_phrase(cb.gate_delta)}")
    if (isinstance(candidate_run, FixedPoint) and cand.exit_code == 0
            and candidate_run.unexplained):
        taints.append(
            f"the gate rewrote {len(candidate_run.unexplained)} tracked path(s) no "
            f"generator relation declares: {_paths_phrase(candidate_run.unexplained)}")
    return taints


def _also(reason: str, taints) -> str:
    """`reason` with the gate taints appended as separate facts.

    "also" rather than "because". At both call sites the outcome was already decided by
    something else — `bundle.omitted` at the first, the runs at the second — and neither of
    the two outcomes a taint DOES decide ever reaches this helper carrying one. So a taint
    arriving here is a second, independent measurement of the same run, and a causal
    connective would be a claim nothing established.
    """
    return f"{reason}; also, {'; '.join(taints)}" if taints else reason


def _run_verdict(cand: Run, base: Run, again) -> tuple[str, str]:
    """What the RUNS alone say, before the bundle is consulted.

    Kept separate so its reason can be quoted verbatim by a `GATE_CHANGED` that displaces
    it. A reason built here that also claimed the bundle was clean would become false the
    moment it was quoted, so everything about the bundle is added by the caller instead.
    """
    if again is not None and (again.exit_code == 0) != (cand.exit_code == 0):
        # Both directions, not only §6.2's fail->pass. A pass->fail rerun left as PASS is
        # precisely the conversion §6.2 forbids, read from the run that happened to be
        # first.
        return FLAKY, (
            f"the gate exited {cand.exit_code} and then {again.exit_code} on a rerun in the "
            "same verifier; §6.2 spells this out for fail->pass and never converts such a "
            "pair to a pass, and this engine reads it in both directions")
    if cand.exit_code == 0:
        agreed = " and again on a rerun" if again is not None else ""
        return PASS, f"the gate exited 0{agreed}"
    if base.exit_code != 0:
        return BASELINE_RED_NO_NEW_IDENTIFIED_FAILURE, (
            f"the candidate's gate exited {cand.exit_code} at step {cand.step_index} and "
            f"the calibration run had already exited {base.exit_code}; no NEW failure was "
            "identified, and §6.2 requires structured test identities to identify one, so "
            "this comparison is degraded rather than an equivalence")
    return FAIL, (f"the calibration run exited 0 and the candidate's gate exited "
                  f"{cand.exit_code} at step {cand.step_index}")


def classify(candidate_run, baseline_run, bundle, *, rerun=None) -> tuple[str, str]:
    """§6.2's outcome for this run, and the sentence that has to hold for it.

    `candidate_run` and `baseline_run` are each a `Run` or the `FixedPoint` around one;
    `baseline_run` is the CALIBRATION — the confirmed command run before the candidate
    existed, which is what makes "a new failure" a statement about the candidate.

    PRECEDENCE, in full: §6.2's table is a list of meanings, not an order, so all three
    of these are this function's reading rather than a transcription.

      1. `HARVEST_INCOMPLETE` whenever `bundle.omitted` is non-empty — before the exit
         code, which is `CandidateBundle.omitted`'s own stated consumer contract. A
         narrower rule would have to decide which omissions are benign, and the only thing
         that could is the phase inventories, which no argument here carries.
      2. `GATE_CHANGED` DISPLACES `PASS` and `BASELINE_RED_…`, and only those two. Both
         rest on a premise a moved gate surface removes: PASS claims the gate was the
         baseline's, and §6.2 names "changed test discovery" as a way two nonzero runs stop
         being comparable, which is the whole of what `BASELINE_RED_…` asserts. `FAIL` and
         `FLAKY` are left standing because neither rests on that premise: FAIL is already
         the most adverse thing that can be said about a candidate, and replacing it with
         "changed, please review" is the verdict reading cleaner than its evidence; FLAKY
         says the run pair cannot answer at all, which a gate mark does not say. Whatever
         the outcome — including `HARVEST_INCOMPLETE`, which is decided without reading the
         runs — the taints are in the reason, so nothing is lost.
      3. `FLAKY` before `BASELINE_RED_…`: a rerun that disagrees with itself is not evidence
         that no new failure exists, it is evidence that this gate cannot say.

    A PASS says in its own reason whether the §7.2 half was measured at all. A bare `Run`
    cannot answer it — only `fixed_point` measures a tracked delta — so a caller that ran
    the gate through `fixed_point` and then hands over `fp.run` gets a weaker PASS than
    §6.2's, and the reason is where that is visible instead of assumed.
    """
    cand = _as_run(candidate_run, "candidate_run")
    base = _as_run(baseline_run, "baseline_run")
    again = None if rerun is None else _as_run(rerun, "rerun")
    taints = _gate_taints(candidate_run, cand, bundle)

    if bundle.omitted:
        # The gate facts are measured BEFORE this branch and appended to its reason. They
        # are independent of the harvesting gap — none of the three taints is a statement
        # about `omitted` — so a `HARVEST_INCOMPLETE` that dropped them would hide, from the
        # reviewer who has to decide whether to re-harvest, that the gate surface was never
        # measured at all. That is the shape of every real bundle today.
        return HARVEST_INCOMPLETE, _also(_harvest_reason(cand, bundle.omitted), taints)

    outcome, reason = _run_verdict(cand, base, again)
    if taints and outcome in (PASS, BASELINE_RED_NO_NEW_IDENTIFIED_FAILURE):
        return GATE_CHANGED, (f"{'; '.join(taints)}. On the runs alone this would have been "
                              f"{outcome}: {reason}")
    reason = _also(reason, taints)
    if outcome == PASS:
        measured = ("the gate itself rewrote no tracked path outside the generator contract"
                    if isinstance(candidate_run, FixedPoint) else
                    "nothing here measured whether the gate rewrote tracked files (§7.2), "
                    "so this rests on the exit code and the bundle alone")
        reason = (f"{reason}; the bundle carried every artifact path, the gate surface was "
                  f"measured and unchanged, and {measured}")
    return outcome, reason


# Files a build or task runner finds BY NAME in the directory it is started in — each entry
# is that tool's own documented search, not a guess: GNU make reads GNUmakefile, makefile,
# Makefile in that order; `just` reads justfile/.justfile/Justfile; rake reads Rakefile;
# nox reads noxfile.py; go-task reads Taskfile.{yml,yaml}. Naming one is how a gate gets
# defined without anything else in the tree mentioning it.
_BUILD_FILES = frozenset({
    "Makefile", "makefile", "GNUmakefile",
    "justfile", ".justfile", "Justfile",
    "Rakefile", "rakefile", "Rakefile.rb", "rakefile.rb",
    "noxfile.py", "Taskfile.yml", "Taskfile.yaml", "dodo.py", "SConstruct", "meson.build",
    "CMakeLists.txt", "build.gradle", "build.gradle.kts", "pom.xml", "build.xml", "BUILD",
    "BUILD.bazel", "WORKSPACE", "MODULE.bazel",
})

# Manifests and configuration that decide WHAT the gate runs rather than being run: npm's
# `scripts` block, pytest's ini options and its per-directory conftest hook, tox's envlist,
# cargo's `[[test]]`, coverage thresholds that turn a green suite red.
_RUNNER_CONFIG_FILES = frozenset({
    "package.json", "pyproject.toml", "setup.cfg", "setup.py", "tox.ini", "pytest.ini",
    "conftest.py", ".coveragerc", "Cargo.toml", "Gemfile", "composer.json", "phpunit.xml",
    "phpunit.xml.dist", ".rspec", "deno.json", "deno.jsonc", ".pre-commit-config.yaml",
    "nx.json", "turbo.json",
})

# The same role, matched as a glob because the tool accepts several extensions for one
# config file (`jest.config.{js,ts,mjs,cjs,json}` and friends).
_RUNNER_CONFIG_GLOBS = (
    "jest.config.*", "vitest.config.*", "vite.config.*", "karma.conf.*", ".mocharc.*",
    "playwright.config.*", "cypress.config.*", "ava.config.*",
)

# CI definitions, matched on the DIRECTORY the CI system reads, at ANY depth. A CI system
# reads only the directory at its own checkout root, so a nested `.github/workflows` costs
# an over-report — while anchoring at the root costs a MISSED gate file in any tree this is
# handed a subdirectory of, and under-reporting is the direction `gate_delta` fails open in.
_CI_DIRS = (".github/workflows/", ".github/actions/", ".circleci/", ".buildkite/",
            ".woodpecker/", ".gitea/workflows/", ".forgejo/workflows/")

# CI definitions matched by name rather than by directory.
_CI_FILES = frozenset({
    ".gitlab-ci.yml", ".gitlab-ci.yaml", ".travis.yml", "azure-pipelines.yml",
    "Jenkinsfile", "bitbucket-pipelines.yml", ".drone.yml", "appveyor.yml",
    "cloudbuild.yaml",
})

# The NAME half of Jest's and Vitest's defaults, expanded from the patterns themselves
# rather than approximated. Read from source, 2026-08: vitest's `include` is
# `**/*.{test,spec}.?(c|m)[jt]s?(x)` and jest's `testMatch` is
# `**/?(*.)+(spec|test).?([mc])[jt]s?(x)`. fnmatch has no optional group, so each `?()`
# becomes its own glob: the empty `prefix` is jest's `?(*.)`, which makes a bare `test.ts` a
# test file, and `mc` is the `.mts`/`.cjs` family both runners collect.
#
# The two runners disagree — vitest does NOT collect a bare `test.ts` — and this engine is
# not told which one owns the tree, so the union is used. That over-reports for one of them,
# which is the direction `gate_delta` is allowed to be wrong in. The DIRECTORY half of the
# same defaults (`__tests__/**`) is positional and cannot be expressed here at all; see
# `gate_surface` for what that leaves uncovered.
_JS_TEST_GLOBS = tuple(
    f"{prefix}{kind}.{mc}[jt]s{x}"
    for prefix in ("*.", "")
    for kind in ("test", "spec")
    for mc in ("", "m", "c")
    for x in ("", "x")
)

# Files a runner DISCOVERS with nothing naming them, so deleting or weakening one changes
# the gate silently. Each is a documented default: pytest's `python_files` (`test_*.py`,
# `*_test.py`), go's `_test.go` suffix, bats' `.bats`, rspec's `_spec.rb`, phpunit's
# `*Test.php`, surefire's four (`Test*.java`, `*Test.java`, `*Tests.java`, `*TestCase.java`).
# `[jt]s` is fnmatch's character class — js and ts.
_TEST_GLOBS = (
    "test_*.py", "*_test.py",
    "*_test.go",
    "*.bats",
    "*_spec.rb",
    "*Test.php",
    "Test*.java", "*Test.java", "*Tests.java", "*TestCase.java",
) + _JS_TEST_GLOBS

# The INI-shaped places a pytest run reads `python_files` from, and the section each keeps
# it in; `pyproject.toml` is the fourth and is read by `tomllib` instead. Both are parsed as
# DATA and never imported — a tree under classification is builder-controlled, so importing
# its config would be the engine executing that code outside the gate, which is the one
# thing §6 exists to prevent.
_PYTEST_CONFIG = (
    ("pytest.ini", "pytest"),
    ("tox.ini", "pytest"),
    ("setup.cfg", "tool:pytest"),
)


def _enumerate(root: Path) -> tuple[str, ...]:
    """Every path in the tree git will name, keyed the way `snapshot` keys one.

    `--cached --others --exclude-standard`. The index ALONE would miss a gate file the
    candidate added, because `bundle.materialize` writes a sidecar to disk and not to the
    index — and a missing gate file is the fail-OPEN direction for a surface.

    `--exclude-standard` is the boundary this function accepts rather than the one it would
    like: without it, `--others` enumerates `.venv/` and `node_modules/`, i.e. every
    installed package. So an IGNORED gate file — the `.venv/bin/pytest` this module's own
    docstring names — is NOT enumerated here, and is reached only when `command` names it.

    binary=True + surrogateescape for `_tracked`'s reason: these keys are compared against
    and returned alongside paths that came off a filesystem.
    """
    out = gitcmd.git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard",
                     env_extra=gitcmd.READONLY, binary=True).stdout
    return tuple(sorted({p for p in out.decode("utf-8", "surrogateescape").split("\0") if p}))


def _ini_python_files(text: str, section: str) -> list:
    parser = configparser.ConfigParser()
    try:
        parser.read_string(text)
        raw = parser.get(section, "python_files")
    except (configparser.Error, ValueError):
        # A config this cannot parse leaves the DEFAULTS standing, which is the wider
        # surface. Raising instead would let a candidate that writes a malformed
        # `setup.cfg` stop the surface being computed at all.
        return []
    return raw.split()


def _toml_python_files(raw: bytes) -> list:
    try:
        value = tomllib.loads(raw.decode("utf-8", "replace"))
    except (tomllib.TOMLDecodeError, ValueError):
        return []
    for key in ("tool", "pytest", "ini_options", "python_files"):
        if not isinstance(value, dict):
            return []
        value = value.get(key)
        if value is None:
            return []
    if isinstance(value, str):
        return value.split()
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _test_globs(root: Path, paths) -> tuple[str, ...]:
    """`_TEST_GLOBS`, WIDENED by whatever discovery the tree's own pytest config declares.

    Union, never replacement, and that is the whole security property: `python_files` is
    read out of a file the candidate may have written, so honouring it as a REPLACEMENT
    would let `python_files = nothing_*.py` drop every real test file out of the surface —
    a candidate narrowing the definition of the gate it is about to be measured against.
    Widening can only add, so the worst a rewritten config can do is over-report.

    Only the tree's root-level config is read. A nested one belongs to a rootdir this
    function was not told about, and pytest reads the ini file at the rootdir alone.
    """
    found = set(_TEST_GLOBS)
    for rel, section in _PYTEST_CONFIG:
        if rel in paths:
            try:
                text = (root / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            found.update(_ini_python_files(text, section))
    if "pyproject.toml" in paths:
        try:
            found.update(_toml_python_files((root / "pyproject.toml").read_bytes()))
        except OSError:
            pass
    return tuple(sorted(found))


def _gate_role(path: str, test_globs) -> str:
    """The gate-defining role `path` plays, or "" when it plays none."""
    name = path.rsplit("/", 1)[-1]
    if name in _BUILD_FILES:
        return "build definition"
    if name in _RUNNER_CONFIG_FILES:
        return "runner or package configuration"
    if any(fnmatch.fnmatchcase(name, g) for g in _RUNNER_CONFIG_GLOBS):
        return "runner configuration"
    if name in _CI_FILES or any(path.startswith(d) or f"/{d}" in path for d in _CI_DIRS):
        return "CI definition"
    if any(fnmatch.fnmatchcase(name, g) for g in test_globs):
        return "discovered test file"
    return ""


def _command_paths(root: Path, command) -> set:
    """Paths inside the tree that the gate's own argv names.

    This is the half of the surface no naming rule can reach: `[["./check.sh"]]` and
    `[["make", "-f", "mk/gate.mk"]]` define the gate completely and neither file's name
    says so. It is also the only route to a gate file git does not enumerate — the ignored
    `.venv/bin/pytest`.

    Containment is `_contained`, applied to the step's `cwd` AND to each token, because the
    surface name is the two joined: a path outside the tree cannot be a candidate path and
    so cannot be in a delta computed against one. Both halves are needed — with the cwd
    unchecked, a clean token under `cwd="/etc"` produced `/etc/passwd`. A `Step` is public
    and `__post_init__` validates argv only, so `cwd` arrives here unexamined.

    What `_contained` returns is what gets joined, and that is the point rather than a
    convenience: the name this emits has to be the one the TREE holds, or no `gate_delta`
    computed against the tree can ever match it. `cwd="."` once produced `./check.sh` and
    `cwd="././/sub"` produced `.//sub/check.sh` — names for a step that really does define
    the gate, and so a miss, in the direction the surface fails open in.

    An escaping step is DROPPED rather than refused, which is where this parts from
    `_step_cwd`: that function decides whether a gate may run, and `run_command` raises
    there. This one answers "which files define the gate", and a step whose paths all lie
    outside the tree contributes none — the surface is still computable and still honest.

    Directories are skipped — a delta is over files, and `pytest tests` is already covered
    by the discovery globs underneath it.
    """
    named = set()
    for step in getattr(command, "steps", ()):
        base = _contained(step.cwd or "")
        if base is None:
            continue
        for token in step.argv:
            named_path = _contained(token)
            if named_path is None:
                continue
            rel = f"{base}/{named_path}" if base else named_path
            p = root / rel
            if p.is_file() or p.is_symlink():
                named.add(rel)
    return named


def _contract_sources(contract, paths, surface) -> set:
    """The SOURCES of any contract relation whose output is already gate surface.

    A gate file that is generated is defined by whatever generates it: if the contract says
    `shared/** -> marketplaces/**` and a Makefile under `marketplaces/` is surface, then
    editing the shared source moves that gate on the next generator pass. Leaving the
    source out would make exactly that edit invisible, which is the fail-open direction.

    The cost is stated rather than hidden: a broad source glob puts every file it matches
    into the surface, so a contract author writing one is declaring that editing any of
    them can rewrite the gate. `fnmatchcase` and `*`-crosses-`/` are `_declared`'s, so a
    relation means the same thing here as it does at admission.
    """
    extra = set()
    for source, output in contract.relations:
        if any(fnmatch.fnmatchcase(p, output) for p in surface):
            extra.update(p for p in paths if fnmatch.fnmatchcase(p, source))
    return extra


def gate_surface(verifier_path, contract, *, command=None) -> tuple[str, ...]:
    """The files in this tree that DEFINE the gate, so a caller can compute `gate_delta`.

    §6.1's list — build definitions, package-script definitions, test-runner config, CI
    helpers, discovered test files — answered against ONE TREE. Four derivations, and each
    is named because a surface nobody can audit is not a defence:

      * `git ls-files --cached --others --exclude-standard` says which paths the tree
        actually has. Nothing here returns a name the tree does not hold, so a repository
        with no Makefile has no Makefile in its surface.
      * the ROLE RULES (`_BUILD_FILES`, `_RUNNER_CONFIG_*`, `_CI_*`, `_TEST_GLOBS`) are
        engine-owned and fixed. They have to be: each one encodes a tool's own documented
        discovery, and deriving them from the tree instead would mean asking the builder's
        config which files count as the gate — the question under suspicion.
      * the tree's own pytest config WIDENS the test-file globs (see `_test_globs`), which
        is the one discovery rule a repository can legitimately redefine.
      * `command`, when given, adds the paths the gate's argv names — the only route to a
        gate file whose name says nothing, and the only route to one git does not
        enumerate.
      * `contract` adds the sources of any relation whose output is already surface.

    WHAT THIS DOES NOT COVER, because `gate_delta` is fail-OPEN when it under-reports and a
    surface that hides its gaps is worse than a small one:

      * INDIRECTION inside a gate file. `include mk/*.mk`, a `package.json` script that
        runs `scripts/ci.sh`, a Makefile recipe naming a helper: reading those means
        parsing each tool's language, and getting it wrong silently drops a real gate file.
        Only the entry point is covered, and only when a rule or `command` names it.
        A discovered test's own IMPORTS are the same gap read from the other end — gutting
        an assertion helper weakens every test that imports it, and a helper whose NAME
        matches no discovery rule is not surface. That one is not build-tool indirection;
        it is an accepted cost.
      * CONTAINER-defined gates. A `Dockerfile` and a `docker-compose.yml` define what
        `docker compose run test` actually runs, and neither carries a role here: adding
        one would mark every repository that ships a deployment image, and `command` does
        not reach them either, since `["docker", "compose", "run", "test"]` names no path.
      * IGNORED paths, per `_enumerate` — `.venv/bin/pytest`, `node_modules/.bin/jest` —
        except when `command` names one directly. This gap is not only installed tooling:
        WHICH paths are ignored is decided by `.gitignore`, which the candidate may write
        and which carries no gate role here, so the edit that hides a gate file is itself
        absent from `gate_delta`. Bounded to files the candidate ADDED — `--cached` ignores
        excludes, so a tracked gate file cannot be hidden this way — which is exactly the
        class `--others` was added for. Closing it would need the rule "an ignore line that
        NEWLY covers a path the surface would otherwise hold", and "newly" is a comparison
        against the baseline's ignore rules, which this one-tree function is not given.
        Measured for the one-tree approximation that IS derivable — "the tree has an
        ignored path some role rule would match" — on a tree carrying an installed `.venv`
        and `node_modules`, 160 of 160 ignored paths matched (a package's own `test_*.py`
        and `conftest.py`, a module's `index.test.js`), so the condition holds wherever
        tooling is installed and the rule degenerates to marking `.gitignore` always.
        Against that, every one of the 7 `.gitignore` edits in this repository's 241
        commits adds or broadens an ignore over build output, a cache, or machine state,
        and none of them touches a gate file. A verdict nobody reads is its own fail-open,
        so this stays a stated gap rather than a role.
      * POSITIONAL test layouts, where membership is a directory rather than a name: Rust's
        `tests/*.rs`, Maven's `src/test/java/**`, Jest's `__tests__/**`, a suite listed only
        inside a CI matrix.
        A directory rule needs to know which tool owns the tree, and this function is not
        told which tool the gate is.
      * A runner reached only through PATH. `make` invoking `pytest` names no path, so the
        binary that runs is whatever the environment resolves.

    `verifier_path` must be a tree the ENGINE built — a verifier, or the baseline checkout.
    A seat's index is writable by the party under suspicion, and `git rm --cached Makefile`
    in a seat would delete a gate file from a surface computed there.
    """
    root = Path(verifier_path)
    paths = _enumerate(root)
    globs = _test_globs(root, paths)
    surface = {p for p in paths if _gate_role(p, globs)}
    surface |= _command_paths(root, command)
    surface |= _contract_sources(contract, paths, surface)
    return tuple(sorted(surface))
