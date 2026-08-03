"""§13.1's ultrareview: a cloud-hosted multi-agent review of the final synthesis checkpoint.

IT ANSWERS A DIFFERENT QUESTION FROM THE COUNCIL. §13's panel checks requirement coverage
against the task; ultrareview hunts BUGS. It runs once, after the council loop has already
terminated, and its findings get the same post-round-2 treatment §13 gives any late blocker.

THE TIMEOUT IS IN MINUTES. Measured 2026-08-03 on this machine, `claude ultrareview --help`:

    --json               Print the raw bugs.json payload instead of formatted findings
    --timeout <minutes>  Maximum minutes to wait for the review to finish (default: 30)

Every other timeout in forge and the council is SECONDS — `engine.MODE_TIMEOUT["deep"]` is
1200, `verify.Step.timeout` defaults to 600 — so a constant copied from either asks for a
20-hour review. `argv` refuses anything outside 1..TIMEOUT_MINUTES_MAX, which makes the guard
structural rather than a comment somebody has to have read.

NOT MEASURED: whether `claude ultrareview` resolves a base branch in a clone with
**no remote**. The same `--help` describes it as reviewing "the current branch (or a PR
number / base branch)" and `fleet.clone_seat` removes `origin` (`fleet.py:270`). Measuring it
spends money, which this package's suite may not do, so it is stated here rather than
assumed. §13.1's degrade path — exit 1 becomes `unavailable`, the run proceeds to handover —
is what stands under that gap. Nothing here claims the path works.

Two more things this module does not know and does not pretend to: the exact strings the CLI
prints for a zero-data-retention org, a missing login or disabled usage credits (`_PHRASES` is
a guess, and an exit that matches none of them is named `exit_1` for what was observed rather
than for the nearest-looking cause), and whether a cloud review reliably returns one parseable
payload at all.

UNAVAILABILITY DEGRADES, NEVER FAILS. §13.1 names five reasons; this module declares SIX. The
sixth, `unreadable_output`, covers an exit-0 whose `--json` payload does not parse, which the
five do not — and folding that into "ran, found no bugs" is the false green every other module
in this package is written against. The extension is deliberate, not an oversight in reading
§13.1: the five it names are all reasons the review did not START, and this one is a review
that ran and came back unreadable.
"""
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import gitcmd, review, storage

TIMEOUT_MINUTES_DEFAULT = 30
# Above two hours a value is far likelier to be a seconds constant than an intention. 1200
# ("deep") and 600 (`verify.Step`'s default) are both refused by this ceiling, which is the
# whole reason it is here rather than being unbounded.
TIMEOUT_MINUTES_MAX = 120

# How much longer the LOCAL wrapper waits than the remote's own bound, so a review that
# finishes at its limit is collected rather than killed a second early by the wrapper. The
# record names this as well as the bound: a message saying only "the 30 minute wait elapsed"
# describes a wait that is actually 31 minutes long, and sends a reader looking for the
# failure a minute before it happened.
GRACE_SECONDS = 60

# §13.1's stated remote limits.
DIFF_FILE_LIMIT = 500
DIFF_LINE_LIMIT = 8000

RAN = "ran"
UNAVAILABLE = "unavailable"
TIMED_OUT = "timeout"
SKIPPED = "skipped"
STATUSES = (RAN, UNAVAILABLE, TIMED_OUT, SKIPPED)

REASONS = ("no_auth", "zdr_org", "diff_too_large", "usage_credits_off", "exit_1",
           "unreadable_output")

# Declared phrases, in precedence order. A phrase list is a guess about someone else's
# strings, so an exit 1 matching NONE of them is `exit_1` — named for what was observed
# rather than for the nearest-looking cause. Each is matched as a WHOLE TOKEN, not as a bare
# substring: `zdr` is three characters, and as a substring it labels any stderr that happens
# to contain them — a path, a session id, a branch name. All six reasons carry the same
# `UNAVAILABLE` status and the same `bugs=None`, so a wrong label costs no verdict; it costs
# the line a human reads to find out why the review did not happen.
_PHRASES = (
    ("no_auth", ("not logged in", "please run /login", "claude.ai auth", "unauthorized")),
    ("zdr_org", ("zero data retention", "zdr")),
    ("usage_credits_off", ("usage credits are disabled", "extra usage credits are disabled",
                           "usage credits")),
    ("diff_too_large", ("file limit", "line limit", "diff is too large", "too many files")),
)

# The host has to be claude.ai or something under it. This URL is recorded for a human to
# open, and an unanchored `claude\.ai` matches it inside the QUERY of somebody else's host
# (`https://elsewhere.example/?claude.ai/…`) — which is a link the record would then invite
# a reader to follow.
_SESSION = re.compile(r"https?://(?:[a-z0-9-]+\.)*claude\.ai/[^\s'\"]+", re.I)


def _mentions(text: str, phrase: str) -> bool:
    """`phrase` as a whole token in `text`, which is already lower-cased."""
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text) is not None


_SEVERITY_MAP = {"blocker": "blocker", "critical": "blocker", "high": "blocker",
                 "important": "important", "medium": "important",
                 "minor": "minor", "low": "minor", "info": "minor"}


class UltraError(RuntimeError):
    """An ultrareview this module will not run as asked."""


@dataclass(frozen=True)
class DiffSize:
    """The change ultrareview is being asked to look at, and what could not be counted.

    `files is None` is a measurement that was VOIDED — the engine does not know which files
    the diff touches either — and is a different fact from `lines is None`, which is a file
    list this run has and a line count git declined to give it. `why` carries which.

    IT VALIDATES ITSELF, like every other record in this package. It was the one that did not,
    and the two things it now refuses are the two that would make it read cleaner than the
    measurement behind it: a voided file count beside a line count (nothing could produce
    one, and a consumer reading `lines` would be reading a number taken over a file list this
    run does not have), and an absent count with no `why` — which is a refusal that declines
    to say what it refused, and `run_ultra` puts `why` in the operator's line.
    """
    files: int | None
    lines: int | None
    why: str

    def __post_init__(self) -> None:
        for name in ("files", "lines"):
            v = getattr(self, name)
            if v is None:
                continue
            # `bool` explicitly: `True` is an `int` and would compare against §13.1's limits
            # as 1, so a diff of "yes" would pass for a diff of one file.
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                raise UltraError(f"{name} is a count or None, not {v!r}")
        if not isinstance(self.why, str):
            raise UltraError(f"why is text, not {type(self.why).__name__}")
        if self.files is None and self.lines is not None:
            raise UltraError(
                f"this record voids the file count and states {self.lines} changed line(s): a "
                "line total is summed over the file list the same read produced, so there is "
                "no measurement it could have come from")
        if (self.files is None or self.lines is None) and not self.why.strip():
            raise UltraError(
                "a count this run could not take is a count this run says why it could not "
                "take; an unexplained `None` reaches the operator as a blank in the line "
                "`run_ultra` writes about why the review was or was not requested")


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
        §13.1's 8,000-line limit. The path is still counted; only its lines are refused.
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
    reports a 0-file, 0-line diff, comfortably under §13.1's limits, and this module pays for
    a review of a change it never measured. A leading `-` is the other direction: the value
    reaches git's argument parser as an option rather than as a revision.
    """
    if not isinstance(value, str) or not value.strip():
        raise UltraError(
            f"{name} names the checkpoint being reviewed and is a non-empty string, not "
            f"{value!r}; an empty side of a `..` range resolves to HEAD, and the empty diff "
            "that follows reads as a change small enough to review")
    if value.startswith("-"):
        raise UltraError(f"{name} is a revision, not an option: {value!r}")
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


def argv(*, timeout_minutes: int, target) -> list:
    """`claude ultrareview`'s argv. THE TIMEOUT IS MINUTES AND THIS IS WHERE THAT IS ENFORCED.

    The range check is the guard, not the docstring: `MODE_TIMEOUT["deep"]` (1200) and
    `verify.Step`'s default (600) are both seconds constants a reader might reach for, and
    both are refused here rather than silently requesting a review measured in days.

    The target is the ONE caller-supplied token in a paid invocation, and it lands in the
    positional slot after the options, so a value beginning with `-` is read as one more
    option by the CLI's own parser rather than as the branch it was meant to be.
    """
    if not isinstance(timeout_minutes, int) or isinstance(timeout_minutes, bool):
        raise UltraError(f"a timeout is a whole number of MINUTES, not {timeout_minutes!r}")
    if not 1 <= timeout_minutes <= TIMEOUT_MINUTES_MAX:
        raise UltraError(
            f"--timeout is in MINUTES (measured: `claude ultrareview --help` -> "
            f"'--timeout <minutes> ... (default: 30)'), and {timeout_minutes} is outside "
            f"1..{TIMEOUT_MINUTES_MAX}. Every other timeout in forge and the council is in "
            "seconds; a value from one of those asks for a review measured in days.")
    out = ["claude", "ultrareview", "--json", "--timeout", str(timeout_minutes)]
    if target:
        if str(target).startswith("-"):
            raise UltraError(
                f"a review target is a branch or a PR number, not {target!r}: it is appended "
                "as the positional argument and a leading `-` makes it an option instead")
        out.append(str(target))
    return out


def classify(exit_code, stdout, stderr):
    """§13.1's unavailability reason for this invocation, or `None` when it did not fail."""
    if exit_code == 0:
        return None
    low = f"{stdout or ''}\n{stderr or ''}".lower()
    for reason, phrases in _PHRASES:
        if any(_mentions(low, p) for p in phrases):
            return reason
    return "exit_1"


def session_url(stderr):
    """§13.1: "stderr's session URL recorded". `None` when there is none to record.

    `bytes` is accepted as well as `str` because the one caller that most needs an answer is
    the TIMEOUT path, where the stream comes off a `TimeoutExpired` rather than off a
    `CompletedProcess` — and a review that is still running in the cloud is exactly the one
    whose URL a human needs.
    """
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", "replace")
    m = _SESSION.search(stderr or "")
    return m.group(0) if m else None


@dataclass(frozen=True)
class Ultra:
    """What §13.1's pass produced, and how much of it was measured.

    `bugs` is `None` — never `()` — for every status but `ran`. An empty tuple means "the
    review ran and reported nothing", which is precisely the sentence an unavailable review
    must not be able to write.

    `reason` runs the same rule the other way: it belongs to `unavailable` and to nothing
    else. A record carrying both `ran` and `no_auth` answers differently depending on which
    field a consumer reads, which is the same defect one field over.

    `diff_measured` is False when the diff's line count could not be taken, so a record can
    never say the change was under §13.1's limits on a measurement nobody took.
    """
    status: str
    reason: str | None
    bugs: tuple | None
    session_url: str | None
    diff_measured: bool
    detail: str

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise UltraError(f"status is one of {list(STATUSES)}, not {self.status!r}")
        if self.reason is not None and self.reason not in REASONS:
            raise UltraError(f"reason is one of {list(REASONS)} or None, "
                             f"not {self.reason!r}")
        if self.status == RAN and self.bugs is None:
            raise UltraError("a review that ran reports its findings, even when there are "
                             "none; `None` is what an unavailable one carries")
        if self.status != RAN and self.bugs is not None:
            raise UltraError(
                f"a {self.status!r} review has no findings to report, and an empty tuple "
                "here would read as a review that ran and found nothing")
        if self.status == UNAVAILABLE and self.reason is None:
            raise UltraError("an unavailable review names which of §13.1's reasons it was")
        if self.status != UNAVAILABLE and self.reason is not None:
            raise UltraError(
                f"a {self.status!r} review carries no unavailability reason, and "
                f"{self.reason!r} here answers 'why did the review not happen' about a "
                "review whose status says it did")


def _child_env() -> dict:
    """The environment `claude ultrareview` is invoked with.

    `cwd=` IS NOT WHAT DECIDES WHICH REPOSITORY A CHILD SEES. Its own `--help` says it
    reviews "the current branch", so it resolves a git repository, and an ambient `GIT_DIR`,
    `GIT_WORK_TREE` or `GIT_COMMON_DIR` wins over the working directory for every git there
    is — `gitcmd.HOSTILE_ENV` names the rest, including the two that enter config at
    command-line precedence. A hook, `git rebase --exec` and `git bisect run` each export
    several into everything they invoke, so this is the ordinary case rather than an exotic
    one, and the review is UPLOADED: the cost of inheriting one is the user's own repository
    leaving the machine in place of the synthesis checkout.

    This is a refusal to PASS an input, not a claim about what the CLI does with it — which
    is why it is the whole tuple and not the three names most likely to matter.
    `gitcmd.HOSTILE_ENV`'s own rule is that a name dropped in one consumer and kept in
    another is a hole, and `gitcmd.git`, `fleet.forge_child_env` and `verify._gate_env` are
    the other three.

    WHERE IT STOPS, and why. The other two consumers also pin `gitcmd.NO_USER_CONFIG`, and
    that is NOT done here: pinning the user's global git config to /dev/null would change
    what a third-party CLI can read about who the user is and which remotes exist, and no
    measurement of this package's says what that does to a cloud review. Dropping a
    redirector can only narrow; blanking a config file this module does not own is a
    behaviour change it cannot predict. `fleet.scrub_env` is out for a different reason: it
    scrubs what points into the ORIGINAL checkout, and the tree here is the one the review
    is supposed to be about.
    """
    return {k: v for k, v in os.environ.items() if k not in gitcmd.HOSTILE_ENV}


def _bugs(payload, checkpoint_round: int) -> tuple:
    """`bugs.json`'s rows as `review.Finding`s, or raise if the payload is not readable."""
    if not isinstance(payload, dict):
        raise ValueError(f"the payload is an object, not {type(payload).__name__}")
    rows = payload.get("bugs")
    if not isinstance(rows, list):
        raise ValueError("the payload carries no `bugs` list")
    out = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("a bug is an object")
        sev = _SEVERITY_MAP.get(str(row.get("severity", "")).lower())
        if sev is None:
            raise ValueError(f"unmapped severity {row.get('severity')!r}")
        claim = " ".join(str(row.get(k)) for k in ("description", "location")
                         if row.get(k)).strip()
        if not claim:
            raise ValueError("a bug carries a description")
        # `review.Finding` requires evidence and refuses an empty string, so a payload that
        # named no location is recorded as having named none — in words. The alternative was
        # to refuse the ROW, which discards a finding out of a review that was paid for and
        # cannot be re-run for free, over a field this module has never measured the remote to
        # emit (the module docstring says the payload's shape is not something it knows).
        where = str(row.get("location") or "").strip()
        out.append(review.Finding(
            id=review.finding_id(checkpoint_round, "ultrareview", sev, claim, where),
            round=checkpoint_round, seat="ultrareview", severity=sev, claim=claim,
            evidence=where or "the ultrareview payload named no location for this bug",
            resolution="open"))
    return tuple(out)


def run_ultra(run_dir, *, checkout, base: str, head: str, round_: int, enabled: bool = True,
              timeout_minutes: int = TIMEOUT_MINUTES_DEFAULT, target=None,
              file_limit: int = DIFF_FILE_LIMIT,
              line_limit: int = DIFF_LINE_LIMIT, run=subprocess.run) -> Ultra:
    """§13.1's single pass, from the synthesis checkout, after the council loop terminated.

    ORDER: the local pre-flight first, so a diff that is definitely over §13.1's limits costs
    nothing; then one invocation; then the payload. `--no-ultra` (`enabled=False`) is priced
    by `gate.quote(ultrareview=False)` and spends nothing here.

    AN UNMEASURABLE DIFF RUNS. The remote is the authority on its own limits, and refusing
    locally on a line count nobody could take would report `diff_too_large` about a diff whose
    size is unknown — a reason stated more confidently than the evidence. What the record does
    instead is carry `diff_measured=False`.

    `round_` IS REQUIRED AND IS AT LEAST 1, AND THAT IS THE WHOLE MECHANISM BY WHICH §13.1's
    FINDINGS REACH A TERMINAL AT ALL. A `review.Finding` carrying round 0 can be written into
    NO record: `review.round_dir` refuses anything below 1, so `review.write_round` cannot
    store it, and `review.terminal_from_record` iterates `range(1, rounds_run + 1)` and would
    never read it. A defaulted 0 therefore made these findings unreachable by the one function
    that decides §13.1's and §14.2's shared terminal — see `review.terminal_from_record`,
    where the two are reconciled — so the reconciliation had no mechanism at all. With a real
    round number the types line up end to end: this returns `Finding`s whose `round` a `Round`
    record accepts, and the terminal reads them like any other round's.

    §13.1'S FINDINGS GET THE POST-ROUND-2 TREATMENT AND THIS FUNCTION DOES NOT APPLY IT: fix →
    fresh-verifier verify → checkpoint → `review.VERIFIED_NOT_INDEPENDENTLY_REVIEWED`, with
    the terminal decided by `review.terminal_from_record` (a fixed-but-unreviewed blocker is
    `degraded`, an unresolved one is `review_blocked`). No new loop and no new state, exactly
    as §13.1 says — the record and the transition are `review`'s. THAT WIRING DOES NOT EXIST
    YET, and the gap is named as a missing mechanism rather than left implied: no caller
    invokes `run_ultra`, nothing writes what it returns into a `review.Round`, and no test
    covers the hand-off. Until something does, this function's findings reach no terminal.
    """
    if not isinstance(round_, int) or isinstance(round_, bool) or round_ < 1:
        raise UltraError(
            f"§13.1's findings are attributed to a review round numbered from 1, not "
            f"{round_!r}. `review.round_dir` refuses anything below 1, so a finding carrying "
            "round 0 can be written into no record `review.terminal_from_record` reads — and "
            "that terminal reading it is the only way §13.1's findings reach a verdict.")
    if not enabled:
        return Ultra(SKIPPED, None, None, None, False,
                     "--no-ultra: §13.1 is default on and this run opted out, so no cloud "
                     "review was requested and none is priced")
    d = measure_diff(checkout, base, head)
    if (d.files is not None and d.files > file_limit) or \
            (d.lines is not None and d.lines > line_limit):
        over = ", ".join(
            f"{n} {got} over §13.1's {cap}" for n, got, cap in
            (("files", d.files, file_limit), ("lines", d.lines, line_limit))
            if got is not None and got > cap)
        return Ultra(UNAVAILABLE, "diff_too_large", None, None, d.lines is not None,
                     f"the diff is {over}; refused locally so the review is not requested "
                     f"and not charged{'. ' + d.why if d.why else ''}")
    out_dir = Path(run_dir) / "ultrareview"
    out_dir.mkdir(parents=True, exist_ok=True)
    a = argv(timeout_minutes=timeout_minutes, target=target)
    wait = timeout_minutes * 60 + GRACE_SECONDS
    try:
        proc = run(a, cwd=str(checkout), capture_output=True, text=True, timeout=wait,
                   env=_child_env())
    except subprocess.TimeoutExpired as e:
        url = session_url(e.stderr)
        return Ultra(TIMED_OUT, None, None, url, d.lines is not None,
                     f"the local wait of {timeout_minutes} minute(s) plus a "
                     f"{GRACE_SECONDS}s grace elapsed; the remote review is still running "
                     "and can be collected in the browser"
                     + (f" at {url}" if url else
                        " — no session URL was read from its stderr, so there is nothing "
                        "here to collect from"))
    url = session_url(proc.stderr)
    reason = classify(proc.returncode, proc.stdout, proc.stderr)
    if reason is not None:
        return Ultra(UNAVAILABLE, reason, None, url, d.lines is not None,
                     f"ultrareview: unavailable ({reason}) — the run proceeds to handover")
    try:
        payload = json.loads(proc.stdout)
        bugs = _bugs(payload, round_)
    except (ValueError, TypeError) as e:
        # KEPT, under a name a readable payload never takes. The review was paid for and
        # cannot be re-run for free, so these bytes are the only evidence anyone will have
        # about what came back — and a record that says "could not be read" while discarding
        # what it could not read leaves nobody able to check that claim.
        storage.atomic_write(out_dir / "unreadable-output.txt",
                             (proc.stdout or "").encode("utf-8", "surrogateescape"))
        return Ultra(UNAVAILABLE, "unreadable_output", None, url, d.lines is not None,
                     f"ultrareview exited 0 and its --json payload could not be read ({e}); "
                     "that is not the same as a review that found nothing, and §13.1's five "
                     "named reasons do not cover it")
    storage.atomic_write(out_dir / "bugs.json",
                         (json.dumps(payload, sort_keys=True, indent=2) + "\n")
                         .encode("utf-8"))
    scale = f"a {d.files}-file diff" if d.files is not None else "a diff of unknown size"
    return Ultra(RAN, None, bugs, url, d.lines is not None,
                 f"{len(bugs)} finding(s) over {scale}"
                 + ("" if d.lines is not None else
                    f"; its changed-line count was not measured ({d.why})"))
