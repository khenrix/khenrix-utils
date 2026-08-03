"""§11: agreement is provenance, never a correctness argument."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

import hashlib  # noqa: E402
import subprocess  # noqa: E402
import pytest  # noqa: E402
from council import engine  # noqa: E402
from forge import fingerprint  # noqa: E402


def _pi(**kw):
    base = dict(prompt_sha256="a" * 64, semantic_sha256="b" * 64, bundle_sha256="c" * 64,
                cli_path="/usr/bin/x", cli_version="1.0", model_requested="m",
                model_reported="m", plugin_closure_sha256="d" * 64)
    base.update(kw)
    return fingerprint.PromptIdentity(**base)


def test_the_exact_hashes_of_three_seats_can_never_match_by_construction():
    """`make_sentinel` is per seat per attempt, so exact-hash comparison answers
    'differently prompted' 100% of the time."""
    task = "do the thing"
    hashes = set()
    for _ in range(3):
        tok = engine.make_sentinel()
        exact, _ = fingerprint.prompt_hashes(engine.apply_sentinel(task, tok), tok)
        hashes.add(exact)
    assert len(hashes) == 3


def test_the_semantic_hash_of_three_identically_prompted_seats_matches():
    task = "do the thing"
    semantics = set()
    for _ in range(3):
        tok = engine.make_sentinel()
        _, sem = fingerprint.prompt_hashes(engine.apply_sentinel(task, tok), tok)
        semantics.add(sem)
    assert len(semantics) == 1, \
        "this is the only comparison §11's question can be answered with"


def test_stripping_the_token_first_leaves_the_note_behind():
    """MEASURED, and argued at length in `runner._rationale`'s docstring: the note CONTAINS
    the token, so token-first leaves ~280
    characters of engine text that still varies per seat — and a plan that then sees three
    different hashes concludes the seats were differently prompted when they were not."""
    tok = engine.make_sentinel()
    full = engine.apply_sentinel("body", tok)
    token_first = full.replace(tok, "")
    assert engine.SENTINEL_NOTE.format(token=tok) not in token_first
    assert fingerprint.without_engine_text(full, tok).strip() == "body"


def test_the_strip_folds_case_because_read_proof_does():
    tok = engine.make_sentinel()
    shouted = engine.apply_sentinel("body", tok).upper()
    assert tok.lower() not in fingerprint.without_engine_text(shouted, tok).lower()


def test_the_engine_text_rule_has_one_spelling():
    """`runner._rationale` and this function must be the same rule; two spellings of one
    predicate eventually disagree."""
    from forge import runner  # noqa: PLC0415
    tok = engine.make_sentinel()
    text = engine.apply_sentinel("an argued conclusion", tok)
    assert runner._rationale(text, tok) == fingerprint.without_engine_text(text, tok)


def test_a_version_that_could_not_be_read_is_none_never_a_string():
    """`inventory.version` returns "(unknown)" — a STRING — so two seats whose versions could
    not be read compare EQUAL, manufacturing the identity match §11 is about."""
    def failing(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom")
    path, ver = fingerprint.probe_cli("claude", run=failing)
    assert ver is None


def test_a_failed_probe_that_still_printed_is_none():
    """The rc check is the measurement, not the emptiness of stdout.

    A CLI that prints its usage to stdout and exits 2 has said nothing about its version,
    and a probe that read the text anyway would record a usage banner as one — two seats
    whose CLIs both failed the same way then comparing equal on it. Without this the
    return-code branch is unreached: every other failing fixture also has empty stdout, so
    deleting the check changes no answer.
    """
    def usage(argv, **kw):
        return subprocess.CompletedProcess(argv, 2, stdout="usage: claude [options]\n",
                                           stderr="")
    assert fingerprint.probe_cli("claude", run=usage)[1] is None


def test_an_empty_version_is_none_and_stderr_is_never_read():
    """`inventory.version` falls back from stdout to stderr and calls empty output
    "(unknown)". Both halves of that are refused here: rc 0 with nothing on stdout is a
    version that was not measured, whatever the CLI wrote on the other stream."""
    def quiet(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, stdout="   \n", stderr="1.2.3")
    assert fingerprint.probe_cli("claude", run=quiet)[1] is None


def test_a_missing_binary_is_none_not_an_exception():
    def missing(argv, **kw):
        raise FileNotFoundError(argv[0])
    path, ver = fingerprint.probe_cli("nosuchcli", run=missing)
    assert (path, ver) == (None, None)


def test_a_timeout_is_none():
    def slow(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 1)
    assert fingerprint.probe_cli("claude", run=slow)[1] is None


def test_a_clean_probe_records_the_version_verbatim():
    def ok(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, stdout="2.1.220 (Claude Code)\n", stderr="")
    assert fingerprint.probe_cli("claude", run=ok)[1] == "2.1.220 (Claude Code)"


def test_two_unread_versions_do_not_agree():
    a = _pi(cli_version=None)
    b = _pi(cli_version=None)
    assert fingerprint.agreement_label([a, b]) == "not-comparable"
    assert fingerprint.creditable("not-comparable") is False


def test_two_unhashed_bundles_do_not_agree():
    """CandidateBundle.gate_delta's three-state rule: None is 'nobody looked'."""
    assert fingerprint.agreement_label([_pi(bundle_sha256=None),
                                        _pi(bundle_sha256=None)]) == "not-comparable"


def test_two_uninstalled_plugin_closures_do_not_agree():
    assert fingerprint.agreement_label([_pi(plugin_closure_sha256=None),
                                        _pi(plugin_closure_sha256=None)]) == "not-comparable"


def test_identical_seats_are_identically_prompted_and_creditable():
    assert fingerprint.agreement_label([_pi(), _pi(), _pi()]) == "identically-prompted"
    assert fingerprint.creditable("identically-prompted") is True


def test_the_exact_hash_is_not_part_of_the_comparison():
    """It cannot be: the sentinel is per seat. A comparison that read it would label every
    real fleet differently-prompted."""
    assert fingerprint.agreement_label([_pi(prompt_sha256="1" * 64),
                                        _pi(prompt_sha256="2" * 64)]) == \
        "identically-prompted"


def test_a_different_semantic_hash_is_differently_prompted_and_not_creditable():
    assert fingerprint.agreement_label([_pi(), _pi(semantic_sha256="z" * 64)]) == \
        "differently-prompted"
    assert fingerprint.creditable("differently-prompted") is False


def test_a_different_model_is_differently_prompted():
    assert fingerprint.agreement_label([_pi(), _pi(model_requested="other")]) == \
        "differently-prompted"


def test_a_measured_difference_outranks_an_unmeasured_field_in_both_orders():
    """A field nobody hashed does not erase a difference somebody DID measure.

    Two seats on different models are differently prompted, and saying "not comparable"
    because their bundles were never hashed reports a fact this run HAS as one it lacks.
    Both orders, because the answer must not depend on where the None sits in the compared
    list: an early None with a late difference and a late None with an early difference are
    the same evidence and must get the same label.
    """
    early_none = [_pi(bundle_sha256=None, model_requested="a"),
                  _pi(bundle_sha256=None, model_requested="b")]
    late_none = [_pi(semantic_sha256="y" * 64, plugin_closure_sha256=None),
                 _pi(semantic_sha256="z" * 64, plugin_closure_sha256=None)]
    assert fingerprint.agreement_label(early_none) == "differently-prompted"
    assert fingerprint.agreement_label(late_none) == "differently-prompted"


def test_a_field_measured_on_one_seat_and_not_the_other_is_not_a_difference():
    """A `None` beside a value is not evidence of a difference — it is the absence of the
    measurement that would settle it, and calling it `differently-prompted` would report a
    verdict cleaner than the evidence in the other direction."""
    assert fingerprint.agreement_label([_pi(), _pi(bundle_sha256=None)]) == "not-comparable"


def test_a_three_cli_fleet_is_differently_prompted_by_construction():
    """The forge fleet is three CLIs on three models, so `cli_version` and `model_requested`
    differ for every real run and the fleet-wide label is `differently-prompted`. That is
    §11's "labelled weaker", not a defect: the comparison that can return
    `identically-prompted` is between attempts or reruns of the SAME seat."""
    fleet = [_pi(cli_version="2.1.220 (Claude Code)", model_requested="opus-5"),
             _pi(cli_version="codex-cli 0.145.0", model_requested="gpt-5"),
             _pi(cli_version="1.1.9", model_requested="gemini-3")]
    assert fingerprint.agreement_label(fleet) == "differently-prompted"
    assert fingerprint.creditable(fingerprint.agreement_label(fleet)) is False


def test_one_seat_cannot_agree_with_itself_alone():
    with pytest.raises(fingerprint.FingerprintError, match="two"):
        fingerprint.agreement_label([_pi()])


def test_a_thing_that_is_not_a_prompt_identity_cannot_be_compared():
    """A stored row is the shape most likely to arrive here by mistake, and `getattr` over
    a dict raises `AttributeError` — an error from this engine, spelled as one that is
    not."""
    with pytest.raises(fingerprint.FingerprintError, match="PromptIdentity"):
        fingerprint.agreement_label([_pi(), fingerprint.as_row(_pi())])


def test_a_label_this_engine_does_not_know_is_refused_rather_than_scored():
    with pytest.raises(fingerprint.FingerprintError, match="agreement label"):
        fingerprint.creditable("probably-fine")


def test_a_requested_model_is_not_recorded_as_an_observed_one():
    pi = fingerprint.build(prompt="p", token="SENTINEL-abc", cli="claude",
                           model_requested="opus-5", model_reported=None,
                           run=lambda argv, **kw: subprocess.CompletedProcess(
                               argv, 0, stdout="2.1.220\n", stderr=""),
                           closure=lambda cli: "d" * 64)
    assert pi.model_requested == "opus-5" and pi.model_reported is None


def test_build_records_all_four_values_and_round_trips(tmp_path):
    pi = fingerprint.build(prompt="p", token="SENTINEL-abc", cli="claude",
                           bundle_sha256="c" * 64, model_requested="opus-5",
                           model_reported="opus-5",
                           run=lambda argv, **kw: subprocess.CompletedProcess(
                               argv, 0, stdout="2.1.220\n", stderr=""),
                           closure=lambda cli: "d" * 64)
    assert pi.prompt_sha256 == hashlib.sha256(b"p").hexdigest()
    assert pi.bundle_sha256 == "c" * 64
    assert pi.plugin_closure_sha256 == "d" * 64
    assert fingerprint.from_row(fingerprint.as_row(pi)) == pi


def test_an_uninstalled_cli_records_none_rather_than_a_shared_absence():
    """`installed_closure` answers None for a CLI that is not installed, and `build` must
    carry that through unchanged: three uninstalled CLIs recorded as one empty-manifest hash
    would "hash identically" and manufacture §20's licence out of three absences."""
    pi = fingerprint.build(prompt="p", token="SENTINEL-abc", cli="nosuchcli",
                           run=lambda argv, **kw: (_ for _ in ()).throw(
                               FileNotFoundError("nosuchcli")),
                           closure=lambda cli: None)
    assert (pi.plugin_closure_sha256, pi.cli_version, pi.cli_path,
            pi.bundle_sha256) == (None, None, None, None)
    assert fingerprint.agreement_label([pi, pi, pi]) == "not-comparable"


@pytest.mark.parametrize("blank", ["", " ", "   ", "\t", "\n", " \t\n "])
def test_a_blank_string_is_not_a_measurement_whatever_it_is_made_of(blank):
    """`None` is how this module spells "nobody looked", and it is the ONLY spelling.

    A blank version string is the `"(unknown)"` defect wearing different clothes: two seats
    carrying it compare equal and manufacture the identity match, and it is not a fact about
    either CLI.

    PARAMETRIZED RATHER THAN SPELLED OUT, because the enumeration is the defect. `""` was
    refused and `"   "` was not — whitespace is truthy, so `not value` let it past — and
    measured before the fix, `agreement_label([_pi(cli_version="   ")] * 2)` returned
    `identically-prompted` with `creditable` True. The rule the code now states is a property
    of the value, so this test asserts the property holds across the shapes rather than
    growing one more `with` block per shape someone thinks of.
    """
    with pytest.raises(fingerprint.FingerprintError, match="non-empty"):
        _pi(cli_version=blank)
    with pytest.raises(fingerprint.FingerprintError, match="non-empty"):
        _pi(semantic_sha256=blank)


def test_a_hash_that_is_absent_is_refused_because_it_is_never_unmeasured():
    """`prompt_sha256`/`semantic_sha256` are computed from bytes this engine already has;
    there is no failure that produces one and not the other, so `None` there is a
    record that was assembled wrongly rather than a measurement that failed."""
    with pytest.raises(fingerprint.FingerprintError, match="prompt_sha256"):
        _pi(prompt_sha256=None)
    with pytest.raises(fingerprint.FingerprintError, match="semantic_sha256"):
        _pi(semantic_sha256=None)


def test_a_row_missing_a_field_is_refused_rather_than_defaulted():
    row = fingerprint.as_row(_pi())
    del row["cli_version"]
    with pytest.raises(fingerprint.FingerprintError, match="missing"):
        fingerprint.from_row(row)


def test_a_row_with_an_unknown_field_is_refused():
    row = fingerprint.as_row(_pi())
    row["novel"] = 1
    with pytest.raises(fingerprint.FingerprintError, match="does not know"):
        fingerprint.from_row(row)


def test_a_row_whose_field_is_not_a_string_is_refused():
    row = fingerprint.as_row(_pi())
    row["cli_version"] = 2.1
    with pytest.raises(fingerprint.FingerprintError, match="cli_version"):
        fingerprint.from_row(row)


@pytest.mark.parametrize("blank", ["", "   ", "\n"])
def test_a_row_carrying_a_blank_string_is_refused_like_any_other_non_measurement(blank):
    """`from_row`'s own docstring calls a stored record an untrusted shape, and this is the
    route a tampered or corrupted one takes to reach the comparison."""
    row = fingerprint.as_row(_pi())
    row["plugin_closure_sha256"] = blank
    with pytest.raises(fingerprint.FingerprintError, match="non-empty"):
        fingerprint.from_row(row)


def test_a_row_that_is_not_an_object_is_refused():
    with pytest.raises(fingerprint.FingerprintError, match="object"):
        fingerprint.from_row(["a" * 64])


def test_as_row_refuses_anything_that_is_not_a_prompt_identity():
    with pytest.raises(fingerprint.FingerprintError, match="PromptIdentity"):
        fingerprint.as_row({"prompt_sha256": "a" * 64})
