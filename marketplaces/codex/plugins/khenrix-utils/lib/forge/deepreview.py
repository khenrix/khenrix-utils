"""The post-fusion deep review: an llm-council fan-out over the fused synthesis diff.

IT ANSWERS A DIFFERENT QUESTION FROM THE §13 COUNCIL LOOP. That panel checks requirement
coverage against the task; this one hunts BUGS in what was actually fused. It runs once,
after the council loop has terminated, and its findings get the same post-round-2 treatment
§13 gives any late blocker.

WHY THIS IS SMALL. It replaced a cloud-review integration that was 564 lines, most of them
about a remote this module no longer talks to: a minutes-vs-seconds timeout unit, the
remote's own 500-file/8,000-line diff limits, guessed phrasings for a zero-data-retention
org / missing login / disabled usage credits, session-URL scraping, and a subprocess exit
classifier. Running three local seats deletes all of it. What survives is the part that was
never about the cloud — measuring the diff — plus a findings contract.

THE FAILURE VOCABULARY IS DIFFERENT, WHICH IS WHY THIS IS NOT A RENAME. `no_auth`,
`zdr_org`, `usage_credits_off` describe a remote service; a local panel fails by having no
valid seats, or by answering unreadably. Keeping the old names over the new mechanism would
be documentation asserting behaviour the code does not have.

UNREADABLE IS NOT "FOUND NOTHING". Three seats can answer at length and still produce no
parseable findings payload. Recording that as `ran` with zero findings is the false green
every record in this package is written against, so it is `unavailable/unreadable_output`.
"""
import dataclasses
import json
import re
from dataclasses import dataclass
from pathlib import Path

from . import gitcmd, review

try:                                    # the bundled plugin layout puts council beside forge
    from council import engine
except ImportError:                     # pragma: no cover - exercised by the packaging gate
    engine = None

RAN = "ran"
UNAVAILABLE = "unavailable"
TIMED_OUT = "timeout"
SKIPPED = "skipped"
STATUSES = (RAN, UNAVAILABLE, TIMED_OUT, SKIPPED)

# Every reason names something THIS mechanism can actually be, which the cloud vocabulary
# it replaced could not: `engine_unavailable` (the council engine is not importable),
# `no_valid_seats` (the panel ran and every seat failed), `unreadable_output` (seats
# answered and none produced a findings payload), `diff_too_large` (our own prompt cap,
# not a remote's), `not_requested` (the operator opted out).
REASONS = ("engine_unavailable", "no_valid_seats", "unreadable_output",
           "diff_too_large", "not_requested")

# The fused diff goes into a prompt, so the cap is OURS and it is in bytes — not the
# remote's file/line limits, which described a service this module no longer calls.
DIFF_BYTE_CAP = 400_000

SEATS = ("claude", "codex", "agy")
_SEVERITY_MAP = {"blocker": "blocker", "critical": "blocker", "high": "blocker",
                 "major": "blocker", "medium": "minor", "low": "minor",
                 "minor": "minor", "nit": "minor", "info": "minor"}


class DeepReviewError(RuntimeError):
    """A caller defect: a record that could not be true, or a revision that is not one."""


@dataclass(frozen=True)
class DiffSize:
    """The change the deep review is being asked to look at, and what could not be counted.

    `files is None` is a measurement that was VOIDED — the engine does not know which files
    the diff touches either — and is a different fact from `lines is None`, which is a file
    list this run has and a line count git declined to give it. `why` carries which.

    IT VALIDATES ITSELF, like every other record in this package. It was the one that did not,
    and the two things it now refuses are the two that would make it read cleaner than the
    measurement behind it: a voided file count beside a line count (nothing could produce
    one, and a consumer reading `lines` would be reading a number taken over a file list this
    run does not have), and an absent count with no `why` — which is a refusal that declines
    to say what it refused, and `run_deep_review` puts `why` in the operator's line.
    """
    files: int | None
    lines: int | None
    why: str

    def __post_init__(self) -> None:
        for name in ("files", "lines"):
            v = getattr(self, name)
            if v is None:
                continue
            # `bool` explicitly: `True` is an `int` and would compare against the prompt-size cap
            # as 1, so a diff of "yes" would pass for a diff of one file.
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                raise DeepReviewError(f"{name} is a count or None, not {v!r}")
        if not isinstance(self.why, str):
            raise DeepReviewError(f"why is text, not {type(self.why).__name__}")
        if self.files is None and self.lines is not None:
            raise DeepReviewError(
                f"this record voids the file count and states {self.lines} changed line(s): a "
                "line total is summed over the file list the same read produced, so there is "
                "no measurement it could have come from")
        if (self.files is None or self.lines is None) and not self.why.strip():
            raise DeepReviewError(
                "a count this run could not take is a count this run says why it could not "
                "take; an unexplained `None` reaches the operator as a blank in the line "
                "`run_deep_review` writes about why the review was or was not requested")


def _numstat_z(blob: str) -> tuple:
    """`git diff --numstat -z`'s whole stream as `(files, lines, refusals)`.

    A STREAM PARSER AND NOT A RECORD PARSER, WHICH IS THE WHOLE REASON THIS EXISTS RATHER
    THAN CALLING `strategy._numstat_record`. Measured on git 2.53.0, `git diff --numstat -z`
    emits a RENAME as three NUL-terminated fields — an empty path cell, then the source name,
    then the destination:

        1\\t1\\t\\0old.txt\\0sub/new.txt\\0

    so the two names are not in the record that introduces them. A reader that splits each
    NUL field on TAB independently gets `["old.txt"]`, one field, and raises `ValueError: not
    enough values to unpack` out of the middle of a size measurement — for the most ordinary
    thing a synthesis agent does. `git apply --numstat -z`, which `strategy._numstat` and
    `bundle._patch_paths` use, does NOT emit that form: measured on the same patch it gives
    `1\\t1\\tsub/new.txt\\0`, one three-field record naming the destination. That is why their
    record-at-a-time parser is correct there and cannot be borrowed here.

    A rename counts as ONE file at added+deleted lines, which is what plain `--numstat`
    reports for the same change (`old.txt => sub/new.txt`, 1 and 1). Whether git offers the
    rename form at all is the repository's own `diff.renames` to decide, and both shapes are
    read; that key names no program, so unlike the presets on the call it is not a hazard.

    THREE WAYS THIS STREAM CAN FAIL TO BE A COUNT, and none of them is zero — `strategy`'s
    enumeration, extended by the rename form above:

      * `-` in either cell is a BINARY file. `int("-")` raises, and a `try/except: continue`
        silently drops the path's lines, which is how a whole-blob rewrite lands under
        the prompt-size cap. The path is still counted; only its lines are refused.
      * A cell that is neither `-` nor a number is something git has never been measured to
        emit, so this is a refusal to guess past it rather than a reading of its behaviour.
        It is reported SEPARATELY from the binary case: git measured one and declined to
        state it, and did something unrecognized in the other.
      * A record that is not three fields, or a rename whose two names are not both there,
        describes NO path — the one failure that cannot be attributed to a file. The whole
        measurement is voided, because a caller that dropped the record would go on reporting
        a file count over the records it could read.
    """
    fields = blob.split("\0")
    files, lines, refusals = 0, 0, []
    i = 0
    while i < len(fields):
        rec, i = fields[i], i + 1
        if not rec:
            continue
        parts = rec.split("\t", 2)
        if len(parts) != 3:
            return None, None, (
                f"git emitted a --numstat record of {len(parts)} field(s) rather than three "
                f"({rec!r}), so neither the path it describes nor that path's line count can "
                "be read off it, and this diff's file list is not one this run measured",)
        added, deleted, path = parts
        if not path:
            # The rename/copy form: the next two fields are <source> and <destination>.
            if i + 1 >= len(fields) or not fields[i] or not fields[i + 1]:
                return None, None, (
                    "git emitted a rename record whose source and destination names are not "
                    "both in the stream that follows it, so one of this diff's files is not "
                    "one this run can name",)
            path, i = f"{fields[i]} -> {fields[i + 1]}", i + 2
        files += 1
        if added == "-" or deleted == "-":
            refusals.append(f"{path}: git reports a binary delta (`-`), so its changed-line "
                            "count is not a number this run measured")
            continue
        try:
            lines += int(added) + int(deleted)
        except ValueError:
            refusals.append(f"{path}: git gave the changed-line counts as {added!r} and "
                            f"{deleted!r}, which are not two numbers this run can add")
    return files, (None if refusals else lines), tuple(refusals)


# How many per-path refusals `measure_diff`'s `why` spells out. `record_worktree_after` is
# the sibling rule: a bounded SAMPLE is never read as the whole, so the count goes beside it.
# The version without one printed five lines out of fifty and read as five refusals — the
# reader's own arithmetic ("500 files, 5 unmeasured") then came out wrong by an order of
# magnitude, on a record whose entire job is to say how much of the diff was measured.
_REFUSAL_SAMPLE = 5


def _why(refusals) -> str:
    """`measure_diff`'s explanation: a bounded sample of the per-path refusals, and how many
    there were. Empty when nothing was refused, which is what `DiffSize` requires of a
    measurement that is complete."""
    refusals = tuple(refusals)
    if not refusals:
        return ""
    shown = "; ".join(refusals[:_REFUSAL_SAMPLE])
    if len(refusals) <= _REFUSAL_SAMPLE:
        return shown
    return (f"{len(refusals)} path(s) were refused a changed-line count; the first "
            f"{_REFUSAL_SAMPLE}: {shown}")


def _revision(name: str, value) -> str:
    """A checkpoint this module will hand `git diff` as one side of a range.

    MEASURED: `git diff --numstat -z "..<head>"` exits 0 and prints NOTHING, because the empty
    side resolves to HEAD and HEAD is the head. So an unresolved base does not fail — it
    reports a 0-file, 0-line diff, comfortably under the prompt-size cap, and this module pays for
    a review of a change it never measured. A leading `-` is the other direction: the value
    reaches git's argument parser as an option rather than as a revision.
    """
    if not isinstance(value, str) or not value.strip():
        raise DeepReviewError(
            f"{name} names the checkpoint being reviewed and is a non-empty string, not "
            f"{value!r}; an empty side of a `..` range resolves to HEAD, and the empty diff "
            "that follows reads as a change small enough to review")
    if value.startswith("-"):
        raise DeepReviewError(f"{name} is a revision, not an option: {value!r}")
    return value


def measure_diff(checkout, base: str, head: str) -> DiffSize:
    """`git diff --numstat` between the two checkpoints, refusing every count it cannot take.

    NO_DAEMON_CACHE because `diff` LOADS AN INDEX — measured on git 2.53.0, `diff` (including
    `diff <commit> -- <pathspec>`) runs `core.fsmonitor`, and the value is a PROGRAM out of the
    repository's own config. The presets go BEFORE the subcommand; after it git answers
    `error: unknown switch 'c'`, rc 129.

    NO_DIFF_DRIVERS AFTER THE SUBCOMMAND, because `--no-ext-diff`/`--no-textconv` are diff
    OPTIONS and not `-c` presets. This is the synthesis checkout — a tree whose `.git/config`
    and `.gitattributes` the synthesis agent owned for its whole run — so `diff.external` and
    `diff.<d>.command`/`textconv` each name a program `git diff` would run here, and
    `core.hooksPath=/dev/null` stops none of them. `bundle._rediff` (`bundle.py:265`) and
    `harvest.artifact_set` (`harvest.py:190`) are the two existing sites and this is the
    third; `test_forge_seams.py`'s
    `test_every_diff_producing_call_disables_the_repositorys_diff_drivers` fails the build
    without it.

    TWO FAILURE MODES AND THEY ARE NOT THE SAME. A `base` or `head` that is not a revision is
    a caller defect and RAISES — nothing degrades, because the run asked for the wrong
    measurement. A measurement git could not give returns a `DiffSize` that says so.
    """
    base, head = _revision("base", base), _revision("head", head)
    r = gitcmd.git(checkout, *gitcmd.NO_DAEMON_CACHE, "diff", *gitcmd.NO_DIFF_DRIVERS,
                   "--numstat", "-z", f"{base}..{head}", "--",
                   env_extra=gitcmd.READONLY, check=False, binary=True)
    if r.returncode != 0:
        return DiffSize(None, None,
                        f"git diff --numstat -> {r.returncode}: "
                        f"{r.stderr.decode('utf-8', 'replace').strip()}")
    files, lines, refusals = _numstat_z(r.stdout.decode("utf-8", "surrogateescape"))
    return DiffSize(files, lines, _why(refusals))


@dataclass
class DeepReview:
    """What the post-fusion review produced, and how much of it was measured.

    `findings` is `None` — never `()` — for every status but `ran`. An empty tuple means
    "the review ran and reported nothing", which is precisely the sentence a review that
    never happened must not be able to write.

    `reason` runs the same rule the other way: it belongs to `unavailable` and to nothing
    else. A record carrying both `ran` and `no_valid_seats` answers differently depending
    on which field a consumer reads, which is the same defect one field over.

    `seats` replaces the cloud record's `session_url`: for a local panel the useful
    provenance is WHICH seats answered, and it is also what makes a degraded review
    (1 of 3) visible instead of reading like a full one.
    """
    status: str
    reason: str | None
    findings: tuple | None
    seats: tuple
    diff_measured: bool
    detail: str

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise DeepReviewError(f"status is one of {list(STATUSES)}, not {self.status!r}")
        if self.reason is not None and self.reason not in REASONS:
            raise DeepReviewError(f"reason is one of {list(REASONS)} or None, "
                                  f"not {self.reason!r}")
        if self.status == RAN and self.findings is None:
            raise DeepReviewError("a review that ran reports its findings, even when there "
                                  "are none; `None` is what a review that did not carries")
        if self.status != RAN and self.findings is not None:
            raise DeepReviewError(
                f"a {self.status!r} review has no findings to report, and an empty tuple "
                "here would read as a review that ran and found nothing")
        if self.status == UNAVAILABLE and self.reason is None:
            raise DeepReviewError("an unavailable review names which reason it was")
        if self.status != UNAVAILABLE and self.reason is not None:
            raise DeepReviewError(
                f"a {self.status!r} review carries no unavailability reason, and "
                f"{self.reason!r} here answers 'why did the review not happen' about a "
                "review whose status says it did")
        if not isinstance(self.seats, tuple):
            raise DeepReviewError(f"seats is a tuple, not {type(self.seats).__name__}")


PROMPT = """Review this diff for BUGS — do not modify anything; answer in your final message.

This is the fused result of several agents implementing one task. Hunt for defects that
would fire in practice: wrong results, broken edge cases, unhandled failures, logic that
contradicts a nearby comment or docstring. Ignore style and preference.

Ground every finding in the diff. Prefer one strong finding over several weak ones. If you
find nothing, say so explicitly and name what you checked.

Output your findings as a JSON object in a ```json fence, then any prose you want after it:

```json
{"findings": [{"severity": "blocker|minor", "location": "path:line or path",
               "description": "what is wrong and why it fires"}]}
```

An empty list is a valid answer. The fence must be present either way.

DIFF UNDER REVIEW:
"""


def _parse_findings(answer: str, seat: str, checkpoint_round: int) -> tuple:
    """One seat's answer -> `review.Finding`s, or `()` when it carried no readable payload.

    RETURNS EMPTY RATHER THAN RAISING, because one seat answering in prose must not discard
    two seats that answered correctly. `run_deep_review` distinguishes "no seat was
    readable" (unavailable/unreadable_output) from "the readable seats found nothing"
    (ran, empty) — a distinction this function deliberately cannot make on its own.
    """
    m = re.search(r"```json\s*(\{.*?\})\s*```", answer or "", re.DOTALL)
    blob = m.group(1) if m else None
    if blob is None and "{" in (answer or ""):
        blob = answer[answer.find("{"):answer.rfind("}") + 1]
    if not blob:
        return ()
    try:
        payload = json.loads(blob)
    except json.JSONDecodeError:
        return ()
    rows = payload.get("findings") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return ()
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        sev = _SEVERITY_MAP.get(str(row.get("severity", "")).lower())
        if sev is None:
            continue
        claim = str(row.get("description") or "").strip()
        if not claim:
            continue
        where = str(row.get("location") or "").strip()
        f = review.Finding(
            id=review.finding_id(checkpoint_round, seat, sev, claim, where),
            round=checkpoint_round, seat=seat, severity=sev, claim=claim,
            # `review.Finding` refuses an empty evidence string, so a row that named no
            # location says so in words rather than being dropped — a finding out of a
            # review that cost three provider calls is not discarded over a missing field.
            evidence=where or f"{seat} named no location for this finding",
            resolution="open")
        # Content-derived ids collide when a seat restates one finding twice — ordinary
        # model output, not malformed input — and `Round` refuses duplicate ids.
        if not any(f.id == prior.id for prior in out):
            out.append(f)
    return tuple(out)


def _council(prompt: str, workdir: Path, *, mode: str = "deep", seats=SEATS):
    """One read-only council fan-out. Returns the engine's manifest.

    READ-ONLY IS MECHANICAL, NOT ASKED FOR. `make_readonly` swaps each provider's bypass
    flag for a plan-only posture, and agy additionally gets a throwaway worktree cwd. A
    reviewer that can write is a reviewer that can "fix" the diff it was asked to judge.
    """
    cfg = engine.MODES[mode]
    timeout = engine.MODE_TIMEOUT[mode]
    workdir.mkdir(parents=True, exist_ok=True)
    specs, worktrees = [], []
    for name in seats:
        spec = engine.build_real_spec(name, prompt, timeout, cfg, workdir)
        engine.make_readonly(spec)
        if spec.name == "agy":
            worktrees.append(engine.isolate_agy_worktree(spec, workdir))
        specs.append(spec)
    try:
        return engine.run_council(specs, retries=1, timeout=timeout, backoff=2.0,
                                  workdir=workdir, prompt=prompt, mode=mode, read_only=True)
    finally:
        for wt in worktrees:
            engine.remove_agy_worktree(wt)


def run_deep_review(run_dir, *, checkout, base: str, head: str, round_: int,
                    enabled: bool = True, mode: str = "deep", council=None) -> DeepReview:
    """The post-fusion pass, from the synthesis checkout, after the council loop terminated.

    ORDER: the local pre-flight first, so a diff definitely over the prompt cap costs
    nothing; then one fan-out; then the payloads.

    AN UNMEASURABLE DIFF STILL RUNS. Refusing on a byte count nobody could take would
    report `diff_too_large` about a diff whose size is unknown — a reason stated more
    confidently than the evidence. The record carries `diff_measured=False` instead.

    `council` is injected so the suite can drive every branch without spending a seat.
    """
    if not enabled:
        return DeepReview(SKIPPED, None, None, (), False,
                          "the operator opted out with --skip-deep-review")
    if engine is None and council is None:
        return DeepReview(UNAVAILABLE, "engine_unavailable", None, (), False,
                          "the llm-council engine is not importable from this install, so "
                          "no panel could be convened")

    size = measure_diff(checkout, base, head)
    measured = size.files is not None
    r = gitcmd.git(checkout, *gitcmd.NO_DAEMON_CACHE, "diff", *gitcmd.NO_DIFF_DRIVERS,
                   f"{base}..{head}", "--", env_extra=gitcmd.READONLY, check=False,
                   binary=True)
    if r.returncode != 0:
        return DeepReview(UNAVAILABLE, "unreadable_output", None, (), measured,
                          f"git could not produce the diff to review "
                          f"(rc {r.returncode}): {r.stderr.decode('utf-8', 'replace').strip()}")
    body = r.stdout.decode("utf-8", "surrogateescape")
    if len(body.encode("utf-8", "surrogateescape")) > DIFF_BYTE_CAP:
        return DeepReview(UNAVAILABLE, "diff_too_large", None, (), measured,
                          f"the fused diff is over the {DIFF_BYTE_CAP}-byte prompt cap "
                          f"({size.files} file(s), {size.lines} line(s)); "
                          f"{size.why or 'measured in full'}")

    workdir = Path(run_dir) / "deep-review"
    workdir.mkdir(parents=True, exist_ok=True)
    manifest = (council or _council)(PROMPT + body, workdir, mode=mode)
    valid = [p for p in manifest.get("providers", []) if p.get("valid")]
    seats = tuple(p["name"] for p in valid)
    if not valid:
        lost = "; ".join(f"{p.get('name')} ({p.get('reason')})"
                         for p in manifest.get("providers", []))
        return DeepReview(UNAVAILABLE, "no_valid_seats", None, (), measured,
                          f"no seat answered: {lost}")

    # CROSS-SEAT AGREEMENT IS THE POINT OF A PANEL, and it has to be merged rather than
    # counted twice. `review.finding_id` hashes the SEAT along with the content, so two
    # seats reporting one bug produce two ids and neither dedupe below would fire — an
    # operator would read "3 findings" for two bugs and a fix pass would address one of
    # them twice. The single-seat cloud review this replaced could not reach the case.
    # Convergence across INDEPENDENT reviewers is evidence the finding is real, so it is
    # recorded on the finding rather than thrown away.
    merged: dict = {}
    readable = 0
    for p in valid:
        try:
            answer = Path(p["result_file"]).read_text()
        except OSError:
            continue
        got = _parse_findings(answer, p["name"], round_)
        # A seat that produced a PAYLOAD is readable even when the payload is empty —
        # "this seat found nothing" is an answer. `_parse_findings` cannot tell those
        # apart, so the emptiness of the fence is re-checked here.
        if got or re.search(r"```json\s*\{.*?\}\s*```", answer, re.DOTALL):
            readable += 1
        for f in got:
            key = (f.severity, " ".join(f.claim.lower().split()))
            if key in merged:
                merged[key][1].append(p["name"])
            else:
                merged[key] = (f, [p["name"]])
    findings = []
    for f, saw in merged.values():
        if len(saw) > 1:
            f = dataclasses.replace(
                f, evidence=f"{f.evidence} [corroborated by {', '.join(saw)}]")
        findings.append(f)
    if not readable:
        return DeepReview(UNAVAILABLE, "unreadable_output", None, seats, measured,
                          f"{len(valid)} seat(s) answered but none produced a readable "
                          "findings payload, which is not the same as finding nothing")
    return DeepReview(RAN, None, tuple(findings), seats, measured,
                      f"{readable} of {len(valid)} answering seat(s) produced a payload; "
                      f"{len(findings)} finding(s) over "
                      f"{size.files if measured else 'an unmeasured number of'} file(s)")
