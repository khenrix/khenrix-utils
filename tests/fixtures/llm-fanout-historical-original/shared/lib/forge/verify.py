"""The gate runs where the builder never was, and the engine runs it (spec §6).

A check the builder could have rigged is not a check. A seat can replace `.venv/bin/pytest`,
delete an auto-discovered test file, or weaken the Makefile — so the candidate is
materialized into a CLONE BUILT FROM THE BASELINE and the gate runs there, never in the
seat. `build_verifier` is `fleet.clone_seat` plus `bundle.materialize`, so every defence the
seat clone already carries — no origin, no hardlinks, no ambient template, its own identity,
a checkout verified against B1's manifest — is reused rather than restated. It is also where
§6.1's GATE DELTA is measured, because the clone and the materialization put the baseline and
the candidate in one directory one after the other, and nowhere else in the run holds both.

`SeatError` and `BundleError` PROPAGATE UNWRAPPED, on `harvest`'s precedent for
`SnapshotError`: both already name the thing that failed and are already RuntimeErrors, so
wrapping them would only put this module's name in front of the one that knows.

THE GATE IS RUN TO A FIXED POINT, not once. A repository whose verify command regenerates
tracked files — this one, whose `make verify` runs `render` — has no clean single-pass tree,
so `fixed_point` re-runs it and stages the outputs a `GeneratorContract` declares. What the
contract does NOT declare is never staged and never silently dropped either; see
`FixedPoint`, whose third field is the whole of that argument.

SETUP IS REPLAYED HERE, and §6 puts a check in front of it. The tree is first compared
against the bundle that describes it (`validate_materialized`), because setup is the first
thing that legitimately changes this tree and afterwards "differs from the bundle" and
"setup installed something" are one observation. Then `run_setup` runs the confirmed setup
command and REFUSES it, rather than continuing, if it moved a tracked path no relation
declares: the gate would then measure a tree whose tracked content differs from the candidate
the bundle describes, with nothing in the run accounting for the difference.

WHAT ORDERS THE REST IS THE ORCHESTRATOR, NOT THIS MODULE. §6's chronology is enforced at
exactly one joint — `run_setup`'s own call to `validate_materialized` — because that check
stops being makeable once setup has run. Everywhere else the five steps are five public functions a
caller composes: nothing here stops `fixed_point` running in a tree setup never touched, or
`classify` reading a run taken before the materialization was checked. `calibrate` is not the
exception it looks like; it sequences the same calls over §5's UNTOUCHED BASELINE and builds
its own empty bundle rather than receiving a builder's, so no candidate's §6 run is ordered by
anything in this module. Sequencing
those is the caller's job, and THAT CALLER NOW EXISTS: `runner.verify_candidate` composes the
five in §6's order for one candidate, and `runner.run` calls it once per seat. It is named
here so the next reader finds the ordering rather than re-deriving that this file does not
make it — which is still the fact, and is why the joint above stays where it is.

HOOKS. A hook that runs in a verifier is a builder-controlled check. Measured on git 2.53,
the two candidate mitigations are NOT equivalent:

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
import errno
import fnmatch
import hashlib
import os
import signal
import stat
import subprocess
import time
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath

from . import bundle, fleet, gitcmd, snapshot
from .inspect import GeneratorContract
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
# UNREACHABLE IN THIS BUILD, AND SAID SO RATHER THAN LEFT TO BE DISCOVERED. `_run_verdict`
# returns this only when `again is not None`, and NOTHING in `shared/lib/forge/` ever passes
# `again` — measured. `fixed_point` cannot supply it either: it returns on the FIRST non-zero
# exit, so the fail-then-pass pair §6.2 describes never forms inside it, and its second pass
# happens only after a green one.
#
# Wiring a real rerun means a second full gate run per candidate, which §5.2 did not price —
# so it is a spend decision and not an oversight to fix in passing. Until that decision is
# taken, this outcome is declared, ranked and never produced, and a reader must not infer
# that flake detection is running. `test_the_flaky_outcome_has_no_producer_and_the_note_says_so`
# fails the day one appears, which is the signal to delete this paragraph.
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
    """The verify command has no fixed point to reach: re-running it keeps moving tracked
    files.

    TWO RAISE SITES, and the tracked movement they see is not the same movement. `fixed_point`
    raises when the command is still rewriting its own DECLARED outputs after its pass budget,
    with each pass's output staged in between. `_confirm_fixed_point` raises when a second
    pass over the untouched baseline moved a tracked path NO generator relation declares —
    which that function does not stage and cannot admit, so a declared rewrite is subtracted
    there rather than reported. The two are complements: neither one sees the other's paths.

    INFRASTRUCTURE-CLASS (spec §7.2) at both, and that is what a consumer reads it for: it is
    a property of the command and the tree rather than a verdict on the candidate — one that
    folds it into FAIL blames a builder for a generator it did not write, or for a baseline
    that was never quiet before any provider ran.
    """


class SetupOverlap(VerifyError):
    """Setup changed a tracked file the run's generator contract does not declare (spec §6).

    Fails the candidate CLOSED. The gate would otherwise measure a tree whose tracked
    content differs from the candidate the bundle describes, with nothing in the run
    accounting for the difference — the verdict would name a candidate nobody built.

    NOT, in general, "the change is already in the candidate and would be applied twice".
    Measured: `harvest.artifact_set` sets `paths` from the WORK window alone, so a tracked
    effect setup produced in the seat never crosses into the bundle, and re-running setup
    here reproduces it exactly once. Stacking is real only where harvest already names the
    path in `ArtifactSet.setup_overlap` — both windows touched it — and there an IDEMPOTENT
    setup leaves no delta for this function to see at all. The refusal earns its keep on the
    first argument, not the second.
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

    ALL FOUR FIELDS ARE DECIDED HERE, not `argv` alone, and the three that joined it are the
    third instance of one defect. `runstate._steps` decides the same four questions on the way
    back out of a manifest; while argv was decided here and the other three only there, the
    two disagreed, and `Step(argv=("true",), timeout=True)`, `timeout=1.5`, `env={"A": 1}` and
    `cwd=5` each passed `gate.confirm` and were refused by `write_manifest` — after the run
    directory, `baseline.index`, `events.jsonl` and a forge ref in the USER's own repository
    already existed. A run that will not happen must leave nothing behind, so the VALUE holds
    the predicate and `parse`, `gate.Confirmation` and `write_manifest` inherit one copy each
    rather than spelling several that are free to drift apart.

    `argv` is MATERIALIZED as well as checked, on `gate.Confirmation`'s precedent that a
    record settles its own shape. A list argv passes every check below and then reads back off
    the manifest as a tuple, so `write_manifest`'s round-trip comparison refuses the record
    four writes in; a string argv passed them too — it iterates into one-character strings,
    none of them shellish — and reached the manifest as the one shape §5.1 rejects rather than
    reinterprets.

    WHAT IS NOT DECIDED HERE is what needs the verifier: `_step_cwd` refuses a cwd that LEAVES
    the root, which is a fact about a root this value has never seen. Nor is MUTATION — `env`
    is a dict, so `step.env["A"] = 1` after construction is still refused by `write_manifest`
    and nowhere earlier, which is the late refusal everything above exists to move. That is
    the boundary `gate.Confirmation` states for `object.__setattr__` and it is the same one:
    the caller at this door is trusted, and what is closed is every state a caller can
    CONSTRUCT.
    """
    argv: tuple[str, ...]
    cwd: str = ""
    env: dict = field(default_factory=dict)
    timeout: int = 600

    def __post_init__(self):
        # A STRING IS NOT AN ARGV, refused before anything iterates one — `Command.parse`
        # makes the same refusal one level up for a whole spec that is one string, and for
        # the same reason: iterating `"make verify"` yields characters, every one of which
        # passes the checks below.
        if isinstance(self.argv, (str, bytes)):
            raise VerifyError(
                f"a verify step's argv is a LIST of words, not one string: {self.argv!r}. "
                'Nothing here runs a shell — write ["make", "verify"].')
        try:
            argv = tuple(self.argv)
        except TypeError as e:
            raise VerifyError(
                f"a verify step's argv is not a sequence of words: {self.argv!r}") from e
        object.__setattr__(self, "argv", argv)
        # `Popen([])` raises IndexError from inside subprocess, which is neither this
        # module's failure type nor a message naming the step at fault. `Step` is public and
        # the brief's own timeout case constructs one directly, so the guard belongs here
        # rather than only in `parse`.
        if not argv:
            raise VerifyError("a verify step names no program: argv is empty")
        # The SAME argument, applied to the same rule `Command.parse` enforces on argv[0]:
        # a `Step` built directly used to accept `("make verify",)` and fail at gate time
        # with an ENOENT naming a program nobody meant to run. Both of parse's preconditions
        # come with it — `_shellish` iterates its argument, so a non-string argv[0] would
        # raise a raw TypeError out of the one path whose whole job is a named refusal.
        if not all(isinstance(t, str) for t in argv):
            raise VerifyError(f"a verify step has a non-string argument: {argv!r}")
        why = _shellish(argv[0])
        if why:
            raise VerifyError(
                f"a verify step names a program that cannot exist: {argv[0]!r} {why}. "
                "Nothing here runs a shell, so it would be exec'd under that literal name.")
        # §5.1's other three fields, on `runstate._steps`' rules. Not merely "the manifest
        # will refuse it anyway": that refusal arrives four writes into a run, and `cwd=5`
        # never even reached it — it raised a bare AttributeError out of `gate.open_run`'s
        # spends detector, where every other refusal is a GateError.
        if not isinstance(self.cwd, str):
            raise VerifyError(
                f"a verify step's cwd is a path relative to the verifier root, not "
                f"{self.cwd!r}; it is joined to that root and read as a directory name")
        if not isinstance(self.env, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in self.env.items()):
            raise VerifyError(
                f"a verify step's env is an object of strings, not {self.env!r}; it is "
                "spliced into a process environment, where anything else raises at spawn "
                "time, and JSON has one key type so a non-string key does not survive the "
                "manifest either")
        # `isinstance(True, int)`, so the type test is not defensiveness: `timeout=True` is a
        # ONE-SECOND budget every real gate fails under, arriving out of a field nobody wrote
        # a number in. `2.0` and `"600"` read as the right number to everything that never
        # compares them.
        if isinstance(self.timeout, bool) or not isinstance(self.timeout, int):
            raise VerifyError(
                f"a verify step's timeout is a whole number of seconds, not {self.timeout!r}")


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

    THREE facts — `run`, `admitted`, `unexplained` — not the `(run, admitted)` pair that
    would look sufficient, and the third is why.
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
    """Where a step runs — inside the verifier, or nowhere.

    BOTH HALVES, because `step.cwd` comes off `Manifest.setup`/`Manifest.verify` — decoded
    from `manifest.json`, which §8.1 does not trust — and this one does not merely READ
    through what the name resolves to, it RUNS the confirmed gate command there. `_contained`
    refuses `..` and absolute; it cannot see a component that is a SYMLINK out of the tree,
    and `run_command`'s docstring already promises a raise for "a cwd that leaves the tree".
    The descent is what makes that sentence true for the second shape.

    WHAT THE DESCENT CANNOT DO HERE, stated rather than implied by its presence: `subprocess`
    takes a NAME for `cwd`, not a descriptor, so this returns a path and the kernel resolves it
    once more at spawn. That leaves the same concurrent-writer residual `bundle._Contained`'s
    docstring measures — a rename between this check and the exec — and it is a strictly
    smaller window than the one the forged manifest used, which is now closed. The stdlib
    offers no fd-taking `cwd`, so closing the rest is not available at this layer.
    """
    rel = _contained(step.cwd or "")
    if rel is None:
        raise VerifyError(
            f"verify step {index} asks to run in {step.cwd!r}, which leaves the verifier; a "
            "step's cwd is relative to the clone root and must stay inside it")
    if not rel:
        return root
    # `bundle.contained` rather than `_leaf`, because this caller needs the REASON the descent
    # stopped and `_leaf` collapses every reason to None. A component that is MISSING is not an
    # escape: there is nothing there to resolve to somewhere else, and a cwd that does not
    # exist has always been allowed through to `Popen`, which reports it exactly. Refusing it
    # here would answer a misconfigured step in the vocabulary of a security refusal, and would
    # break the one-rule agreement `_contained` and this function are pinned to.
    escapes = True
    at = None
    try:
        at = bundle.contained(root, rel, "a step cwd")
    except bundle.BundleError as e:
        if isinstance(e.__cause__, OSError) and e.__cause__.errno == errno.ENOENT:
            escapes = False
    if at is not None:
        with at:
            # THE LEAF TOO, and it is the component that matters most here: the descent proves
            # the PARENTS, but `sub/dir` with `dir` itself a link to `/etc` would otherwise
            # pass and the gate would run in `/etc`.
            #
            # A LEAF THAT IS NOT THERE IS NOT AN ESCAPE, and this arm is why the test is a
            # symlink test rather than an existence test: a cwd that simply does not exist was
            # always allowed through to `Popen`, which reports it precisely, and turning that
            # into a VerifyError here would refuse a misconfigured step in the vocabulary of a
            # security refusal. Only a link is refused, because only a link RESOLVES — to
            # somewhere else.
            try:
                escapes = stat.S_ISLNK(
                    os.stat(at.leaf, dir_fd=at.fd, follow_symlinks=False).st_mode)
            except FileNotFoundError:
                escapes = False
            except OSError:
                escapes = True
    if escapes:
        raise VerifyError(
            f"verify step {index} asks to run in {step.cwd!r}, which leaves the verifier "
            "through a symlinked path component; a step's cwd is relative to the clone root "
            "and must stay inside it")
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


def _assert_hooks_pinned(path: Path, *, by: str = "candidate") -> None:
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
            # WHO IS NAMED IS NOT COSMETIC. This check cannot distinguish "the candidate
            # rewrote ./setup.sh" from "the operator's confirmed setup command did what it
            # always does" — and a CALIBRATION has no candidate at all, so the default
            # sentence sent an operator hunting through a seat's diff for a line `npm ci`
            # wrote. The caller knows which tree it is standing in; this takes its word.
            actor = {
                "candidate": (
                    "the candidate rewrote the verifier's git config"),
                "setup": (
                    "the confirmed setup command moved the pin in the calibration clone "
                    "(no seat has run yet, so this is that command's own effect)"),
            }[by]
            raise VerifyError(
                f"{actor}: core.hooksPath in "
                f"{where} is now {r.stdout.strip()!r}, not {os.devnull!r}. A clone whose "
                "config the builder chose can run builder-supplied hooks — and "
                "core.fsmonitor and core.sshCommand name commands git executes — so it "
                "cannot be used as a verifier.")


@dataclass(frozen=True)
class Verifier:
    """A tree the builder never had access to, and what building it measured.

    `candidate` is NOT the bundle the caller passed in: it is that bundle with §6.1's whole
    measurement — `gate_delta` and the `gate_surface` it ranged over — filled from the two
    trees `build_verifier` had and no other caller has. A caller that classifies the INPUT
    bundle instead reads `gate_delta is None`, which is UNKNOWN, which is `GATE_CHANGED` —
    correct, and useless.

    `contract` rides along because the gate that runs in this tree admits verify-origin
    rewrites under exactly one contract, and a caller that sources a second one from
    somewhere else is the manifest-records-X-while-the-gate-admitted-Y shape one call site
    over. `fixed_point` still takes its own, so this field is what a caller reads to fill
    that argument rather than a check on it.

    The two surfaces are kept because the delta alone cannot say what was measured: a tree
    with no gate files at all and a tree whose gate files the candidate left untouched both
    produce `()`, and a surface is what separates them. THE VERDICT PATH CANNOT REACH THESE
    TWO — `classify` is handed a bundle and never a `Verifier` — so their union goes onto the
    candidate as `CandidateBundle.gate_surface`, which is what answers that question for a
    verdict. What is left here is the one thing the union drops: which SIDE a surface path
    came from, and so whether a path entered the surface or left it.

    Neither carries a default. `()` is a measured, empty surface and `build_verifier` is the
    sole constructor, so a default could only ever supply that measurement on behalf of a
    caller who took none — the fail-open reading of the distinction the paragraph above
    exists to preserve.
    """
    path: Path
    candidate: bundle.CandidateBundle
    contract: GeneratorContract
    baseline_surface: tuple[str, ...]
    candidate_surface: tuple[str, ...]
    # THE PRE-MATERIALIZE READING, RETAINED BECAUSE IT CANNOT BE RE-DERIVED. `build_verifier`
    # takes it in the one moment the clone holds exactly B1, and after the candidate is laid
    # down and setup has run there is no tree left to read it from. `remeasure_gate_surface`
    # needs it: §6.1's question is whether THIS tree's gate differs from the one the baseline
    # was measured with, and an intermediate reading is a different question.
    baseline_state: dict


def _sha256_fd(fd: int) -> str:
    """Deliberately NOT bound to `snapshot._digest`, `baseline._sha256_file` and
    `fleet._sha256_file`, which must agree with each other byte for byte. Nothing compares
    this one against a manifest; see `_surface_state` for what that buys.

    A DESCRIPTOR, for `fleet._sha256_file`'s reason and on the same threat model. The gate
    surface is not a set of names this module produced: `_command_paths` derives part of it
    from `Manifest.verify`, read back off `manifest.json`, which §8.1 declares untrusted. A
    path-taking digest would re-resolve that name and describe whatever it now points at.
    """
    h = hashlib.sha256()
    while chunk := os.read(fd, 1 << 16):
        h.update(chunk)
    return h.hexdigest()


def _leaf(root: Path, rel: str, what: str):
    """`rel`'s parent inside `root` as an open descriptor, or None when it does not stay there.

    THE ACCESS HALF OF `_contained`, WHICH IS THE NAME HALF. The two are not alternatives and
    neither subsumes the other: `_contained` normalizes a string into the name the TREE holds
    — which is what a gate delta is computed over, so it cannot be skipped — while this
    performs the read at a descriptor, which is what a string rule cannot do. `_contained`'s
    own docstring already says a lexical rule "disagrees with the kernel whenever `a` is a
    symlink"; this is the half that agrees with it.

    LIVE ON A RECORD, not a latent shape. `Manifest.setup` and `Manifest.verify` are decoded
    from `manifest.json` by `read_manifest`, `runner` rebuilds `verify.Command` straight off
    them, and every `Step.cwd` and argv token then reaches `_command_paths` and `_surface_state`
    — where an edited `check.sh` under a symlinked directory put a HOST file into the gate
    surface and had its content hashed into the delta.

    None rather than a raise, because both callers already have an answer for a path that is
    not in the tree: `_command_paths` drops the step, `_surface_state` refuses.
    """
    try:
        return bundle.contained(root, rel, what)
    except bundle.BundleError:
        return None


def _surface_state(root: Path, paths) -> dict:
    """Each gate-surface path's CONTENT identity, so a delta can see an in-place rewrite.

    A gate surface is a set of NAMES, and a candidate that weakens `Makefile` where it
    stands adds and removes no name at all. Measured on that candidate: the two surfaces
    come back equal and a difference over the names alone is empty, while §6.1 requires
    that edit to mark the candidate for review. So the delta is taken over these pairs and
    the names are read back off it.

    The identity is the bytes AND the mode. `check.sh` losing its executable bit is a gate
    that stops running, and no digest of its content says so. A symlink is its TARGET
    TEXT, never read through, on `snapshot._symlink_entry`'s argument: reading through it
    would put content from outside the tree into this measurement. A FIFO or a device node
    is its file TYPE, never opened, on `snapshot._special_entry`'s: a read-open on a FIFO
    blocks until a writer appears, and there is no timeout anywhere in this call path.

    This digest is compared ONLY against another value this function produced in the same
    call, which is what separates it from `snapshot._digest`, `baseline._sha256_file` and
    `fleet._sha256_file` — those three must agree byte for byte, because a snapshot digest
    is checked against a baseline manifest hash of the same file. Nothing checks this one
    against anything, so it is free to carry the mode the other three keep separate.

    A path with NO entry is one the tree does not hold. `_enumerate` reads the index as
    well as the worktree, so a tracked path missing from the worktree is an ordinary
    answer, and its absence here is exactly what puts it in the delta when the other side
    has it. A path that cannot be READ raises instead, because dropping it would give it
    the same absent identity on both sides — a gate file rewritten and then made
    unreadable would compare equal to itself, in the direction the delta fails open in.
    """
    state = {}
    for rel in paths:
        # DESCENDED, NEVER JOINED — see `_leaf`. A REFUSAL rather than a skip, on this
        # function's own argument two paragraphs up: a surface path that cannot be measured
        # honestly must not get an identity that compares equal to itself across the two
        # trees, and "it left the tree" is the sharpest version of cannot-be-measured.
        at = _leaf(root, rel, "a gate-surface path")
        if at is None:
            raise VerifyError(
                f"the gate-surface path {rel!r} does not stay inside this tree, so no gate "
                "delta computed over it would describe the tree it claims to")
        with at:
            try:
                st = os.stat(at.leaf, dir_fd=at.fd, follow_symlinks=False)
                if stat.S_ISLNK(st.st_mode):
                    target = os.readlink(at.leaf, dir_fd=at.fd).encode(
                        "utf-8", "surrogateescape")
                    state[rel] = f"symlink:{hashlib.sha256(target).hexdigest()}"
                elif stat.S_ISREG(st.st_mode):
                    # `O_NOFOLLOW`/`O_NONBLOCK` via `open_leaf`, and the MODE still comes from
                    # the `stat` above: the two are one inode here because the open cannot
                    # have followed anything.
                    fd = bundle.open_leaf(at, os.O_RDONLY, "a gate-surface path")
                    try:
                        state[rel] = f"file:{st.st_mode & 0o777:o}:{_sha256_fd(fd)}"
                    finally:
                        os.close(fd)
                else:
                    state[rel] = f"special:{stat.S_IFMT(st.st_mode)}"
            except FileNotFoundError:
                continue
            except OSError as e:
                raise VerifyError(
                    f"the gate-surface path {rel!r} could not be read, so no gate delta over "
                    f"this tree would be honest: {e}") from e
    return state


def _gate_delta(before: dict, after: dict) -> tuple[str, ...]:
    """§6.1's measurement: every gate-surface path the candidate moved.

    BOTH directions and the middle. A gate file the candidate DELETES leaves the surface,
    one it ADDS enters it, and one it rewrites in place is in neither difference — so the
    union of the names is walked and each is decided by its identity. An `after`-only
    reading would report the added test file and miss the deleted one, which is §6's
    threat verbatim; a `before`-only reading misses the added one.
    """
    return tuple(sorted(p for p in before.keys() | after.keys()
                        if before.get(p) != after.get(p)))


def build_verifier(repo, baseline, candidate, dest, *, identity, contract,
                   command) -> Verifier:
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

    `command` is the confirmed verify command, and it is REQUIRED for `contract`'s reason
    rather than defaulted — though `None` remains a legal VALUE, because a caller that has
    not chosen a command yet can still build a tree and has only to say so at the call site.
    It is the sole route to a gate file whose name says nothing (`./check.sh`) and to one git
    does not enumerate (`.venv/bin/pytest`), so leaving it out costs coverage of exactly
    those. MEASURED, on a repository whose only gate is `./check.sh` and a candidate that
    gutted it: with no command both surfaces come back empty, the delta is a clean `()` and
    the runs earn a PASS. No value-level check can separate that from a deliberate `None` —
    by the time this function runs they are one value — so the requirement is the signature's
    to carry, and what a `None` still costs is recorded rather than assumed: the empty surface
    it produces travels to `classify` on the candidate, which says so in the verdict.

    THE GATE DELTA IS TAKEN HERE BECAUSE THIS IS THE ONLY PLACE BOTH TREES EXIST.
    `gate_surface` answers one tree; a delta needs two, and between the clone and the
    materialization this one directory is first exactly the baseline and then exactly the
    candidate. It costs two surface reads and NO extra clone, which is the expensive half.
    `bundle.build` cannot do it: it is handed the seat's tree, which is the one tree a gate
    surface must not be read from, since the party under suspicion can write it.
    """
    if candidate.generator_contract_id != contract.id:
        raise ContractMismatch(
            f"the candidate was built under generator contract "
            f"{candidate.generator_contract_id!r} and this verifier was handed "
            f"{contract.id!r}; a run has one contract, confirmed once at the §5 gate")
    seat = fleet.clone_seat(repo, baseline, dest, name=VERIFIER_NAME, identity=identity)
    # Before the candidate, so nothing between the clone and the gate runs unpinned; and
    # read back after it, because the candidate is the only writer BEFORE THIS READBACK.
    # `run_setup` writes later, which is why `assert_hooks_pinned` exists as a second READ
    # for the caller that owns §6's chronology to take between setup and the gate.
    _hooks_pin(seat.path)
    # The baseline half of the delta, and this is the only moment it can be taken: the
    # clone holds exactly B1 and `clone_seat` has already verified the checkout against its
    # manifest, so this is the one gate surface in the run known not to be a builder's.
    baseline_surface = gate_surface(seat.path, contract, command=command)
    before = _surface_state(seat.path, baseline_surface)
    bundle.materialize(candidate, seat.path)
    _assert_hooks_pinned(seat.path)
    candidate_surface = gate_surface(seat.path, contract, command=command)
    return Verifier(
        path=seat.path,
        # The UNION of the two surfaces, because that is the domain the delta was taken
        # over: a path present in one tree's surface and not the other's was compared just
        # as much as one in both — its absence on the other side is what puts it in the
        # delta. Either surface alone would under-report the measurement, which is the
        # direction a gate-surface claim must never be wrong in.
        candidate=bundle.with_gate_measurement(
            candidate,
            surface=tuple(sorted(set(baseline_surface) | set(candidate_surface))),
            delta=_gate_delta(before, _surface_state(seat.path, candidate_surface))),
        contract=contract,
        baseline_surface=baseline_surface,
        candidate_surface=candidate_surface,
        baseline_state=before)


def remeasure_gate_surface(v: "Verifier", *, command: "Command") -> "Verifier":
    """§6.1's surface, ENUMERATED AND READ AGAIN after the confirmed setup command has run.

    THE FIRST READ CANNOT SEE WHAT SETUP CREATES, AND SETUP IS CANDIDATE-OWNED. `build_verifier`
    enumerates before the candidate's setup executes, so a verify command naming
    `.venv/bin/pytest` is measured while that path does not exist — and a candidate that edits
    the setup entrypoint to write a stub there moves nothing tracked, leaves `core.hooksPath`
    alone, and earns a PASS over a gate it authored. `assert_hooks_pinned` was the only
    post-setup fact anything checked.

    THE ENUMERATION IS RE-RUN, NOT REUSED. `gate_surface` resolves the command's paths against
    the tree, and the whole point is that the tree changed; reusing `v.candidate_surface` would
    ask the pre-setup question a second time.

    WHAT THIS DOES NOT CLOSE, because the re-read runs the SAME enumeration: a gate resolved
    through PATH (`verify = [["pytest"]]` with setup prepending `.venv/bin`); a `make` recipe
    invoking a stub, where `_command_paths` yields only the Makefile; interpreter-side rigging
    — `sitecustomize.py`, a `.pth`, `PYTHONPATH`, a `-p` plugin — which is the
    language-indirection gap this module already admits; a token `_command_paths` drops as
    uncontained, absent from BOTH reads so the delta is empty twice; and a symlinked gate whose
    referent is rewritten, since `_surface_state` hashes only target text. Those routes stay
    open and are named rather than implied closed.
    """
    after_surface = gate_surface(v.path, v.contract, command=command)
    return replace(
        v,
        candidate_surface=after_surface,
        candidate=bundle.with_gate_measurement(
            v.candidate,
            surface=tuple(sorted(set(v.baseline_surface) | set(after_surface))),
            delta=_gate_delta(v.baseline_state, _surface_state(v.path, after_surface)),
            # NAMED, not silent: `build_verifier`'s reading is the write-ahead one the runner
            # already handed to its sink, and this is a later reading of a tree setup changed.
            supersedes=v.candidate))


def _materialized_sidecar(root: Path, rel: str) -> bundle.SidecarEntry | None:
    """The `bundle.SidecarEntry` this tree would produce at `rel`, or None for no entry.

    Built in the bundle's OWN shape rather than as a tuple this module invents, so the
    comparison is entry against entry and every field `bundle.build` records — kind, mode,
    payload — is in it by construction rather than by a second list kept in step by hand.

    None is "nothing a sidecar could describe": missing, unreadable, or a shape no sidecar
    has (a FIFO, a directory). They collapse into one answer because `SidecarEntry` only
    carries "file" or "symlink", so none of them can compare equal to one — and a path that
    cannot be READ is one this function cannot vouch for in either direction.

    That collapse is deliberately NOT what `_surface_state` does with the same OSError: it
    raises a distinct refusal, because a gate-surface path is compared against ITSELF across
    two reads and an unreadable one would compare equal to itself and vanish from the delta.
    Here the comparison is against a payload the bundle already holds, so an unreadable path
    can only ever produce a mismatch — fail-closed either way, and one answer is enough.

    THE READ SIDE OF THE PATH `bundle.materialize` WROTE, and it has to descend for the same
    reason the write did. `bundle.materialize` lays this exact `e.path` down through the full
    `contained` descent (`bundle.py`'s sidecar loop) and this function READ it back off a
    plain join — the write hardened and the read left open, which is the asymmetry the read-half
    sweep exists to find. An uncontainable path is None on the rule above: it is a path this
    function cannot vouch for, so it can only ever mismatch, which is the fail-closed direction.
    """
    at = _leaf(root, rel, "a materialized sidecar path")
    if at is None:
        return None
    with at:
        try:
            st = os.stat(at.leaf, dir_fd=at.fd, follow_symlinks=False)
            if stat.S_ISLNK(st.st_mode):
                # A link's own mode is not meaningful and `bundle` fabricates 0 for it; the
                # target TEXT is what materialization reproduces. Bytes, via surrogateescape,
                # because a link target is a filesystem name — `.encode()` raises on the
                # surrogates `os.readlink` puts there for a non-UTF-8 one.
                return bundle.SidecarEntry(
                    rel, "symlink", 0,
                    os.readlink(at.leaf, dir_fd=at.fd).encode("utf-8", "surrogateescape"))
            if stat.S_ISREG(st.st_mode):
                fd = bundle.open_leaf(at, os.O_RDONLY, "a materialized sidecar path")
                try:
                    return bundle.SidecarEntry(
                        rel, "file", st.st_mode & 0o777, bundle.read_fd(fd))
                finally:
                    os.close(fd)
        except OSError:
            return None
    return None


def assert_hooks_pinned(verifier: Verifier, *, by: str = "candidate") -> None:
    """§6 step 5, re-read after the confirmed setup ran and before the gate does.

    A READ, never a re-pin, and the distinction is the whole value: re-pinning here would
    restore the property and destroy the evidence that it had been lost, which is exactly
    what `build_verifier`'s own note means by a second pin hiding the loss of the first. An
    assertion detects instead, and a detection is what §6's premise needs.

    WHY THE SECOND READ IS NEEDED AT ALL, measured. `build_verifier` pins before
    materialization and asserts once afterwards, when the candidate is the only writer so
    far. THE CONFIRMED SETUP COMMAND RUNS AFTER THAT, in the candidate's own tree, and a
    candidate may legitimately own the script setup invokes: a `./setup.sh` the candidate
    rewrote to `git config --local core.hooksPath .githooks` leaves the gate running under
    builder-supplied hooks, and `run_setup`'s overlap check cannot see it because
    `.git/config` is not a tracked path. MEASURED, with this call neutralized: the verifier's
    `core.hooksPath` read `.githooks` at the moment the gate ran and `verify_candidate`
    returned PASS — reason "the gate exited 0; the bundle carried every artifact path, the
    gate surface was measured over 1 file(s) and unchanged". A verdict reading perfectly
    clean over a gate the builder had reached, which is the founding premise inverted.

    Takes the `Verifier` rather than a path so a caller cannot aim it at the seat's own
    clone, where the answer would be about the tree §6 exists to stop trusting.
    """
    _assert_hooks_pinned(verifier.path, by=by)


def validate_materialized(verifier: Verifier) -> None:
    """§6 step 2: the tree matches the bundle it was built from, or nothing else in §6 holds.

    Runs BEFORE setup because setup is the first thing that legitimately changes this tree:
    after it, "differs from the bundle" and "setup installed something" are the same
    observation, and the check can no longer be made at all.

    Only the SIDECARS are content-checked. The patch is applied by `git apply --index`, which
    fails loudly on a context mismatch, onto a clone `materialize` has already checked against
    the bundle's own baseline commit; re-hashing its postimages would re-derive what git
    enforced. A sidecar is a raw write, and a raw write is what a truncating or
    mode-dropping materialization gets wrong.

    The MODE is part of the comparison, not only the bytes. `check.sh` without its executable
    bit is a gate that stops running and no digest of its content says so — the same argument
    `_surface_state` makes for a gate surface, and §6's own reason for applying a patch rather
    than writing blobs.
    """
    bad = sorted(e.path for e in verifier.candidate.sidecars
                 if _materialized_sidecar(verifier.path, e.path) != e)
    if bad:
        raise VerifyError(
            f"the materialized candidate does not match its bundle at {_paths_phrase(bad)}; "
            "the gate would be measuring a tree nobody described")


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


@dataclass(frozen=True)
class SetupResult:
    """What re-running the confirmed setup command in the verifier measured.

    `overlap` is `()` on every result this module hands back, and that is a postcondition
    rather than a field with nothing to say: it is the written form of "setup left this
    tree's tracked files clean", which is the premise §6 requires before the gate may run
    here. A non-empty one has no result to travel in — it raises `SetupOverlap`, whose
    message carries the names.
    """
    run: Run
    overlap: tuple[str, ...] = ()


def run_setup(verifier: Verifier, setup: Command, *, env=None) -> SetupResult:
    """§6 step 3: run the confirmed setup command here, and refuse a tracked effect.

    A RIG THE BUILDER PLACED IN ITS OWN WORK PHASE IS ANSWERED HERE, if it is answered at
    all. `harvest` differences setup's output out of the artifact set, so a rig placed during
    the seat's SETUP phase never crosses; one placed during its WORK phase crosses as an
    ordinary sidecar, and the only thing that displaces it is the real toolchain this call
    installs over it. The argument reaches exactly as far as setup does — a rig at a path
    setup never writes is still standing when the gate runs, and §6.1's `gate_delta` rather
    than this is what marks that candidate.

    The contract comes off `verifier`, not from a parameter. A second contract argument would
    be a second place for one run to disagree with itself, which is what binding the contract
    to the bundle closed.

    `validate_materialized` is called from here rather than left to the caller because §6
    orders it before this command and the ordering is the whole of its value — a caller that
    made the check afterwards would be reading a tree setup had already moved.

    The delta is measured whatever the exit code, unlike `fixed_point`'s. That loop skips it
    because §6.2 already has the candidate's verdict from a nonzero gate and the post-run
    `_inventory` fails closed; here a nonzero exit is the SETUP failing, which produces no
    verdict to protect, and the tree is inventoried again by `fixed_point`'s own first line
    regardless.
    """
    validate_materialized(verifier)
    before = _inventory(verifier.path)
    tracked_before = _tracked(verifier.path)
    run = run_command(verifier.path, setup, env=env)
    tracked = _tracked(verifier.path)
    # `fixed_point`'s predicate, for `fixed_point`'s reason: setup can move CONTENT under a
    # path git tracks, or it can move TRACKED-NESS itself — `git add` of a sidecar the
    # candidate carried, `git rm --cached` of a tracked file — with the bytes on disk
    # untouched. A content-only reading sees neither of the second kind.
    moved = ({p for p in snapshot.diff(before, _inventory(verifier.path)) if p in tracked}
             | (tracked_before ^ tracked))
    overlap = tuple(sorted(p for p in moved if not _declared(verifier.contract, p)))
    if overlap:
        raise SetupOverlap(
            f"the setup command changed {_paths_phrase(overlap)}, which the run's generator "
            "contract does not declare; the gate would measure a tree whose tracked content "
            "is not the candidate the bundle describes")
    return SetupResult(run=run, overlap=overlap)


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


def _confirm_fixed_point(root: Path, command, contract, *, env=None) -> Run:
    """§5 step 3's second pass: run the gate again and require every TRACKED path it moved
    to be one the contract declares.

    `fixed_point` cannot answer this and is not changed to. It re-runs only while DECLARED
    output is still moving, so under a contract that declares nothing the gate runs ONCE and
    a command that is quiet on its first pass and writes on its second — exactly what a
    second pass exists to catch — converges. Its contract is nonetheless right for its own
    caller: §6 runs that loop for every seat's candidate, where an unconditional extra pass
    charges another full gate run each time, while §5 runs THIS one once, before any
    provider spends a token, which is where the spec puts the cost.

    The delta predicate is `fixed_point`'s and `run_setup`'s, spelled out at both: content
    can move under a tracked path, or TRACKED-NESS can move in either direction — a
    `git add`, a `git rm --cached` — with the bytes on disk untouched.

    Declared output is subtracted because this pass does not checkpoint. Admitting an output
    and STAGING it is what settles declared movement and `fixed_point` owns both, so a raise
    here would name an instability the engine's own admission step was never given a chance
    to answer. What that leaves for no one to catch is a declared output whose first move is
    on this pass.

    A nonzero exit returns the run unmeasured, on `fixed_point`'s reason for the same skip:
    the post-run `_inventory` FAILS CLOSED, so a gate that failed in a tree over quota would
    come back as a `VerifyError` — and here that would take the green first pass, which is
    the value §6.2 needs, down with a measurement that only confirms it. A baseline gate that
    disagrees with itself is also not a generator that failed to settle, so it is reported
    rather than renamed: §5 step 2 has the user confirm a policy for calibration failure.
    """
    before = _inventory(root)
    tracked_before = _tracked(root)
    run = run_command(root, command, env=env)
    if run.exit_code != 0:
        return run
    tracked = _tracked(root)
    moved = ({p for p in snapshot.diff(before, _inventory(root)) if p in tracked}
             | (tracked_before ^ tracked))
    delta = tuple(sorted(p for p in moved if not _declared(contract, p)))
    if delta:
        raise GeneratorUnstable(
            f"the verify command changed {_paths_phrase(delta)} on a second pass over the "
            "untouched baseline, which no generator relation declares, so it has no fixed "
            "point: §5 step 3 requires that pass to show zero tracked delta before any "
            "provider spends a token. This is an infrastructure failure of the command, "
            "never a candidate's verdict.")
    return run


@dataclass(frozen=True)
class Calibration:
    """What the untouched baseline does under the confirmed commands (spec §5 step 3).

    `run` is the only value in this package a caller may pass as `classify`'s
    `baseline_run`. Nothing downstream can enforce that — `_as_run` says in as many words
    that passing its type check is no evidence about where a `Run` came from — so what
    makes §6.2's `BASELINE_RED_NO_NEW_IDENTIFIED_FAILURE` an honest outcome is the TREE
    this ran in, and that is the whole reason this function exists rather than a caller
    running the command wherever it happens to be standing. A calibration taken somewhere
    a builder could reach turns that outcome into a verdict the builder chose.

    WHAT `classify` READS IT FOR IS THAT ONE OUTCOME AND THE FAIL BESIDE IT. `baseline_run`
    is consulted only once the candidate's gate has already exited nonzero, to choose between
    `BASELINE_RED_NO_NEW_IDENTIFIED_FAILURE` and `FAIL`; a PASS is decided without reading it
    at all. An orchestrator that expects a green calibration to strengthen a candidate's PASS
    is reading a comparison `classify` never makes. What this object contributes to a run
    that goes on to pass is elsewhere: the §5 step 3 refusals made on the way here, and
    `unexplained` below.

    `setup` is the confirmed setup command's own run. `run_setup` RETURNS a failing setup
    rather than raising — only a tracked overlap is a refusal — so a setup that exited
    nonzero over a gate that still passed exists nowhere else once this object is built, and
    §5 step 2 has the user confirm a policy for calibration failure, which takes seeing one.

    `unexplained` is `fixed_point`'s and carries ITS PRECONDITION: READ IT ONLY WHEN
    `run.exit_code == 0`. That loop returns on a nonzero gate before the pass that failed is
    measured, so on a red calibration it holds whatever EARLIER passes measured — nothing at
    all under a contract that declares nothing, since the gate then runs once, however much
    of the tree it rewrote on its way to failing. `_gate_taints` states the same precondition
    for the same value. `admitted` needs no such guard and is given none: it is what the
    engine ADMITTED, which is a fact about this tree whatever the gate went on to exit.
    ADMITTED, not staged — `_checkpoint` stages only a path that still exists or is already
    tracked, so a declared path the gate created and then deleted is admitted and never
    reaches the index.

    On a green run, `unexplained` is what §5 step 3 measures where a contract declares
    NOTHING — which is every contract this engine can DETECT today, since
    `inspect.detect_generators` takes its `repo` argument unused and answers with no
    relations, though `build_verifier` and the §5 gate both take one a human confirmed. A
    verify command that rewrites tracked paths no relation covers is a `_gate_taints` taint,
    so it displaces the outcome of every candidate that would otherwise have PASSed;
    measuring it on the baseline is how that is known before a provider spends a token rather
    than after three of them have. It is also the only channel that reports an IDEMPOTENT
    rewrite, which the confirming pass cannot see: that pass writes the same bytes again and
    measures nothing.

    `second_pass` is the confirming gate run. When it exited 0 its tracked delta was empty,
    because a non-empty one raised `GeneratorUnstable` instead of reaching this object; when
    it exited nonzero nothing was measured at all. `None` means the FIRST pass was red and no
    second was spent — §6.2 already has its verdict there. A red `second_pass` over a green
    `run` is a baseline gate that disagrees with itself, which is recorded rather than
    refused; `classify` reads the same pair in the candidate direction as FLAKY.

    `setup` IS OPTIONAL, and `None` is a different fact from a `Run` that exited 0: it says
    the run's confirmation named NO setup command, so none was run here. `gate.Confirmation`
    admits an empty setup in as many words — it refuses only an empty VERIFY — and this field
    used to be typed as though it could not happen, which made §5 step 3 raise for every
    repository that needs no toolchain. It is the same fact `runner`'s `status.builder_setup ==
    "none"` records one module over — NOT its `"not-run"`, which is §8's value for a
    confirmed command whose measurement was withheld and is a different state entirely; this
    sentence named that one, and the two sharing a spelling in `seat` is what made a
    no-toolchain run a crash. `_with_setup_caveat` already reads `None` here as "nothing to
    say about a setup that never ran" rather than as a passing one.
    """
    run: Run
    path: Path
    setup: Run | None
    second_pass: Run | None
    admitted: tuple[str, ...] = ()
    unexplained: tuple[str, ...] = ()


def calibrate(repo, baseline, dest, *, identity, contract, setup, command,
              env=None) -> Calibration:
    """Run the confirmed setup and verify commands on the untouched baseline (spec §5 step 3).

    The candidate is EMPTY — no patch, no sidecars — so `build_verifier` produces exactly
    the baseline through the path every other tree in the run takes. Building it any other
    way would make the calibration the one clone in the run whose construction nobody
    reviewed, and it is the run §6.2 leans on hardest.

    `validate_materialized` is deliberately not called here, on EITHER branch below, and the
    reason is the bundle rather than the delegation: the candidate is empty by construction,
    so there are no sidecars to compare and the check can only ever be a no-op. Where a
    setup command was confirmed, `run_setup` makes the check as its first statement anyway,
    because §6 orders it before setup; where none was, nothing calls it and nothing is lost.
    `verify_candidate`'s no-setup branch DOES call it, and the difference is not an
    inconsistency — that one holds a real candidate with real sidecars.

    A CONFIRMATION THAT NAMED NO SETUP IS RUN, NOT REFUSED. `run_command` declines to report
    exit 0 for a gate that ran nothing, so calling `run_setup` unconditionally made §5 step 3
    raise `VerifyError` for every repository needing no toolchain — a run the §5 gate
    explicitly admits, since `gate.Confirmation` refuses only an empty VERIFY. The branch
    below is that closed, and `Calibration.setup` carries the `None` that says so.

    `env` is passed through rather than derived from `repo`, though this IS a caller holding
    the repo path. The calibration and the candidates it will be compared against have to
    run in one environment or `classify` is differencing two machines, and the caller is
    what holds both.
    """
    empty = bundle.CandidateBundle(
        version=bundle.VERSION, baseline_ref=baseline.ref,
        baseline_commit=baseline.commit, generator_contract_id=contract.id)
    # `command` is passed as it is for a candidate, and NOTHING can tell the difference: a
    # calibration's gate delta is empty by construction, since both surfaces are read off
    # one untouched tree. It is passed anyway because "built the way every other tree in the
    # run is built" is this function's whole claim, and an argument dropped for being
    # unobservable is one the next reader has to re-derive as harmless.
    v = build_verifier(repo, baseline, empty, dest, identity=identity, contract=contract,
                       command=command)
    setup_run = run_setup(v, setup, env=env).run if setup.steps else None
    # THE READ EVERY CANDIDATE GETS, AND THE CONTROL DID NOT. `build_verifier` pins and
    # asserts once, before materialization; `assert_hooks_pinned` exists because the confirmed
    # setup command runs AFTER that, and `verify_candidate` took the second read while this
    # function — holding the `Verifier` right here, between setup and the gate — did not.
    #
    # `Calibration`'s own docstring rests §6.2's BASELINE_RED_… on "the TREE this ran in", and
    # a tree whose pin was never re-read after setup is not the tree every candidate is
    # required to have run in. The ordinary trigger is husky: `npm ci` runs `prepare`, `husky
    # install` writes core.hooksPath, the calibration comes back GREEN under the repository's
    # own hooks, and then all three candidate verifiers refuse — after three providers are
    # paid, blaming a candidate for the operator's command. `by="setup"` because this clone
    # has no candidate to blame.
    assert_hooks_pinned(v, by="setup")
    fp = fixed_point(v.path, command, v.contract, env=env)
    second = (_confirm_fixed_point(v.path, command, v.contract, env=env)
              if fp.run.exit_code == 0 else None)
    return Calibration(run=fp.run, path=v.path, setup=setup_run, second_pass=second,
                       admitted=fp.admitted, unexplained=fp.unexplained)


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

    Two facts, and the first has four states rather than two. `gate_delta is None` is
    "nobody looked", which `bundle.CandidateBundle` names as the fail-OPEN reading of an
    empty tuple and requires a consumer to treat as UNKNOWN; a non-empty tuple is §6.1's
    `gate_changed`; `()` is a clean measurement, and it splits again on whether the bundle
    records WHAT was measured. An empty delta with `gate_surface is None` is the first state
    one field over — the record says nothing moved and nothing says over what — so it is a
    taint too, and the two are kept as separate sentences because one is a measurement to go
    and take while the other is a record to go and repair. `gate_surface == ()` is NOT a
    taint: a tree really can define its gate somewhere no rule and no command reaches, and
    refusing every such repository a PASS would answer a stated coverage gap with a verdict
    nobody can act on. What it costs is said in the PASS sentence instead — see `classify`.

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
    elif cb.gate_surface is None:
        taints.append("this candidate records a clean gate delta with no record of what it "
                      "measured (gate_surface is None), so an unexamined gate and an "
                      "unchanged one cannot be told apart here")
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

    A PASS says in its own reason how much of it was measured, on two independent axes, and
    both are weaker claims rather than different outcomes. A bare `Run` cannot answer §7.2's
    half — only `fixed_point` measures a tracked delta — so a caller that ran the gate
    through `fixed_point` and then hands over `fp.run` gets a weaker PASS than §6.2's. And
    `bundle.gate_surface` says whether §6.1's half had anything to range over: `()` is a real
    measurement that found no gate-defining file, and the sentence names that instead of
    claiming the surface was unchanged. Both are visible in the reason rather than assumed.

    PASS NEVER READS `baseline_run`. `_run_verdict` returns it on `cand.exit_code == 0`
    before `base` is consulted at all, so a calibration is what makes
    `BASELINE_RED_NO_NEW_IDENTIFIED_FAILURE` an honest outcome and contributes nothing to a
    PASS — an orchestrator that supplies a green `Run` from anywhere gets the same PASS it
    would get from `calibrate`. What a PASS rests on is the candidate's own exit code, the
    bundle, and the two measurements above.
    """
    cand = _as_run(candidate_run, "candidate_run")
    base = _as_run(baseline_run, "baseline_run")
    again = None if rerun is None else _as_run(rerun, "rerun")
    taints = _gate_taints(candidate_run, cand, bundle)

    if bundle.omitted:
        # The gate facts are measured BEFORE this branch and appended to its reason. They
        # are independent of the harvesting gap — no taint `_gate_taints` can append is a
        # statement about `omitted` — so a `HARVEST_INCOMPLETE` that dropped them would hide,
        # from the reviewer who has to decide whether to re-harvest, whatever the gate facts
        # say: that nobody measured the surface, that the candidate moved a file defining the
        # gate, or that the surface came back empty.
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
        # `gate_surface` is `()` only when a real measurement found no gate-defining file,
        # and the sentence says which of the two it was: an empty delta over an empty
        # surface is the shape a candidate that GUTS `./check.sh` produces when no rule
        # names that file and no confirmed command was passed, and "measured and unchanged"
        # over it is a verdict reading cleaner than its evidence.
        surface = (f"the gate surface was measured over {len(bundle.gate_surface)} file(s) "
                   "and unchanged"
                   if bundle.gate_surface else
                   "the gate surface was EMPTY — no file in this tree matched a "
                   "gate-surface rule and no verify command named one — so whatever defines "
                   "this gate is outside what §6.1 measured")
        reason = f"{reason}; the bundle carried every artifact path, {surface}, and {measured}"
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

    `NO_DAEMON_CACHE` is load-bearing on the ONE tree this function is also read over that
    the engine did not build: `gate.must_show` resolves the surface against the USER's
    worktree at §5 step 2, which is before the user has authorized anything. §5 step 1 admits
    no repository-supplied code before authorization, and a surface computed by running the
    repository's own program is the one measurement that cannot claim it.

    LOADING THE INDEX IS WHAT RUNS THE MONITOR — not `--others`. Measured
    on git 2.53.0 with `core.fsmonitor` pointed at a script that touches a file: `ls-files`
    ran it in EVERY form, `-z` and `--cached` alone included, alongside `status`, `diff`,
    `add`, `write-tree`, `check-attr` and `update-index --refresh`; `rev-parse`, `show-ref`,
    `for-each-ref`, `symbolic-ref`, `cat-file`, `config`, `update-ref`, `commit-tree`,
    `clone` and `apply --numstat` did not. The rule is the index, so the flags belong on any
    call here that reads one — the narrower rule "`--others` has to decide what is untracked"
    was this comment's second wrong mechanism, and it reads a cached-only `ls-files` as safe,
    which is how `baseline.materialize` kept the hole after this call was fixed.
    """
    out = gitcmd.git(root, *gitcmd.NO_DAEMON_CACHE, "ls-files", "-z", "--cached", "--others",
                     "--exclude-standard", env_extra=gitcmd.READONLY, binary=True).stdout
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
            # DESCENDED, NEVER JOINED — see `_leaf`, and this is the site that puts the name
            # into the surface `_surface_state` then reads. `is_file()` FOLLOWS every
            # component, so a token naming `evil/check.sh` where `evil` is a link out of the
            # tree used to answer True and enter the surface as a tree-relative name that no
            # longer describes anything in the tree. An uncontainable one is dropped, which is
            # what this function already does with an escaping step.
            at = _leaf(root, rel, "a gate-surface path")
            if at is None:
                continue
            with at:
                try:
                    st = os.stat(at.leaf, dir_fd=at.fd, follow_symlinks=False)
                except FileNotFoundError:
                    # THE ONE OSError THAT IS AN ANSWER. A command naming a path the tree
                    # does not hold names nothing, so it is not part of §6.1's surface.
                    continue
                except OSError:
                    # EVERY OTHER OSError IS "COULD NOT LOOK", AND DROPPING IT WAS A
                    # FAIL-OPEN. A path this engine cannot stat — EACCES, ELOOP, a mount that
                    # went away — is one whose gate-defining-ness is UNKNOWN, and a surface
                    # that omits it reports a candidate rewriting it as an ordinary change
                    # rather than `gate_changed`. Included, because "we could not look" must
                    # not read as "this is not part of the gate": the whole point of the
                    # surface is that a candidate cannot quietly gut what judges it.
                    named.add(rel)
                    continue
                if stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode):
                    named.add(rel)
                if stat.S_ISLNK(st.st_mode):
                    # A GATE THAT IS A LINK IS RUN THROUGH ITS REFERENT, so the referent is
                    # gate-defining too. Naming only the link watched the wrong file:
                    # reproduced — `./gate.sh -> real-gate.sh` put `gate.sh` in the surface
                    # and nothing else, so a candidate rewriting `real-gate.sh` changed what
                    # the gate DOES while the link's own target text stayed byte-identical
                    # and `gate_changed` never fired. That is the founding premise inverted:
                    # the party under judgement editing what judges it.
                    #
                    # CONTAINED, AND ONLY ONE HOP. `_leaf` is the same containment this
                    # function already applies — a link out of the tree names nothing the
                    # candidate could rewrite through this repository, and it is dropped as
                    # an escaping step already is. One hop rather than a full `realpath`
                    # walk, because each hop is itself a tracked path this loop will meet on
                    # its own terms if the tree holds it.
                    try:
                        tgt = os.readlink(at.leaf, dir_fd=at.fd)
                    except OSError:
                        tgt = None
                    if tgt is not None and not os.path.isabs(tgt):
                        ref = os.path.normpath(os.path.join(os.path.dirname(rel), tgt))
                        if not ref.startswith("..") and _leaf(root, ref,
                                                              "a gate-surface path") is not None:
                            named.add(ref)
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
    """The files in this tree that DEFINE the gate — one half of a `gate_delta`.

    `build_verifier` is the caller that takes the other half and differences the two; what
    it differences is not these names but each name's content identity, because a candidate
    that rewrites a gate file in place changes no name at all.

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

    `verifier_path` must be a tree THE PARTY UNDER SUSPICION COULD NOT HAVE WRITTEN. A
    verifier and the baseline checkout qualify by being engine-built; a seat's does not —
    its index is the candidate's, and `git rm --cached Makefile` there would delete a gate
    file from a surface computed over it. The user's own worktree qualifies at §5 step 2 and
    only there, which is what `gate.must_show` reads it for: no seat exists yet, so the
    suspect party does not either. What that costs is `_enumerate`'s flags — see its own
    note for the program a repository can otherwise have git run on the reader's behalf.
    """
    root = Path(verifier_path)
    paths = _enumerate(root)
    globs = _test_globs(root, paths)
    surface = {p for p in paths if _gate_role(p, globs)}
    surface |= _command_paths(root, command)
    surface |= _contract_sources(contract, paths, surface)
    return tuple(sorted(surface))
