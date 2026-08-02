"""§5's confirmation gate: the cost quote, the detector that can refuse a verify command,
and the one question a run is opened on.

ONE QUESTION, AND EVERYTHING THE ANSWER IS WORTH. §5 step 2 asks once — both command
sequences, a policy for calibration failure, and the strategy rule to be applied later — and
§5 step 5 says "record the decision; do not ask again". Three functions carry that: `must_show`
is what a human reads before answering, `confirm` turns the answer into a record that cannot
be defaulted, and `open_run` writes the manifest §14.2 forbids rewriting. Nothing here spends
a provider call, and nothing after it asks again — which is why every refusal in this file
lands before a run directory exists rather than after.

A QUOTE IS A REFUSAL SURFACE, NOT A DISPLAY. Everything here is sized so that being wrong is
wrong in the OPERATOR's disfavour: a worst case that reads low is a number somebody agrees to
and then discovers at 3am, and there is no later gate to catch it — §5 step 2 asks once.

THE PROVIDER-CALL ARITHMETIC, AND WHERE IT PARTS FROM §5.2's OWN SUM. §5.2 writes
`9 + 1 + 6 = 16`: 3 builders × 3 attempts, plus one synthesis, plus two review rounds × three
reviewers at the `--retries 0` forge wires (the council's own `--retries 2` default would make
the review term 18 and the total 28 — quoted here as a line, since it is not what is wired).
The same paragraph then requires that "the quote includes post-review synthesis invocations",
and 9 + 1 + 6 has exactly ONE synthesis in it. §13's loop has two more: a round-1 blocker is
fixed before round 2 runs at all, and "a blocker fixed after round 2 is reported *verified but
not independently reviewed*" — so a fix happens there too, and §12.3 caps the count rather than
forbidding it. §13.1's ultrareview is DEFAULT ON and its findings "follow the exact
post-round-2 rule above: fix → fresh-verifier verify → checkpoint", which is a third one: §5.2
prices the ultrareview RUN in money and says nothing about the FIX it triggers.
`provider_calls` therefore adds one synthesis invocation per review round and one for
ultrareview, and is 19 rather than 16 at the wired settings. `--no-ultra` is a PARAMETER here
rather than a reason to leave the term out of the default quote. §5.2's own sum is kept visible
as its own line, so nothing is hidden in either direction, and §21's "worst case 16 → 10 runs"
for cutting the retry path reads 13 here for the same reason.

WHAT THE SETUP AND VERIFY COUNTS ARE FOR. §6 is the expensive clause in the design — "it costs
one extra setup per candidate; that cost is essential, not optional hardening" — and a quote
that folds it into the provider line is the one an operator finds wrong first, because setup is
where the wall clock and the disk actually go. Three sources, each counted separately:
calibration runs setup+verify TWICE (§5 step 3, so the second pass can show zero tracked
delta); every builder clone runs setup once, and §8.1 gives a retry a FRESH clone, so the
builder term is seats × attempts and not seats; and a fresh verifier clone runs setup+verify
per candidate (§6) plus once after each post-review fix (§13, "re-run verify and cut a new
checkpoint after every fix", and §13.1 for ultrareview's).

A builder's own clone is NOT counted as a verify. §7 lists `Fverify` among a seat's four
inventories, but §6 overrides it in as many words — "`Fverify` cannot be 'the fourth inventory
of the builder clone' when verification happens elsewhere" — and the whole §6 argument is that
running the gate where the builder was measures nothing.

PEAK DISK IS COUNTED IN CLONES, NOT IN FLEETS. §5.2 gives one figure — "three no-hardlink
clones plus three dependency trees is plausibly 6-10 GB" — and it prices the BUILDER FLEET at
three seats. Quoting that as the peak understates it by the whole rest of the run, in the
operator's favour: §8.1 preserves a failed retry's clone as partial input rather than resetting
in place, and §6 gives every candidate a FRESH verifier clone. §15's automatic cleanup covers
"known-failed temporary clones only" and would reclaim the failed retries — but §8.1 preserves
those specifically, as partial input, so the specific rule wins and none is removed while the
run is alive. The
spec's own upper bound is therefore divided by the fleet it was stated for — 10 GB / 3 clones —
and multiplied by every clone the worst case builds: calibration (§5 step 3), builders
(seats × attempts), and verifiers. That is 17 clones at the wired settings, not 3. Left out and
said so in the line: the synthesis WORKTREE, which §4 makes a worktree precisely so it shares
the parent's objects, and where setup never runs.

THE DETECTOR IS STATIC AND READS ONLY. §5 step 1 admits no repository-supplied code before
authorization, and a detector whose job is "would this command spend provider calls" is exactly
the one tempted to find out by running it. THE WHOLE MODULE is bound by that rule, not only
the detector, because `must_show` runs at §5 step 2 and the operator has still not answered.
Nothing here executes a Makefile, a recipe, a script or a `make -n`. The detector's own git
call is `rev-parse --show-toplevel`, which carries `NO_DAEMON_CACHE` even though it does not
need it: measured on git 2.53.0, with `core.fsmonitor` pointed at a script that touches a
file, `git status` runs the script and `git rev-parse --show-toplevel` does not, because the
second refreshes no index. The flags are on it so the guarantee holds for whatever git call is
added here next, which is the direction this package has already lost the property in once —
see `gitcmd.NO_DAEMON_CACHE`'s own note on the `ls-files --eol` and `check-attr` calls in
`inspect`. That prediction has since been paid: `must_show` reaches `verify.gate_surface`,
whose `ls-files --cached --others` DOES run the monitor, and the flags went on it for this
reason rather than for caching. `test_the_detector_runs_nothing_the_repository_supplied` and
`test_reading_the_surface_at_this_gate_runs_nothing_the_repository_supplied` pin both halves
against controls showing the same recipe does run under `make` and the same hook does run
under `git status`.

A DETECTOR THAT MUST BE EXHAUSTIVE CANNOT BE, so the bound is declared instead of implied, and
every place the reader ran out is EMITTED rather than dropped. Findings carry one of three
prefixes, and §5.2's "detected and refused (or priced as its own explicit line)" is the reason
there are three rather than one:

  * `spends:`     a resolved recipe or argv runs a provider CLI, or executes a file naming a
                  council entry point without a hermetic flag. This is the refusal.
  * `remedy:`     a file the gate executes DOCUMENTS a `make <target>` whose own closure
                  spends. `make` does not follow it; an operator does, and only when the gate
                  fails. That is a price line, not a refusal — and it carries §5.2's steer to
                  `make verify` (receipts advisory), which is the half of that sentence an
                  operator can act on: the grep alone only names the chain.
  * `unresolved:` the reader could not follow the target: a recipe using a make function or
                  automatic variable it does not expand, a `cd` part-way through a shell line,
                  a script path the tree does not hold, an undefined target, a missing
                  Makefile, a documented remedy whose own closure could not be read. Fail
                  closed by SAYING SO — an unresolved target is a legitimate answer for a
                  human to price, and a silent one is a fail-open.

The three prefixes are EXPORTED (`SPENDS`, `REMEDY`, `UNRESOLVED`), because telling a refusal
from a price is the caller's whole reason for calling and a caller spelling `"spends:"` by hand
is one rename away from reading every refusal as a price.

`cd` inside a recipe is followed for the same reason `make -C` is: §5.1 names `cd frontend &&
npm ci` as the shape real monorepos have, and resolving the rest of the recipe against the
wrong directory finds nothing and says nothing. Only a LEADING `cd` is followed; one appearing
later in the same shell line truncates the read there and is reported, since everything after
it is relative to a directory this reader did not track.

WHAT IT CANNOT SEE, measured on this repository rather than imagined. `make precommit` and
`make verify` here come back with the same findings in the same classes — three `unresolved:`
and one `remedy:`, differing only in the trail that names which target was typed — because the
only difference between the two is `advisory=False` and `advisory=True` inside a `python3 -c`
one-liner: the first exits 1 and sends the operator to `make eval`, the second prints a warning
and exits 0. That is a difference in RUNTIME EXIT CODE, and no static read separates them. Both
get the `remedy:` line, and the operator reads which target they typed. Also unread: `include` directives,
`define`/`$(call …)` macro bodies, `$(shell …)` and the other make functions, a package.json
script chain, and anything a container image defines — `verify.gate_surface`'s docstring names
the same class of indirection as its own accepted gap. And a RECIPE is checked for a provider
CLI at program position only, so `timeout 600 claude -p …` written inside one is missed; the
loose whole-token rule is applied to the user's own argv, where the command is short and read
whole, and not to a recipe, because this repository's Makefile passes `--providers claude` as
an argument and a whole-line rule would refuse every flag shaped like it. The last gap is the
one accepted deliberately: a file handed to a RUNNER as data — `pytest -q tests/x.py` — really
is imported and run, and is not read here, because searching past the first operand refuses
`make council-test` on `tests/test_council_characterization.py`'s stubbed `run_council`, and
with it every `make verify`. See `test_a_file_handed_to_a_runner_as_data_is_not_read_as_the_program`.
"""
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from . import baseline, bundle, gitcmd, journal, preflight, runstate, storage, verify


class GateError(RuntimeError):
    """What stops a run at this gate: an argument the gate will not answer for, an answer §5
    will not record, or the two conditions §2.3 and §5.2 refuse a run over.

    `Quote.lines` and `provider_invoking_verify`'s findings stay DESCRIPTIONS. They say what
    is true of a repository or a command; a raise says the run does not open. The overlap is
    deliberate and one-directional — `open_run` turns a `spends:` finding and a non-empty
    `preflight.refusals` into this, and nothing turns this back into a line."""


# The three classes a finding from `provider_invoking_verify` can carry, as the module
# docstring defines them. Public: `SPENDS` is the refusal and the other two are prices, and a
# caller cannot act on the difference without a name for it.
SPENDS = "spends:"
REMEDY = "remedy:"
UNRESOLVED = "unresolved:"

# §5 step 3: setup + verify run TWICE on the untouched baseline, so the second pass can show
# the zero tracked delta that turns §7.2's fixed-point assumption into evidence.
_CALIBRATION_PASSES = 2

# §5.2's stated upper bound, and the seat count it was stated for. Kept as a pair because the
# figure prices ONE FLEET and the run holds several at once: "three no-hardlink clones plus
# three dependency trees is plausibly 6-10 GB" is 10/3 GB per clone-and-its-dependencies, and
# that quotient is the only per-clone figure the spec gives.
_SPEC_PEAK_DISK_GB = 10.0
_SPEC_PEAK_DISK_SEATS = 3

# The sacrificial clone §5 step 3 calibrates in. Counted against the peak because §15 removes
# known-failed temporary clones only, and a calibration pass that succeeded is not one.
_CALIBRATION_CLONES = 1

# What the council's own CLI defaults to. Forge does not use it (§13 invokes review with
# --retries 0), so it is quoted as the alternative §5.2 asks to be shown, never summed in.
_COUNCIL_DEFAULT_ATTEMPTS = 3


@dataclass(frozen=True)
class Quote:
    """What §5 step 2 shows before anyone agrees to anything.

    `ultrareview` is a STRING and the type is the point: §13.1 prices it in usage credits
    ($5-25) or against three one-time free runs, and an integer field would render as one more
    call in a column of call counts — free, next to numbers that are free.

    `lines` is the quote a human reads; the scalars are the same quote for something that has
    to compare — so a scalar that reads low is not rescued by a line naming what it left out.
    Every line names the section it comes from, because an operator who thinks a number is
    wrong needs the derivation, not the total.
    """
    provider_calls: int
    ultrareview: str
    setup_runs: int
    verify_runs: int
    peak_disk_gb: float
    lines: tuple[str, ...]


def quote(report, *, seats=3, attempts=3, review_rounds=2, ultrareview=True) -> Quote:
    """Price the worst case of a run over `report`'s repository.

    A `preflight.Report` is required rather than a path: the quote reads the repository's
    SHAPE — that a static look happened, and what it already refuses — not its contents. A
    caller who has not run preflight has not reached §5 step 2 yet.

    `ultrareview` defaults True because §13.1 is titled "default on"; `--no-ultra` sets it
    False. It moves three scalars and not just the money line, because §13.1's findings get
    the post-round-2 treatment — a fix, and a fresh verifier setup+verify to check the fix.
    """
    if not isinstance(report, preflight.Report):
        raise GateError(f"a preflight.Report is required, not {type(report).__name__}; "
                        "the quote is shown at §5 step 2, after the static look")
    if seats < 1 or attempts < 1 or review_rounds < 0:
        raise GateError(f"seats={seats} attempts={attempts} review_rounds={review_rounds}: "
                        "seats and attempts are at least 1, review rounds at least 0")

    builders = seats * attempts
    synthesis = 1
    review = review_rounds * seats
    ultra_fixes = 1 if ultrareview else 0
    review_fixes = review_rounds + ultra_fixes
    calls = builders + synthesis + review + review_fixes

    # One candidate per seat, plus synthesis's own; §6 gives each a fresh verifier clone, and
    # §13/§13.1 add one more verification after each post-review fix.
    verifier_runs = seats + 1 + review_fixes
    setup_runs = _CALIBRATION_PASSES + builders + verifier_runs
    verify_runs = _CALIBRATION_PASSES + verifier_runs
    clones = _CALIBRATION_CLONES + builders + verifier_runs
    peak_disk_gb = round(clones * _SPEC_PEAK_DISK_GB / _SPEC_PEAK_DISK_SEATS, 1)

    ultra_line = ("$5-25 in usage credits, or one of the 3 one-time free runs (§13.1, default "
                  "on); --no-ultra opts out. Its fix is one of the post-review synthesis "
                  "invocations counted above"
                  if ultrareview else
                  "not run — --no-ultra (§13.1 is default on, so this is the opted-out quote); "
                  "no money, and no post-review fix for it above")

    blocked = preflight.refusals(report)
    lines = []
    if blocked:
        lines.append(f"refused first: preflight names {len(blocked)} thing(s) that stop this "
                     "run, so nothing below is spent until they are cleared (§5 step 1's "
                     "static look is what found them)")
    lines += [
        f"provider calls: {calls} worst case = builders {seats}x{attempts}={builders} "
        f"(§8.1 gives a retry a fresh clone) + synthesis 1 + review {review_rounds} round(s) "
        f"x {seats} reviewers = {review} + post-review synthesis {review_fixes}",
        f"  of which §5.2 states the first three as {builders} + {synthesis} + {review} = "
        f"{builders + synthesis + review}; the post-review synthesis invocations are the ones "
        "the same paragraph requires the quote to include (§13's round-1 fix, a blocker fixed "
        f"after round 2, and §13.1's ultrareview fix = {review_fixes})",
        f"  review retries: §13 has forge invoke the council with --retries 0, so each reviewer "
        f"is asked once per round; §5.2 asks for whichever is wired. At the council CLI's own "
        f"--retries 2 the review term alone would be "
        f"{review_rounds * seats * _COUNCIL_DEFAULT_ATTEMPTS}",
        f"ultrareview: {ultra_line}",
        f"setup runs: {setup_runs} = calibration {_CALIBRATION_PASSES} (§5 step 3 runs "
        f"setup+verify twice on the untouched baseline) + one per builder clone {builders} + "
        f"one per verifier clone {verifier_runs} (§6: a fresh verifier per candidate, plus one "
        "after each post-review fix). §6 calls that per-candidate setup essential, not "
        "optional hardening",
        f"verify runs: {verify_runs} = calibration {_CALIBRATION_PASSES} + verifier "
        f"{verifier_runs}. A builder's own clone is not verified — §6 puts every verification "
        "in a clone the builder never had access to",
        f"peak disk: ~{peak_disk_gb} GB under XDG_STATE_HOME = {clones} clones x "
        f"{_SPEC_PEAK_DISK_GB}/{_SPEC_PEAK_DISK_SEATS} GB, which is §5.2's upper bound for "
        f"'three no-hardlink clones plus three dependency trees' divided by the fleet it "
        f"prices. The {clones} are calibration {_CALIBRATION_CLONES} (§5 step 3) + builders "
        f"{builders} (§8.1 preserves a failed attempt's clone rather than resetting in place) "
        f"+ verifiers {verifier_runs} (§6, fresh per candidate). §8.1's \"preserved as partial "
        "input\" is the SPECIFIC rule and wins over §15's general auto-removal of known-failed "
        "temporary clones, which would otherwise reclaim the retries; nothing reclaims the rest "
        "until `--gc`. Not counted: the synthesis worktree, which §4 keeps a worktree so it "
        "shares the parent's objects",
        "wall clock: not quoted — a duration is measured by running the gate, and §5 step 3's "
        "calibration is the first time anything does. Nothing static reads one off a command, "
        "and the surface `must_show` resolves is a file list rather than a timing",
        "provider cost: quoted as a call count, not as currency; ultrareview above is the one "
        "line §13.1 prices in money. Shell-command time is the wall-clock line above",
        "verify command: run `provider_invoking_verify` on it once it is named — §5.2 refuses "
        "one that reaches a provider CLI, or prices it as its own explicit line, and steers "
        "the operator to `make verify` (receipts advisory) rather than the gate whose "
        "documented remedy is what spends",
    ]
    return Quote(provider_calls=calls, ultrareview=ultra_line, setup_runs=setup_runs,
                 verify_runs=verify_runs, peak_disk_gb=peak_disk_gb, lines=tuple(lines))


# ---------------------------------------------------------------------------------------
# §5.2's detector.

_PROVIDER_CLIS = frozenset({"claude", "codex", "agy"})

# Matched against a PROGRAM position only — start of a recipe, or just after a shell separator
# or a command substitution. `--providers claude` is an argument to something else and must not
# read as an invocation, and it occurs in this repository's own Makefile.
_PROVIDER_CMD = re.compile(r"(?:^|[;&|(`]|\$\()\s*[@+-]?\s*(?:\S*/)?(claude|codex|agy)\b")

_MAKE = frozenset({"make", "gmake"})

# A program that takes a repo file as its operand, so the operand is what actually runs. `uvx`
# and `uv` are here because this repository's own gate reaches pytest through them.
_INTERPRETERS = frozenset({"python", "python2", "python3", "bash", "sh", "zsh", "dash",
                           "node", "ruby", "perl", "uv", "uvx", "env"})
_PYTHON_MINOR = re.compile(r"^python3\.\d+$")

# Literal substrings, never regexes, and the difference is measured rather than stylistic:
# `grep -rl "council.engine"` selects this repository's `scripts/lib/checks.py`, because the
# dot matches the slash in the PATH `council/engine.py` that checks.py carries in a closure
# list while spending nothing — and checks.py is executed by `make verify`. `grep -rlF` on the
# same string does not. Across the tree the two literals select `scripts/eval_harness.py`,
# `scripts/eval_trigger.py`, `shared/lib/council/engine.py` and llm-council's `fanout.py`
# facade, plus council test files and plan documents, which are only opened when a recipe
# executes them.
_SPEND_SYMBOLS = ("run_council", "council.engine")

# A flag whose presence the invoker is declaring hermetic. This repository's `make verify`
# reaches `scripts/eval_harness.py --self-test`, which is the same file `make eval SKILL=…`
# reaches to spend ~24 provider calls; without this the steer §5.2 makes would refuse its own
# recommendation. The bound: a program taking one of these AND still calling out is misread.
_HERMETIC_FLAGS = frozenset({"--self-test", "--check", "--status", "--dry-run", "--seed-receipt",
                             "--help", "-h", "--version"})

# What the reader is willing to call a script the tree should hold. A path-shaped token with
# one of these suffixes that does not exist is `unresolved:` rather than ignored.
_SCRIPT_SUFFIXES = frozenset({"py", "sh", "bash", "mk", "js", "ts", "mjs", "rb", "pl", "bats"})

_TOKENS = re.compile(r"[A-Za-z0-9_./+-]+")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_VARREF = re.compile(r"\$[({]([A-Za-z_.][A-Za-z0-9_.]*)[)}]")
_UNEXPANDED = re.compile(r"\$[({][^)}]{0,60}")
# Every OTHER `$` make can still be holding. `$$` is folded out before this runs, so what is
# left is an automatic variable (`$<`, `$@`, `$^`, `$*`, `$?`, `$+`, `$|`, `$%`) or a
# one-letter name — none of which reach `_TOKENS`, so without this the token does not merely
# fail to resolve, it disappears, and `@python3 $<` reads as a recipe running nothing. The
# rule is "any `$` this reader did not resolve" rather than the list somebody remembered,
# because a list is how `$(UNDEFINED)` came to fail closed while `$<` failed open.
_UNEXPANDED_SHORT = re.compile(r"\$.?")
_CALL_MACRO = re.compile(r"\$[({]call\s+([A-Za-z_][A-Za-z0-9_]*)")
# A remedy in prose or in an error message: "run `make eval SKILL=<skill>`".
_MAKE_REMEDY = re.compile(r"\bmake\s+([A-Za-z][A-Za-z0-9_.-]*)")

# A recipe's own working directory, which §5.1 names as the shape real monorepos have
# (`cd frontend && npm ci`). `_CD_LEAD` consumes a leading one; `_CD_ANY` finds one at any
# later program position, where following it would need the shell this module refuses to run.
_CD_LEAD = re.compile(r"\s*cd\s+([^\s;&|()`]+)\s*(?:&&|;|\|\|)?\s*")
# `\s` alone missed a BARE `cd` — no argument, which moves to $HOME — because nothing
# follows it but a separator. Contrived inside a Makefile, and the one hole left in this
# family once the rest is closed, so it is matched rather than left to a later reader.
_CD_ANY = re.compile(r"(?:^|[;&|(`])\s*cd(?:\s|;|$|&|\|)")

_ASSIGN = re.compile(r"^([A-Za-z_.][A-Za-z0-9_.]*)\s*(::=|:=|\?=|\+=|=)\s*(.*)$")
_RULE = re.compile(r"^([^\t#][^:=]*?)\s*::?\s*(.*)$")
_MAKEFILE_NAMES = ("GNUmakefile", "makefile", "Makefile")
_DOLLAR = "\x00"                 # make's `$$` — a literal `$` for the shell, not a reference
_MAX_FILE_BYTES = 4 * 1024 * 1024
_MAX_DEPTH = 12


def _basename(token: str) -> str:
    return PurePosixPath(token).name


def _logical_lines(text: str):
    """(is_recipe, joined) per LOGICAL make line — backslash continuations folded.

    Folding matters in both directions here: this repository's `FORGE_TESTS :=` spans four
    physical lines, and its `bats-test` recipe is one continued line eighteen deep.
    """
    out, buf, recipe = [], None, False
    for raw in text.splitlines():
        if buf is None:
            recipe, buf = raw.startswith("\t"), raw
        else:
            buf += " " + raw.strip()
        if buf.endswith("\\"):
            buf = buf[:-1]
            continue
        out.append((recipe, buf))
        buf = None
    if buf is not None:
        out.append((recipe, buf))
    return out


class _Makefile:
    """The subset of GNU make this reader claims: simple variables, rules, recipes.

    `define` bodies are recorded by NAME and skipped: expanding one means running the shell
    grammar inside it, and a `$(call …)` left unexpanded is reported rather than guessed at.

    `cwd` is the repo-relative directory MAKE WAS RUNNING IN when this file was read, which is
    not the directory the file sits in: `-C sub` moves both, `-f sub/build.mk` moves only the
    latter. Every path in a recipe resolves against the former, so it travels with the parsed
    makefile rather than being re-derived by each caller — the re-derivation is what
    `_scan_remedies` got wrong, resolving a `-C sub` remedy chain against the repository root
    and reporting the clean `()` that a chain which spends must never produce.
    """

    def __init__(self, path: Path, cwd: str, text: str):
        self.path = path
        self.cwd = cwd
        self.vars: dict[str, str] = {}
        self.recipes: dict[str, list[str]] = {}
        self.prereqs: dict[str, list[str]] = {}
        self.defines: set[str] = set()
        self.includes = False
        self.default_goal = ""
        self.first_target = ""
        current: list[str] = []
        in_define = False
        for is_recipe, line in _logical_lines(text):
            if is_recipe:
                if not in_define:
                    for t in current:
                        self.recipes.setdefault(t, []).append(line.lstrip("\t"))
                continue
            stripped = line.strip()
            if in_define:
                in_define = not stripped.startswith("endef")
                continue
            if stripped.startswith("define "):
                in_define = True
                self.defines.add(stripped.split()[1])
                continue
            if not stripped or stripped.startswith("#"):
                continue
            stripped = stripped.split("#", 1)[0].strip()
            if not stripped:
                continue
            if stripped.startswith(("include ", "-include ", "sinclude ")):
                self.includes = True
                current = []
                continue
            m = _ASSIGN.match(stripped)
            if m:
                name, op, value = m.groups()
                if op == "+=" and name in self.vars:
                    self.vars[name] = f"{self.vars[name]} {value}"
                elif op != "?=" or name not in self.vars:
                    self.vars[name] = value
                if name == ".DEFAULT_GOAL":
                    self.default_goal = value.strip()
                current = []
                continue
            m = _RULE.match(stripped)
            if m:
                current = self.expand(m.group(1))[0].split()
                for t in current:
                    self.prereqs.setdefault(t, []).extend(self.expand(m.group(2))[0].split())
                    if not self.first_target and not t.startswith("."):
                        self.first_target = t
                continue
            current = []

    def expand(self, text: str) -> tuple[str, str]:
        """(expanded, unresolved-reference) — the second is "" when everything resolved.

        `$$` is folded out first because it is make's escape for a literal `$`: without that,
        a recipe's own `$$(bash …)` reads as an unexpanded make reference and every shell
        command substitution in the tree reports as unresolved.
        """
        text = text.replace("$$", _DOLLAR)
        for _ in range(8):
            new = _VARREF.sub(lambda m: self.vars.get(m.group(1), m.group(0)), text)
            if new == text:
                break
            text = new
        macro = _CALL_MACRO.search(text)
        if macro:
            left = (f"the make macro `{macro.group(1)}`" if macro.group(1) in self.defines
                    else f"`$(call {macro.group(1)} …)`")
        elif (m := _UNEXPANDED.search(text)):
            left = f"`{m.group(0)}…`"           # cut at the closing paren this did not reach
        elif (m := _UNEXPANDED_SHORT.search(text)):
            left = f"`{m.group(0)}`"
        else:
            left = ""
        return text.replace(_DOLLAR, "$"), left

    @property
    def goal(self) -> str:
        return self.default_goal or self.first_target


class _Resolver:
    """One static walk of what a verify command would run. Holds no state between calls."""

    def __init__(self, root: Path, *, follow_remedies=True):
        self.root = root
        self.follow_remedies = follow_remedies
        self.findings: list[str] = []
        self.makefiles: dict[str, _Makefile | None] = {}
        self.seen_targets: set[tuple[str, str]] = set()
        # rel -> the text read from it, so a file reached twice is opened and reported once
        # while a SECOND reach that arrived through a makefile can still be asked the question
        # the first one had no makefile to answer — see `_scan_file`.
        self.seen_files: dict[str, str] = {}
        self.seen_remedies: set[tuple[str, str, str]] = set()

    # -- reporting ----------------------------------------------------------------------
    def _add(self, kind: str, trail, message: str) -> None:
        where = " -> ".join(trail)
        self.findings.append(f"{kind} {where}: {message}")

    def _of(self, findings, kind: str) -> list:
        return [f for f in findings if f.startswith(kind)]

    # -- paths --------------------------------------------------------------------------
    def _contained(self, rel: str) -> str | None:
        """The repo-relative name, or None when the token leaves the tree.

        `bundle._assert_contained` is IMPORTED rather than restated, for the reason
        `preflight` gives for importing it: this package already spells the lexical rule in
        four places and a fifth spelling is the drift the reuse exists to avoid.
        """
        rel = rel.strip().strip("'\"")
        if not rel or rel in (".", "./"):
            return ""
        try:
            bundle._assert_contained(rel, "verify command")
        except bundle.BundleError:
            return None
        return rel.rstrip("/")

    def _join(self, base: str, rel: str) -> str | None:
        joined = f"{base}/{rel}" if base and not rel.startswith("/") else rel
        return self._contained(joined)

    def _repo_file(self, cwd: str, token: str) -> str | None:
        rel = self._join(cwd, token)
        if rel is None or not rel:
            return None
        p = self.root / rel
        return rel if p.is_file() else None

    def _exists(self, cwd: str, name: str) -> bool:
        rel = self._join(cwd, name)
        return rel is not None and bool(rel) and (self.root / rel).exists()

    def _repo_dir(self, cwd: str, token: str) -> str | None:
        rel = self._join(cwd, token)
        if rel is None or not rel:
            return None
        return rel if (self.root / rel).is_dir() else None

    # -- entry --------------------------------------------------------------------------
    def scan_step(self, index: int, step) -> None:
        argv = tuple(getattr(step, "argv", ()) or ())
        trail = [f"verify step {index + 1} `{' '.join(argv)}`"]
        if not argv:
            return
        cwd = self._contained(getattr(step, "cwd", "") or "")
        if cwd is None:
            self._add(UNRESOLVED, trail, "its cwd leaves the repository, so nothing under "
                                         "it was read")
            return
        prog = _basename(argv[0])
        if prog in _PROVIDER_CLIS:
            self._add(SPENDS, trail, f"the gate's own program is the provider CLI {prog!r}")
            return
        if prog in _MAKE:
            self._scan_make(trail, cwd, list(argv[1:]))
            return
        self._scan_tokens(trail, cwd, list(argv), None)

    # -- make ---------------------------------------------------------------------------
    def _makefile(self, trail, cwd: str, named: str | None) -> _Makefile | None:
        key = f"{cwd}\0{named or ''}"
        if key in self.makefiles:
            return self.makefiles[key]
        names = (named,) if named else _MAKEFILE_NAMES
        mk = None
        for name in names:
            rel = self._join(cwd, name)
            if rel is None:
                continue
            p = self.root / rel
            if p.is_file():
                try:
                    mk = _Makefile(p, cwd, p.read_text(encoding="utf-8", errors="replace"))
                except OSError as e:
                    self._add(UNRESOLVED, trail, f"{rel} could not be read: {e}")
                break
        if mk is None:
            self._add(UNRESOLVED, trail,
                      f"no makefile at {cwd or '.'}/{named or 'Makefile'} — what `make` would "
                      "run here could not be read")
        elif mk.includes:
            self._add(UNRESOLVED, trail, f"{mk.path.name} uses `include`, and this reader "
                                         "does not follow it")
        self.makefiles[key] = mk
        return mk

    def _scan_make(self, trail, cwd: str, args: list) -> None:
        named, targets, i = None, [], 0
        while i < len(args):
            a = args[i]
            if a in ("-C", "--directory") and i + 1 < len(args):
                i += 1
                moved = self._join(cwd, args[i])
                if moved is None:
                    self._add(UNRESOLVED, trail, f"`-C {args[i]}` leaves the repository")
                    return
                cwd = moved
            elif a in ("-f", "--file", "--makefile") and i + 1 < len(args):
                i += 1
                named = args[i]
            elif a.startswith("-") or "=" in a:
                pass
            else:
                targets.append(a)
            i += 1
        mk = self._makefile(trail, cwd, named)
        if mk is None:
            return
        for target in targets or [mk.goal]:
            self._scan_target(trail, cwd, mk, target)

    def _scan_target(self, trail, cwd: str, mk: _Makefile, target: str) -> None:
        if not target:
            self._add(UNRESOLVED, trail, "`make` was given no target and the makefile "
                                         "declares no default goal")
            return
        key = (cwd, target)
        if key in self.seen_targets or len(trail) > _MAX_DEPTH:
            return
        self.seen_targets.add(key)
        trail = [*trail, f"target `{target}`"]
        if target not in mk.recipes and target not in mk.prereqs:
            if not self._exists(cwd, target):
                self._add(UNRESOLVED, trail, f"{mk.path.name} defines no such target and the "
                                             "tree holds no such file")
            return
        for prereq in mk.prereqs.get(target, []):
            if prereq in mk.recipes or prereq in mk.prereqs:
                self._scan_target(trail, cwd, mk, prereq)
            elif not self._exists(cwd, prereq):
                self._add(UNRESOLVED, trail, f"prerequisite `{prereq}` is neither a target "
                                             "nor a file in this tree")
        for line in mk.recipes.get(target, []):
            self._scan_recipe(trail, cwd, mk, line)

    def _scan_recipe(self, trail, cwd: str, mk: _Makefile, line: str) -> None:
        # `@`, `-` and `+` at the head of a recipe are make's own modifiers, not part of the
        # program. Left on, `@python3` is a program name nothing recognises and every silenced
        # recipe in the tree reads as running something unknown.
        expanded, left = mk.expand(line.lstrip("\t @-+"))
        if left:
            self._add(UNRESOLVED, trail, f"a recipe uses {left}, which this reader does not "
                                         "expand, so what it runs is unknown")
        # The whole line, before any `cd` is consumed: `cd tools && claude -p …` still runs a
        # provider, and `_PROVIDER_CMD` reads the `&&` as the program position it is.
        found = _PROVIDER_CMD.search(expanded)
        if found:
            self._add(SPENDS, trail,
                      f"a recipe runs the provider CLI {found.group(1)!r}")
        moved = self._chdir(trail, cwd, expanded)
        if moved is None:
            return
        cwd, body = moved
        self._scan_tokens(trail, cwd, _TOKENS.findall(body), mk, raw=body)

    def _chdir(self, trail, cwd: str, expanded: str):
        """`(cwd, body)` after following the recipe's leading `cd`, or None when it cannot be.

        Returning the truncated body rather than refusing the whole line keeps what came
        BEFORE a mid-line `cd` resolved — the part that really does run in `cwd`.
        """
        for _ in range(_MAX_DEPTH):
            m = _CD_LEAD.match(expanded)
            if not m:
                break
            moved = self._repo_dir(cwd, m.group(1))
            if moved is None:
                self._add(UNRESOLVED, trail, f"a recipe runs `cd {m.group(1)}`, which is not a "
                                             "directory in this tree, so what it runs there "
                                             "could not be read")
                return None
            cwd, expanded = moved, expanded[m.end():]
        later = _CD_ANY.search(expanded)
        if later:
            self._add(UNRESOLVED, trail, "a recipe changes directory part-way through a shell "
                                         "line; this reader follows only a leading `cd`, so "
                                         "everything after it went unread")
            expanded = expanded[:later.start()]
        return cwd, expanded

    # -- commands -----------------------------------------------------------------------
    def _operand(self, cwd: str, rest: list) -> tuple[str | None, list]:
        """The repo file an interpreter would actually run, and the arguments after it.

        Stops at the first non-flag operand rather than searching on: `uvx --with pytest pytest
        -q tests/a.py` runs `pytest`, and continuing past it would report the test files it was
        handed as data as though the recipe executed them.
        """
        for i, token in enumerate(rest):
            if token.startswith("-") or "=" in token.split("/")[0]:
                continue
            return self._repo_file(cwd, token), list(rest[i + 1:])
        return None, []

    def _inline_modules(self, cwd: str, tokens: list) -> list:
        """Repo modules a `python -c` one-liner imports off a path it inserts itself.

        The shape is this repository's own gate: `sys.path.insert(0,'scripts/lib'); import
        checks`. Nothing shorter reaches `scripts/lib/checks.py`, which is where §5.2's whole
        chain on this repository starts, and nothing here parses Python — a directory token and
        an identifier token from the SAME command, joined and tested for existence.
        """
        dirs = [t for t in tokens[:80]
                if (self._join(cwd, t) or None) and (self.root / self._join(cwd, t)).is_dir()]
        idents = [t for t in tokens[:80] if _IDENT.match(t)]
        out = []
        for d in dirs[:8]:
            for name in idents[:40]:
                rel = self._repo_file(cwd, f"{d}/{name}.py")
                if rel:
                    out.append(rel)
        return out

    def _scan_tokens(self, trail, cwd: str, tokens: list, mk, raw: str = "") -> None:
        if not tokens:
            return
        direct = self._repo_file(cwd, tokens[0])
        if direct:
            self._scan_file(trail, direct, tokens[1:], mk)
        for i, token in enumerate(tokens):
            base = _basename(token)
            rest = list(tokens[i + 1:])
            if base in _MAKE:
                self._scan_make(trail, cwd, rest)
                continue
            if base in _PROVIDER_CLIS and not raw:
                self._add(SPENDS, trail, f"the argv names the provider CLI {base!r}")
                continue
            if base not in _INTERPRETERS and not _PYTHON_MINOR.match(base):
                continue
            if "-c" in rest[:3]:
                for rel in self._inline_modules(cwd, tokens):
                    self._scan_file([*trail, f"`-c` imports {rel}"], rel, rest, mk)
                continue
            script, args = self._operand(cwd, rest)
            if script:
                self._scan_file(trail, script, args, mk)
        for token in tokens:
            self._unresolved_script(trail, cwd, token)

    def _unresolved_script(self, trail, cwd: str, token: str) -> None:
        if "/" not in token or token.rsplit(".", 1)[-1] not in _SCRIPT_SUFFIXES:
            return
        rel = self._join(cwd, token)
        if rel is None or (self.root / rel).exists():
            return
        self._add(UNRESOLVED, trail, f"a recipe names `{token}`, which this tree does not "
                                     "hold, so what it does could not be read")

    # -- files --------------------------------------------------------------------------
    def _scan_file(self, trail, rel: str, args: list, mk) -> None:
        trail = [*trail, rel]
        # A hermetic reach is NOT remembered: `make verify` reaches `scripts/eval_harness.py
        # --self-test` before anything else does, and remembering it there would mask the
        # spending reach of the same file from a later target in the same walk.
        if set(args) & _HERMETIC_FLAGS:
            return
        if rel in self.seen_files:
            # Read once, but asked the remedy question again, because "have I opened this" and
            # "have I asked which makefile's targets it documents" are different questions and
            # only the first is a property of the file. A step running the file DIRECTLY
            # arrives with `mk is None` and can answer neither; memoizing on the first question
            # alone made `[[python3, gate.py], [make, verify]]` come back clean on a tree where
            # `[[make, verify]]` alone reports the remedy.
            text = self.seen_files[rel]
        else:
            p = self.root / rel
            try:
                if p.stat().st_size > _MAX_FILE_BYTES:
                    self.seen_files[rel] = ""
                    self._add(UNRESOLVED, trail, "is larger than this reader will open")
                    return
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                self.seen_files[rel] = ""
                self._add(UNRESOLVED, trail, f"could not be read: {e}")
                return
            self.seen_files[rel] = text
            for symbol in _SPEND_SYMBOLS:
                if symbol in text:
                    self._add(SPENDS, trail, f"the gate executes it and it names `{symbol}` — a "
                                             "council entry point that launches real providers")
                    break
        if text and self.follow_remedies and mk is not None:
            self._scan_remedies(trail, rel, text, mk)

    def _scan_remedies(self, trail, rel: str, text: str, mk: _Makefile) -> None:
        """`make <target>` NAMED IN a file the gate executes — §5.2's own chain on this repo.

        `make` does not follow a sentence in an error message; an operator does, and only when
        the gate fails. So this is priced rather than refused, and the finding says which.

        The sub-resolver walks the remedy from `mk.cwd`, which is where an operator typing
        `make <target>` after reading that sentence would be — not the repository root. And its
        `unresolved:` findings are carried out rather than filtered away with everything that
        is not a spend: a remedy whose own closure could not be read is the case where BOTH
        backstops would otherwise be silent, and silence here reads as a chain that is free.
        """
        key = (rel, str(mk.path), mk.cwd)
        if key in self.seen_remedies:
            return
        self.seen_remedies.add(key)
        steer = ("`make verify` (receipts advisory)" if "verify" in mk.recipes
                 else "the repository's advisory gate, which this makefile does not declare")
        for target in dict.fromkeys(_MAKE_REMEDY.findall(text)):
            # A target this walk already resolved directly needs no second, weaker account of
            # itself: `scripts/lib/checks.py` names `make verify` while being reached BY `make
            # verify`, and reporting that reads as a chain nobody followed when it is the one
            # already reported above.
            # ORDER-DEPENDENT, and declared rather than left to be rediscovered: whether a
            # target was already walked depends on prerequisite order, so `verify: a b` and
            # `verify: b a` can differ in whether this remedy is reported. Nothing is LOST —
            # the stronger `spends:` finding is present either way, which is why this is
            # noise rather than the refusal-deciding order dependence `_scan_file` guards
            # against — but a caller diffing two findings tuples should know.
            if target not in mk.recipes or (mk.cwd, target) in self.seen_targets:
                continue
            sub = _Resolver(self.root, follow_remedies=False)
            sub._scan_target([f"`make {target}`"], mk.cwd, mk, target)
            spent = self._of(sub.findings, SPENDS)
            if spent:
                self._add(REMEDY, trail,
                          f"it documents `make {target}` as the remedy, and that target does "
                          f"spend: {spent[0]}. `make` does not follow it — an operator does, "
                          f"once per failing candidate. §5.2 steers that operator to {steer} "
                          "rather than to this gate")
                continue
            unread = self._of(sub.findings, UNRESOLVED)
            if unread:
                self._add(UNRESOLVED, trail,
                          f"it documents `make {target}` as the remedy, and whether that target "
                          f"spends could not be read: {unread[0]}")


def _worktree_root(repo) -> Path:
    try:
        out = gitcmd.git(repo, *gitcmd.NO_DAEMON_CACHE, "rev-parse", "--show-toplevel",
                         env_extra=gitcmd.READONLY).stdout.strip()
    except (gitcmd.GitError, OSError, ValueError) as e:
        raise GateError(f"{repo}: not a git worktree the detector can resolve against: {e}") from e
    return Path(out)


def provider_invoking_verify(repo, command) -> tuple[str, ...]:
    """Everything about `command` that would spend provider calls, or that could not be read.

    `()` means this reader followed the command to the end and found no provider spend — never
    that the command is cheap, and the module docstring names what it does not follow.
    """
    steps = getattr(command, "steps", None)
    if steps is None:
        raise GateError(f"a verify Command is required, not {type(command).__name__}; "
                        "parse the spec with verify.Command.parse first")
    resolver = _Resolver(_worktree_root(repo))
    for i, step in enumerate(steps):
        resolver.scan_step(i, step)
    return tuple(dict.fromkeys(resolver.findings))


# ---------------------------------------------------------------------------------------
# §5 step 2: the one question, and the record its answer writes.

# The gaps a run may be asked to accept, as IDS rather than as sentences. `Confirmation`
# records the id, so a handover cites a name that resolves to a line `must_show` emitted
# rather than a paraphrase of one, and a caller can tell the empty-surface condition from the
# other three without matching prose that is free to be reworded.
GATE_SURFACE_EMPTY = "gate-surface-empty"
REMOTES_AND_CONFIGURATION = "remotes-and-configuration-unrecorded"
GC_UNBUILT = "gc-unbuilt"
GENERATOR_CONTRACT_EMPTY = "generator-contract-empty"
ACCEPTABLE_GAPS = (GATE_SURFACE_EMPTY, REMOTES_AND_CONFIGURATION, GC_UNBUILT,
                   GENERATOR_CONTRACT_EMPTY)

# §5 step 2's first policy, spelled as the two branches a later phase can act on. §5 writes
# the second as "continue as degraded"; the value is the branch name, and `degraded` is also
# §14's terminal phase for a run that took it.
CALIBRATION_POLICIES = ("abort", "degraded")

# §12's strategy rule, and the reason `partition` is NOT one of these. §12.1 makes the size
# gate "trigger analysis, it does not force partitioning", and admits a partition only "where
# stable seams exist" — a natural-language criterion §10.1 forbids presenting as a checked
# predicate, so it cannot be pre-committed to at a gate that has seen no artifact. What CAN
# be confirmed in advance is which of §12.1's three rules the run applies to a measured size:
#
#   * `size-gated`     §12.1 itself — fusion below ~400 changed lines / ~15 files, and above
#                      it, partition only where stable seams exist, else base-and-port.
#   * `fusion`         from-scratch fusion whatever the size measures.
#   * `base-and-port`  §12.1's "correct primary strategy" where no stable seams exist,
#                      chosen up front rather than after the analysis.
STRATEGY_RULES = ("size-gated", "fusion", "base-and-port")

# What §5 step 2 asks. `accepted_gaps` is the one that may be left out, and the asymmetry is
# the point: either calibration policy is an ACTION a later phase takes, so silence there has
# no safe reading, while silence about a gap means the operator accepted none — which is the
# reading that cannot later be cited as agreement.
_ANSWERS = ("setup", "verify", "on_calibration_failure", "strategy", "accepted_gaps")
_REQUIRED = ("setup", "verify", "on_calibration_failure", "strategy")


@dataclass(frozen=True)
class Confirmation:
    """The answer to §5 step 2, in the shape the manifest records it.

    `setup` and `verify` are `verify.Step` tuples — §5.1's record whole, all four fields —
    and they are the manifest's own type so `open_run` performs no conversion. A conversion
    here is where a `cwd` or a `timeout` would go missing between what a human agreed to and
    what a resume runs.

    NO DEFAULTS, including on `accepted_gaps`, on `runstate.State`'s rule: a field the
    constructor supplies is a fact nobody answered for. `confirm` is where an omitted
    `accepted_gaps` becomes the empty tuple, because that is a normalization of an answer
    sheet and this is the record.
    """
    setup: tuple
    verify: tuple
    on_calibration_failure: str
    strategy: str
    accepted_gaps: tuple[str, ...]


def must_show(report, quote_, command) -> tuple[str, ...]:
    """Everything a human must see before answering §5 step 2, in the order it matters.

    THE COMMAND IS AN ARGUMENT BECAUSE THE FIRST LINE CANNOT BE READ OFF A REPORT.
    `preflight.Report.gate_surface` is None and says why: §6.1's surface belongs to the
    RESOLVED gate, and preflight has no command to resolve. Here the command is in hand — it
    is what the human is being asked to confirm — so the surface is computable, and the
    difference between "measured, found nothing" and "nobody looked" is exactly the condition
    the qualified-PASS ruling rests on.

    Measured on a repository holding `check.sh` and nothing else a role rule names:
    `gate_surface` with no command returns `()`, and with `[["./check.sh"]]` returns
    `('check.sh',)`. So the surface resolved WITHOUT the command reports the empty condition
    for an ordinary repository whose gate is a named script — a false alarm on the one line
    whose whole value is that it is rare. The CONFIRMATION is deliberately not what is taken
    instead: it is the answer, and this is the question.

    WHY THE USER'S OWN WORKTREE MAY BE RESOLVED AGAINST, when `gate_surface` requires a tree
    the engine built. That requirement is about a tree the party under suspicion could have
    written — a seat's index, where `git rm --cached Makefile` would delete a gate file from
    its own surface. At §5 step 2 no seat exists and no provider has run, so the worktree is
    the user's own. The other half of "engine-built" is that reading it must run nothing the
    repository supplies; `verify._enumerate` carries `NO_DAEMON_CACHE` so that holds here.

    WHAT IS NOT SCREENED, stated because the detector's findings below read as though the
    whole confirmation had been read: `provider_invoking_verify` is run on the VERIFY command
    alone, which is §5.2's own scope and the vocabulary its findings are written in. A SETUP
    command that reaches a provider CLI is not detected, and it is the more expensive miss —
    setup runs once per builder clone and once per verifier clone.
    """
    if not isinstance(report, preflight.Report):
        raise GateError(f"a preflight.Report is required, not {type(report).__name__}; "
                        "§5 step 2 is what the static look at step 1 is shown for")
    if not isinstance(quote_, Quote):
        raise GateError(f"a Quote is required, not {type(quote_).__name__}; §5.2 prices the "
                        "run at this gate and there is no later one to correct it")
    if getattr(command, "steps", None) is None:
        raise GateError(f"a verify Command is required, not {type(command).__name__}; the "
                        "surface below is §6.1's answer for the RESOLVED gate, and an "
                        "unresolved one reports the empty condition for any repository")

    lines = list(preflight.refusals(report))
    if lines:
        # First, and in `quote`'s own order: nothing below is reachable while one of these
        # stands, and §2.3 does not let the operator answer past them.
        lines.append("the lines above are §2.3 refusals — they fail closed, this gate cannot "
                     "be answered past them, and `open_run` will not open a run over one")

    surface = verify.gate_surface(report.facts.root, report.contract, command=command)
    if surface:
        lines.append(f"gate surface: §6.1's rules and this command resolve to {len(surface)} "
                     f"file(s) — {', '.join(surface[:6])}"
                     f"{' …' if len(surface) > 6 else ''}. A candidate that rewrites one of "
                     "them is marked `gate_changed` and never silently keeps independent-gate "
                     "status")
    else:
        lines.append(
            f"gap {GATE_SURFACE_EMPTY}: §6.1's rules and this command resolve to NO file in "
            "this repository, so the engine cannot see what defines your gate. A candidate "
            "can gut the gate and still earn a PASS whose reason says the surface was "
            "measured and unchanged. That verdict qualifies itself rather than being "
            "displaced — displacing it would attribute to the candidate a fact about the "
            "engine — on the condition that you are told once, before a token is spent. "
            "This line is that condition.")

    lines += [
        f"gap {REMOTES_AND_CONFIGURATION}: §9 protects seven things and the t0 snapshot "
        "records five. A remote added or a repository-local config key set while the run is "
        "out leaves no t0 fact behind it, so `drift` cannot speak for either and a handover "
        "will not stop for one",
        f"gap {GC_UNBUILT}: every interrupted write leaves a staging file nothing collects, "
        "so `--gc` is mandatory rather than tidy — and it is not built. The peak disk below "
        "is what accumulates until it is",
        f"gap {GENERATOR_CONTRACT_EMPTY}: the generator contract is empty for every "
        "repository the detector can read, this one included, so no relation admits a "
        "verify-origin rewrite of a TRACKED file. A PASS therefore requires a gate that "
        "rewrites no tracked file, whatever this repository's own generators do",
    ]
    lines += list(quote_.lines)
    lines += list(provider_invoking_verify(report.repo, command))
    return tuple(lines)


def _confirmed_steps(name: str, value) -> tuple:
    """§5.1's steps as the manifest's own type, from either shape a caller can hold.

    ALREADY-BUILT `verify.Step`s are accepted whole, and that route is the only one that can
    express a per-step `cwd`: `verify.Command.parse` takes a list of ARGV LISTS and has no
    place to put one, so §5.1's own motivating example — `cd frontend && npm ci` as two steps
    in two directories — is unconfirmable through it. The argv route is still the ordinary
    one, and it reaches `parse` whole rather than step by step so its refusals keep naming
    the index of the step at fault.

    A MIXED sequence is refused rather than handled. The two routes differ in what they
    settle: a `Step` carries a cwd, an env and a timeout the caller chose, while an argv list
    takes `Step`'s defaults. Reading half a command each way makes which of those a given
    step got depend on how it happened to be written.
    """
    if isinstance(value, (str, bytes)):
        raise GateError(
            f"{name} is {value!r}, a single string. A command is a LIST of steps and a step "
            'is a list of words — write [["make", "verify"]]. Nothing here runs a shell.')
    try:
        rows = list(value)
    except TypeError as e:
        raise GateError(f"{name} is not a sequence of steps: {value!r}") from e
    built = [isinstance(r, verify.Step) for r in rows]
    if any(built) and not all(built):
        raise GateError(
            f"{name} mixes verify.Step records with bare argv lists. A Step carries the cwd, "
            "env and timeout the caller chose and an argv list takes the defaults, so a mixed "
            "command hides which of the two each step was agreed under.")
    if all(built) and rows:
        return tuple(rows)
    try:
        return verify.Command.parse(rows).steps
    except verify.VerifyError as e:
        # Re-raised in this module's vocabulary, on `runstate._steps`' precedent: `confirm`'s
        # documented contract is that an answer it will not stand behind arrives as a
        # GateError, and the caller catching it has no other name for this.
        raise GateError(f"{name}: {e}") from e


def confirm(report, quote_, answers) -> Confirmation:
    """§5 step 2's answer, validated into the record §14.2 writes once.

    `report` and `quote_` are required and type-checked though neither is stored, for
    `quote`'s own reason one step on: a caller holding neither has not reached this gate, and
    an answer collected before the static look and the price is an answer to a different
    question.

    NO POLICY IS DEFAULTED. §5 asks for both once and §5 step 5 says the decision is not
    re-asked, so a missing one cannot be filled in here — the value this function would
    choose IS the decision, taken by the reader, at the one gate whose whole purpose is that
    the operator takes it. `accepted_gaps` is the single omissible answer and its silence
    reads as "accepted none", which is the reading that cannot later be cited as agreement.

    An UNKNOWN answer key is refused rather than ignored: a caller that spells a policy
    slightly wrong would otherwise have it silently defaulted by the missing-key check it
    just passed.

    WHAT `accepted_gaps` IS CHECKED FOR is that each id is one this engine can raise —
    `ACCEPTABLE_GAPS` — and not that `must_show` raised it for THIS repository. The narrower
    check needs the command the surface was measured with, and a `Confirmation` is the answer
    to a question its caller rendered. So accepting a gap nobody raised is recorded rather
    than refused; it is noise in a handover, never a licence.
    """
    if not isinstance(report, preflight.Report):
        raise GateError(f"a preflight.Report is required, not {type(report).__name__}")
    if not isinstance(quote_, Quote):
        raise GateError(f"a Quote is required, not {type(quote_).__name__}")
    if not isinstance(answers, dict):
        raise GateError(f"the answers to §5 step 2 are a mapping, not {type(answers).__name__}")
    missing = [k for k in _REQUIRED if k not in answers]
    if missing:
        raise GateError(
            f"§5 step 2 is asked once and {missing} went unanswered. Neither policy has a "
            f"safe default — calibration failure is one of {list(CALIBRATION_POLICIES)} and "
            f"the strategy rule is one of {list(STRATEGY_RULES)} — and a value chosen here "
            "would be the reader's decision recorded as the operator's.")
    unknown = sorted(set(answers) - set(_ANSWERS))
    if unknown:
        raise GateError(f"§5 step 2 does not ask {unknown}; it asks {list(_ANSWERS)}")

    verify_steps = _confirmed_steps("verify", answers["verify"])
    if not verify_steps:
        # An empty SETUP is an ordinary repository that needs none. An empty VERIFY is a gate
        # that decides nothing, and §6.2's outcomes are all read off its exit code — so every
        # candidate would pass, including one that deleted the tests.
        raise GateError(
            "the verify command names no step, so the gate would decide nothing and every "
            "candidate would pass it. §6.2 reads every outcome off this command.")

    policy = answers["on_calibration_failure"]
    if policy not in CALIBRATION_POLICIES:
        raise GateError(
            f"on_calibration_failure is {policy!r}; §5 step 2 offers {list(CALIBRATION_POLICIES)} "
            "— abort the run when the untouched baseline fails its own gate, or continue and "
            "carry the run to §14's `degraded` ending")
    rule = answers["strategy"]
    if rule not in STRATEGY_RULES:
        raise GateError(
            f"strategy is {rule!r}; §12's rule is confirmed here and applied to measured "
            f"artifact size later, so it is one of {list(STRATEGY_RULES)}. `partition` is "
            "deliberately absent: §12.1 admits one only where stable seams exist, which is "
            "§10.1's non-mechanical criterion and cannot be pre-committed to.")

    gaps = answers.get("accepted_gaps", ())
    if isinstance(gaps, (str, bytes)):
        raise GateError(
            f"accepted_gaps is {gaps!r}, a single string; iterating one yields its characters, "
            "so a run would record acceptance of a list of letters")
    gaps = tuple(gaps)
    stray = sorted(set(gaps) - set(ACCEPTABLE_GAPS))
    if stray:
        raise GateError(
            f"accepted_gaps names {stray}, which `must_show` cannot raise. An accepted gap has "
            f"to resolve to a line the operator was shown; the ids are {list(ACCEPTABLE_GAPS)}.")
    return Confirmation(setup=_confirmed_steps("setup", answers["setup"]), verify=verify_steps,
                        on_calibration_failure=policy, strategy=rule, accepted_gaps=gaps)


def open_run(report, confirmation: Confirmation, run_id: str) -> Path:
    """Open the run this confirmation agreed to, and record it where a resume will read it.

    THE REPOSITORY IS THE REPORT'S, not a second argument. `report.facts.root` is git's own
    answer for the repository preflight looked at, and it is what `baseline.materialize`
    builds from and what `drift` resolves the manifest's `repo_path` against. A `repo`
    argument beside it would be a second answer to a settled question, and the wrong one
    answers cleanly — the failure `runstate._refuse_a_repository_the_manifest_did_not_record`
    exists for. It also decides `repo_path`: a caller who ran preflight on a SUBDIRECTORY has
    a `report.repo` that is not the toplevel, and a manifest recording it would be refused by
    every later `drift` as a repository this run was not recorded against.

    ORDER, AND WHAT EACH STEP MAKES TRUE. The two refusals come first, so a run that is not
    going to happen leaves nothing at all on disk — not a run directory, not an object and
    not a ref. Then the run root, which fails on a run id already taken rather than sharing a
    directory with the run that owns it. Then B, whose ref and OID cannot be recorded any
    earlier because §9's whitelist is "the exact OID recorded AT CREATION" and only
    `materialize`'s return value knows it. Then t0 — §14.2 puts it at this gate, which is why
    `preflight` deliberately takes no snapshot — with B's ref declared, so forge's own
    baseline is not reported as the user's ref appearing. Then the manifest, once. Then the
    receipt.

    WRITE-ONCE IS TWO KERNEL REFUSALS, neither of which is a check this function performs: a
    second `open_run` for the same run id meets `mkdir`'s, and a second manifest inside one
    run meets `os.link`'s. The first is what a repeated call actually hits, because B's ref
    is created with a compare-and-swap against "must not already exist" and would refuse
    before the manifest was reached at all.

    THE POLICIES ARE JOURNALED, not in the manifest — §14.2's list of what the manifest holds
    is the repository, `base_commit`, B's identity, the selected paths and the confirmed
    commands, and `events.jsonl` is one of the six sources it names for a resume. What that
    costs is that no decoder type-checks them on the way back in, the way `read_manifest`
    does for every field it carries; `confirm` is the only thing that ever validated them.

    The record is WRITE-AHEAD around everything that touches the user's repository, so a
    crash between the two halves leaves the orphan §14.1 requires rather than a run directory
    that cannot say whether a ref was made in the user's repository on its behalf.
    """
    if not isinstance(report, preflight.Report):
        raise GateError(f"a preflight.Report is required, not {type(report).__name__}")
    if not isinstance(confirmation, Confirmation):
        raise GateError(
            f"a Confirmation is required, not {type(confirmation).__name__}; the manifest is "
            "written once and never rewritten, so what it records has to be what a human "
            "answered rather than what a caller assembled")
    if not isinstance(run_id, str) or not run_id:
        raise GateError(f"a run needs an id: {run_id!r}")

    blocked = preflight.refusals(report)
    if blocked:
        raise GateError(
            f"preflight refuses this repository, so there is nothing to agree to: {list(blocked)}. "
            "§2.3 fails closed before a run starts, and a manifest opened over one of these "
            "would record an agreement about a repository the engine said it could not handle.")
    spends = [f for f in provider_invoking_verify(report.repo, verify.Command(confirmation.verify))
              if f.startswith(SPENDS)]
    if spends:
        # §5.2's own disposition of the three classes: a verify command that reaches a
        # provider CLI is "detected and refused", while a documented remedy is what the same
        # sentence prices "as its own explicit line" — which is `must_show`'s job and not
        # this one's. Re-detected here rather than trusted from `must_show`, because the
        # command a human confirmed is allowed to differ from the one they were shown.
        raise GateError(
            f"the confirmed verify command reaches a provider CLI: {spends}. §5.2 refuses one "
            "— it would be re-run fresh per candidate, two orders of magnitude above the "
            "quote. Point the gate at the advisory target instead.")

    try:
        run_dir = storage.run_root(report.facts.root, run_id, must_be_new=True)
    except FileExistsError as e:
        raise GateError(
            f"run {run_id!r} already has a directory for this repository, and a second run "
            "sharing it would write over the first run's record of what it agreed to. Draw "
            "another id with `storage.new_run_id()`.") from e

    log = journal.Journal(storage.journal_path(run_dir))
    log.record(journal.intent("confirm"), operation_id=run_id)
    b = baseline.materialize(report.facts.root, run_dir, report.facts,
                             list(report.selected), run_id)
    protected, status_digest = runstate.snapshot_refs(report.facts.root, report.selected,
                                                      forge_refs=(b.ref,))
    runstate.write_manifest(run_dir, runstate.Manifest(
        run_id=run_id,
        repo_path=str(report.facts.root),
        base_commit=b.base_commit,
        baseline_ref=b.ref,
        baseline_commit=b.commit,
        tracked_tree_oid=b.tracked_tree_oid,
        # `report.selected` rather than a contained subset: an escaping entry is one of the
        # refusals above, so by here the two are the same tuple and narrowing it would only
        # hide a disagreement between what was refused and what is recorded.
        selected_paths=tuple(report.selected),
        generator_contract=report.contract,
        setup=confirmation.setup,
        verify=confirmation.verify,
        protected_refs=protected,
        forge_refs={b.ref: b.commit},
        status_digest=status_digest,
        index_digest=runstate.snapshot_index(report.facts.root),
        created_at=datetime.now(timezone.utc).isoformat()))
    log.record(journal.done("confirm"), operation_id=run_id,
               on_calibration_failure=confirmation.on_calibration_failure,
               strategy=confirmation.strategy,
               accepted_gaps=list(confirmation.accepted_gaps))
    return run_dir
