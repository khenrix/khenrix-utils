"""§11: per-seat prompt identity — agreement is provenance, never a correctness argument.

The trajectories are NOT statistically independent: they share the task text, the repository's
conventions and `CLAUDE.md`, the same test suite, the same skill instructions and correlated
model biases. A repeated mistake can be 3/3; the only correct fix can be unique to one. So this
file records what conditions the agreement, and NOTHING here promotes anything.

NOT NAMED `identity`. `fleet.clone_seat(..., identity=...)` and `runner.run_seat(...,
identity=...)` already mean the git author `(name, email)` pair, refused when either half is
empty because a seat that cannot commit is unusable. Reusing the word is how the two get
conflated in a review six weeks from now.

WHY THE SEMANTIC HASH IS THE ONLY COMPARISON THAT MEANS ANYTHING. `engine.make_sentinel` is
called PER SEAT, PER ATTEMPT, not once per run — so three seats' EXACT prompt hashes can never
match, by construction. Code that compares exact hashes to decide "identically prompted"
answers "differently prompted" 100% of the time and labels every agreement weaker. The exact
hash is still recorded, because it is the provenance of what was actually sent; it is simply
not what the comparison reads.

WHAT A `None` MEANS HERE, ON EVERY FIELD THAT HAS ONE: nobody looked. It must NEVER compare
equal to another `None` for "the same". `bundle.CandidateBundle.gate_delta`'s three-state rule
is the in-repo precedent, and §11 is where it matters most: two seats whose versions could not
be read, recorded as one string, would MANUFACTURE the identity match this section is written
to make honest. `None` is also the ONLY spelling of it, and that is stated as a PROPERTY
rather than as a list of the spellings anyone has thought of: a value that is not a non-empty
STRIPPED string records no measurement, and is refused at construction. The list is how this
was got wrong three times — the brief said `None`, then `""` was found admitted, then `"   "`
was found admitted by the guard written to refuse `""` — and each time the fix was one more
`or`. `"   "` compares equal to `"   "` exactly as `""` and `"(unknown)"` do, and so does
whichever character someone types next; the property is what leaves nothing left to enumerate.
"""
import hashlib
import re
import shutil
import subprocess
from dataclasses import dataclass, fields

from council import engine

from . import taskbundle

# How long a `--version` probe may take. A CLI that cannot say its own version in this long is
# a CLI whose version was not measured, which is `None`.
_VERSION_TIMEOUT = 20

LABELS = ("identically-prompted", "differently-prompted", "not-comparable")

# The two fields computed from bytes this engine already holds. There is no failure that
# produces one and not the other, so `None` there is a record assembled wrongly rather than a
# measurement that failed — and the rest of this module may assume both are present.
_ALWAYS_MEASURED = ("prompt_sha256", "semantic_sha256")


class FingerprintError(RuntimeError):
    """This seat's prompt identity cannot be recorded or compared honestly."""


def _absent(value) -> bool:
    """Whether this value records no measurement — §11's "nobody looked", as ONE predicate.

    THE POINT IS THAT IT IS NOT A LIST. Written as an enumeration this guard has been wrong
    three times, each fix adding the spelling the last review happened to try; a value that is
    not a non-empty string AFTER STRIPPING is absent, and there is no fourth spelling to find
    because the rule no longer names any. Whitespace is the one that got through: `"   "` is
    truthy, so `not value` admitted it, and two seats carrying it scored `identically-prompted`
    — an unread version manufacturing the identity match §11 exists to make honest.

    `probe_cli` already answers in exactly this shape, stripping its stdout before choosing
    between the string and `None`. This is that same rule at the door every other path in and
    out of a record — `build`, `from_row`, a caller assembling one by hand — has to pass.
    """
    return not isinstance(value, str) or not value.strip()


@dataclass(frozen=True)
class PromptIdentity:
    """§11's four values, spread across eight fields because two of them are pairs.

    `prompt_sha256` — the exact text handed to the CLI. Captured BEFORE spec construction: for
    codex the prompt is `ProviderSpec.stdin`, for claude and agy it is buried inside `argv`, so
    recovering "the prompt" from a spec is per-provider guesswork.

    `semantic_sha256` — the same prompt with the engine's own text removed. See
    `without_engine_text`.

    `bundle_sha256` — §20's task-bundle manifest hash, or None when no bundle was supplied.

    `cli_path` / `cli_version` / `model_requested` / `model_reported` /
    `plugin_closure_sha256` — §11's fourth value, which is four sub-values with four different
    failure modes. MODEL IS TWO FIELDS: `ProviderSpec.model` is what forge REQUESTED, and the
    provider's own envelope is what it REPORTED. Collapsing them records a request as an
    observation.

    The CLI binary is recorded as a resolved absolute PATH and never hashed — these are
    multi-hundred-megabyte node bundles and the hash would dominate seat setup.

    EVERY FIELD IS A NON-EMPTY STRING — `_absent`'s sense of non-empty, so stripped — OR
    `None`, CHECKED HERE AND NOWHERE ELSE. One gate rather than one per constructor: `build`
    assembles from live measurements and `from_row` from a stored record, and a rule spelled
    twice is a rule that eventually holds in one of them.
    """
    prompt_sha256: str
    semantic_sha256: str
    bundle_sha256: str | None
    cli_path: str | None
    cli_version: str | None
    model_requested: str | None
    model_reported: str | None
    plugin_closure_sha256: str | None

    def __post_init__(self):
        for f in fields(self):
            value = getattr(self, f.name)
            optional = f.name not in _ALWAYS_MEASURED
            # `None` is checked by identity and ONLY here, which is what makes it the single
            # spelling of absence the rest of the module may compare against; every other way
            # of being absent is `_absent`'s business and is refused rather than admitted.
            if value is None and optional:
                continue
            if _absent(value):
                raise FingerprintError(
                    f"{f.name} is a non-empty string{' or None' if optional else ''}, "
                    f"not {value!r}")


def without_engine_text(text: str, token: str) -> str:
    """`text` with the text FORGE supplied removed — the nonce-stripping rule, spelled once.

    THE ORDER IS LOAD-BEARING AND MEASURED. `runner._rationale`'s docstring is where it was
    measured and argues it at length; that function now calls this one, so this is the only
    copy of the strip. `engine.SENTINEL_NOTE` is
    ~280 characters of instruction CONTAINING the token; strip the token first and the note no
    longer matches itself, leaving a "semantic" hash that still varies per seat — and a caller
    that then sees three different hashes concludes the seats were differently prompted when
    they were identically prompted.

    Case-insensitively, because `seat.read_proof` folds case when it looks for the same token:
    if a differently-cased echo counts as proof, it has to count as engine text here too, or
    one spelling would be proof AND content at once.

    WHAT THIS DOES NOT DO: it removes the note's EXACT text as `engine.apply_sentinel` writes
    it. A reflowed or paraphrased echo survives. It closes the one echo the engine's own
    instruction makes free, which is the one every seat is invited to make.

    `runner._rationale` DELEGATES HERE. Two spellings of one predicate eventually disagree, and
    this one decides both §8's rationale floor and §11's semantic hash.
    """
    if not token:
        return text
    for supplied in (engine.SENTINEL_NOTE.format(token=token), token):
        text = re.sub(re.escape(supplied), "", text, flags=re.IGNORECASE)
    return text


def prompt_hashes(prompt: str, token: str) -> tuple[str, str]:
    """`(exact, nonce-stripped)`, in that order."""
    exact = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    semantic = hashlib.sha256(
        without_engine_text(prompt, token).encode("utf-8")).hexdigest()
    return exact, semantic


def probe_cli(name: str, *, run=subprocess.run) -> tuple[str | None, str | None]:
    """`(absolute path, version)` for `name`, with `None` for anything unmeasured.

    NOT `inventory.version`, AND THIS IS THE ONE THAT WILL BE GOT WRONG. That function ignores
    the return code, falls back from stdout to stderr, swallows every exception into the string
    "(unavailable: ...)" and returns "(unknown)" for empty output. Those are STRINGS: two seats
    whose versions could not be read both record "(unknown)" and COMPARE EQUAL — an unread
    version manufacturing an identity match, which is precisely §11's "agreement is provenance"
    being fabricated.

    rc == 0 is REQUIRED, and stderr is never read. `seat.read_proof`'s rule, one module over:
    a missing measurement fails closed — it returns False, not True.

    THE PROBE IS ALWAYS SPAWNED, even for a name `shutil.which` could not resolve. Skipping it
    would answer `(None, None)` for the same case by a path that never exercises the refusals
    below, and the refusals are what the answer means.

    Measured on this machine (2026-08-03): `claude --version` -> "2.1.220 (Claude Code)",
    `codex --version` -> "codex-cli 0.145.0", `agy --version` -> "1.1.10", all rc 0. That
    contradicts `scripts/lib/inventory.py:50`, which routes agy through `agy changelog` because
    `--version` was believed absent. THE THREE FORMATS SHARE NOTHING — no prefix, no shape —
    so the recorded string is the CLI's own answer verbatim and is only ever compared with
    another answer from the same CLI.
    """
    path = shutil.which(name)
    try:
        r = run([name, "--version"], capture_output=True, text=True,
                timeout=_VERSION_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return path, None
    if r.returncode != 0:
        return path, None
    out = (r.stdout or "").strip()
    return path, out or None


def build(*, prompt: str, token: str, cli: str, bundle_sha256=None,
          model_requested=None, model_reported=None, run=subprocess.run,
          closure=taskbundle.installed_closure) -> PromptIdentity:
    """The whole fingerprint for one seat.

    ONE `<cli> --version` SUBPROCESS PER CALL, declared here rather than discovered by strace
    later: `run` defaults to `subprocess.run`, so a caller that injects this builder without
    threading a `run=` through spawns one probe per seat launch, bounded at `_VERSION_TIMEOUT`
    and failing closed to `None`.

    `closure` is injected so the suite can exercise the record without depending on which CLIs
    happen to be installed on the machine running it. Its default is the real resolver, which
    returns None for a CLI that is not installed — and None must fail the equality test, or
    three uninstalled CLIs "hash identically" and §20's licence to use an ambient skill is
    manufactured out of three absences.
    """
    exact, semantic = prompt_hashes(prompt, token)
    path, version = probe_cli(cli, run=run)
    return PromptIdentity(exact, semantic, bundle_sha256, path, version,
                          model_requested, model_reported, closure(cli))


def as_row(pi: PromptIdentity) -> dict:
    if not isinstance(pi, PromptIdentity):
        raise FingerprintError(f"a PromptIdentity is required, not {type(pi).__name__}")
    return {f.name: getattr(pi, f.name) for f in fields(PromptIdentity)}


def from_row(row) -> PromptIdentity:
    """The seat record's stored fingerprint, type-checked.

    Missing refused, unknown refused. A field defaulted here is a fact the run never measured,
    read back as one it did — §8's `proven_read`/`partial` rule: a measurement that was never
    taken is `partial`, not a free pass. The per-field rules are `PromptIdentity`'s own and are
    not restated: this function decides only which KEYS may be present.
    """
    if not isinstance(row, dict):
        raise FingerprintError(f"a prompt identity is an object, not {type(row).__name__}")
    names = [f.name for f in fields(PromptIdentity)]
    missing = [n for n in names if n not in row]
    if missing:
        raise FingerprintError(f"this prompt identity is missing {missing}")
    unknown = sorted(set(row) - set(names))
    if unknown:
        raise FingerprintError(
            f"this prompt identity carries fields this engine does not know: {unknown}")
    return PromptIdentity(**{n: row[n] for n in names})


# WHAT IS COMPARED, and why `prompt_sha256` is not in it: the sentinel is minted per seat per
# attempt, so the exact hashes are different by construction and a comparison reading them
# would label every real fleet differently-prompted. Everything here is a fact about what the
# seat was GIVEN, not about the nonce it was given it with. `cli_path` is out for a weaker
# reason than `model_reported`'s: the three CLIs install to three different absolute paths, so
# comparing it would answer for the filesystem rather than for the prompt.
_COMPARED = ("semantic_sha256", "bundle_sha256", "cli_version", "model_requested",
             "plugin_closure_sha256")


def agreement_label(ids) -> str:
    """How comparable these seats were: one of `LABELS`.

    THREE VALUES, NOT TWO. "We could not tell" must not be spelled the same way as "no" — but
    it must not credit agreement either, which is why `creditable` is True for exactly one of
    them. A `None` in any compared field is `not-comparable`: two seats with
    `bundle_sha256=None` are two seats whose bundles were never hashed, not two seats with the
    same bundle. `is None` IS THE WHOLE TEST, and that is a consequence of `_absent` rather
    than a second opinion about it: nothing carrying an absence spelled any other way can reach
    here, because `PromptIdentity` refused it at construction.

    A MEASURED DIFFERENCE OUTRANKS AN UNMEASURED FIELD, so the answer does not depend on where
    the `None` sits in `_COMPARED`. Two seats on different models are differently prompted
    whether or not their bundles were hashed; reporting `not-comparable` there would spend the
    weaker word on evidence this run actually has, and would change label if the compared list
    were ever reordered. The reverse is not symmetric: a `None` beside a value is the absence
    of the measurement that would settle it, never a difference.

    `model_reported` is NOT compared — it is `None` for any provider without an envelope, which
    would make every codex/claude pair not-comparable for a reason that is about the envelope
    rather than about the prompt. It is recorded, and a report that wants it says so.

    WHAT A REAL FLEET GETS. The three seats are three different CLIs, so `cli_version` differs
    for every run forge performs and the fleet-wide label is `differently-prompted` — §11's
    "labelled weaker", stated rather than discovered. The comparison that can return
    `identically-prompted` is between attempts or reruns of the SAME seat.

    `model_requested` DOES NOT CARRY ANY OF THAT WEIGHT, though this paragraph used to say
    "three CLIs on three models" and rest half the argument on it. `launch.make_launcher` fills
    it from `spec.model`, which is `build_real_spec`'s reading of a caller-supplied `cfg`;
    with no `cfg` it is `None` for claude, codex and agy alike (measured), and by the paragraph
    above an absence on both sides is not a difference. It differs only when a caller names a
    different model per provider, and today no production caller names one at all. `cli_version`
    is what actually reaches the verdict.

    NOTHING DOWNSTREAM MAY TREAT `identically-prompted` AS A CORRECTNESS ARGUMENT. §11's last
    line: agreement never substitutes for one.
    """
    ids = list(ids)
    if len(ids) < 2:
        raise FingerprintError(
            "an agreement label describes at least two seats; one seat agreeing with itself "
            "is not a measurement")
    wrong = [type(pi).__name__ for pi in ids if not isinstance(pi, PromptIdentity)]
    if wrong:
        raise FingerprintError(
            f"an agreement label compares PromptIdentity records, not {sorted(set(wrong))}")
    verdict = "identically-prompted"
    for name in _COMPARED:
        values = [getattr(pi, name) for pi in ids]
        if any(v is None for v in values):
            verdict = "not-comparable"
        elif len(set(values)) != 1:
            return "differently-prompted"
    return verdict


def creditable(label: str) -> bool:
    """Whether this label lets a consumer treat the seats' agreement as conditioned evidence.

    True for exactly one label. `differently-prompted` is §11's "labelled weaker" and
    `not-comparable` is "nobody measured" — neither is agreement, and a consumer that folded
    them together would lose which one it had.
    """
    if label not in LABELS:
        raise FingerprintError(f"an agreement label is one of {list(LABELS)}, not {label!r}")
    return label == "identically-prompted"
