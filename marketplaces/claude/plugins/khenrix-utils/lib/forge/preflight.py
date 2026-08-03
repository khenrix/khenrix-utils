"""§5 step 1: everything a STATIC, READ-ONLY look at the repository can say, and the one
place a refusal can stop a run before anybody is asked to authorize one.

This is the first consumer `inspect.rejections` and `screen.screen_tree` have ever had. Both
computed §2.3 and §3 in full and returned into a void, and
`test_forge_seams.py::test_preflight_consults_both_refusals_and_what_they_name_it_refuses` is
where that stopped being true. What it costs to leave them unread is the sharpest argument
for this file, and it is the BIT rather than any one git command. `--skip-worktree` is one of
§2.3's conditions, `rejections` names it, and its whole effect is to hide an edit from the
porcelain — which is the input `baseline.materialize` decides everything from. Measured on
git 2.53.0, bit set on an edited `seed.txt`: the porcelain is empty, so `dirty` is False, the
clean early-return fires and B IS HEAD. B lacks the edit because of that, not because
anything skipped it — `add` is never invoked on that repository at all
(`rev-parse, rev-parse, ls-files, rev-parse, update-ref, rev-parse`). A selection that does
make the run dirty reaches `git add -u -- :/`, and that command exits ONE on such a path —
"paths ... exist outside of your sparse-checkout definition" — so `materialize` raises
`GitError`. Neither branch is a silent skip.

Both failures are LOUD and LATE rather than silent, which is the §4 shape a refusal exists to
replace and not an argument against one. On the clean-tree branch the loud one is three
stages further on: B's manifest hashes the raw worktree bytes while B's tree does not carry
them, so `clone_seat` raises `SeatError: seat content differs from the baseline manifest`. An
infrastructure failure attributed to a seat, hours in, stands in for a sentence naming a bit
the user can clear.

EVERY REPO-RELATIVE PATH HERE IS RESOLVED AGAINST `facts.root`, git's answer, never against
the `repo` the caller named. One call is an exception only because it reads nothing:
`detect_generators(repo)` takes its argument and does not use it, so the invariant currently
rests on that rather than on construction — the day it reads the tree it needs `facts.root`. The two differ whenever the caller passes a subdirectory, which
`Report` documents as supported and which `baseline.materialize` builds from `facts.root`
regardless. Joining the selection onto `repo` instead was measured: with `scratch/.env` at
the root and a clean `sub/scratch/`, `inspect_repo(<repo>/sub, ("scratch",))` returned no
secrets, no breaches and no refusals, while `materialize(<repo>/sub, …, ["scratch"], …)` put
`scratch/.env` into B's manifest. A clean screen in front of a baseline carrying the
credential is worse than no screen, because a clean result is what the gate shows a human.
With the subdirectory copy merely absent it degraded into a false sentence instead:
"scratch: not screened — selected path does not exist", about a path that does.
`baseline.materialize` guards the same disagreement by refusing a `repo` and a `facts` that
name different repositories, and `inspect.repo_facts` resolves the root before its own index
reads for the same reason.

STATIC AND READ-ONLY IS A CLAIM ABOUT EXECUTION, not only about writes. §5: "No arbitrary
project setup code runs before authorization." Nothing here runs a program the repository
supplies — no `Makefile`, no `setup.py`, no `conftest.py`, and no `core.fsmonitor`, which is
the one that is easy to miss because git runs it on the caller's behalf rather than the
caller running it. See `inspect`'s third hard rule for that measurement.

THE SCREEN IS SCOPED TO THE SELECTION, and the reason is measured rather than inherited.
§2.3's scoping paragraph is about REJECTIONS, so it does not settle §3 — but the whole-tree
alternative fails on its own terms: `screen_tree(<this repository>, ["."])` returned one
breach and no findings, `files: <n> > 5000` — several hundred past `Quota.default()`'s cap,
and stated as a bound rather than a count because the number moves with every commit (5750
on 2026-08-02). So an unscoped screen would refuse forge's first run here on the cap alone.
No quota is passed, so `screen_tree` applies `Quota.default()` — the pre-launch screen's own
question, and deliberately not `Quota.for_harvest()`'s, which sizes a seat holding a
dependency tree.

WHAT THAT LEAVES UNSCREENED is named because the scoping above reads as though §3 were
satisfied. §3 asks for "the entire selected baseline", and the baseline is tracked content
PLUS the selection while this screens the selection alone — so a credential in a tracked
file, including the uncommitted edit to one that §3's opening sentence calls out by name,
reaches three providers unscreened. Closing it needs a path set bounded by the porcelain
rather than by the tree, and a rule for the tracked paths the porcelain lists as deleted,
which would otherwise arrive here as "selected path does not exist" breaches and refuse an
ordinary repository.

CONTAINMENT IS NEW HERE and belongs here rather than in `rejections`: it is a property of
the SELECTION, which is the one input preflight takes from outside the repository.
`rejections` answered `[]` for both `../outside.txt` and `/etc/hostname` — measured — and a
selection that escapes is not merely unrefused, it is read: `runstate.snapshot_refs`'s
carried digest moved when a file outside the repository changed under a selected
`../outside.txt`. The rule itself is `bundle._assert_contained`, IMPORTED rather than
restated. The import creates no cycle (`bundle` imports `gitcmd` and stdlib only), and this
package already carries the same lexical rule in three places — `bundle`, `verify._contained`
and `screen_tree`'s own loop — so a fourth spelling is the drift this reuse exists to avoid.
What is NOT borrowed is the message: `BundleError` describes a candidate that cannot cross,
and a caller at the confirmation gate needs a sentence about the selection it just made.

An escaping selection is kept OUT of `rejections` and `screen_tree` as well as refused.
Both join it onto the root — `rejections` stats it and walks it when it is a directory — so
passing one through would have preflight reading a host path it has already decided to
refuse, and would report the same escape twice in two vocabularies.

`refusals` IS THE WHOLE ANSWER ABOUT THE REPOSITORY, in one order: `rejections`, containment,
screen findings, screen breaches. The last two are different claims and stay adjacent rather
than merged — a finding is "this looks like a credential", a breach is "we did not read
this" — and both stop the run, because a screen that certifies what it did not open is worth
no more than one that finds nothing.

`task_refusals` IS THE SECOND ANSWER AND NEITHER SUBSUMES THE OTHER. §20 asks a question this
static look cannot reach: not "what is in this tree" but "can this TASK be handed to three
different CLIs at all". The task is not on `Report` because it does not arrive from the
repository — it arrives with the front end, after the repository has been described — so it is
a second function taking the instruction, and a caller at the confirmation gate reads both.
"""
import re
from dataclasses import dataclass
from pathlib import Path

from . import bundle, inspect, screen, taskbundle


class PreflightError(RuntimeError):
    """An argument preflight will not answer for — never a state of the repository, which is
    what `Report` and `refusals` are for."""


@dataclass(frozen=True)
class Report:
    """What one static look saw. Every field is an observation; none is a decision.

    `repo` is the repository AS THE CALLER NAMED IT, and `facts.root` is git's answer for the
    same repository — they differ whenever the caller passed a subdirectory, and both are
    kept because the second is what every path here is relative to.

    `selected` is the selection as given, including any entry `escaping` refuses. A report
    whose `selected` silently dropped the refused entries would describe a run the caller did
    not ask for.

    `secrets` holds `screen.Finding`s rather than sentences so a caller can group or override
    per path, which §3 point 3 contemplates; `refusals` is where they become sentences. A
    finding carries a path, a line and the PATTERN's name — never the matched text, which
    would put the credential into the very report a user pastes into a chat.

    `gate_surface` is None and that is an answer: §6.1's surface is the resolved gate's own
    scripts, runners and discovered test files, and preflight has no confirmed verify command
    to resolve — the user names it at §5 step 2, and `gate.must_show` resolves the surface
    there, where the command is in hand. `()` would say this repository defines no gate, which
    is a far stronger claim than nobody looked, and it is the claim the §5 gate shows a human
    once because a `PASS` can later rest on it.
    """
    repo: Path
    facts: inspect.RepoFacts
    rejections: tuple[str, ...]
    selected: tuple[str, ...]
    escaping: tuple[str, ...]
    secrets: tuple
    breaches: tuple[str, ...]
    contract: inspect.GeneratorContract
    gate_surface: tuple[str, ...] | None


def _secret_line(finding) -> str:
    """One screen finding as the sentence `refusals` returns.

    The line number is omitted when it is 0, which is the high-risk-FILENAME rule's value:
    that rule fires on the name alone and never opened the file, so `path:0` would invite a
    reader to go and look at a line that had nothing to do with it.
    """
    where = f"{finding.path}:{finding.line}" if finding.line else finding.path
    return f"{where}: {finding.pattern}"


def inspect_repo(repo, selected_untracked=()) -> Report:
    """Describe `repo` and the paths the caller means to carry, running nothing and writing
    nothing.

    `selected_untracked` is REQUIRED to be a sequence of paths and refused as a bare string,
    on `runstate._texts`' argument: a string iterates into its characters, so `"scratch"`
    would arrive downstream as seven single-character selections and each would come back as
    a breach about a path nobody named.
    """
    if isinstance(selected_untracked, (str, bytes)):
        raise PreflightError(
            f"selected_untracked is a sequence of repo-relative paths, not {selected_untracked!r}; "
            "a string iterates into its characters, so one path would arrive as several")
    selected = tuple(selected_untracked)
    contained, escaping = [], []
    for rel in selected:
        try:
            bundle._assert_contained(rel, "selection")
        except bundle.BundleError:
            escaping.append(f"selected path escapes the repository: {rel!r}")
        else:
            contained.append(rel)

    facts = inspect.repo_facts(repo)
    # `facts.root`, not `repo`: a selection is worktree-ROOT-relative, which is what
    # `rejections` and `baseline.materialize` both resolve it against. See the module
    # docstring for the measured outcome of joining it onto a caller's subdirectory instead.
    findings, breaches = screen.screen_tree(facts.root, contained)
    # `runstate.snapshot_refs` is deliberately NOT called, though it is the other §5 read a
    # reader expects here: §14.2 puts t0 at the confirmation gate, so a snapshot taken now
    # would date the run before the user agreed to it and read every action taken DURING the
    # gate as drift. `Report` has no field for one for the same reason.
    return Report(repo=Path(repo),
                  facts=facts,
                  rejections=tuple(inspect.rejections(facts, contained)),
                  selected=selected,
                  escaping=tuple(escaping),
                  secrets=tuple(findings),
                  breaches=tuple(breaches),
                  contract=inspect.detect_generators(repo),
                  gate_surface=None)


def refusals(report) -> tuple[str, ...]:
    """Everything that must stop this run, in the order the module docstring gives. `()`
    means preflight found nothing standing in the way — not that the run is safe."""
    if not isinstance(report, Report):
        raise PreflightError(f"a Report is required, not {type(report).__name__}")
    return (*report.rejections, *report.escaping,
            *(_secret_line(f) for f in report.secrets), *report.breaches)


# §20's referents: things that exist on ONE of the three CLIs and that no file can carry. The
# patterns are NARROW on purpose and each one names what it matched, because a detector whose
# refusal does not say what it saw teaches an operator to rewrite around it — which is the same
# as not having a detector. These are matched against the OPERATOR'S OWN task text, not against
# a seat's merged stderr, so the phantom-match hazard `council.engine` documents at
# TOOL_PERMISSION_SENTINELS does not apply here: nobody is echoing a file into this string.
_PROVIDER_SPECIFIC = (
    (re.compile(r"\bsubagent_type\b"), "a named subagent type"),
    (re.compile(r"\$\{?CLAUDE_PLUGIN_ROOT\}?"), "a Claude plugin-root variable"),
    (re.compile(r"\$\{?PLUGIN_ROOT\}?"), "a Codex plugin-root variable"),
    (re.compile(r"\bcodex\s+exec\b"), "a codex-only subcommand"),
    (re.compile(r"--dangerously-skip-permissions"), "a provider-only permission flag"),
    # THE HYPHEN IS IN THE CLASS. An MCP tool name is `mcp__<server>__<tool>` and a server
    # name carries hyphens (`mcp__chrome-devtools__take_snapshot`); without it the match was
    # `mcp__chrome`, so the refusal named a five-character prefix of the thing it saw. Naming
    # the referent is the one property these patterns exist to have, and a truncated one is
    # worse than none: an operator who cannot find `mcp__chrome` in their own task reads the
    # refusal as a bug in the detector.
    (re.compile(r"\bmcp__[A-Za-z0-9_-]+"), "an MCP tool name"),
    (re.compile(r"~/\.(?:claude|codex|gemini)\b"), "a provider configuration directory"),
)

# A named skill, in the two forms an operator writes one. BOTH BOUNDARIES ARE NARROWED, and
# each one was measured against a task text nobody would write around:
#
#   * the leading boundary is a LINE START or whitespace and never a `/`, so a URL path
#     segment (`https://x/docs/markitdown`) does not match;
#   * the trailing boundary refuses a `/` too, so an ordinary absolute path in a task
#     (`read /home/user/notes.md`) is not read as a reliance on an ambient `home` skill.
#
# A skill named inside a fenced code block IS matched, deliberately: a task whose fenced
# block runs `/markitdown` relies on it exactly as much as one whose prose does, and
# stripping fences would be the fail-open direction of the same choice.
_AMBIENT_SKILL = (
    re.compile(r"(?:^|(?<=\s))/([a-z][a-z0-9-]{2,63})(?![\w/-])"),
    # `(?i:...)` around the LITERALS only, never `re.I` over the whole pattern: a
    # case-insensitive capture returns `MARKITDOWN` and `markitdown` as two different skills
    # for one reference, and each would get its own refusal line naming a skill that does not
    # exist under that spelling.
    re.compile(r"(?i:\buse (?:the )?)`?([a-z][a-z0-9-]{2,63})`?(?i: skill\b)"),
)

_PORTABLE_ASK = ("Supply a portable task bundle instead — a directory whose entrypoint states "
                 "the task in provider-neutral terms, with every file it references beside it.")


def task_refusals(instruction, *, bundle=None, closures=None) -> tuple[str, ...]:
    """§20's refusal, about the TASK. `refusals` above answers about the REPOSITORY.

    NEITHER SUBSUMES THE OTHER AND BOTH ARE READ, which is why this is a second function and
    not a field on `Report`. `Report` describes one static look at a repository and
    `gate.open_run` reads `refusals` off it; the task is not in scope there — it arrives with
    the front end. A caller runs both before opening a run.

    `()` MEANS EXAMINED AND NOTHING STANDS IN THE WAY. An instruction this function could not
    read may not borrow that sentence, so a non-string, an empty string and whitespace all
    RAISE rather than return the clean answer.

    A BUNDLE CLEARS NOTHING HERE, AND `bundle` IS READ BY NO CONDITION BELOW. §20: do not
    automatically translate provider-specific tools or subagent semantics. A bundle carries
    files; it cannot give codex a subagent type, and it cannot make `/markitdown` the same
    skill on three CLIs — which is the whole reason §20 asks for a three-way closure hash. The
    parameter is kept because a caller reads more naturally passing what it has, and because a
    future referent might genuinely be answerable by a file; it is NOT a licence, and a
    `bundle is None` guard on the ambient bar would be dead code in the only production caller,
    since the front end always builds one. What the caller does with a CLEARED ambient skill is
    `ambient_notes` below.

    `closures` IS THE THREE LIVE INSTALLED HASHES, `{cli: sha or None}`, and it is an ARGUMENT
    because resolving it walks three plugin caches and this function is called at a gate.
    `None` for a CLI is what `taskbundle.installed_closure` returns for one that is not
    installed, and `ambient_verdict` reads any `None` as False — so three ABSENCES do not hash
    identically. Passing `closures=None` means nobody resolved them, which is not the same as
    resolving them and finding a mismatch: it is treated as the same refusal but says so in
    different words, because a named skill relied on without the check is exactly what §20
    forbids.
    """
    _readable(instruction)
    _measurable(closures)
    out = []
    for pattern, what in _PROVIDER_SPECIFIC:
        m = pattern.search(instruction)
        if m:
            out.append(
                f"this task is not portable: it names {what} ({m.group(0)!r}), which two of the "
                f"three seats do not have. §20 refuses to translate it automatically. "
                f"{_PORTABLE_ASK}")
    named = _named_skills(instruction)
    if named and not _ambient_ok(closures):
        why = ("the three installed closures were never resolved" if not closures else
               "the three installed closures do not hash identically: " + _closure_line(closures))
        for skill in named:
            out.append(
                f"this task relies on the ambient `{skill}` skill and {why}. §20 permits a "
                "named skill only when all three installed copies hash identically and it "
                f"is declared provider-neutral. {_PORTABLE_ASK}")
    return tuple(out)


def _readable(instruction) -> None:
    """The instruction this engine is being asked about, or a refusal — never a clean `()`."""
    if not isinstance(instruction, str):
        raise PreflightError(
            f"§20 resolves a task into a portable instruction, and this one is "
            f"{type(instruction).__name__}. An empty answer here would say 'examined, nothing "
            "stands in the way' about a task nobody examined.")
    if not instruction.strip():
        raise PreflightError(
            "§20's resolution has nothing to resolve: this instruction is empty or whitespace. "
            "A run opened over it would launch three seats with no task.")


def _measurable(closures) -> None:
    """`closures` is the three hashes or nobody's answer at all — and nothing in between.

    `None` and `{}` are both "nobody resolved them" and both refuse a named skill. A LIST or a
    string is neither: `ambient_verdict` calls `.get` on it, so it left this function as an
    `AttributeError` out of a gate whose whole contract is that it answers in refusal lines.
    """
    if closures is not None and not isinstance(closures, dict):
        raise PreflightError(
            f"§20's closures are a mapping of cli name to hash-or-None, not "
            f"{type(closures).__name__}. A shape this engine cannot read is not a mismatch it "
            "can describe, and it must not arrive at the ambient bar as one.")


def _named_skills(instruction: str) -> tuple[str, ...]:
    named = []
    for pattern in _AMBIENT_SKILL:
        named += [m.group(1) for m in pattern.finditer(instruction)]
    return tuple(sorted(set(named)))


def _ambient_ok(closures) -> bool:
    """`closures` unresolved is NOT `closures` resolved and disagreeing, and neither licenses
    an ambient skill — but they are different facts and the refusal says which."""
    return bool(closures) and taskbundle.ambient_verdict(closures)


def _closure_line(closures) -> str:
    """The three closures as an operator can act on them, with THREE readings kept apart.

    NOT `(closures.get(c) or "not installed")[:12]`: that truncates to `not installe`, which
    reads as a twelve-character hash prefix.

    And "not measured" is not "not installed". A CLI absent from the mapping is one this
    caller never asked about; a CLI mapped to `None` is one `installed_closure` looked for and
    could not hash. Collapsing them would put a sentence about a missing installation in front
    of an operator whose real problem is a caller that only resolved two of the three.

    "not installed or unreadable" IS AS FAR AS THIS CAN GO, and the vaguer wording is the
    honest one. `installed_closure` answers `None` for a CLI with no install location, for one
    whose cache is absent or empty, AND for one whose closure raised while being walked; its
    declared type is `str | None`, so the distinction is gone before this function is reached.
    Writing "not installed" alone would name a cause this engine did not measure.
    """
    out = []
    for c in ("claude", "codex", "agy"):
        if c not in closures:
            out.append(f"{c}=not measured")
        elif not closures[c]:
            out.append(f"{c}=not installed or unreadable")
        else:
            out.append(f"{c}={str(closures[c])[:12]}")
    return ", ".join(out)


def ambient_notes(instruction, *, closures) -> tuple[str, ...]:
    """§20's note for every named skill this task may rely on, for the caller to add to the
    prompt. `()` when nothing was named, and `()` when the bar was not cleared.

    THIS IS `taskbundle.ambient_note`'s CALLER, and until it existed there was none:
    `task_refusals` answers what STOPS a run, and §20's other half — declare it
    provider-neutral in the prompt — needs a producer, or the check clears a skill and then
    says nothing about it.

    NOT CLEARED MEANS NO NOTE, not a hedged one. `task_refusals` has already refused that run;
    emitting a note beside a refusal would put a sentence licensing the skill into the same
    output that says it may not be used.
    """
    _readable(instruction)
    _measurable(closures)
    if not _ambient_ok(closures):
        return ()
    return tuple(taskbundle.ambient_note(s) for s in _named_skills(instruction))
