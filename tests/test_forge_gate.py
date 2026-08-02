"""What the §5 gate quotes, and what it refuses.

Every arithmetic assertion here is written as the DERIVATION rather than as the total, because
a total is the one thing that can be back-fitted to whatever the code returned. Every refusal
is paired with a discrimination check — a command differing in exactly the property the
refusal names, and admitted — since a fixture odd enough to trip one rule is odd enough to
trip another, and a non-empty tuple says nothing about which.
"""
import itertools
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))
sys.path.insert(0, str(ROOT / "tests"))

from forge import gate, preflight, verify  # noqa: E402
from forge_fixtures import commit_all, git as _git, make_repo, write  # noqa: E402


_SERIAL = itertools.count()


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
    quote is 19 instead: the same paragraph requires that "the quote includes post-review
    synthesis invocations", and `9 + 1 + 6` contains exactly one synthesis. Three more exist —
    a round-1 blocker is fixed before round 2 runs, a blocker fixed after round 2 is reported
    "verified but not independently reviewed" (§13), and §13.1's ultrareview findings "follow
    the exact post-round-2 rule above: fix -> fresh-verifier verify -> checkpoint". A quote
    stopping at 16 is wrong by three, in the operator's favour, which is the direction that
    matters.
    """
    q = gate.quote(_report(tmp_path), seats=3, attempts=3, review_rounds=2)
    builders, synthesis, review, fixes = 3 * 3, 1, 2 * 3, 2 + 1
    assert (builders, synthesis, review, fixes) == (9, 1, 6, 3)
    assert q.provider_calls == builders + synthesis + review + fixes == 19, q.lines
    assert any("9 + 1 + 6 = 16" in line for line in q.lines), \
        "§5.2's own sum stays visible, or the difference is a silent correction"


def test_a_review_round_costs_a_reviewer_each_and_one_synthesis_fix(tmp_path):
    """The discrimination check for the term above: only the review terms may move with it."""
    rounds = [gate.quote(_report(tmp_path), review_rounds=n).provider_calls for n in (0, 1, 2)]
    assert rounds == [11, 15, 19], rounds
    assert rounds[0] == 3 * 3 + 1 + 1, \
        "with no review, only builders, synthesis and ultrareview's own fix are left"
    assert [b - a for a, b in zip(rounds, rounds[1:])] == [4, 4], \
        "a round is 3 reviewers plus the one synthesis invocation that answers it"


def test_ultrareviews_own_fix_is_an_invocation_the_quote_cannot_leave_out(tmp_path):
    """§13.1 is titled "default on" and its findings take the post-round-2 treatment: "fix ->
    fresh-verifier verify -> checkpoint". §5.2 prices the ultrareview RUN in money and says
    nothing about the fix it triggers, so a quote that priced only the run is low by one
    invocation, one setup and one verify — with no exclusion line, which is the shape §5.2's
    own `9 + 1 + 6` has.

    `--no-ultra` is the discrimination check and the answer to "then make it optional": it is
    a parameter, and turning it off must move exactly those three scalars and nothing else.
    """
    on = gate.quote(_report(tmp_path), seats=3, attempts=3, review_rounds=2)
    off = gate.quote(_report(tmp_path), seats=3, attempts=3, review_rounds=2, ultrareview=False)
    assert (on.provider_calls, on.setup_runs, on.verify_runs) == (19, 18, 9), on.lines
    assert (off.provider_calls, off.setup_runs, off.verify_runs) == (18, 17, 8), off.lines
    assert "$" in on.ultrareview and "$" not in off.ultrareview
    assert "--no-ultra" in on.ultrareview, "the opt-out is named in the quote that includes it"


def test_the_council_default_retry_setting_is_shown_but_never_summed(tmp_path):
    """§5.2 asks for whichever is wired, and forge wires `--retries 0` (§13). The 28-call
    variant exists only if review retries ever return, so it is a line and not the total —
    pinned by differencing the review term OUT of the total, which is the only way to show
    which of the two numbers the total was built from.
    """
    q = gate.quote(_report(tmp_path), seats=3, attempts=3, review_rounds=2)
    assert any(str(2 * 3 * 3) in line and "--retries 2" in line for line in q.lines), q.lines
    no_review = gate.quote(_report(tmp_path), seats=3, attempts=3, review_rounds=0)
    assert q.provider_calls - no_review.provider_calls == 2 * 3 + 2, \
        "two rounds cost 6 reviewer calls and 2 fixes — the wired --retries 0 term, not 18"


def test_ultrareview_is_quoted_as_money_not_as_a_count(tmp_path):
    """§13.1: $5-25 in usage credits, or one of three one-time free runs. An integer field
    would render as one more row in a column of call counts — free, beside numbers that
    are."""
    q = gate.quote(_report(tmp_path))
    assert "$" in q.ultrareview and "5-25" in q.ultrareview, q.ultrareview
    assert isinstance(q.ultrareview, str)
    assert str(q.provider_calls) not in q.ultrareview


def test_the_quote_names_the_per_candidate_setup_it_cannot_avoid(tmp_path):
    """§6: "it costs one extra setup per candidate; that cost is essential, not optional
    hardening". Three independent sources, and the plan's own sketch understated two of
    them — calibration is setup+verify TWICE (§5 step 3, and §5.2 says "x2"), and a builder
    retry gets a FRESH clone (§8.1), so the builder term is seats x attempts.
    """
    q = gate.quote(_report(tmp_path), seats=3, attempts=3, review_rounds=2)
    calibration, builders, verifiers = 2, 3 * 3, 3 + 1 + 2 + 1
    assert q.setup_runs == calibration + builders + verifiers == 18, q.lines
    assert q.setup_runs >= 5, "the plan's floor holds, but at under a third of the real figure"


def test_a_builder_retry_is_a_fresh_clone_and_so_a_fresh_setup(tmp_path):
    """§8.1's discrimination check: attempts is the only input that may move this term."""
    one = gate.quote(_report(tmp_path), seats=3, attempts=1)
    three = gate.quote(_report(tmp_path), seats=3, attempts=3)
    assert three.setup_runs - one.setup_runs == 3 * (3 - 1)
    assert three.verify_runs == one.verify_runs, \
        "a builder clone is never verified, so its retries cannot move the verify count"


def test_the_builders_own_clone_is_not_counted_as_a_verification(tmp_path):
    """§7 lists `Fverify` among a seat's four inventories and §6 overrules it in as many
    words: verification runs in a clone the builder never had, so running the gate where the
    builder was is not one of these runs."""
    q = gate.quote(_report(tmp_path), seats=3, attempts=3, review_rounds=2)
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
    q = gate.quote(_report(tmp_path), seats=3, attempts=3, review_rounds=2)
    per_clone = 10.0 / 3           # §5.2's own upper bound, over the fleet it was stated for
    clones = 1 + 3 * 3 + (3 + 1 + 2 + 1)
    assert clones == 17
    assert q.peak_disk_gb == pytest.approx(round(clones * per_clone, 1), abs=0.05)
    assert q.peak_disk_gb != 10.0, "§5.2's fleet figure is one term of the peak, not the peak"
    assert any("--gc" in line for line in q.lines), q.lines


def test_a_preserved_retry_clone_is_disk_the_peak_has_to_carry(tmp_path):
    """The discrimination check for the term above, on the input §8.1 governs: "every retry
    attempt gets a fresh clone. The failed attempt is PRESERVED as partial input." So two
    extra attempts per seat is six more clones on disk — and not one more verify, since a
    builder clone is never the tree a candidate is verified in (§6).
    """
    one = gate.quote(_report(tmp_path), seats=3, attempts=1)
    three = gate.quote(_report(tmp_path), seats=3, attempts=3)
    assert three.peak_disk_gb - one.peak_disk_gb == pytest.approx(6 * 10.0 / 3, abs=0.05)
    assert three.verify_runs == one.verify_runs


def test_every_line_of_the_quote_names_the_section_it_came_from(tmp_path):
    """`Quote`'s own docstring promises it, and the promise is the point: an operator who
    thinks a number is wrong needs the derivation. Run over the refused shape too, because the
    conditional first line is the one that had no citation."""
    repo = make_repo(tmp_path)
    _git(repo, "update-index", "--skip-worktree", "seed.txt")
    for q in (gate.quote(_report(tmp_path)), gate.quote(preflight.inspect_repo(repo))):
        assert q.lines
        missing = [line for line in q.lines if "§" not in line]
        assert missing == [], missing


def test_wall_clock_is_declined_rather_than_invented(tmp_path):
    """§5.2 asks for wall clock, and preflight cannot answer: the setup and verify commands
    are named at §5 step 2, which is why `Report.gate_surface` is None. A number here would
    be one nobody measured."""
    report = _report(tmp_path)
    assert report.gate_surface is None
    assert any("wall clock: not quoted" in line for line in gate.quote(report).lines)


def test_a_refused_run_is_named_above_its_price(tmp_path):
    """A price for a run preflight already refuses is a price for something that will not
    happen. Paired with the clean repository below, which must not carry the line."""
    repo = make_repo(tmp_path)
    _git(repo, "update-index", "--skip-worktree", "seed.txt")
    blocked = gate.quote(preflight.inspect_repo(repo))
    assert blocked.lines[0].startswith("refused first:"), blocked.lines


def test_a_clean_repository_is_priced_without_a_refusal_line(tmp_path):
    assert not gate.quote(_report(tmp_path)).lines[0].startswith("refused first:")


def test_the_quote_refuses_anything_that_is_not_a_preflight_report(tmp_path):
    with pytest.raises(gate.GateError):
        gate.quote(str(make_repo(tmp_path)))
    with pytest.raises(gate.GateError):
        gate.quote(_report(tmp_path), seats=0)


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
