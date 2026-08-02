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

`refusals` IS THE WHOLE ANSWER a caller acts on, in one order: `rejections`, containment,
screen findings, screen breaches. The last two are different claims and stay adjacent rather
than merged — a finding is "this looks like a credential", a breach is "we did not read
this" — and both stop the run, because a screen that certifies what it did not open is worth
no more than one that finds nothing.
"""
from dataclasses import dataclass
from pathlib import Path

from . import bundle, inspect, screen


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
    to resolve — the user names it at §5 step 2. `()` would say this repository defines no
    gate, which is a far stronger claim than nobody looked.
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
