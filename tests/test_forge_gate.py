"""What the §5 gate quotes, and what it refuses.

Every arithmetic assertion here is written as the DERIVATION rather than as the total, because
a total is the one thing that can be back-fitted to whatever the code returned. Every refusal
is paired with a discrimination check — a command differing in exactly the property the
refusal names, and admitted — since a fixture odd enough to trip one rule is odd enough to
trip another, and a non-empty tuple says nothing about which.
"""
import dataclasses
import itertools
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))
sys.path.insert(0, str(ROOT / "tests"))

from forge import baseline, gate, journal, preflight, runstate, storage, verify  # noqa: E402
from forge import inspect as finspect  # noqa: E402
from forge_fixtures import (GLOBAL_IDENTITY, commit_all, git as _git,  # noqa: E402
                            global_identity, make_repo, write)


_SERIAL = itertools.count()


# The REAL reader, bound at import — BEFORE the autouse fixture below displaces the module
# attribute. Reading it back off `gate.free_bytes` inside a test would read the neutralisation.
_REAL_FREE_BYTES = gate.free_bytes


# A GATE'S GREEN MAY NOT DEPEND ON THE TESTER'S FREE DISK. `must_show` and `open_run` both
# consult `free_bytes` against the real XDG_STATE_HOME, so a machine under ~63 GB free would
# add a refusal line at `lines[0]` for every direct `must_show` call in this file — including
# the set-equality assertions — and make `open_run` raise. This is the same neutralisation
# `XDG_STATE_HOME` already gets, one function over. Every test that means to measure the real
# behaviour reaches for `_REAL_FREE_BYTES` or patches over the fixture.
@pytest.fixture(autouse=True)
def _enough_disk(monkeypatch):
    monkeypatch.setattr(gate, "free_bytes", lambda _p: 10 ** 15)


def _report(tmp_path, selected=()):
    """A real `preflight.Report` over a throwaway repository.

    Built rather than faked: `quote` refuses anything that is not a Report, and a stub that
    satisfied the isinstance check would also have to satisfy `preflight.refusals`, which the
    quote calls to put a refused run above its own price.

    A fresh directory each call, because several tests below quote twice to difference two
    settings and `make_repo`'s seed commit fails on a repository that already has one.
    """
    return preflight.inspect_repo(make_repo(tmp_path, f"repo{next(_SERIAL)}"), selected)


# The shape this repository's own gate has, reduced to what §5.2's chain needs: `precommit`
# depends on `verify`, its recipe imports a checker off a path it inserts itself, and that
# checker's error message names `make eval` as the remedy. Read off the real Makefile
# (`precommit: verify`, and the two `receipt_gate` recipes under it) rather than invented, so
# a rule that only fires on a shape nobody writes cannot pass.
_MAKEFILE = (
    "PY := python3\n"
    "EVAL := scripts/eval_harness.py\n"
    "\n"
    "verify: ## validate\n"
    "\t$(PY) scripts/render.py --check\n"
    "\n"
    "precommit: verify ## commit-boundary gate\n"
    "\t@$(PY) -c \"import sys; sys.path.insert(0,'scripts/lib'); import checks; "
    "sys.exit(1 if checks.receipt_gate(advisory=False) else 0)\"\n"
    "\n"
    "eval:\n"
    "\t$(PY) $(EVAL) --skill $(SKILL)\n"
    "\n"
    "eval-test:\n"
    "\t$(PY) $(EVAL) --self-test\n"
)


def _repo_shaped_like_this_one(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "Makefile", _MAKEFILE)
    write(repo, "scripts/render.py", "def main():\n    return 0\n")
    write(repo, "scripts/lib/checks.py",
          "def receipt_gate(*, advisory):\n"
          "    return [f'receipt: llm-forge changed — run `make eval SKILL=llm-forge`']\n")
    write(repo, "scripts/eval_harness.py",
          "import fanout\n"
          "m = fanout.run_council(specs, retries=0, timeout=300)\n")
    commit_all(repo, "gates")
    return repo


def _found(repo, *argvs):
    return gate.provider_invoking_verify(repo, verify.Command.parse([list(a) for a in argvs]))


# --------------------------------------------------------------------------- the quote

def test_the_worst_case_is_quoted_not_the_happy_path(tmp_path):
    """§5.2's terms, each derived on its own rather than compared against one total.

    The sum §5.2 itself writes is `9 + 1 + 6 = 16`, and the fourth term below is why the
    quote is 22 instead: the same paragraph requires that "the quote includes post-review
    synthesis invocations", and `9 + 1 + 6` contains exactly one synthesis. Three more exist —
    a round-1 blocker is fixed before round 2 runs, a blocker fixed after round 2 is reported
    "verified but not independently reviewed" (§13), and §13.1's deep_review findings "follow
    the exact post-round-2 rule above: fix -> fresh-verifier verify -> checkpoint". A quote
    stopping at 16 is wrong by six, in the operator's favour, which is the direction that
    matters.

    THE FAN-OUT ITSELF IS THE FIFTH TERM, and it is new: the cloud review this replaced was
    billed in credits and spent no provider call, so it was priced only in prose. A local
    panel is `seats` calls, and leaving them out of the headline number would under-report
    what the run actually spends — the exact defect the money line's own comment warns of.
    """
    q = gate.quote(_report(tmp_path), seats=3, attempts=3, review_rounds=2, seat_timeout_sec=3600)
    builders, synthesis, review, fixes, deep = 3 * 3, 1, 2 * 3, 2 + 1, 3
    assert (builders, synthesis, review, fixes, deep) == (9, 1, 6, 3, 3)
    assert q.provider_calls == builders + synthesis + review + fixes + deep == 22, q.lines
    assert any("9 + 1 + 6 = 16" in line for line in q.lines), \
        "§5.2's own sum stays visible, or the difference is a silent correction"


def test_a_review_round_costs_a_reviewer_each_and_one_synthesis_fix(tmp_path):
    """The discrimination check for the term above: only the review terms may move with it."""
    rounds = [gate.quote(_report(tmp_path), review_rounds=n, seat_timeout_sec=3600).provider_calls for n in (0, 1, 2)]
    assert rounds == [14, 18, 22], rounds
    assert rounds[0] == 3 * 3 + 1 + 1 + 3, \
        "with no review, only builders, synthesis, the deep fan-out and its own fix are left"
    assert [b - a for a, b in zip(rounds, rounds[1:])] == [4, 4], \
        "a round is 3 reviewers plus the one synthesis invocation that answers it"


def test_deep_reviews_own_fix_is_an_invocation_the_quote_cannot_leave_out(tmp_path):
    """§13.1 is titled "default on" and its findings take the post-round-2 treatment: "fix ->
    fresh-verifier verify -> checkpoint". §5.2 prices the deep_review RUN in money and says
    nothing about the fix it triggers, so a quote that priced only the run is low by one
    invocation, one setup and one verify — with no exclusion line, which is the shape §5.2's
    own `9 + 1 + 6` has. With the council backend it is ALSO low by the fan-out's own
    `seats` calls, so turning it off now moves four scalars.

    `--skip-deep-review` is the discrimination check and the answer to "then make it optional": it is
    a parameter, and turning it off must move exactly those three scalars and nothing else.
    """
    on = gate.quote(_report(tmp_path), seats=3, attempts=3, review_rounds=2, seat_timeout_sec=3600)
    off = gate.quote(_report(tmp_path), seats=3, attempts=3, review_rounds=2, deep_review=False, seat_timeout_sec=3600)
    assert (on.provider_calls, on.setup_runs, on.verify_runs) == (22, 18, 9), on.lines
    assert (off.provider_calls, off.setup_runs, off.verify_runs) == (18, 17, 8), off.lines
    assert on.provider_calls - off.provider_calls == 3 + 1, \
        "the fan-out (3 seats) AND the fix it triggers (1) both leave with it"
    assert "provider call(s)" in on.deep_review and "not run" in off.deep_review
    assert "--skip-deep-review" in on.deep_review, "the opt-out is named in the quote that includes it"


def test_the_council_default_retry_setting_is_shown_but_never_summed(tmp_path):
    """§5.2 asks for whichever is wired, and forge wires `--retries 0` (§13). The 28-call
    variant exists only if review retries ever return, so it is a line and not the total —
    pinned by differencing the review term OUT of the total, which is the only way to show
    which of the two numbers the total was built from.
    """
    q = gate.quote(_report(tmp_path), seats=3, attempts=3, review_rounds=2, seat_timeout_sec=3600)
    assert any(str(2 * 3 * 3) in line and "--retries 2" in line for line in q.lines), q.lines
    no_review = gate.quote(_report(tmp_path), seats=3, attempts=3, review_rounds=0, seat_timeout_sec=3600)
    assert q.provider_calls - no_review.provider_calls == 2 * 3 + 2, \
        "two rounds cost 6 reviewer calls and 2 fixes — the wired --retries 0 term, not 18"


def test_the_deep_review_line_explains_the_calls_it_is_counted_in(tmp_path):
    """The line EXPLAINS the calls rather than replacing them.

    It used to be a string because money is not a call count. With the council backend the
    review IS provider calls, so those calls are now inside `provider_calls` and this line
    says so — a price that lived only in prose while the scalar under-reported it is the
    "scalar that reads low is not rescued by a line naming what it left out" defect the
    Quote docstring names."""
    q = gate.quote(_report(tmp_path), seat_timeout_sec=3600)
    assert "provider call(s)" in q.deep_review, q.deep_review
    assert "$" not in q.deep_review, "the cloud's currency is gone with the cloud"
    assert isinstance(q.provider_calls, int) and q.provider_calls == 22
    assert isinstance(q.deep_review, str)
    assert str(q.provider_calls) not in q.deep_review


def test_the_quote_names_the_per_candidate_setup_it_cannot_avoid(tmp_path):
    """§6: "it costs one extra setup per candidate; that cost is essential, not optional
    hardening". Three independent sources, and the plan's own sketch understated two of
    them — calibration is setup+verify TWICE (§5 step 3, and §5.2 says "x2"), and a builder
    retry gets a FRESH clone (§8.1), so the builder term is seats x attempts.
    """
    q = gate.quote(_report(tmp_path), seats=3, attempts=3, review_rounds=2, seat_timeout_sec=3600)
    calibration, builders, verifiers = 2, 3 * 3, 3 + 1 + 2 + 1
    assert q.setup_runs == calibration + builders + verifiers == 18, q.lines
    assert q.setup_runs >= 5, "the plan's floor holds, but at under a third of the real figure"


def test_a_builder_retry_is_a_fresh_clone_and_so_a_fresh_setup(tmp_path):
    """§8.1's discrimination check: attempts is the only input that may move this term."""
    one = gate.quote(_report(tmp_path), seats=3, attempts=1, seat_timeout_sec=3600)
    three = gate.quote(_report(tmp_path), seats=3, attempts=3, seat_timeout_sec=3600)
    assert three.setup_runs - one.setup_runs == 3 * (3 - 1)
    assert three.verify_runs == one.verify_runs, \
        "a builder clone is never verified, so its retries cannot move the verify count"


def test_the_builders_own_clone_is_not_counted_as_a_verification(tmp_path):
    """§7 lists `Fverify` among a seat's four inventories and §6 overrules it in as many
    words: verification runs in a clone the builder never had, so running the gate where the
    builder was is not one of these runs."""
    q = gate.quote(_report(tmp_path), seats=3, attempts=3, review_rounds=2, seat_timeout_sec=3600)
    assert q.verify_runs == 2 + (3 + 1 + 2 + 1) == 9, q.lines
    assert q.verify_runs < q.setup_runs, "setup also runs in every builder clone"


def test_peak_disk_is_every_clone_that_coexists_not_the_builder_fleet(tmp_path):
    """§5.2's disk figure — "three no-hardlink clones plus three dependency trees is plausibly
    6-10 GB" — prices ONE FLEET at three seats, and the run holds several fleets at once: §8.1
    preserves a failed retry's clone rather than resetting in place, §6 gives every candidate a
    fresh verifier clone, and §15's automatic cleanup covers "known-failed temporary clones
    only". Quoting the fleet as the peak is a scalar wrong by ~5x in the operator's favour, and
    a line naming the exclusion does not rescue a field something else compares.
    """
    q = gate.quote(_report(tmp_path), seats=3, attempts=3, review_rounds=2, seat_timeout_sec=3600)
    per_clone = 10.0 / 3           # §5.2's own upper bound, over the fleet it was stated for
    clones = 1 + 3 * 3 + (3 + 1 + 2 + 1) + 2   # + one review clone per round (§13)
    assert clones == 19
    assert q.peak_disk_gb == pytest.approx(round(clones * per_clone, 1), abs=0.05)
    assert q.peak_disk_gb != 10.0, "§5.2's fleet figure is one term of the peak, not the peak"
    assert any("--gc" in line for line in q.lines), q.lines


def test_a_preserved_retry_clone_is_disk_the_peak_has_to_carry(tmp_path):
    """The discrimination check for the term above, on the input §8.1 governs: "every retry
    attempt gets a fresh clone. The failed attempt is PRESERVED as partial input." So two
    extra attempts per seat is six more clones on disk — and not one more verify, since a
    builder clone is never the tree a candidate is verified in (§6).
    """
    one = gate.quote(_report(tmp_path), seats=3, attempts=1, seat_timeout_sec=3600)
    three = gate.quote(_report(tmp_path), seats=3, attempts=3, seat_timeout_sec=3600)
    assert three.peak_disk_gb - one.peak_disk_gb == pytest.approx(6 * 10.0 / 3, abs=0.05)
    assert three.verify_runs == one.verify_runs


def test_every_line_of_the_quote_names_the_section_it_came_from(tmp_path):
    """`Quote`'s own docstring promises it, and the promise is the point: an operator who
    thinks a number is wrong needs the derivation. Run over the refused shape too, because the
    conditional first line is the one that had no citation."""
    repo = make_repo(tmp_path)
    _git(repo, "update-index", "--skip-worktree", "seed.txt")
    for q in (gate.quote(_report(tmp_path), seat_timeout_sec=3600), gate.quote(preflight.inspect_repo(repo), seat_timeout_sec=3600)):
        assert q.lines
        missing = [line for line in q.lines if "§" not in line]
        assert missing == [], missing


def test_the_quote_bounds_wall_clock_rather_than_declining_to(tmp_path):
    """§5.2 lists wall clock. A duration cannot be measured statically; a BOUND can, because
    builders run serially under one window each. This test replaces the one that asserted the
    decline — `preflight` still cannot answer, but the window is the caller's and it is now
    required rather than invented here.

    THE ASSERTION QUOTES THE RENDERED CASE. The line writes `UPPER BOUND` in capitals, on
    purpose, because the whole risk is a reader taking it for an estimate — a `.lower()` here
    would pass over a line that stopped shouting."""
    report = _report(tmp_path)
    assert report.gate_surface is None
    q = gate.quote(report, seats=3, attempts=3, review_rounds=2, seat_timeout_sec=3600)
    line = next(l for l in q.lines if l.startswith("wall clock"))
    assert "UPPER BOUND" in line
    assert "9.0 h" in line and "not quoted" not in line


def test_the_wall_clock_line_says_what_it_excludes(tmp_path):
    """A bound that named only what it covers reads as a ceiling for the whole run."""
    q = gate.quote(_report(tmp_path), seats=3, attempts=3, review_rounds=2,
                   seat_timeout_sec=3600)
    line = next(l for l in q.lines if l.startswith("wall clock"))
    assert "setup" in line and "verify" in line


def test_an_unusable_seat_window_is_refused_and_never_defaulted(tmp_path):
    """§19's second-timeout-mechanism failure arriving through a default."""
    for bad in (0, -1, None, True, "3600"):
        with pytest.raises(gate.GateError, match="seat_timeout_sec"):
            gate.quote(_report(tmp_path), seats=3, attempts=3, review_rounds=2,
                       seat_timeout_sec=bad)


def test_must_show_screens_the_setup_command_too(tmp_path):
    """Setup runs 18 times on a default run — the more expensive miss, and the one the
    detector was never pointed at.

    THE ASSERTION HAS TO PICK A BRANCH. Both branches of the new block start with the words
    `setup command`, so `any("setup command" in l)` would match the CLEAN one too and pass
    with the detector wired to nothing. What distinguishes them is the finding."""
    repo = make_repo(tmp_path)
    write(repo, "Makefile", "setup:\n\t@claude -p 'go'\n")
    commit_all(repo, "gates")
    report = preflight.inspect_repo(repo)
    q = gate.quote(report, seats=3, attempts=3, review_rounds=2, seat_timeout_sec=3600)
    lines = gate.must_show(report, q, verify.Command.parse([["true"]]),
                           setup=verify.Command.parse([["make", "setup"]]))
    hit = [l for l in lines if "the setup command reaches a provider CLI" in l]
    assert len(hit) == 1, lines
    assert f"setup runs {q.setup_runs} times" in hit[0]
    assert not any("no provider spend was found in it" in l for l in lines)


def test_a_clean_setup_command_says_it_was_followed_and_claims_nothing_more(tmp_path):
    """The other branch, asserted separately — because "screened and clean" and "screened and
    dirty" are the two records this whole argument is about keeping apart."""
    report = _report(tmp_path)
    q = gate.quote(report, seats=3, attempts=3, review_rounds=2, seat_timeout_sec=3600)
    lines = gate.must_show(report, q, verify.Command.parse([["true"]]),
                           setup=verify.Command.parse([["true"]]))
    assert any("no provider spend was found in it" in l for l in lines)
    assert not any("the setup command reaches a provider CLI" in l for l in lines)


def test_must_show_refuses_a_missing_setup_command(tmp_path):
    """An unscreened command and a screened-clean one must not leave the same record."""
    report = _report(tmp_path)
    q = gate.quote(report, seats=3, attempts=3, review_rounds=2, seat_timeout_sec=3600)
    with pytest.raises(gate.GateError, match="setup Command"):
        gate.must_show(report, q, verify.Command.parse([["true"]]), setup=None)


def test_a_refused_run_is_named_above_its_price(tmp_path):
    """A price for a run preflight already refuses is a price for something that will not
    happen. Paired with the clean repository below, which must not carry the line."""
    repo = make_repo(tmp_path)
    _git(repo, "update-index", "--skip-worktree", "seed.txt")
    blocked = gate.quote(preflight.inspect_repo(repo), seat_timeout_sec=3600)
    assert blocked.lines[0].startswith("refused first:"), blocked.lines


def test_a_clean_repository_is_priced_without_a_refusal_line(tmp_path):
    assert not gate.quote(_report(tmp_path), seat_timeout_sec=3600).lines[0].startswith("refused first:")


def test_the_quote_refuses_anything_that_is_not_a_preflight_report(tmp_path):
    with pytest.raises(gate.GateError):
        gate.quote(str(make_repo(tmp_path)), seat_timeout_sec=3600)
    with pytest.raises(gate.GateError):
        gate.quote(_report(tmp_path), seats=0, seat_timeout_sec=3600)


# --------------------------------------------------------------------- the §5.2 detector

def test_a_verify_command_that_invokes_a_provider_is_detected(tmp_path):
    """§5.2 on this repository's own shape: `precommit` -> `receipt_gate` -> the documented
    remedy `make eval SKILL=<skill>` -> `run_council` with real providers, ~24 calls per
    verify, re-run fresh per candidate.

    The last hop is a SENTENCE IN AN ERROR MESSAGE, not a call: `make` never runs `make
    eval`, an operator does, and only when the gate fails — which it does on every candidate
    that changes a skill. So the finding is priced rather than refused, which is §5.2's own
    "or priced as its own explicit line", and it still has to name the whole chain.
    """
    repo = _repo_shaped_like_this_one(tmp_path)
    found = _found(repo, ["make", "precommit"])
    assert found, "a verify that spends provider calls must be named before the gate"
    remedy = [f for f in found if f.startswith("remedy:")]
    assert len(remedy) == 1, found
    assert "scripts/lib/checks.py" in remedy[0] and "make eval" in remedy[0]
    assert "run_council" in remedy[0], "the finding names what actually spends, not the hop"


def test_the_same_repositorys_advisory_target_is_not_flagged(tmp_path):
    """The discrimination check for the case above, in the SAME tree: §5.2 steers the
    operator to `make verify`, so a detector that refuses both has refused its own remedy."""
    repo = _repo_shaped_like_this_one(tmp_path)
    assert _found(repo, ["make", "verify"]) == ()


def test_the_remedy_line_carries_the_steer_and_not_only_the_chain(tmp_path):
    """§5.2 is one sentence with two halves — "greps the resolved target for council/eval entry
    points, STEERS THE OPERATOR TO `make verify` (receipts advisory)" — and the grep half alone
    tells an operator what is wrong without telling them what to gate on instead. Paired with a
    tree that declares no advisory target, where naming one would be an invention.
    """
    repo = _repo_shaped_like_this_one(tmp_path)
    remedy = [f for f in _found(repo, ["make", "precommit"]) if f.startswith(gate.REMEDY)]
    assert len(remedy) == 1 and "`make verify` (receipts advisory)" in remedy[0], remedy

    bare = make_repo(tmp_path, "no-advisory")
    write(bare, "Makefile", "gate:\n\t@python3 check.py\neval:\n\t@python3 spend.py\n")
    write(bare, "check.py", "print('stale — run `make eval`')\n")
    write(bare, "spend.py", "import fanout\nfanout.run_council([])\n")
    commit_all(bare, "gates")
    other = [f for f in _found(bare, ["make", "gate"]) if f.startswith(gate.REMEDY)]
    assert len(other) == 1 and "does not declare" in other[0], other


def test_the_three_finding_classes_are_exported_rather_than_spelled_by_callers(tmp_path):
    """Telling a refusal from a price is the whole reason to call this, and a caller writing
    `"spends:"` by hand is one rename away from reading every refusal as a price."""
    assert (gate.SPENDS, gate.REMEDY, gate.UNRESOLVED) == ("spends:", "remedy:", "unresolved:")
    repo = _repo_shaped_like_this_one(tmp_path)
    classes = {f.split(":")[0] + ":" for f in _found(repo, ["make", "eval"])}
    assert classes <= {gate.SPENDS, gate.REMEDY, gate.UNRESOLVED} and gate.SPENDS in classes


def test_a_remedy_chain_under_make_C_is_followed_from_the_directory_make_moved_to(tmp_path):
    """`-C` moves the directory every path in that makefile resolves against, including the
    paths inside the remedy target the recipe's own file documents. Resolved against the
    repository root instead, the sub-walk finds nothing, and BOTH backstops go quiet at once:
    no `spends:` to price and no `unresolved:` to say the chain went unread.

    Two controls in the same tree — the remedy target reached DIRECTLY under the same `-C`
    still spends, and the identical shape laid out at the repository root still reports the
    remedy — so the miss is the `-C` and not a fixture that resolves to nothing.
    """
    repo = make_repo(tmp_path, "sub")
    write(repo, "tools/Makefile", "verify:\n\t@python3 gate.py\neval:\n\t@python3 spend.py\n")
    write(repo, "tools/gate.py", "print('receipt stale — run `make eval`')\n")
    write(repo, "tools/spend.py", "import fanout\nfanout.run_council([])\n")
    commit_all(repo, "gates")
    found = _found(repo, ["make", "-C", "tools", "verify"])
    assert [f for f in found if f.startswith(gate.REMEDY)], found
    assert "tools/spend.py" in found[0], "the finding names the file that spends, not the hop"
    assert [f.split(":")[0] for f in _found(repo, ["make", "-C", "tools", "eval"])] == ["spends"]

    flat = make_repo(tmp_path, "flat")
    write(flat, "Makefile", "verify:\n\t@python3 gate.py\neval:\n\t@python3 spend.py\n")
    write(flat, "gate.py", "print('receipt stale — run `make eval`')\n")
    write(flat, "spend.py", "import fanout\nfanout.run_council([])\n")
    commit_all(flat, "gates")
    assert [f.split(":")[0] for f in _found(flat, ["make", "verify"])] == ["remedy"]


def test_a_documented_remedy_this_reader_cannot_follow_is_unresolved_not_dropped(tmp_path):
    """The second backstop, on its own. A remedy whose closure could not be read is not a
    remedy that is free — and filtering the sub-walk to `spends:` threw its `unresolved:` away
    with everything else. Paired with the same remedy written so it resolves.
    """
    repo = make_repo(tmp_path, "opaque")
    write(repo, "Makefile",
          "define RUN\n\t@python3 $(1)\nendef\n"
          "verify:\n\t@python3 gate.py\n"
          "eval:\n\t$(call RUN,spend.py)\n")
    write(repo, "gate.py", "print('stale — run `make eval`')\n")
    write(repo, "spend.py", "import fanout\nfanout.run_council([])\n")
    commit_all(repo, "gates")
    found = _found(repo, ["make", "verify"])
    assert [f.split(":")[0] for f in found] == ["unresolved"], found
    assert "make eval" in found[0] and "could not be read" in found[0], found

    write(repo, "Makefile", "verify:\n\t@python3 gate.py\neval:\n\t@python3 spend.py\n")
    commit_all(repo, "written-out")
    assert [f.split(":")[0] for f in _found(repo, ["make", "verify"])] == ["remedy"]


def test_a_target_that_documents_itself_is_not_reported_as_its_own_remedy(tmp_path):
    """`scripts/lib/checks.py` names `make verify` while being reached BY `make verify`, which
    is a real shape on this repository. A target this walk already resolved directly has been
    reported on its own terms; a second, weaker account of it reads as a chain nobody followed
    when it is the one already above. The `make eval` remedy in the same file must survive.
    """
    repo = make_repo(tmp_path, "selfref")
    write(repo, "Makefile",
          "verify:\n\t@python3 gate.py\n\t@python3 missing/thing.py\n"
          "eval:\n\t@python3 spend.py\n")
    write(repo, "gate.py", "print('see `make verify`; when stale run `make eval`')\n")
    write(repo, "spend.py", "import fanout\nfanout.run_council([])\n")
    commit_all(repo, "gates")
    found = _found(repo, ["make", "verify"])
    assert len([f for f in found if f.startswith(gate.REMEDY)]) == 1, found
    assert len([f for f in found if f.startswith(gate.UNRESOLVED)]) == 1, found
    assert not any("`make verify` as the remedy" in f for f in found), found


def test_a_remedy_is_still_found_when_an_earlier_step_already_opened_the_file(tmp_path):
    """`seen_files` answers "have I opened this", and `_scan_remedies` asks a different
    question — "which makefile's targets does it document" — that a step running the file
    DIRECTLY arrives with no makefile to answer. Memoizing the two together let step order
    decide the verdict: the same tree, the same second step, and a clean `()`.
    """
    repo = make_repo(tmp_path, "twostep")
    write(repo, "Makefile", "verify:\n\t@python3 gate.py\neval:\n\t@python3 spend.py\n")
    write(repo, "gate.py", "print('stale — run `make eval`')\n")
    write(repo, "spend.py", "import fanout\nfanout.run_council([])\n")
    commit_all(repo, "gates")
    alone = _found(repo, ["make", "verify"])
    after = _found(repo, ["python3", "gate.py"], ["make", "verify"])
    assert [f.split(":")[0] for f in alone] == ["remedy"], alone
    assert [f.split(":")[0] for f in after] == ["remedy"], after


def test_a_plain_verify_command_is_not_flagged(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "Makefile", "verify:\n\t@pytest -q\n")
    commit_all(repo, "gates")
    assert _found(repo, ["make", "verify"]) == ()


def test_a_hermetic_run_of_the_same_entry_point_is_not_flagged(tmp_path):
    """The discrimination check for the entry-point rule, against the one file that decides
    it: `--skill` spends and `--self-test` does not, and both reach `scripts/eval_harness.py`.
    Without this, §5.2's steer to `make verify` would refuse itself on this repository, whose
    `eval-test` target runs that exact file hermetically inside `verify`.
    """
    repo = _repo_shaped_like_this_one(tmp_path)
    spends = [f for f in _found(repo, ["make", "eval"]) if f.startswith("spends:")]
    assert len(spends) == 1 and "eval_harness.py" in spends[0], spends
    assert _found(repo, ["make", "eval-test"]) == ()


def test_a_hermetic_reach_does_not_certify_the_same_file_for_the_rest_of_the_walk(tmp_path):
    """Order dependence, in the shape this repository has: `verify` reaches
    `scripts/eval_harness.py --self-test` through `eval-test` before anything else touches
    it. A reader that remembered "already looked at" rather than "already looked at, and it
    was hermetic" would then call a later spending reach of the same file clean, and which
    prerequisite make happens to walk first would decide whether a run is refused.
    """
    repo = _repo_shaped_like_this_one(tmp_path)
    write(repo, "Makefile", _MAKEFILE + "\nboth: eval-test eval\n")
    commit_all(repo, "both")
    spends = [f for f in _found(repo, ["make", "both"]) if f.startswith("spends:")]
    assert len(spends) == 1 and "eval_harness.py" in spends[0], spends


def test_makes_own_recipe_modifiers_are_not_read_as_the_program(tmp_path):
    """`@`, `-` and `+` at the head of a recipe are make's, not the shell's. Measured: with
    the `-` left on, the program token is `-python3`, no interpreter matches it, and the
    script it runs is never opened — a recipe marked "ignore errors" would be the way to hide
    a provider call from this reader."""
    repo = make_repo(tmp_path)
    write(repo, "Makefile", "verify:\n\t-python3 scripts/spender.py\n")
    write(repo, "scripts/spender.py", "import fanout\nfanout.run_council([])\n")
    commit_all(repo, "gates")
    assert [f.split(":")[0] for f in _found(repo, ["make", "verify"])] == ["spends"]


def test_a_shell_dollar_in_a_recipe_is_not_read_as_an_unexpanded_make_reference(tmp_path):
    """`$$` is make's escape for a literal `$`. Read as a make reference it makes every shell
    command substitution in the tree report unresolved — this repository's own `bats-test`
    recipe is `out=$$(bash …)`, inside `verify`, so the target §5.2 steers to would come back
    unresolvable."""
    repo = make_repo(tmp_path)
    write(repo, "Makefile", "verify:\n\t@out=$$(python3 scripts/ok.py); echo $$out\n")
    write(repo, "scripts/ok.py", "print('fine')\n")
    commit_all(repo, "gates")
    assert _found(repo, ["make", "verify"]) == ()


def test_a_file_handed_to_a_runner_as_data_is_not_read_as_the_program(tmp_path):
    """An accepted fail-open, pinned so it stays a decision. `uvx --with pytest pytest -q
    tests/x.py` runs `pytest`, and pytest really does import `tests/x.py` — so a marker in
    there is a spend this reader misses.

    Searching past the first operand instead was measured against this repository and refused:
    `make council-test` hands pytest `tests/test_council_characterization.py`, which calls
    `run_council` against stub specs and spends nothing, and every `make verify` would be
    refused on it — the target §5.2 steers the operator TO. The pair is not separable from a
    static read, and the direction that keeps the steer usable is this one.
    """
    repo = make_repo(tmp_path)
    write(repo, "Makefile", "verify:\n\t@uvx --with pytest pytest -q tests/x.py\n")
    write(repo, "tests/x.py", "import fanout\nfanout.run_council([], retries=0)\n")
    commit_all(repo, "gates")
    assert _found(repo, ["make", "verify"]) == ()


def test_a_provider_cli_named_by_the_verify_argv_itself_is_refused(tmp_path):
    repo = make_repo(tmp_path)
    found = _found(repo, ["claude", "-p", "review the diff"])
    assert [f.split(":")[0] for f in found] == ["spends"], found
    assert "the gate's own program" in found[0], found
    assert _found(repo, ["pytest", "-q"]) == ()


def test_a_provider_cli_behind_a_wrapper_in_the_same_argv_is_refused(tmp_path):
    """A second rule, and the reason it is not the first: an argv is the user's own confirmed
    command, short and read whole, so every token is checked. A RECIPE is checked at program
    position only, because this repository's own Makefile carries `--providers claude` as an
    argument to something else and a whole-line rule would refuse every flag like it."""
    repo = make_repo(tmp_path)
    found = _found(repo, ["timeout", "600", "claude", "-p", "review"])
    assert [f.split(":")[0] for f in found] == ["spends"], found
    assert "the argv names" in found[0], found
    assert _found(repo, ["timeout", "600", "pytest", "-q"]) == ()


def test_a_recipe_whose_program_is_a_provider_cli_is_refused(tmp_path):
    """A provider named as a PROGRAM, told apart from one named as an argument — this
    repository's own Makefile carries `--providers claude` in a target that spends for a
    different reason, and reading that as an invocation would flag every `--providers` flag
    anyone writes."""
    repo = make_repo(tmp_path)
    write(repo, "Makefile", "gate:\n\t@claude -p 'review'\nplain:\n\t@./run.sh --providers claude\n")
    write(repo, "run.sh", "#!/bin/sh\nexit 0\n")
    commit_all(repo, "gates")
    assert any(f.startswith("spends:") for f in _found(repo, ["make", "gate"]))
    assert _found(repo, ["make", "plain"]) == ()


def test_a_recipe_naming_a_script_the_tree_does_not_hold_is_unresolved(tmp_path):
    """Fail closed by SAYING SO. The plan's own fixture is this shape — a recipe running
    `scripts/receipt_gate.py` that was never written — and a reader that silently answered
    `()` for it would certify a target it never opened."""
    repo = make_repo(tmp_path)
    write(repo, "Makefile", "precommit:\n\t@python3 scripts/receipt_gate.py\n")
    commit_all(repo, "gates")
    found = _found(repo, ["make", "precommit"])
    assert [f.split(":")[0] for f in found] == ["unresolved"], found
    assert "scripts/receipt_gate.py" in found[0]


def test_the_same_recipe_with_the_script_present_resolves(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "Makefile", "precommit:\n\t@python3 scripts/receipt_gate.py\n")
    write(repo, "scripts/receipt_gate.py", "print('receipts are fresh')\n")
    commit_all(repo, "gates")
    assert _found(repo, ["make", "precommit"]) == ()


def test_a_target_the_makefile_does_not_define_is_unresolved(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "Makefile", "verify:\n\t@true\n")
    commit_all(repo, "gates")
    found = _found(repo, ["make", "gate"])
    assert [f.split(":")[0] for f in found] == ["unresolved"], found
    assert _found(repo, ["make", "verify"]) == ()


def test_a_missing_makefile_is_unresolved_rather_than_clean(tmp_path):
    repo = make_repo(tmp_path)
    found = _found(repo, ["make", "verify"])
    assert [f.split(":")[0] for f in found] == ["unresolved"], found


def test_a_recipe_this_reader_cannot_expand_is_unresolved(tmp_path):
    """`define`/`$(call …)` bodies are the bound this reader declares rather than guesses at,
    and it is a real one: of the four `$(call RUN_PYTEST,…)` sites in this repository's
    Makefile, three sit inside `make verify`. Paired with the same recipe written out, which
    resolves."""
    repo = make_repo(tmp_path)
    write(repo, "Makefile",
          "define RUN_IT\n\t@python3 $(1)\nendef\n"
          "macro:\n\t$(call RUN_IT,scripts/x.py)\n"
          "plain:\n\t@python3 scripts/x.py\n")
    write(repo, "scripts/x.py", "print('ok')\n")
    commit_all(repo, "gates")
    found = _found(repo, ["make", "macro"])
    assert [f.split(":")[0] for f in found] == ["unresolved"], found
    assert "RUN_IT" in found[0]
    assert _found(repo, ["make", "plain"]) == ()


def test_an_automatic_make_variable_fails_closed_like_every_other_unexpanded_one(tmp_path):
    """`$<` is ordinary make and `python3 $<` is a recipe that runs something. The reader
    resolves neither, and the asymmetry is what made it a fail-OPEN rather than a declared
    bound: `$(NOPE)` reports unresolved, while `$<` matched no unexpanded-reference pattern AND
    no path token, so the recipe read as running nothing at all.

    Three shapes in one tree, each the same recipe: the automatic variable, the undefined
    `$(…)` reference that already failed closed, and the prerequisite written out — which
    resolves to the file that spends, so the fixture is known to be capable of a finding.
    """
    repo = make_repo(tmp_path)
    write(repo, "Makefile",
          "SCRIPT := scripts/spend.py\n"
          "auto: $(SCRIPT)\n\t@python3 $<\n"
          "undef:\n\t@python3 $(NOPE)\n"
          "plain:\n\t@python3 scripts/spend.py\n")
    write(repo, "scripts/spend.py", "import fanout\nfanout.run_council([])\n")
    commit_all(repo, "gates")
    auto = _found(repo, ["make", "auto"])
    assert [f.split(":")[0] for f in auto] == ["unresolved"], auto
    assert "$<" in auto[0], auto
    assert [f.split(":")[0] for f in _found(repo, ["make", "undef"])] == ["unresolved"]
    assert [f.split(":")[0] for f in _found(repo, ["make", "plain"])] == ["spends"]


def test_a_recipe_that_changes_directory_is_followed_where_make_C_is(tmp_path):
    """§5.1 names `cd frontend && npm ci` as the shape real monorepos have, and a recipe's `cd`
    moves what every path after it means exactly as `make -C` does. Resolved against the
    unmoved directory the script simply is not there, and a token with no `/` in it is not even
    reported missing — so the recipe came back clean.

    Controls: the same file reached without the `cd` is refused, and a `cd` appearing later in
    the shell line — where following it needs the shell this module will not run — is reported
    rather than guessed at.
    """
    repo = make_repo(tmp_path)
    write(repo, "Makefile",
          "lead:\n\tcd tools && python3 spend.py\n"
          "flat:\n\t@python3 tools/spend.py\n"
          "mid:\n\t@python3 ok.py; cd tools && python3 spend.py\n"
          "gone:\n\tcd nowhere && python3 spend.py\n")
    write(repo, "tools/spend.py", "import fanout\nfanout.run_council([])\n")
    write(repo, "ok.py", "print('fine')\n")
    commit_all(repo, "gates")
    assert [f.split(":")[0] for f in _found(repo, ["make", "lead"])] == ["spends"]
    assert [f.split(":")[0] for f in _found(repo, ["make", "flat"])] == ["spends"]
    mid = _found(repo, ["make", "mid"])
    assert [f.split(":")[0] for f in mid] == ["unresolved"], mid
    assert "part-way through" in mid[0], mid
    gone = _found(repo, ["make", "gone"])
    assert [f.split(":")[0] for f in gone] == ["unresolved"], gone
    assert "cd nowhere" in gone[0], gone


def test_a_provider_cli_after_a_cd_is_still_the_program_that_runs(tmp_path):
    """The `cd` above consumes the head of the line, and the provider check must not be reading
    what is left of it. Paired with the flag-shaped argument this repository's own Makefile
    carries, which must stay admitted."""
    repo = make_repo(tmp_path)
    write(repo, "Makefile",
          "gate:\n\tcd tools && claude -p 'review'\n"
          "plain:\n\tcd tools && ./run.sh --providers claude\n")
    write(repo, "tools/run.sh", "#!/bin/sh\nexit 0\n")
    commit_all(repo, "gates")
    assert [f.split(":")[0] for f in _found(repo, ["make", "gate"])] == ["spends"]
    assert _found(repo, ["make", "plain"]) == ()


def test_a_path_outside_the_repository_is_never_opened(tmp_path):
    """Containment is `bundle._assert_contained`, for the reason preflight imports it too.
    The escaping file really does carry the marker, so a reader that joined the token onto
    the root without checking would flag it — and would have read a host path to do so."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "spender.py").write_text("import fanout\nfanout.run_council([])\n")
    repo = make_repo(tmp_path)
    write(repo, "Makefile", "verify:\n\t@python3 ../outside/spender.py\n")
    write(repo, "inside.py", "import fanout\nfanout.run_council([])\n")
    commit_all(repo, "gates")
    assert _found(repo, ["make", "verify"]) == ()
    write(repo, "Makefile", "verify:\n\t@python3 inside.py\n")
    commit_all(repo, "gates2")
    assert any(f.startswith("spends:") for f in _found(repo, ["make", "verify"])), \
        "the same marker inside the tree is found, so the miss above is containment"


def test_the_detector_runs_nothing_the_repository_supplied(tmp_path):
    """§5 step 1: no arbitrary project setup code runs before authorization, and this is the
    function most tempted to break it — the cheapest way to learn what a target does is to
    run it.

    Two suppliers, each with a control showing the same recipe or the same hook DOES fire
    when something really runs them — without those, a fixture whose recipe was inert would
    read as a detector that stayed its hand. `core.fsmonitor` is the second supplier because
    git runs it on the caller's behalf rather than the caller running it, which is what makes
    it easy to miss.

    The hook control also states the bound honestly. Measured on git 2.53.0, the hook fires
    under `git status` and NOT under `rev-parse --show-toplevel`, which is the only git call
    this module makes — so today that assertion holds because of the command chosen, and
    `NO_DAEMON_CACHE` on it is what keeps holding it for the next command added.
    """
    repo = make_repo(tmp_path)
    hook = write(repo, "fsmonitor.sh", f"#!/bin/sh\n: > {repo}/HOOK-RAN\nprintf ''\n")
    hook.chmod(0o755)
    write(repo, "Makefile", f"verify:\n\t@: > {repo}/RECIPE-RAN\n")
    commit_all(repo, "gates")
    _git(repo, "config", "core.fsmonitor", str(hook))

    assert _found(repo, ["make", "verify"]) == ()
    assert not (repo / "RECIPE-RAN").exists(), "the detector ran the recipe"
    assert not (repo / "HOOK-RAN").exists(), "git ran the repository's fsmonitor for us"

    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run(["make", "-C", str(repo), "verify"], check=True, capture_output=True, env=env)
    assert (repo / "RECIPE-RAN").exists(), \
        "the control failed: this recipe does nothing even when make runs it"
    _git(repo, "status", "--porcelain")
    assert (repo / "HOOK-RAN").exists(), \
        "the control failed: this fsmonitor does nothing even when an ordinary git runs it"


def test_the_detector_refuses_something_that_is_not_a_parsed_command(tmp_path):
    repo = make_repo(tmp_path)
    with pytest.raises(gate.GateError):
        gate.provider_invoking_verify(repo, [["make", "verify"]])
    with pytest.raises(gate.GateError):
        gate.provider_invoking_verify(tmp_path / "not-a-repo", verify.Command.parse([["make"]]))


def test_a_facade_over_the_council_engine_is_reached_through_its_own_name():
    """The second spend symbol earns its place on a file in this tree that the first misses:
    `shared/skills/llm-council/scripts/fanout.py` is a `sys.modules` swap onto
    `council.engine` and contains no `run_council` at all, so a one-symbol reader calls this
    repository's most expensive target clean. Matched as a LITERAL, because as a regex
    `council.engine` also matches the PATH `council/engine.py` that `scripts/lib/checks.py`
    carries in a closure list while spending nothing.
    """
    found = _found(ROOT, ["make", "smoke-llm-council"])
    spends = [f for f in found if f.startswith("spends:")]
    assert len(spends) == 1 and "fanout.py" in spends[0], found
    assert "council.engine" in spends[0]


def test_this_repositorys_own_precommit_reaches_the_council(tmp_path):
    """§5.2's claim is about THIS repository, so it is measured against it rather than only
    against a fixture. `make precommit` -> `scripts/lib/checks.py` -> `make eval` ->
    `scripts/eval_harness.py` -> `run_council`.

    What this also pins is the limit reported alongside it: `make verify` produces the same
    remedy line, because the only difference between the two is `advisory=True` and
    `advisory=False` inside a `python3 -c` one-liner — a runtime exit code, which no static
    read separates. Neither is refused; both are priced.
    """
    both = {t: _found(ROOT, ["make", t]) for t in ("precommit", "verify")}
    for target, found in both.items():
        remedy = [f for f in found if f.startswith("remedy:")]
        assert len(remedy) == 1, (target, found)
        assert "scripts/lib/checks.py" in remedy[0] and "eval_harness.py" in remedy[0]
        assert not [f for f in found if f.startswith("spends:")], (target, found)
    assert any(f.startswith("spends:") for f in _found(ROOT, ["make", "eval"])), \
        "the target the remedy names does spend, or the remedy line above means nothing"


def test_a_remedy_findings_order_dependence_never_loses_the_spending_finding(tmp_path):
    """Whether a remedy is reported depends on prerequisite order — declared at the guard.

    What must NOT depend on order is the finding that decides a refusal. Both spellings are
    asserted here so the day someone makes the walk order-independent, the weaker claim is
    what changes and the stronger one is already pinned.
    """
    repo = make_repo(tmp_path)
    write(repo, "scripts/spend.py", "# run_council\n")
    for order, name in ((("a", "b"), "ab"), (("b", "a"), "ba")):
        write(repo, "Makefile",
              f"verify: {order[0]} {order[1]}\n"
              "a:\n\t@python3 scripts/gate.py\n"
              "b:\n\t@python3 scripts/spend.py\n")
        write(repo, "scripts/gate.py", "# remedy: run 'make b'\n")
        commit_all(repo, f"order {name}")
        found = gate.provider_invoking_verify(repo, verify.Command.parse([["make", "verify"]]))
        spends = [f for f in found if f.startswith(gate.SPENDS)]
        assert len(spends) == 1, (name, found)


# --------------------------------------------------------------------------- the gate

def _state(monkeypatch, tmp_path):
    """Point the run directory at the fixture's own tree. Every `open_run` below writes one,
    and `storage.run_root` reads XDG_STATE_HOME at call time."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return tmp_path / "state" / "khenrix-forge"


def _report_and_quote(tmp_path):
    r = _report(tmp_path)
    return r, gate.quote(r, seat_timeout_sec=3600)


AUTHOR = ("Ada Lovelace", "ada@example.invalid")


def _answers(**kw):
    """A complete answer sheet for §5 step 2 — both commands, both policies, the author, and
    §13.1's opt-in. `deep_review=True` matches the quote every helper here builds, because
    `gate.quote` defaults §13.1 on and `confirm` refuses an answer that contradicts the price
    the operator was shown."""
    return {"setup": [["true"]], "verify": [["true"]],
            "on_calibration_failure": "abort", "strategy": "size-gated",
            "author": AUTHOR, "deep_review": True, **kw}


def _confirmation(**kw):
    """A complete `Confirmation`, with any single field replaced by `kw`."""
    fields = dict(setup=(verify.Step(argv=("true",)),), verify=(verify.Step(argv=("true",)),),
                  on_calibration_failure="abort", strategy="size-gated", accepted_gaps=(),
                  author=AUTHOR, seats=3, attempts=3, review_rounds=2, synthesis_fix_cap=3,
                  deep_review=True)
    return gate.Confirmation(**{**fields, **kw})


def test_a_repository_whose_gate_cannot_be_seen_is_shown_to_the_human(tmp_path):
    """The condition the qualified-PASS ruling rests on: a verdict that says the gate surface
    was measured and unchanged is honest only if the operator was told once, before a token
    was spent, that the engine can see no gate here.

    Pinned by the gap ID rather than by the word "gate", which appears in half the lines this
    function emits — including the line that fires when the surface is NOT empty. The
    discrimination check is that same repository given a Makefile and a discovered test file.
    """
    bare = make_repo(tmp_path, "bare")
    r = preflight.inspect_repo(bare)
    cmd = verify.Command.parse([["true"]])
    shown = gate.must_show(r, gate.quote(r, seat_timeout_sec=3600), cmd, setup=verify.Command.parse([['true']]))
    assert any(gate.GATE_SURFACE_EMPTY in line for line in shown), shown

    gated = make_repo(tmp_path, "gated")
    write(gated, "Makefile", "verify:\n\techo ok\n")
    write(gated, "tests/test_x.py", "def test_x():\n    pass\n")
    commit_all(gated, "a gate the rules can see")
    r2 = preflight.inspect_repo(gated)
    shown2 = gate.must_show(r2, gate.quote(r2, seat_timeout_sec=3600), cmd, setup=verify.Command.parse([['true']]))
    assert not any(gate.GATE_SURFACE_EMPTY in line for line in shown2), shown2
    assert any("Makefile" in line and "tests/test_x.py" in line for line in shown2), shown2


def test_the_empty_surface_is_measured_with_the_command_or_it_is_a_false_alarm(tmp_path):
    """Why `must_show` takes the command and not only the report.

    A repository whose gate is a named script has an EMPTY surface until the command names
    it — §6.1's role rules reach `Makefile` and `tests/test_*.py` by name, and `check.sh` by
    nothing. So the line above, resolved without the command, would fire on an ordinary
    repository whose gate the engine can see perfectly well, and a warning that is usually
    wrong is one an operator learns to click past. Both halves are measured here rather than
    asserted, because it is the DIFFERENCE that decides the argument.
    """
    repo = make_repo(tmp_path)
    write(repo, "check.sh", "#!/bin/sh\nexit 0\n")
    commit_all(repo, "a gate no naming rule reaches")
    r = preflight.inspect_repo(repo)

    assert verify.gate_surface(r.facts.root, r.contract) == (), \
        "the premise: with no command this repository's gate surface is empty"
    assert verify.gate_surface(r.facts.root, r.contract,
                               command=verify.Command.parse([["./check.sh"]])) == ("check.sh",)

    shown = gate.must_show(r, gate.quote(r, seat_timeout_sec=3600), verify.Command.parse([["./check.sh"]]), setup=verify.Command.parse([['true']]))
    assert not any(gate.GATE_SURFACE_EMPTY in line for line in shown), shown


def test_the_generator_contract_line_is_read_off_the_report_it_was_handed(tmp_path):
    """Which verify-origin rewrites a run admits is what §7.2 decides a PASS by, so the line
    the operator reads about it has to come from the contract in hand rather than from a
    sentence that happens to be true of every repository today.

    `detect_generators` returns the empty contract for every repository it can read, so an
    asserted line and a derived one agree everywhere the suite can go — which is exactly the
    condition under which the assertion goes on being emitted after it stops being true.

    The report is REPLACED for the same reason `test_the_manifest_records_the_contract_the_
    report_carried` replaces one: no fixture repository yields a non-empty contract, so the
    derivation is unpinnable from a real tree and a hardcoded line is an equivalent mutant
    against every other test in this file.
    """
    r = _report(tmp_path)
    cmd = verify.Command.parse([["true"]])
    assert r.contract.relations == (), "the premise: a real repository declares none"
    assert any(gate.GENERATOR_CONTRACT_EMPTY in line for line in gate.must_show(r, gate.quote(r, seat_timeout_sec=3600),
                                                                                cmd, setup=verify.Command.parse([['true']])))

    declared = dataclasses.replace(
        r, contract=r.contract.__class__(id="render-x", relations=(("shared/**", "gen/**"),)))
    shown = gate.must_show(declared, gate.quote(declared, seat_timeout_sec=3600), cmd, setup=verify.Command.parse([['true']]))
    assert not any(gate.GENERATOR_CONTRACT_EMPTY in line for line in shown), shown
    assert any("render-x" in line and "shared/** -> gen/**" in line for line in shown), \
        "an operator agreeing to a contract has to be shown which rewrites it admits"


def test_reading_the_surface_at_this_gate_runs_nothing_the_repository_supplied(tmp_path):
    """§5 step 1 admits no repository-supplied code before authorization, and `must_show` runs
    BEFORE the operator answers — so the rule still binds here.

    `core.fsmonitor` is the supplier, because git runs it on the caller's behalf. Measured on
    git 2.53.0: LOADING AN INDEX runs the program, so `ls-files` does in every form and
    `rev-parse --show-toplevel` does not — the surface read is one of several calls in this
    package to have needed `NO_DAEMON_CACHE` for a reason that is not caching. The control
    shows the same hook firing under an ordinary git.
    """
    repo = make_repo(tmp_path)
    hook = write(repo, "fsmonitor.sh", f"#!/bin/sh\n: > {repo}/HOOK-RAN\nprintf ''\n")
    hook.chmod(0o755)
    commit_all(repo, "monitor")
    _git(repo, "config", "core.fsmonitor", str(hook))

    r = preflight.inspect_repo(repo)
    gate.must_show(r, gate.quote(r, seat_timeout_sec=3600), verify.Command.parse([["true"]]), setup=verify.Command.parse([['true']]))
    assert not (repo / "HOOK-RAN").exists(), "git ran the repository's fsmonitor for us"

    _git(repo, "status", "--porcelain")
    assert (repo / "HOOK-RAN").exists(), \
        "the control failed: this fsmonitor does nothing even when an ordinary git runs it"


def test_every_gap_the_gate_shows_is_one_the_confirmation_can_cite(tmp_path):
    """The four lines are ids, not sentences, so what a handover records resolves to a line
    the operator actually saw.

    Both directions. Every `gap <id>` `must_show` emits is a value `confirm` accepts — an id
    shown and then refused is a gap nobody could ever accept — and the three the engine
    carries whatever the repository looks like are all present, so a dropped one fails here
    rather than in a handover that never mentions it.
    """
    r, q = _report_and_quote(tmp_path)
    shown = gate.must_show(r, q, verify.Command.parse([["true"]]), setup=verify.Command.parse([['true']]))
    named = {line.split(":")[0][len("gap "):] for line in shown if line.startswith("gap ")}
    assert named <= set(gate.ACCEPTABLE_GAPS), named
    assert {gate.REMOTES_AND_CONFIGURATION,
            gate.GENERATOR_CONTRACT_EMPTY} <= named, named
    # THE RETIRED ONE, ASSERTED ABSENT FROM BOTH. `--gc` is built, so `must_show` raises no
    # such line and a `Confirmation` may no longer cite it: an accepted gap has to resolve to
    # something the operator was shown. The constant stays defined for the runs that recorded
    # it before that commit.
    assert gate.GC_UNBUILT not in named, named
    assert gate.GC_UNBUILT not in gate.ACCEPTABLE_GAPS
    for gap in named:
        assert gate.confirm(r, q, _answers(accepted_gaps=[gap])).accepted_gaps == (gap,)


def test_the_gate_shows_the_refusals_the_operator_may_not_answer_past(tmp_path):
    """§2.3's list fails closed, so the operator meets it as a line rather than as "why did it
    stop". The discrimination check is the same repository without the bit."""
    repo = make_repo(tmp_path)
    clean = preflight.inspect_repo(repo)
    assert preflight.refusals(clean) == ()
    _git(repo, "update-index", "--skip-worktree", "seed.txt")
    r = preflight.inspect_repo(repo)

    shown = gate.must_show(r, gate.quote(r, seat_timeout_sec=3600), verify.Command.parse([["true"]]), setup=verify.Command.parse([['true']]))
    assert set(preflight.refusals(r)) <= set(shown), shown
    assert any("§2.3" in line and "open_run" in line for line in shown), shown
    assert not any("§2.3" in line and "open_run" in line
                   for line in gate.must_show(clean, gate.quote(clean, seat_timeout_sec=3600),
                                              verify.Command.parse([["true"]]), setup=verify.Command.parse([['true']])))


def test_the_quote_shown_is_the_quote_that_was_priced(tmp_path):
    """`must_show` carries the quote whole rather than re-deriving it, so a run priced with
    `--skip-deep-review` is not shown the default's numbers. Differenced against the same report,
    which is the only way to show which quote the lines came from."""
    r = _report(tmp_path)
    on, off = gate.quote(r, seat_timeout_sec=3600), gate.quote(r, deep_review=False, seat_timeout_sec=3600)
    cmd = verify.Command.parse([["true"]])
    assert set(on.lines) <= set(gate.must_show(r, on, cmd, setup=verify.Command.parse([['true']])))
    assert set(off.lines) <= set(gate.must_show(r, off, cmd, setup=verify.Command.parse([['true']])))
    assert on.lines != off.lines, "the premise: --skip-deep-review moves the lines it is shown by"
    assert "provider call(s)" in " ".join(gate.must_show(r, on, cmd, setup=verify.Command.parse([['true']])))
    assert "not run" in " ".join(gate.must_show(r, off, cmd, setup=verify.Command.parse([['true']]))), \
        "a run priced without the deep review is shown that it was excluded"


def test_the_gate_shows_a_verify_command_that_spends_rather_than_only_pricing_it(tmp_path):
    """§5.2's finding is what an operator prices the run against, so it belongs beside the
    quote and not in a log. The discrimination check is the same repository with a command
    that reaches nothing."""
    repo = _repo_shaped_like_this_one(tmp_path)
    r = preflight.inspect_repo(repo)
    q = gate.quote(r, seat_timeout_sec=3600)
    shown = gate.must_show(r, q, verify.Command.parse([["make", "eval"]]), setup=verify.Command.parse([['true']]))
    assert any(line.startswith(gate.SPENDS) for line in shown), shown
    assert not any(line.startswith(gate.SPENDS)
                   for line in gate.must_show(r, q, verify.Command.parse([["true"]]), setup=verify.Command.parse([['true']])))


def test_must_show_refuses_what_it_cannot_speak_for(tmp_path):
    r, q = _report_and_quote(tmp_path)
    cmd = verify.Command.parse([["true"]])
    with pytest.raises(gate.GateError):
        gate.must_show(r.facts, q, cmd, setup=verify.Command.parse([['true']]))
    with pytest.raises(gate.GateError):
        gate.must_show(r, q.lines, cmd, setup=verify.Command.parse([['true']]))
    with pytest.raises(gate.GateError):
        # An unparsed spec: the surface would resolve against no command and report the empty
        # condition for any repository at all.
        gate.must_show(r, q, [["true"]], setup=verify.Command.parse([['true']]))


def test_the_confirmation_records_both_policies_or_refuses(tmp_path):
    """§5 asks once. A missing policy cannot be defaulted — the default IS the decision, and
    it would be the reader's rather than the operator's.

    Each is dropped on its own, so a check that happens to cover one covers neither by
    accident, and the complete sheet is confirmed as the discrimination check.
    """
    r, q = _report_and_quote(tmp_path)
    with pytest.raises(gate.GateError):
        gate.confirm(r, q, {"setup": [["true"]], "verify": [["true"]]})
    for dropped in ("on_calibration_failure", "strategy", "setup", "verify"):
        answers = _answers()
        del answers[dropped]
        with pytest.raises(gate.GateError, match=dropped):
            gate.confirm(r, q, answers)
    c = gate.confirm(r, q, _answers())
    assert (c.on_calibration_failure, c.strategy) == ("abort", "size-gated")


def test_a_policy_value_no_later_phase_can_act_on_is_refused(tmp_path):
    """Recording an answer nobody can apply is the same failure as not asking: §5 step 5 says
    the decision is not re-asked, so a value no phase branches on would have to be.

    `partition` is the case worth naming — §12.1 admits one only where stable seams exist,
    which §10.1 forbids presenting as a checked predicate, so it cannot be pre-committed to
    at a gate that has measured no artifact.
    """
    r, q = _report_and_quote(tmp_path)
    for bad in ("continue as degraded", "ABORT", "", None):
        with pytest.raises(gate.GateError):
            gate.confirm(r, q, _answers(on_calibration_failure=bad))
    for bad in ("partition", "whatever the seats agree on", ""):
        with pytest.raises(gate.GateError):
            gate.confirm(r, q, _answers(strategy=bad))
    assert gate.confirm(r, q, _answers(on_calibration_failure="degraded",
                                       strategy="base-and-port")).strategy == "base-and-port"


def test_a_verify_command_that_names_no_step_is_not_a_gate(tmp_path):
    """§6.2 reads every outcome off this command's exit code, so a command with no step passes
    every candidate — including one that deleted the tests. An empty SETUP is an ordinary
    repository that needs none, and is the discrimination check."""
    r, q = _report_and_quote(tmp_path)
    with pytest.raises(gate.GateError):
        gate.confirm(r, q, _answers(verify=[]))
    assert gate.confirm(r, q, _answers(setup=[])).setup == ()


def test_a_per_step_cwd_reaches_the_manifest_only_through_a_step(tmp_path, monkeypatch):
    """§5.1's motivating example is `cd frontend && npm ci`, and `verify.Command.parse` takes
    a list of argv lists with nowhere to put a cwd — so the argv route cannot express one, and
    a run over a real monorepo has to hand `confirm` built `verify.Step`s.

    Both routes are measured rather than described: the argv route records `Step`'s own
    defaults, and the Step route records what the caller chose, all four fields.
    """
    _state(monkeypatch, tmp_path)
    r, q = _report_and_quote(tmp_path)
    assert gate.confirm(r, q, _answers()).verify[0].cwd == "", \
        "the gap: nothing in an argv list is a cwd"

    step = verify.Step(argv=("npm", "ci"), cwd="frontend", env={"CI": "1"}, timeout=90)
    c = gate.confirm(r, q, _answers(setup=[step], verify=[verify.Step(argv=("true",))]))
    assert c.setup == (step,)
    back = runstate.read_manifest(gate.open_run(r, c, "r1", quote_=gate.quote(r, seat_timeout_sec=3600))).setup[0]
    assert (back.argv, back.cwd, back.env, back.timeout) == \
        (("npm", "ci"), "frontend", {"CI": "1"}, 90)


def test_half_a_command_each_way_is_refused(tmp_path):
    """A `Step` carries the cwd, env and timeout a caller chose; an argv list takes the
    defaults. Mixed, which of the two a given step was agreed under depends on how it happened
    to be written, and the manifest records the result as one agreement."""
    r, q = _report_and_quote(tmp_path)
    with pytest.raises(gate.GateError, match="mixes"):
        gate.confirm(r, q, _answers(verify=[verify.Step(argv=("a",)), ["b"]]))


def test_a_confirmed_run_writes_its_manifest_exactly_once(tmp_path, monkeypatch):
    """§14.2: written once at `confirmed`, never rewritten, so commands are never re-detected.

    TWO kernel refusals stand behind that, not one, and the plan's expectation of which fires
    is measured here rather than assumed. A repeated `open_run` never reaches the manifest:
    `storage.run_root` refuses the taken run id first — and if it did not, B's ref is created
    with a compare-and-swap against "must not already exist", which would refuse next. The
    manifest's own `os.link` refusal is real all the same, and is exercised directly against
    the run that already has one.
    """
    _state(monkeypatch, tmp_path)
    r, q = _report_and_quote(tmp_path)
    c = gate.confirm(r, q, _answers())
    run = gate.open_run(r, c, "r1", quote_=gate.quote(r, seat_timeout_sec=3600))
    assert runstate.read_manifest(run).verify == c.verify
    with pytest.raises(gate.GateError, match="already has a directory"):
        gate.open_run(r, c, "r1", quote_=gate.quote(r, seat_timeout_sec=3600))
    with pytest.raises(runstate.ManifestError):
        runstate.write_manifest(run, runstate.read_manifest(run))


def test_the_manifest_records_the_repository_git_names(tmp_path, monkeypatch):
    """A caller may run preflight on a SUBDIRECTORY — `Report` documents that — and `drift`
    resolves `repo_path` through `rev-parse --show-toplevel`. A manifest recording the
    directory the caller typed would therefore be refused by every later drift check as a
    repository this run was not recorded against, which is a resume that cannot start."""
    _state(monkeypatch, tmp_path)
    repo = make_repo(tmp_path, "sub-repo")
    (repo / "sub").mkdir()
    write(repo, "sub/keep.txt", "x\n")
    commit_all(repo, "a subdirectory to be run from")
    r = preflight.inspect_repo(repo / "sub")
    assert str(r.repo) != str(r.facts.root), "the premise: the caller named a subdirectory"

    run = gate.open_run(r, gate.confirm(r, gate.quote(r, seat_timeout_sec=3600), _answers()), "r1", quote_=gate.quote(r, seat_timeout_sec=3600))
    m = runstate.read_manifest(run)
    assert m.repo_path == str(r.facts.root)
    assert runstate.drift(m, repo) == (), "t0 is taken here, so nothing has moved yet"


def test_the_run_records_the_baseline_it_was_opened_over(tmp_path, monkeypatch):
    """§9 whitelists forge's ref by exact name AND the OID recorded at creation, and only
    `materialize`'s return value knows that OID — so the manifest cannot be written before B
    exists, and the snapshot beside it must already exclude B's ref or the run reports its own
    baseline as a ref the user made.

    The repository is DIRTY, which is what makes the agreement checkable: over a clean tree B1
    IS the base commit and a manifest that recorded the wrong one of the two would satisfy
    every assertion here.
    """
    _state(monkeypatch, tmp_path)
    repo = make_repo(tmp_path)
    write(repo, "seed.txt", "the user's uncommitted work\n")
    r = preflight.inspect_repo(repo)
    m = runstate.read_manifest(gate.open_run(r, gate.confirm(r, gate.quote(r, seat_timeout_sec=3600), _answers()), "r1", quote_=gate.quote(r, seat_timeout_sec=3600)))

    assert m.baseline_commit != m.base_commit, "the premise: B1 is a commit of its own"
    assert m.forge_refs == {m.baseline_ref: m.baseline_commit}
    assert m.baseline_ref not in m.protected_refs, \
        "forge's own ref in protected_refs is a run that reports its baseline as drift"
    assert _git(repo, "rev-parse", m.baseline_ref).stdout.strip() == m.baseline_commit
    assert runstate.drift(m, repo) == ()


def test_a_refused_repository_never_reaches_a_manifest(tmp_path, monkeypatch):
    """Preflight's refusals are not advice. A run that opens over them is a run whose manifest
    records an agreement about a repository the engine said it could not handle — and it
    stops before anything is written, which is measured by the state directory being empty
    rather than by the exception alone."""
    state = _state(monkeypatch, tmp_path)
    repo = make_repo(tmp_path)
    _git(repo, "update-index", "--skip-worktree", "seed.txt")
    r = preflight.inspect_repo(repo)
    assert preflight.refusals(r), "the premise: this repository is refused"

    with pytest.raises(gate.GateError):
        gate.open_run(r, _confirmation(), "r1", quote_=gate.quote(r, seat_timeout_sec=3600))
    assert not state.exists(), "a refused run left a directory behind"
    assert _git(repo, "show-ref").stdout.count("khenrix-forge") == 0, \
        "and no ref in the user's repository"


def test_the_gate_opens_a_run_on_a_repository_whose_identity_is_only_global(tmp_path,
                                                                            monkeypatch):
    """THE ORDINARY REPOSITORY, and the one no fixture in this suite could build.

    `make_repo` sets `user.name`/`user.email` LOCALLY, so every other `open_run` test here
    runs against a repository whose identity `baseline._resolve_author` can see — while
    `gitcmd` pins the global and system files to /dev/null on every call, which is where a
    real machine keeps one. This repository's own `khenrix-utils` checkout has no local
    identity, so before the `author` answer existed the gate failed on the repository forge is
    developed in.

    DIRTY, because the clean branch returns before `_resolve_author` is reached at all: over a
    clean tree B1 IS the base commit, no commit is authored, and a run opens whether or not
    any of this works. The premises are asserted rather than assumed for that reason.
    """
    _state(monkeypatch, tmp_path)
    global_identity(tmp_path, monkeypatch)
    repo = make_repo(tmp_path, local_identity=False)
    assert _git(repo, "config", "--local", "--get", "user.name",
                check=False).returncode != 0, "the premise: no LOCAL identity"
    assert _git(repo, "config", "--get", "user.name").stdout.strip() == GLOBAL_IDENTITY[0], \
        "the premise: an ordinary git DOES find one, in the global file"
    write(repo, "seed.txt", "the user's uncommitted work\n")

    r = preflight.inspect_repo(repo)
    assert r.facts.unstaged, "the premise: dirty, so B1 is a commit that must be authored"
    m = runstate.read_manifest(gate.open_run(r, gate.confirm(r, gate.quote(r, seat_timeout_sec=3600), _answers()),
                                             "r1", quote_=gate.quote(r, seat_timeout_sec=3600)))
    assert m.baseline_commit != m.base_commit, "B1 is a commit of its own, and it was authored"


def test_b1_carries_the_identity_the_operator_confirmed_and_not_the_one_lying_about(tmp_path,
                                                                                    monkeypatch):
    """The author is an ANSWER, so a route that quietly preferred the repository's own config
    would satisfy every green fixture in this suite and record the wrong person on the one
    commit §2.1 makes the user's.

    Discriminating by CONTRADICTION: the repository has a local identity, and the operator
    confirms a different one. A `materialize` call that dropped `author=` reads back
    `Fixture`; the confirmed answer reads back `Ada Lovelace`.
    """
    _state(monkeypatch, tmp_path)
    repo = make_repo(tmp_path)
    assert _git(repo, "config", "--get", "user.name").stdout.strip() == "Fixture", \
        "the premise: the repository names somebody else"
    write(repo, "seed.txt", "the user's uncommitted work\n")

    r = preflight.inspect_repo(repo)
    m = runstate.read_manifest(gate.open_run(r, gate.confirm(r, gate.quote(r, seat_timeout_sec=3600), _answers()),
                                             "r1", quote_=gate.quote(r, seat_timeout_sec=3600)))
    ident = _git(repo, "log", "-1", "--format=%an|%ae", m.baseline_commit).stdout.strip()
    assert ident == f"{AUTHOR[0]}|{AUTHOR[1]}", ident


def test_an_identity_the_gate_cannot_stand_behind_is_refused_before_anything_exists(tmp_path,
                                                                                    monkeypatch):
    """The SECOND half of the finding, and a different failure from the first: not that the
    run could not be authored, but WHERE it stopped. `materialize` raised from `open_run`
    after `storage.run_root` had made the run directory and `journal.intent` had written
    `confirm_start` — an orphan §14.1 reserves for a crash, produced on the ordinary path, and
    a retry with the same id then meeting the taken-run-id refusal instead.

    An unusable answer is refused at `confirm`, which runs before `open_run` is called at
    all, so the state directory does not merely lack a manifest — it does not exist. The
    half-identity row is the one `baseline` refuses rather than completes; the `<`/`>` and
    newline rows are refused here because git does not refuse them (measured on git 2.53.0:
    `commit-tree` with an author name `Ada <x>` exits 0 and the commit reads `Ada x`).
    """
    state = _state(monkeypatch, tmp_path)
    r, q = _report_and_quote(tmp_path)
    # `"ab"` is the string that does NOT fail on arity: it unpacks into ("a", "b"), two
    # non-empty strings carrying nothing forbidden, so every check below the string guard
    # passes it and B1 is authored `a <b>`. A longer string is refused by the unpack alone,
    # which is why a fixture made only of realistic-looking answers cannot reach that guard.
    for bad in (None, "Ada <ada@example.invalid>", "ab", b"ab", ("Ada",), ("Ada", ""),
                ("", "a@b.invalid"), ("Ada", "a@b.invalid\nname"), ("Ada <x>", "a@b.invalid")):
        with pytest.raises(gate.GateError):
            gate.confirm(r, q, _answers(author=bad))
    with pytest.raises(gate.GateError, match="unanswered"):
        gate.confirm(r, q, {k: v for k, v in _answers().items() if k != "author"})
    assert not state.exists(), \
        "an answer the gate would not record still created a run directory"

    assert gate.confirm(r, q, _answers()).author == AUTHOR, \
        "the discrimination check: an ordinary pair is taken and stripped of nothing it needs"


def test_the_gate_reads_the_identity_the_engines_own_git_is_blind_to(tmp_path, monkeypatch):
    """`propose_identity` is the one call in this package that reads the user's own config,
    and the asymmetry it exists for is measured here: the same repository and the same key,
    answered by this call and refused by `baseline._resolve_author` beside it.

    HALF AN IDENTITY IS NONE, and the control is what makes that a choice rather than a
    limitation: over the same half-configured repository `git var GIT_AUTHOR_IDENT` — the
    call `baseline`'s docstring recommended — answers a complete ident at exit 0, with an
    email it built out of user@hostname. That value would reach B1 and `git log` with nothing
    marking the invented half, which is why this reads `config --get` instead.
    """
    cfg = global_identity(tmp_path, monkeypatch)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    repo = make_repo(tmp_path, local_identity=False)

    assert gate.propose_identity(repo) == GLOBAL_IDENTITY
    assert _git(repo, "config", "--local", "--get", "user.name",
                check=False).returncode != 0, \
        "the premise: the identity is in the global file and nowhere in the repository"
    with pytest.raises(baseline.BaselineError):
        baseline._resolve_author(repo, None)

    cfg.write_text(f"[user]\n\tname = {GLOBAL_IDENTITY[0]}\n")
    assert gate.propose_identity(repo) is None, "a name with no email is not an identity"
    guessed = _git(repo, "var", "GIT_AUTHOR_IDENT").stdout
    assert guessed.startswith(GLOBAL_IDENTITY[0]) and "@" in guessed, \
        f"the control: git invents the missing half and reports success — {guessed!r}"


def test_the_seats_and_attempts_the_quote_priced_are_what_the_manifest_records(tmp_path,
                                                                              monkeypatch):
    """§5.2 answers the peak-disk figure "by parameter, not by shrinking", and the parameter
    has to survive the gate for that to be an answer. §5 step 5 forbids asking again, so a
    launcher that read a seat count from its own default would build a fleet nobody priced.

    TWO rows disjoint in both numbers, because one row cannot tell a carried field from a
    writer that hardcoded whatever that row happened to ask for. The quote LINE is read back
    beside the manifest, so a record agreeing with the confirmation while the price the
    operator actually saw disagreed with both would still fail.
    """
    _state(monkeypatch, tmp_path)
    for run_id, seats, attempts in (("r1", 2, 4), ("r2", 5, 1)):
        r = _report(tmp_path)
        q = gate.quote(r, seats=seats, attempts=attempts, review_rounds=2, seat_timeout_sec=3600)
        c = gate.confirm(r, q, _answers())
        assert (c.seats, c.attempts) == (seats, attempts), \
            "the confirmation takes the shape off the quote that was shown"
        m = runstate.read_manifest(gate.open_run(r, c, run_id, quote_=gate.quote(r, seat_timeout_sec=3600)))
        assert (m.seats, m.attempts) == (seats, attempts)
        assert f"builders {seats}x{attempts}={seats * attempts}" in q.lines[0], \
            "and the price the operator read was computed from the same two numbers"


@pytest.mark.parametrize("value", [0, -1, True, 2.0, "3", None])
@pytest.mark.parametrize("field", ["seats", "attempts"])
def test_a_quote_whose_shape_is_not_a_count_never_becomes_a_record(tmp_path, field, value):
    """The gate and the manifest decide the same thing about the same two fields, so they
    decide it with the same predicate — `runstate.count`, called rather than re-spelled.

    THE ROWS THAT ARE NOT MAGNITUDES ARE THE FINDING. `confirm` used to test `< 1` alone while
    `runstate.count` tested the floor AND the type, and the two disagreed on exactly `True`
    and `2.0`: both passed the gate, opened a run, created a ref in the user's repository, and
    died in `write_manifest`. `True` is not contrived — it is what `seats=True` looks like
    after any boolean-ish config parse, and `bool` subclasses `int`, so every naive
    `isinstance(x, int)` admits it. `"3"` is the row a magnitude test cannot even reach: it
    raised a bare `TypeError` out of a comparison, where every other refusal on this path is a
    `GateError`.

    Both doors are measured, because the Quote reaching `confirm` need not have come through
    `quote` — a frozen dataclass is public and constructible, and the manifest is written once
    with no later gate to correct it.
    """
    r, q = _report_and_quote(tmp_path)
    with pytest.raises(gate.GateError):
        gate.confirm(r, dataclasses.replace(q, **{field: value}), _answers())
    with pytest.raises(gate.GateError):
        gate.quote(r, **{field: value}, seat_timeout_sec=3600)


@pytest.mark.parametrize("value", [-1, True, 2.0, "2", None])
def test_the_round_count_the_quote_prices_is_refused_the_same_way(tmp_path, value):
    """`review_rounds` is priced by the same line and is not a seat count: zero rounds is a
    run, so its floor is 0 while `seats` and `attempts` are at least 1. What it shares is the
    TYPE half — `"2"` raised the same bare `TypeError` and `True` priced a one-round review out
    of a boolean — so it reaches the same predicate at a different floor rather than a second
    spelling of it. Zero is the discrimination check below."""
    r = _report(tmp_path)
    with pytest.raises(gate.GateError):
        gate.quote(r, review_rounds=value, seat_timeout_sec=3600)
    assert gate.quote(r, review_rounds=0, seat_timeout_sec=3600).provider_calls < gate.quote(r, seat_timeout_sec=3600).provider_calls, \
        "the discrimination check: the floor is 0 and a run priced there is a real run"


def test_a_run_shape_the_manifest_will_not_record_reaches_nothing_of_the_users(tmp_path,
                                                                               monkeypatch):
    """THE SECOND PROPERTY, AND A SEPARATE ONE: not that the bad value is refused, but that
    the refusal costs the user nothing. Measured rather than reasoned about, because the
    failure it replaces was invisible from the exception alone — `write_manifest` raised
    `ManifestError` for `seats=True` AFTER `storage.run_root`, `journal.intent`, B1 and
    `update-ref` had all run, leaving a run directory, an `events.jsonl` and one forge ref in
    the user's own repository, for a run that never opened.

    DIRTY, so B1 is a commit of its own and the ref is really created: over a clean tree the
    baseline is the base commit and the expensive half of the ordering never runs. The
    discrimination check is the same repository and the same sheet with an ordinary shape —
    without it a fixture pointed at the wrong state directory would pass every assertion here
    while measuring nothing.
    """
    state = _state(monkeypatch, tmp_path)
    repo = make_repo(tmp_path, "shape")
    write(repo, "seed.txt", "the user's uncommitted work\n")
    r = preflight.inspect_repo(repo)
    assert r.facts.unstaged, "the premise: dirty, so an opened run authors B1 and writes a ref"
    q = gate.quote(r, seat_timeout_sec=3600)

    for i, seats in enumerate((True, 2.0, "3")):
        with pytest.raises(gate.GateError):
            gate.open_run(r, gate.confirm(r, dataclasses.replace(q, seats=seats), _answers()),
                          f"refused{i}", quote_=gate.quote(r, seat_timeout_sec=3600))
    assert not state.exists(), "a run that will not happen left a directory behind"
    assert _git(repo, "show-ref").stdout.count("khenrix-forge") == 0, \
        "and a ref in the user's own repository"

    run = gate.open_run(r, gate.confirm(r, q, _answers()), "ok", quote_=gate.quote(r, seat_timeout_sec=3600))
    assert runstate.read_manifest(run).seats == q.seats
    assert _git(repo, "show-ref").stdout.count("khenrix-forge") == 1, \
        "the discrimination check: an ordinary shape does make the ref the rows above must not"


# The same four fields, one door further in than `_NOT_A_STEP` in the verify suite: these are
# the values that reached `write_manifest` through `confirm` with no hand-built record, and
# `cwd=5` is the one that did not even get that far — it raised a bare AttributeError out of
# `open_run`'s spends detector, where every other refusal is a GateError.
_A_STEP_THE_MANIFEST_REFUSES = ({"timeout": True}, {"timeout": 1.5}, {"env": {"A": 1}},
                                {"cwd": 5})


@pytest.mark.parametrize("key", ["setup", "verify"])
def test_a_step_the_manifest_will_not_record_reaches_nothing_of_the_users(key, tmp_path,
                                                                         monkeypatch):
    """THE SECOND PROPERTY, AND A SEPARATE ONE, on `…run_shape…`'s precedent: not that the
    step is refused, but that the refusal costs the user nothing.

    Measured, because the exception alone said nothing about it: `write_manifest` raised
    `ManifestError` for each row above AFTER `storage.run_root`, `journal.intent`, B1 and
    `update-ref` had run. All four leftovers are asserted by name and the REF is the reason
    the list is spelled out — it is the one that escapes the run directory, so a test
    asserting only on the run dir passes while the real damage stands in the user's own
    repository.

    DIRTY, so B1 is a commit of its own and the ref is really created; and both the setup and
    the verify command, because `_confirmed_steps` is called twice and the earlier defect was
    reproduced on both.
    """
    state = _state(monkeypatch, tmp_path)
    repo = make_repo(tmp_path, "step")
    write(repo, "seed.txt", "the user's uncommitted work\n")
    r = preflight.inspect_repo(repo)
    assert r.facts.unstaged, "the premise: dirty, so an opened run authors B1 and writes a ref"
    q = gate.quote(r, seat_timeout_sec=3600)

    for i, bad in enumerate(_A_STEP_THE_MANIFEST_REFUSES):
        with pytest.raises((gate.GateError, verify.VerifyError)):
            # The whole sequence inside the raises, because WHERE it is refused is exactly
            # what this wave moved: the value refuses itself now, so `confirm` is never
            # reached — and a test pinned to `confirm` would have to be rewritten to say so.
            step = verify.Step(argv=("true",), **bad)
            gate.open_run(r, gate.confirm(r, q, _answers(**{key: [step]})), f"refused{i}", quote_=gate.quote(r, seat_timeout_sec=3600))
    assert not state.exists(), "a run that will not happen left a directory behind"
    assert not list(state.rglob("baseline.index")), "…and the index copy B1 is built through"
    assert not list(state.rglob("events.jsonl")), "…and a journal for a run that never opened"
    assert _git(repo, "show-ref").stdout.count("khenrix-forge") == 0, \
        "…and a ref in the USER's own repository, which is the leftover that escapes the run dir"

    ok = verify.Step(argv=("true",), cwd="", env={"CI": "1"}, timeout=45)
    run = gate.open_run(r, gate.confirm(r, q, _answers(**{key: [ok]})), "ok", quote_=gate.quote(r, seat_timeout_sec=3600))
    # The discrimination check names the same four things: without it a fixture pointed at
    # the wrong state directory would pass every assertion above while measuring nothing, and
    # `baseline.index` in particular was reported ABSENT by the last wave.
    assert (run / "baseline.index").is_file() and (run / "events.jsonl").is_file()
    assert getattr(runstate.read_manifest(run), key) == (ok,)
    assert _git(repo, "show-ref").stdout.count("khenrix-forge") == 1, \
        "the discrimination check: an ordinary step does make the ref the rows above must not"


def test_a_verify_command_that_spends_never_opens_a_run(tmp_path, monkeypatch):
    """§5.2's disposition of the three classes, which is why they are three. A `spends:` is
    "detected and refused"; a `remedy:` is what the same sentence prices "as its own explicit
    line", so it is shown and the run opens over it.

    Re-detected at `open_run` rather than trusted from `must_show`, because the command a
    human confirmed is allowed to differ from the one they were shown — which is the whole
    reason they were shown it.
    """
    state = _state(monkeypatch, tmp_path)
    repo = _repo_shaped_like_this_one(tmp_path)
    r = preflight.inspect_repo(repo)
    q = gate.quote(r, seat_timeout_sec=3600)
    spending = gate.confirm(r, q, _answers(verify=[["make", "eval"]]))
    with pytest.raises(gate.GateError, match="provider CLI"):
        gate.open_run(r, spending, "r1", quote_=gate.quote(r, seat_timeout_sec=3600))
    assert not state.exists(), "a refused run left a directory behind"

    priced = gate.confirm(r, q, _answers(verify=[["make", "precommit"]]))
    assert any(f.startswith(gate.REMEDY)
               for f in gate.provider_invoking_verify(repo, verify.Command(priced.verify))), \
        "the discrimination check: this command is priced rather than refused"
    assert runstate.read_manifest(gate.open_run(r, priced, "r2", quote_=gate.quote(r, seat_timeout_sec=3600))).verify == priced.verify


def test_the_gate_records_what_the_operator_accepted(tmp_path, monkeypatch):
    """A gap the human was shown and accepted is a different fact from one nobody raised, and
    only the first belongs in a handover. An id `must_show` cannot raise is refused, so what
    is recorded resolves to a line rather than to a sentence someone typed."""
    _state(monkeypatch, tmp_path)
    r, q = _report_and_quote(tmp_path)
    c = gate.confirm(r, q, _answers(accepted_gaps=[gate.GATE_SURFACE_EMPTY]))
    assert gate.GATE_SURFACE_EMPTY in c.accepted_gaps
    assert gate.confirm(r, q, _answers()).accepted_gaps == ()
    with pytest.raises(gate.GateError):
        gate.confirm(r, q, _answers(accepted_gaps=["the-disk-is-fine"]))
    with pytest.raises(gate.GateError):
        # A string iterates into its characters, so this would record acceptance of letters.
        gate.confirm(r, q, _answers(accepted_gaps=gate.REMOTES_AND_CONFIGURATION))


def test_the_confirmed_policies_survive_to_a_resume(tmp_path, monkeypatch):
    """§5 records the decision and does not ask again, so both policies have to outlive the
    process that collected them. They are journaled rather than carried in the manifest —
    §14.2's manifest is the repository, B's identity, the selection and the commands, and
    `events.jsonl` is one of the six sources it names — so this is where a resume reads them.

    TWO runs, disjoint in every recorded value. One run cannot tell a field that was carried
    from a field a writer hardcoded to whatever that run happened to answer — the hole row 18
    of the mutation table found in the manifest assertions, which is the same hole here and
    was not fixed here at the time. Nothing constant satisfies both rows, so each assertion
    discriminates on its own.
    """
    _state(monkeypatch, tmp_path)
    r, q = _report_and_quote(tmp_path)
    sheets = [
        ("r1", dict(on_calibration_failure="degraded", strategy="fusion",
                    accepted_gaps=[gate.GATE_SURFACE_EMPTY],
                    author=("Ada Lovelace", "ada@example.invalid"))),
        ("r2", dict(on_calibration_failure="abort", strategy="base-and-port",
                    accepted_gaps=[gate.GENERATOR_CONTRACT_EMPTY,
                                   gate.REMOTES_AND_CONFIGURATION],
                    author=("Grace Hopper", "grace@example.invalid"))),
    ]
    for run_id, sheet in sheets:
        run = gate.open_run(r, gate.confirm(r, q, _answers(**sheet)), run_id, quote_=gate.quote(r, seat_timeout_sec=3600))

        events = journal.Journal(storage.journal_path(run)).read()
        done = [e for e in events if e.event == journal.done("confirm")]
        assert len(done) == 1, [e.event for e in events]
        assert done[0].data["on_calibration_failure"] == sheet["on_calibration_failure"]
        assert done[0].data["strategy"] == sheet["strategy"]
        assert done[0].data["accepted_gaps"] == sheet["accepted_gaps"]
        # THE ANSWER THAT REACHED NO RECORD AT ALL. `author` went straight to `materialize`
        # and was written into neither the manifest nor this record, so over a CLEAN tree —
        # where B1 is the user's own base commit and forge creates no commit of its own — a
        # run could not say whose name the operator agreed it could work under. Disjoint per
        # row for this case's stated reason: a hardcoded name satisfies neither.
        assert done[0].data["author"] == list(sheet["author"])
        assert journal.orphans(events) == (), \
            "the write-ahead pair is closed, so a crash inside open_run stays distinguishable"


def test_the_gate_refuses_an_agreement_it_did_not_validate(tmp_path, monkeypatch):
    _state(monkeypatch, tmp_path)
    r, q = _report_and_quote(tmp_path)
    for bad in ({"setup": (), "verify": ()}, _answers()):
        with pytest.raises(gate.GateError):
            gate.open_run(r, bad, "r1", quote_=gate.quote(r, seat_timeout_sec=3600))
    with pytest.raises(gate.GateError):
        gate.open_run(r.facts, _confirmation(), "r1", quote_=gate.quote(r, seat_timeout_sec=3600))
    with pytest.raises(gate.GateError):
        gate.open_run(r, _confirmation(), "", quote_=gate.quote(r, seat_timeout_sec=3600))


# Every field of a `Confirmation`, and one value the gate would have refused if the answer
# sheet had carried it. Named rather than generated: each row is a measured failure or the
# guard that stopped one, and the whole point is that a fixture of realistic values reaches
# none of them.
_UNCONFIRMABLE = [
    # `open_run` reproduced verbatim: `materialize` raised BaselineError over the ordinary
    # repository (identity in ~/.gitconfig, which this package is blind to), after the run
    # directory and `events.jsonl` existed.
    {"author": None},
    # Measured: git 2.53.0 STRIPS these rather than failing, so B1 read `Ada x|a@binvalid`
    # and the run opened. An author is written into a commit header, so `<`, `>` and a
    # newline are structural.
    {"author": ("Ada <x>", "a@b.invalid")},
    {"author": ("Ada", "a@b.invalid\nname")},
    # The string that does not fail on arity: it unpacks into two non-empty characters.
    {"author": "ab"},
    {"author": ("Ada", "")},
    {"on_calibration_failure": "maybe"},
    # §12.1 admits a partition only where stable seams exist, which cannot be pre-committed to.
    {"strategy": "partition"},
    # §6.2 reads every outcome off the verify command, so a gate with no step passes every
    # candidate, including one that deleted the tests.
    {"verify": ()},
    {"verify": ("make verify",)},
    {"setup": [verify.Step(argv=("a",)), ["b"]]},
    {"accepted_gaps": ("the-disk-is-fine",)},
    {"accepted_gaps": gate.REMOTES_AND_CONFIGURATION},
    # THE RETIRED ID, as a SEQUENCE, which is the row that reads `ACCEPTABLE_GAPS` rather than
    # the bare-string guard above it: `--gc` is built, `must_show` raises no such line, and an
    # acceptance of it resolves to nothing the operator was ever shown.
    {"accepted_gaps": (gate.GC_UNBUILT,)},
    # ZERO characters, and the row that makes the bare-string guard load-bearing rather than
    # cosmetic. The last wave called dropping that guard an equivalent mutant, on the ground
    # that every gap id is multi-character so any string decomposes into single characters the
    # stray check refuses — vacuously true at length zero, where `tuple("")` and `tuple(b"")`
    # are both `()` and read as "accepted none". That is an acceptance silently DROPPED, which
    # is the one direction a handover cannot recover from, arriving with nothing raised.
    {"accepted_gaps": ""},
    {"accepted_gaps": b""},
    {"seats": True},
    {"attempts": 2.0},
    {"seats": 0},
]


@pytest.mark.parametrize("bad", _UNCONFIRMABLE, ids=lambda b: next(iter(b)))
def test_a_confirmation_cannot_exist_in_a_state_the_gate_would_not_have_taken(bad, tmp_path,
                                                                              monkeypatch):
    """`open_run` type-checks this class and validates no field of it, and that is only honest
    if the TYPE cannot hold a field the gate would have refused.

    THE FINDING THIS REPLACES was that the narrowing was real but not what was claimed: every
    check lived in `confirm`, so it held for a `Confirmation` reached THROUGH `confirm` and
    for no other. `Confirmation` is a public frozen dataclass with no defaults, and one
    assembled beside `confirm` carried none of it — `author=None` orphaned a run directory in
    `materialize`, and an author git strips rather than refuses reached B1 intact.

    Validating in `__post_init__` is what closes it without a third copy of each predicate:
    the value refuses itself, so `confirm`, `open_run` and `write_manifest` inherit one rule
    each instead of spelling three that are free to disagree — which is exactly how `seats`
    came to be checked two ways.

    THE STATE DIRECTORY IS ASSERTED because a refusal's cost is a separate property from the
    refusal: the failures above all raised, and all of them raised too late.
    """
    state = _state(monkeypatch, tmp_path)
    with pytest.raises(gate.GateError):
        _confirmation(**bad)
    assert not state.exists(), "an unusable agreement still reached the disk"


def test_the_record_normalizes_what_it_takes_so_a_route_in_cannot_decide_the_shape(tmp_path):
    """The discrimination check for the refusals above, and the reason they are in the value
    rather than in `confirm`: the same two helpers that refuse also NORMALIZE, so an argv list
    becomes `verify.Step`s and an author is stripped whichever route built the record.
    `open_run` performs no conversion, so a route that skipped one would put a bare list where
    the manifest expects a step."""
    c = _confirmation(setup=[["make", "setup"]], verify=[["make", "verify"]],
                      author=("  Ada Lovelace  ", " ada@example.invalid "),
                      accepted_gaps=[gate.REMOTES_AND_CONFIGURATION])
    assert c.verify == (verify.Step(argv=("make", "verify")),)
    assert c.setup == (verify.Step(argv=("make", "setup")),)
    assert c.author == AUTHOR
    assert c.accepted_gaps == (gate.REMOTES_AND_CONFIGURATION,)
    assert dataclasses.replace(c, seats=5).seats == 5, "and replace() re-enters the same door"
    with pytest.raises(gate.GateError):
        dataclasses.replace(c, seats=True)


# Values chosen for what they are NOT. Every one of them was accepted by some door of this
# gate and refused by a later one at least once on this branch, or is the immediate neighbour
# of one that was — `True` for `seats`, `True` and `1.5` for a step's timeout, a non-string
# env value, a non-string cwd, a bare string where a sequence was meant. They are applied to
# EVERY field the walk below reaches rather than to the field each was measured on, because
# the point of the walk is that the next field is covered before anyone writes a row for it.
_HOSTILE = (True, 0, -1, 1.5, "3", b"x", None, (), [], {}, {"A": 1}, {1: "A"}, ("x", 2),
            ["ok"], object())


def _below(value):
    """(label, rebuild) for every dataclass field reachable BELOW `value`.

    `rebuild(x)` returns a value of the same shape with that one leaf replaced by `x`, built
    through `dataclasses.replace` so every constructor on the way back up runs its own
    `__post_init__`. Driven off `dataclasses.fields` rather than a list of the types this
    package holds today: an enumeration is the thing that has now failed three times, and a
    field added to `verify.Step` — or a new record hung off `Confirmation` — has to be
    covered by the walk before anybody remembers to write a row for it.
    """
    if dataclasses.is_dataclass(value):
        for f in dataclasses.fields(value):
            yield f".{f.name}", lambda x, n=f.name, v=value: dataclasses.replace(v, **{n: x})
            for label, rebuild in _below(getattr(value, f.name)):
                yield (f".{f.name}{label}",
                       lambda x, n=f.name, v=value, rb=rebuild:
                       dataclasses.replace(v, **{n: rb(x)}))
    elif isinstance(value, tuple):
        for i, item in enumerate(value):
            for label, rebuild in _below(item):
                yield (f"[{i}]{label}",
                       lambda x, i=i, v=value, rb=rebuild: v[:i] + (rb(x),) + v[i + 1:])


def _manifest(**kw):
    """A `runstate.Manifest` every other field of which `write_manifest` takes, so the only
    thing a row below can be refused for is the field it replaced."""
    fields = dict(run_id="r1", repo_path="/repo", base_commit="a" * 40,
                  baseline_ref="refs/khenrix-forge/r1/base", baseline_commit="b" * 40,
                  tracked_tree_oid="c" * 40, selected_paths=(),
                  generator_contract=finspect.GeneratorContract(), setup=(),
                  verify=(verify.Step(argv=("true",)),), protected_refs={},
                  forge_refs={"refs/khenrix-forge/r1/base": "b" * 40}, status_digest="d",
                  index_digest="e", created_at="2026-08-02T00:00:00+00:00", seats=3,
                  attempts=3, review_rounds=2, synthesis_fix_cap=3, concurrency=1)
    return runstate.Manifest(**{**fields, **kw})


def test_no_value_a_confirmation_can_hold_is_one_the_manifest_would_refuse(tmp_path):
    """THE CLASS, rather than the third instance of it.

    Three times now the same defect: a predicate spelled at the gate and again in
    `runstate`, disagreeing, with `write_manifest` raising after the run directory,
    `baseline.index`, `events.jsonl` and a forge ref in the user's own repository already
    existed — the identity `baseline` required, then `seats`/`attempts`, then §5.1's three
    step fields that were not `argv`. Each fix closed one value.

    So the property is stated over the RECORD instead: every field a `Confirmation` shares
    with the `Manifest` — by name intersection, so a field added to both is covered without
    anyone editing this test — and every dataclass field below it, substituted with a value
    that is not what the field is for. Each substitution must either be REFUSED where it costs
    nothing, or produce a manifest `write_manifest` takes. There is no third outcome, and the
    third outcome is the defect.

    A refusal must also arrive in this package's vocabulary. `cwd=5` did not: it passed
    `confirm` and raised a bare `AttributeError` out of `open_run`'s spends detector, which is
    a refusal no caller of this module is catching.

    WHAT MAKES THIS A CLOSURE and not a fourth list of rows: the manifest half runs the real
    `write_manifest`, decoder and round-trip check included. A rule added to `runstate._steps`
    and not to `verify.Step` fails here — and that is the direction all three instances came
    from, `runstate` deciding something about a value the gate had already accepted.

    WHAT THIS CANNOT SEE, stated because a closure test that silently covers less than it
    claims is worse than none: it walks dataclasses and tuples, so a field holding a dict is
    fuzzed as a whole value (`{"A": 1}`, `{1: "A"}`) and not key by key, and it fuzzes
    CONSTRUCTION only — a mutable field mutated after the fact, `step.env["A"] = 1`, is the
    boundary `verify.Step` and `Confirmation` both state rather than close.
    """
    shared = [f.name for f in dataclasses.fields(gate.Confirmation)
              if f.name in {g.name for g in dataclasses.fields(runstate.Manifest)}]
    assert sorted(shared) == ["attempts", "concurrency", "review_rounds", "seats", "setup",
                              "synthesis_fix_cap", "verify"], \
        "the two records' shared fields moved; the rows below decide what this test measures"
    c = _confirmation()
    targets = [(f".{n}", lambda x, n=n: dataclasses.replace(c, **{n: x})) for n in shared]
    targets += [(f".{n}{label}", lambda x, n=n, rb=rb: dataclasses.replace(c, **{n: rb(x)}))
                for n in shared for label, rb in _below(getattr(c, n))]
    assert {t[0] for t in targets} >= {".seats", ".verify", ".verify[0].timeout",
                                       ".verify[0].env", ".verify[0].cwd", ".verify[0].argv"}, \
        "the walk stopped short of the fields three waves of this defect were found in"

    refused = admitted = 0
    for label, rebuild in targets:
        for hostile in _HOSTILE:
            try:
                fuzzed = rebuild(hostile)
            except (gate.GateError, verify.VerifyError):
                refused += 1
                continue
            except Exception as e:
                raise AssertionError(
                    f"{label} = {hostile!r} was refused as {type(e).__name__}: {e}. Every "
                    "refusal on this path is a GateError or a VerifyError; a caller of this "
                    "gate has no name for anything else.") from e
            run = tmp_path / f"run{refused}-{admitted}"
            run.mkdir()
            try:
                runstate.write_manifest(run, _manifest(
                    **{n: getattr(fuzzed, n) for n in shared}))
            except runstate.ManifestError as e:
                raise AssertionError(
                    f"{label} = {hostile!r} built a Confirmation the manifest then refuses: "
                    f"{e}. `open_run` reaches `write_manifest` with the run directory, "
                    "`baseline.index`, `events.jsonl` and a forge ref in the user's own "
                    "repository already written.") from e
            admitted += 1
    # Both branches, because an assertion nothing reaches is not one: if every substitution
    # were refused the loop would prove only that the gate refuses everything, and if none
    # were the manifest half would never have been exercised.
    assert refused and admitted, f"refused={refused} admitted={admitted}"


def test_an_answer_key_this_gate_does_not_ask_is_refused(tmp_path):
    """§5 step 2 is asked once, so a key nobody reads is an answer silently discarded.

    `accepted_gaps` is the whole subject and the fixture says so. A misspelled REQUIRED key
    cannot reach this check — the misspelling leaves the real key absent and the missing-key
    refusal fires first, which is the second assertion — while `accepted_gaps` is the one key
    whose absence is legal, so its misspelling passes every other check and is read as
    "accepted none". Without this refusal an operator's acceptance would be dropped from the
    record with nothing raised, which is the one direction a handover cannot recover from.
    """
    r, q = _report_and_quote(tmp_path)
    with pytest.raises(gate.GateError, match="does not ask"):
        gate.confirm(r, q, _answers(acccepted_gaps=[gate.REMOTES_AND_CONFIGURATION]))
    with pytest.raises(gate.GateError, match="unanswered"):
        gate.confirm(r, q, {**{k: v for k, v in _answers().items() if k != "strategy"},
                            "startegy": "fusion"})
    assert gate.confirm(
        r, q, _answers(accepted_gaps=[gate.REMOTES_AND_CONFIGURATION])).accepted_gaps \
        == (gate.REMOTES_AND_CONFIGURATION,), \
        "the discrimination check: the correctly spelled key is taken"


def test_the_manifest_records_the_selection_the_run_was_opened_over(tmp_path, monkeypatch):
    """§14.2 names the selected paths among what the manifest holds, and §2.2's selection is
    what decides which untracked files enter B at all — so a manifest that recorded none of
    them describes a run over a different tree than the one that was built.

    Every other `open_run` test here selects nothing, where `()` and "the selection, dropped"
    are the same tuple. The assertion is against B's own tree rather than against the input,
    because agreeing with the argument is what a hardcoded `()` would also do once the
    argument was `()`.
    """
    _state(monkeypatch, tmp_path)
    repo = make_repo(tmp_path, "selected-repo")
    write(repo, "wanted.txt", "the user asked for this one\n")
    write(repo, "ignored.txt", "and not this one\n")
    r = preflight.inspect_repo(repo, ["wanted.txt"])

    m = runstate.read_manifest(gate.open_run(r, _confirmation(), "r1", quote_=gate.quote(r, seat_timeout_sec=3600)))
    assert m.selected_paths == ("wanted.txt",)
    tree = _git(repo, "ls-tree", "-r", "--name-only", m.tracked_tree_oid).stdout.split()
    assert "wanted.txt" in tree and "ignored.txt" not in tree, \
        "B was built over the selection, so the manifest that omitted it would misdescribe B"


def test_the_manifest_records_the_contract_the_report_carried(tmp_path, monkeypatch):
    """§7.2's contract decides which verify-origin rewrites a run may admit without failing
    the candidate, and §14.2 records it once — so a manifest carrying a contract nobody
    confirmed is a licence the operator never gave.

    The report is REPLACED rather than found, and that is the finding rather than a shortcut:
    `inspect.detect_generators` returns the empty contract for every repository it can read, so
    a substitution of the empty contract for `report.contract` is an equivalent mutant against
    every fixture in this suite and the field is unpinnable from a real repository. Both
    directions are measured — a declared relation reaches the manifest, and an undeclared
    report does not gain one — because a writer hardcoding either constant satisfies one.
    """
    _state(monkeypatch, tmp_path)
    r, q = _report_and_quote(tmp_path)
    assert r.contract.relations == (), "the premise: no repository yields a non-empty contract"
    declared = dataclasses.replace(
        r, contract=r.contract.__class__(id="render-x", relations=(("src/*", "gen/*"),)))

    m = runstate.read_manifest(gate.open_run(declared, gate.confirm(r, q, _answers()), "r1", quote_=gate.quote(declared, seat_timeout_sec=3600)))
    assert m.generator_contract.id == "render-x"
    assert m.generator_contract.relations == (("src/*", "gen/*"),)
    plain = runstate.read_manifest(gate.open_run(r, gate.confirm(r, q, _answers()), "r2", quote_=gate.quote(r, seat_timeout_sec=3600)))
    assert plain.generator_contract.relations == (), \
        "a report that declared no relation gained one on the way to the manifest"


def test_the_manifest_says_when_the_run_was_opened(tmp_path, monkeypatch):
    """`created_at` is the only field in the manifest nothing else in the run can reconstruct:
    B's commit carries forge's own committer date only when the tree was dirty, and a clean
    run reuses the user's base commit. §15's `--gc` and every handover that has to say which
    of two runs came first read this and nothing else."""
    _state(monkeypatch, tmp_path)
    r, q = _report_and_quote(tmp_path)
    before = datetime.now(timezone.utc)
    m = runstate.read_manifest(gate.open_run(r, gate.confirm(r, q, _answers()), "r1", quote_=gate.quote(r, seat_timeout_sec=3600)))
    after = datetime.now(timezone.utc)

    stamp = datetime.fromisoformat(m.created_at)
    assert stamp.tzinfo is not None, "a naive stamp cannot be compared across machines"
    assert before <= stamp <= after, (m.created_at, before, after)


def test_nothing_in_this_module_runs_the_repositorys_own_program(tmp_path, monkeypatch):
    """§5 step 1 binds the three functions the operator answers BEFORE, and the claim now
    reaches past it: `open_run` is on the far side of the answer and still runs nothing the
    repository supplied. The line is drawn by measurement rather than asserted in prose,
    because it has moved twice — written for the detector alone, widened to the module when
    `open_run` arrived, then narrowed again to exempt the two hooks `baseline.materialize`
    fired. `gitcmd.NO_HOOKS` closed that exemption on the user's decision, so the measurement
    is what says which shape is live.

    TWO suppliers, because `core.fsmonitor` is not the only program a repository hands git:
    `update-ref` runs `reference-transaction` and an index write runs `post-index-change`,
    both out of the repository's own hooks directory.

    THE CONTROLS ARE THE WHOLE COST OF SUPPRESSING SOMETHING. Once nothing fires on either
    side of the answer, an armed program git would never have run and a suppressed one read
    identically, so both are fired deliberately at the end — a suite that could not tell those
    apart would report an unreachable claim as a kept promise.
    """
    _state(monkeypatch, tmp_path)
    repo = make_repo(tmp_path, "armed")
    write(repo, "Makefile", "verify:\n\t@true\n")
    mon = write(repo, "fsmonitor.sh", f"#!/bin/sh\n: > {repo}/HOOK-RAN\nprintf ''\n")
    mon.chmod(0o755)
    commit_all(repo, "a gate and a monitor")
    _git(repo, "config", "core.fsmonitor", str(mon))
    fired = tmp_path / "fired"
    fired.mkdir()
    for name in ("reference-transaction", "post-index-change"):
        h = repo / ".git" / "hooks" / name
        h.write_text(f"#!/bin/sh\n: > {fired}/{name}\nexit 0\n")
        h.chmod(0o755)

    write(repo, "seed.txt", "uncommitted, so B is a commit of its own\n")
    r = preflight.inspect_repo(repo)
    cmd = verify.Command.parse([["make", "verify"]])
    q = gate.quote(r, seat_timeout_sec=3600)
    gate.must_show(r, q, cmd, setup=verify.Command.parse([['true']]))
    c = gate.confirm(r, q, _answers(verify=[["make", "verify"]]))
    assert not (repo / "HOOK-RAN").exists() and not list(fired.iterdir()), \
        "something before the operator's answer ran a program the repository supplied"

    gate.open_run(r, c, "r1", quote_=gate.quote(r, seat_timeout_sec=3600))
    assert not (repo / "HOOK-RAN").exists(), \
        "materialize dropped NO_DAEMON_CACHE from a call that loads the index"
    assert not list(fired.iterdir()), \
        "materialize dropped NO_HOOKS from a call that writes an index or a ref"

    _git(repo, "status", "--porcelain")
    assert (repo / "HOOK-RAN").exists(), \
        "the control failed: this fsmonitor does nothing even when an ordinary git runs it"
    _git(repo, "update-ref", "refs/heads/control", "HEAD")
    _git(repo, "add", "-A")
    assert {p.name for p in fired.iterdir()} == {"reference-transaction", "post-index-change"}, \
        "the control failed: these hooks do nothing even when an ordinary git runs them"


def test_the_priced_synthesis_fix_cap_is_the_recorded_one(tmp_path, monkeypatch):
    """§12.3's cap has one number, and §5.2 is where it is priced.

    `Manifest.attempts` is the BUILDER budget — `quote` computes `builders = seats *
    attempts` — and reusing it would silently re-price a 3-attempt run as 9. `State.attempt`
    is already spent on builder attempts. So the cap is its own field, derived from the same
    arithmetic the quote showed the operator.
    """
    _state(monkeypatch, tmp_path)
    repo = make_repo(tmp_path)
    report = preflight.inspect_repo(repo)
    q = gate.quote(report, review_rounds=2, deep_review=True, seat_timeout_sec=3600)
    assert (q.review_rounds, q.synthesis_fix_cap) == (2, 3)
    assert gate.quote(report, review_rounds=2, deep_review=False, seat_timeout_sec=3600).synthesis_fix_cap == 2
    assert gate.quote(report, review_rounds=0, deep_review=False, seat_timeout_sec=3600).synthesis_fix_cap == 0

    c = gate.confirm(report, q, _answers())
    assert (c.review_rounds, c.synthesis_fix_cap) == (2, 3)
    run = gate.open_run(report, c, "r-cap", quote_=gate.quote(report, seat_timeout_sec=3600))
    back = runstate.read_manifest(run)
    assert (back.review_rounds, back.synthesis_fix_cap) == (2, 3), \
        "the priced number and the recorded number are the same number"


def test_a_cap_below_the_rounds_it_must_cover_is_refused():
    with pytest.raises(gate.GateError):
        gate.Confirmation(setup=(), verify=(verify.Step(argv=("true",)),),
                          on_calibration_failure="abort", strategy="size-gated",
                          accepted_gaps=(), author=("A", "a@b.invalid"), deep_review=True,
                          seats=3, attempts=3, review_rounds=2, synthesis_fix_cap=1)


def test_the_deep_review_decision_is_answered_once_and_must_match_what_was_priced(tmp_path,
                                                                                  monkeypatch):
    """§13.1's --skip-deep-review moves three scalars on the quote AND decides whether a cloud review
    runs an hour later, in another process. Two spellings of one decision will disagree, so it
    is a §5 step 2 ANSWER recorded once — and `confirm` refuses an answer that contradicts the
    Quote the operator was shown, which is the invariant living in the value."""
    _state(monkeypatch, tmp_path)
    report = _report(tmp_path)
    priced_on = gate.quote(report, seat_timeout_sec=3600)
    priced_off = gate.quote(report, deep_review=False, seat_timeout_sec=3600)
    assert priced_on.provider_calls > priced_off.provider_calls

    answers = _answers(deep_review=True)
    assert gate.confirm(report, priced_on, answers).deep_review is True
    assert gate.confirm(report, priced_off,
                        dict(answers, deep_review=False)).deep_review is False

    with pytest.raises(gate.GateError) as e:
        gate.confirm(report, priced_off, answers)           # priced off, answered on
    assert "priced" in str(e.value)
    with pytest.raises(gate.GateError):
        gate.confirm(report, priced_on, dict(answers, deep_review=False))
    with pytest.raises(gate.GateError):
        gate.confirm(report, priced_on, {k: v for k, v in answers.items()
                                         if k != "deep_review"})
    with pytest.raises(gate.GateError):
        gate.confirm(report, priced_on, dict(answers, deep_review="yes"))


def test_the_deep_review_answer_is_journalled_where_collect_will_look_for_it(tmp_path,
                                                                            monkeypatch):
    """The other half of the decision, and the half `--collect` spends money on. The answer is
    recorded on the `confirm` done record — the manifest holds no policy — under the FIELD
    NAME, which is the name `runner._confirmed_policy` reads `on_calibration_failure` back by
    and the one `cli._confirmed_deep_review` reads this by. A policy written under one name
    and read under another is silence one field over.

    Both values, because a key that is always `True` is a key whose reader cannot be wrong.
    """
    _state(monkeypatch, tmp_path)
    for run_id, on in (("deep-on", True), ("deep-off", False)):
        r = _report(tmp_path)
        q = gate.quote(r, deep_review=on, seat_timeout_sec=3600)
        run = gate.open_run(r, gate.confirm(r, q, _answers(deep_review=on)), run_id, quote_=gate.quote(r, seat_timeout_sec=3600))
        done = [e for e in journal.Journal(storage.journal_path(run)).read()
                if e.event == journal.done("confirm")]
        assert len(done) == 1, done
        assert done[0].data["deep_review"] is on, done[0].data


def test_free_bytes_walks_to_the_nearest_existing_ancestor(tmp_path):
    """The forge root under XDG_STATE_HOME does not exist before the first run, and a run
    directory's filesystem is its ancestor's."""
    assert _REAL_FREE_BYTES(tmp_path / "a" / "b" / "c") > 0


def test_free_bytes_refuses_a_path_with_no_readable_ancestor(monkeypatch, tmp_path):
    def _boom(_):
        raise OSError(5, "I/O error")
    monkeypatch.setattr(gate.shutil, "disk_usage", _boom)
    with pytest.raises(gate.GateError, match="free space"):
        _REAL_FREE_BYTES(tmp_path)


def test_the_gate_is_not_green_because_this_machine_has_room(tmp_path):
    """THE NEUTRALISATION ABOVE IS NOT A WEAKENING, and this is what keeps it honest: the
    refusal still reaches `must_show`'s output when the disk is short. Without this, an
    autouse fixture that silenced the check everywhere would be indistinguishable from never
    having wired it."""
    r = _report(tmp_path)
    q = gate.quote(r, seats=3, attempts=3, review_rounds=2, seat_timeout_sec=3600)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(gate, "free_bytes", lambda _p: 1_000_000_000)
        lines = gate.must_show(r, q, verify.Command.parse([["true"]]), setup=verify.Command.parse([['true']]))
    assert "§4 rejects this run" in lines[0]


def test_a_run_that_does_not_fit_is_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(gate, "free_bytes", lambda _p: 1_000_000_000)
    q = gate.quote(_report(tmp_path), seats=3, attempts=3, review_rounds=2, seat_timeout_sec=3600)
    why = gate.refuse_for_disk(q, root=tmp_path)
    # The peak is READ off the quote rather than spelled here, so this assertion cannot drift
    # away from what the operator was actually shown.
    assert why and str(q.peak_disk_gb) in why and "1.0 GB" in why


def test_a_run_that_fits_is_not_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(gate, "free_bytes", lambda _p: 500_000_000_000)
    q = gate.quote(_report(tmp_path), seats=3, attempts=3, review_rounds=2, seat_timeout_sec=3600)
    assert gate.refuse_for_disk(q, root=tmp_path) is None


def test_unreadable_free_space_is_not_read_as_enough_space(monkeypatch, tmp_path):
    """"Nobody measured" must not compare equal to "there is room" — and it must not render
    the same sentence as a genuine shortfall either."""
    def _boom(_p):
        raise gate.GateError("free space at /x could not be read: I/O error")
    monkeypatch.setattr(gate, "free_bytes", _boom)
    q = gate.quote(_report(tmp_path), seats=3, attempts=3, review_rounds=2, seat_timeout_sec=3600)
    why = gate.refuse_for_disk(q, root=tmp_path)
    assert why and "could not be read" in why and "1.0 GB free" not in why


def test_open_run_cannot_be_reached_around_the_disk_check(tmp_path):
    """`quote_` is keyword-only with NO DEFAULT. The external question is not "does open_run
    refuse a short disk" — the tests above ask that — but "can a caller open a run without
    the check running at all", which a default would answer yes."""
    r = _report(tmp_path)
    c = gate.confirm(r, gate.quote(r, seat_timeout_sec=3600), _answers())
    with pytest.raises(TypeError, match="quote_"):
        gate.open_run(r, c, "r1")


def test_the_gate_no_longer_announces_a_gap_it_has_closed(tmp_path):
    """THE RULE IN THE OTHER DIRECTION. A verdict must never read cleaner than its evidence
    AND never dirtier: a gap line for a race that has been closed tells an operator to accept
    a risk that is not there, and trains them to skim the gap lines that do matter.

    `baseline.materialize` screens the path set B1 actually holds, after the manifest and
    before the ref, so `screen-predates-the-baseline` has no window left to describe."""
    r = _report(tmp_path)
    q = gate.quote(r, seats=3, attempts=3, review_rounds=2, seat_timeout_sec=3600)
    lines = gate.must_show(r, q, verify.Command.parse([["true"]]),
                           setup=verify.Command.parse([["true"]]))
    assert not any("screen-predates-the-baseline" in l for l in lines), lines
    assert not hasattr(gate, "SCREEN_RACE"), \
        "the constant outlived its gap; a name nothing uses is the next reader's false lead"
    # The §3 screen LINE itself stays — it reports what was scanned and is not a gap.
    assert any("§3 screen" in l for l in lines), lines


def test_a_one_seat_run_is_told_it_cannot_fuse_before_it_spends(tmp_path):
    """`seats` has a floor of 1 and the skill's description promises "all three agentic
    CLIs". Reproduced: `runner._fleet` resolves seats=1 to ('claude',) — a legitimate cheap
    run, and not the thing the description sells, since there is nothing to fuse with one
    candidate.

    A LINE AND NOT A REFUSAL: refusing would remove a mode the flag exists to offer. What
    must not happen is an operator paying for it believing they bought a fusion."""
    r = _report(tmp_path)
    q = gate.quote(r, seats=1, attempts=1, review_rounds=0, seat_timeout_sec=3600)
    assert any("cannot produce a FUSION" in l for l in q.lines), q.lines
    q3 = gate.quote(r, seats=3, attempts=1, review_rounds=0, seat_timeout_sec=3600)
    assert not any("cannot produce a FUSION" in l for l in q3.lines), \
        "the line must not fire on the fleet the skill is actually for"


def test_a_quote_cannot_carry_a_number_nobody_could_agree_to(tmp_path):
    """REPRODUCED: `Quote(provider_calls=-5, seats=-1, …)` was accepted whole. A quote is
    what an operator AGREES TO at §5 step 2, and everything downstream reads the run's shape
    off it — `confirm` cross-checks it, `open_run` writes it into the manifest, §4's disk
    rejection compares against `peak_disk_gb`.

    `quote` validates its own inputs through `_confirmed_count`, so this closes the OTHER
    door: §12.4's consumer and every test build a `Quote` directly, exactly as `Report` and
    `Confirmation` are built directly — and both of those validate for the same reason."""
    good = gate.quote(_report(tmp_path), seats=3, attempts=3, review_rounds=2,
                      seat_timeout_sec=3600)
    for field, bad in (("provider_calls", -5), ("seats", -1), ("attempts", True),
                       ("peak_disk_gb", -1.0), ("deep_review", ""), ("terms", ())):
        with pytest.raises(gate.GateError):
            dataclasses.replace(good, **{field: bad})
    # ...and the real quote still round-trips, or the guard is refusing its own producer.
    assert dataclasses.replace(good, seats=3) == good


def test_every_spelling_of_make_C_moves_the_scan(tmp_path):
    """REPRODUCED: `--directory=sub` and the attached `-Csub` fell through to the catch-all
    that ignores flags, so the scan stayed in the wrong directory and reported `unresolved`
    where the truth is `spends`.

    WRONG IS WORSE THAN UNKNOWN HERE. An operator shown "could not resolve this target" may
    accept the gap; one shown "this verify command spends a provider call" refuses. The
    separated form worked, so the detector looked healthy on the spelling anyone would test."""
    repo = make_repo(tmp_path)
    (Path(repo) / "sub").mkdir()
    write(repo, "sub/Makefile", "go:\n\t@claude -p 'spend'\n")
    write(repo, "Makefile", "outer:\n\t@true\n")
    commit_all(repo, "gates")
    for argv in (["make", "-C", "sub", "go"],
                 ["make", "--directory=sub", "go"],
                 ["make", "-Csub", "go"]):
        found = gate.provider_invoking_verify(repo, verify.Command.parse([argv]))
        assert found and found[0].startswith("spends:"), (argv, found)


# ---- concurrency: the quoted ceiling ----------------------------------------------------
def _quote(tmp_path, **kw):
    return gate.quote(_report(tmp_path), seat_timeout_sec=3600, **kw)


def test_the_serial_quote_did_not_move_when_concurrency_was_added(tmp_path):
    """THE REGRESSION GUARD FOR EVERY EXISTING RUN. `ceil(seats/1) x attempts x window` must
    equal the old `seats x attempts x window` exactly — a generalisation that shifts the
    default is a repricing nobody asked for."""
    q = _quote(tmp_path, seats=3, attempts=3)
    assert q.concurrency == 1
    assert q.waves == 3
    line = [l for l in q.lines if l.startswith("wall clock:")][0]
    assert "9.0 h" in line, line


def test_running_three_builders_at_once_thirds_the_quoted_ceiling(tmp_path):
    """The point of the feature, stated as the number the operator is shown: three seats in
    one wave instead of three waves."""
    q = _quote(tmp_path, seats=3, attempts=3, concurrency=3)
    assert q.waves == 1
    line = [l for l in q.lines if l.startswith("wall clock:")][0]
    assert "3.0 h" in line, line


def test_an_uneven_fleet_rounds_its_waves_up_rather_than_down(tmp_path):
    """FAIL CLOSED ON THE CEILING. 3 seats at width 2 is two waves, not one and a half — a
    bound that rounded down would quote a time the run can exceed, which is the one direction
    an upper bound may not be wrong in."""
    q = _quote(tmp_path, seats=3, attempts=1, concurrency=2)
    assert q.waves == 2
    line = [l for l in q.lines if l.startswith("wall clock:")][0]
    assert "2.0 h" in line, line


def test_a_parallel_quote_states_the_contention_trade_it_is_selling(tmp_path):
    """A VERDICT MUST NOT READ CLEANER THAN ITS EVIDENCE. The lower ceiling is the visible
    half; the hazard — §19's window is a TIMEOUT, so contention can spend a seat and return no
    candidate — is what the operator is actually agreeing to, and it must be on the quote."""
    serial = [l for l in _quote(tmp_path, seats=3, attempts=1).lines if l.startswith("wall clock:")][0]
    parallel = [l for l in _quote(tmp_path, seats=3, attempts=1, concurrency=3).lines
                if l.startswith("wall clock:")][0]
    assert "TIMEOUT" in parallel and "timing" not in serial
    assert "TIMEOUT" not in serial, "the serial quote grew a warning about a risk it has not"


def test_a_width_above_the_seat_count_is_refused_rather_than_quoted(tmp_path):
    """It saturates: `ceil(seats/concurrency)` is 1 for every width at or above `seats`, so
    accepting 9 for 3 seats quotes the SAME ceiling while implying more was bought."""
    with pytest.raises(gate.GateError, match="buys nothing"):
        _quote(tmp_path, seats=3, attempts=1, concurrency=9)


def test_a_width_that_is_not_a_count_never_prices_a_run(tmp_path):
    """`seats=True` once priced a run, opened a directory and authored B1 — the failure
    `_confirmed_count` exists for. A new count must go through the same door."""
    for bad in (0, -1, True, "3", None, 1.5):
        with pytest.raises(gate.GateError):
            _quote(tmp_path, seats=3, attempts=1, concurrency=bad)


def test_a_confirmed_setup_that_spends_is_refused_like_a_verify_that_does(tmp_path):
    """THE ASYMMETRY THIS CLOSES. `open_run` refused a VERIFY command reaching a provider CLI
    and accepted the identical spend in SETUP — while setup carries the larger multiplier:
    `2 + (seats x attempts) + verifiers` against verify's one per verifier. The refusal's own
    reason ("re-run fresh per candidate") applies harder to the command it did not screen."""
    repo = make_repo(tmp_path)
    report = preflight.inspect_repo(repo)
    q = gate.quote(report, seats=1, attempts=1, seat_timeout_sec=3600)
    c = _confirmation(setup=(verify.Step(argv=("claude", "-p", "do it")),),
                      seats=q.seats, attempts=q.attempts,
                      review_rounds=q.review_rounds,
                      synthesis_fix_cap=q.synthesis_fix_cap)
    with pytest.raises(gate.GateError, match="setup command reaches a provider CLI"):
        gate.open_run(report, c, "r1", quote_=q)


def test_an_ordinary_setup_still_opens_a_run(tmp_path):
    """THE DISCRIMINATION CHECK — a refusal that also fired on `pip install` would be a gate
    nobody could pass, and every real run takes this branch."""
    repo = make_repo(tmp_path)
    report = preflight.inspect_repo(repo)
    q = gate.quote(report, seats=1, attempts=1, seat_timeout_sec=3600)
    c = _confirmation(setup=(verify.Step(argv=("true",)),),
                      seats=q.seats, attempts=q.attempts,
                      review_rounds=q.review_rounds,
                      synthesis_fix_cap=q.synthesis_fix_cap)
    assert gate.open_run(report, c, "r2", quote_=q).is_dir()
