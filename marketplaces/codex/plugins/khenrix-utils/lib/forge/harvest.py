"""The four-phase artifact set (spec §7.1, §7.2).

Origin is PROVENANCE, not eligibility. Four inventories are taken — F0 (baseline
checkout), Fsetup (after the engine's setup), Fwork (after the agent exits), Fverify
(after the engine's verify) — and every changed path carries the phase that produced it.

The artifact PATH set is Fsetup -> Fwork: setup's output (node_modules, .venv) is not the
agent's work, and verify's output is recorded separately because §7.2 may admit it as a
required deliverable under a declared generator contract. Nothing decides that question yet
— no caller folds Fverify content into the artifact set, and none refuses one either — so
discarding it here would foreclose the option before anything exists to make the call.

The artifact CONTENT is `git diff <B> <final>` over those paths, against the PINNED
baseline commit — never the seat's own HEAD, which a seat that commits would leave empty.

Empty directories are invisible throughout: `snapshot.Entry` never carries kind "dir", so
a directory an agent creates or removes reaches none of these sets unless its contents do.

`tracked_diff` IS NOT A COMPLETE VIEW OF `paths`, and §6's CandidateBundle must carry the
difference by another route or it hands over a candidate that silently drops files the
ledger says the agent changed. Three measured causes, only one of which is closed here:

- **untracked files** — `git diff <B>` never reports them at all. They are in `paths` and
  contribute nothing to the diff. Structural; no flag closes it.
- **submodule contents** — a submodule's `.git` is a FILE, so `snapshot`'s `.git` dirname
  skip does not apply and it walks the submodule's working tree (`sub/inner.txt`, and
  `sub/.git` itself). The superproject's diff reports only the gitlink, so a changed file
  under a submodule enters `paths` and yields ZERO bytes of diff at exit 0. Structural.
- **files git treats as binary** — closed, but only by the `--binary` flag below. Without
  it git emits `Binary files a/x and b/x differ` and no content, at exit 0. That covers
  ordinary binaries AND text files a seat marked `-diff` in its own in-tree
  `.gitattributes`, which is a routine pattern for lockfiles and generated output.
"""
from dataclasses import dataclass, field

from . import gitcmd, inspect, snapshot
from .storage import Quota


class HarvestError(RuntimeError):
    """An inventory could not be completed, so nothing honest can be said about the seat."""


@dataclass(frozen=True)
class Phases:
    f0: dict[str, snapshot.Entry]
    fsetup: dict[str, snapshot.Entry]
    fwork: dict[str, snapshot.Entry]
    fverify: dict[str, snapshot.Entry]


@dataclass(frozen=True)
class ArtifactSet:
    """`verify_overlap` is last so the four fields above keep their positions for a
    caller that constructs one positionally."""
    paths: tuple[str, ...] = ()
    origin: dict[str, str] = field(default_factory=dict)
    setup_overlap: tuple[str, ...] = ()
    tracked_diff: str = ""
    verify_overlap: tuple[str, ...] = ()


def record(seat_path, *, quota=None) -> dict[str, snapshot.Entry]:
    """One inventory of a seat, .git excluded.

    `Quota.for_harvest`, not `Quota.default`: the default bounds the pre-launch screen's
    decode of the user's source, and under it this function fails closed on the very thing
    the phase design exists to observe — measured, `files: 5001 > 5000` on a 5200-file
    `node_modules`. See that classmethod for the numbers and what they were measured
    against. An explicit `quota=` still wins, so a caller can tighten it.

    `snapshot.take`'s `skip_dirs` is deliberately NOT forwarded, and raising the cap is not
    a workaround for the absence of a name list. Skipping `node_modules` by name would hide
    it from `Fsetup` AND `Fwork` alike, so an agent that edited a file underneath one — a
    vendored patch, `patch-package` — would have that work silently dropped from `paths`:
    fail-OPEN, and reintroducing exactly the policy name-list the design rejects. Setup's
    output is excluded by DIFFERENCING it, which requires seeing it.

    The two ways `snapshot.take` can decline are handled differently on purpose:

    - a quota breach returns `({}, [breach])`, and that empty dict is exactly what `diff`
      would read as "the agent deleted the whole tree". Dropping the breach line here is
      therefore not a lost warning, it is a fabricated result — so it becomes a raise.
    - `SnapshotError` (an unwalkable root or subtree) PROPAGATES UNWRAPPED. It already
      names the path that could not be read and is already a RuntimeError, so wrapping it
      would only put this module's name in front of the tree's.
    """
    entries, breaches = snapshot.take(seat_path, quota=quota or Quota.for_harvest())
    if breaches:
        raise HarvestError("; ".join(breaches))
    return entries


def _literal(path: str) -> str:
    """A git pathspec is a GLOB WITH MAGIC, not a filename, and both halves of that bite
    a path set taken from the filesystem. Measured on git 2.53:

      `git diff <B> -- 'a*.txt'`      also emitted ab.txt's diff — content from a file the
                                      agent never touched, laundered into the candidate.
      `git diff <B> -- ':weird.txt'`  exited 0 with NO output — `:x` parses as pathspec
                                      magic, so the file's content silently vanished.

    Neither needs an adversarial seat; a generated fixture named `[case].json` is enough.
    """
    return f":(literal){path}"


def _refuse_seat_configured_filters(seat_path, paths) -> None:
    """Refuse a harvest whose diff would run a clean driver the SEAT defined.

    The diff below has to put worktree content into git's form, so `filter.<name>.clean` runs
    and git offers no flag that refuses it — and the cost is SUBSTITUTION: the agent's own
    bytes leave the patch and the driver's stand in their place, at exit 0. Nothing downstream
    can notice, because `ArtifactSet` carries no per-path Fwork hashes and `bundle.build`
    treats a path present in the patch as covered, so the live file is never re-read.

    THE ATTRIBUTE ALONE IS NOT THE OFFENCE, and this is the whole reason the check is two
    questions rather than one. `check-attr` reports the EFFECTIVE attribute from the full
    per-directory stack, and `filter=lfs` sits in the BASELINE's own `.gitattributes` in a
    large fraction of real repositories — so refusing on the attribute would raise for every
    seat, after every provider call is paid, on all of them. What a seat can rig is
    `.git/config`, which `git clone` does not copy and which is where a named driver is
    DEFINED; an attribute naming a driver nothing defines is inert.

    WHAT IT COVERS, because the mechanism is easy to under-read: `filter=<name>` selects
    `.clean`, `.smudge` AND `.process` together, so there is no separate smudge attribute to
    miss; `check-attr` walks `.gitattributes` in every directory, plus
    `$GIT_DIR/info/attributes` and `core.attributesFile`, which outrank in-tree files and
    which no tree-only inspection would see.

    WHAT IT DOES NOT COVER: the `text`, `eol` and `working-tree-encoding` attributes also
    convert bytes on this call — a narrower substitution channel, and it stays open. Nor does
    it close the detached-child route to the same outcome, where a `setsid` descendant of the
    seat rewrites a file between Fwork and this diff; that needs Fwork's bytes bound to the
    bundle, which is a different fix.
    """
    if not paths:
        return
    named = inspect._filtered_paths(seat_path, list(paths))
    if not named:
        return
    rigged = []
    for rel in named:
        attr = gitcmd.git(seat_path, *gitcmd.NO_DAEMON_CACHE, "check-attr", "-z", "filter",
                          "--", rel, env_extra=gitcmd.READONLY).stdout.split("\0")
        driver = attr[2] if len(attr) > 2 else ""
        # `--local`: the seat's OWN config, not a value inherited from the machine. A driver
        # the operator has in ~/.gitconfig is not something this seat introduced.
        defined = gitcmd.git(seat_path, "config", "--local", "--get", f"filter.{driver}.clean",
                             env_extra=gitcmd.READONLY, check=False)
        if defined.returncode == 0 and defined.stdout.strip():
            rigged.append(f"{rel} (filter={driver})")
    if rigged:
        raise HarvestError(
            f"this seat's own config defines a clean filter for harvested path(s) "
            f"{sorted(rigged)}. §7's diff runs in the trusted parent and git has no flag that "
            "refuses a clean driver, so the driver's output would REPLACE the builder's bytes "
            "in the candidate — and the verifier would then materialize, run and pass content "
            "the builder never wrote, under a verdict presented as being about the builder's "
            "artifact. The attribute alone is inert; what is refused here is the seat having "
            "defined the program it names.")


def artifact_set(phases: Phases, seat_path, baseline_commit: str) -> ArtifactSet:
    """The path set, its provenance, and the tracked content behind it.

    The error surface is stated because it is NOT closed by this package's three classes,
    and a caller that catches only `HarvestError` / `GitError` / `SnapshotError` misses the
    last two: a failing git raises `gitcmd.GitError`; a path set long enough to
    exceed ARG_MAX raises `OSError(E2BIG)` out of subprocess (`git diff` has no
    `--pathspec-from-file`, measured, so chunking would be the fix); and a wedged git
    raises `subprocess.TimeoutExpired` at `gitcmd`'s 60s.
    """
    setup_changes = snapshot.diff(phases.f0, phases.fsetup)
    work_changes = snapshot.diff(phases.fsetup, phases.fwork)
    verify_changes = snapshot.diff(phases.fwork, phases.fverify)

    # One precedence rule — builder > setup > verify — applied weakest first, rather than
    # an assignment for one phase and a setdefault for another that happen to compose.
    # Builder wins because the path set IS the work delta: a path labelled otherwise would
    # sit in `paths` disowned. Nothing is lost by overwriting, because both overlaps are
    # reported below; §7.2 requires the work+verify pair to be labelled, not just resolved.
    origin: dict[str, str] = {}
    for p in verify_changes:
        origin[p] = "verify"
    for p in setup_changes:
        origin[p] = "setup"
    for p in work_changes:
        origin[p] = "builder"

    paths = tuple(sorted(work_changes))
    overlap = tuple(sorted(set(work_changes) & set(setup_changes)))
    verify_overlap = tuple(sorted(set(work_changes) & set(verify_changes)))

    # The guard is load-bearing, not an optimisation: `git diff <B> --` with no pathspec
    # diffs the WHOLE TREE, so a seat that changed nothing would hand back setup's tracked
    # churn as the candidate's content.
    diff_text = ""
    if paths:
        # --binary: without it a file git considers binary — including a TEXT file the
        # seat marked `-diff` in its own .gitattributes — yields `Binary files … differ`
        # and no content, at exit 0. The path is in `paths` asserting the agent changed
        # it while the diff carries nothing to reconstruct it: the same silent, exit-0
        # content loss the :(literal) fix exists to prevent, in the same call.
        #
        # check=True: git exits 0 for a pathspec matching nothing tracked (measured), so
        # a nonzero exit is a real failure — most likely a pinned B absent from this
        # clone's object store. Under check=False that failure and a legitimately empty
        # diff are the same empty string, and the candidate hands over empty in silence.
        #
        # binary=True + surrogateescape, NOT text=True: git echoes the raw bytes of a
        # changed file, so ONE latin-1 file made subprocess' strict UTF-8 decode raise
        # UnicodeDecodeError and take every other path's content down with it — in a
        # class that is neither HarvestError, GitError nor SnapshotError. surrogateescape
        # is chosen over errors="replace" because it is REVERSIBLE: a consumer writing
        # this patch back out for `git apply` must use
        # `tracked_diff.encode("utf-8", "surrogateescape")`, which reproduces git's bytes
        # exactly. A plain `.encode()` raises UnicodeEncodeError on the surrogates, and
        # "replace" would have produced a patch that no longer applies.
        # NO_DAEMON_CACHE because the tree this reads is the SEAT. `diff` loads an index, and
        # `core.fsmonitor` is read from the config of whichever repository the call runs in —
        # here a directory the agent had write access to for the whole of its run, `.git/config`
        # included. Measured: a seat that sets the key has its program run by THIS call, by the
        # engine, after the agent's own process is gone. The flags suppress a cache, so the
        # patch bytes are unchanged.
        #
        # NO_DIFF_DRIVERS closes the same window through the keys that name a program to run
        # DURING a diff — `diff.external`, and `diff.<d>.command`/`diff.<d>.textconv` selected
        # by the seat's own `.gitattributes`. Measured at this call site, all three ran; and
        # unlike the cache flags these change the bytes when they are missing, `diff.external`
        # emptying the patch at exit 0.
        #
        # `filter.<d>.clean` still runs — git offers no flag for it — so this call is hardened,
        # not program-free, and the cost is SUBSTITUTION rather than noise: measured here, the
        # agent's own text was absent from the patch and the filter's stood in its place, at
        # exit 0.
        # A CONFIG READBACK HERE WOULD NOT BE `verify._assert_hooks_pinned`, whatever it looks
        # like. That canary bounds two engine actions with one known writer between them;
        # harvest has no closing boundary, since a permissions-bypassed agent's detached
        # children outlive the process whose exit would be the only candidate for one. It also
        # has nothing to pin — a canary reads back a value the ENGINE wrote, while the driver
        # name is the seat's to choose out of an unbounded namespace — and readback and diff
        # would be two invocations regardless. What fits is DETECTION, of the shape
        # `inspect._filtered_paths` already runs against the user's repository: `check-attr -z
        # filter` over `paths` names a rigged seat instead of reading a value back. BUILT
        # BELOW, reusing that function rather than respelling it.
        _refuse_seat_configured_filters(seat_path, paths)
        diff_text = gitcmd.git(
            seat_path, *gitcmd.NO_DAEMON_CACHE, "diff", *gitcmd.NO_DIFF_DRIVERS,
            "--binary", baseline_commit, "--", *(_literal(p) for p in paths),
            env_extra=gitcmd.READONLY, binary=True).stdout.decode("utf-8", "surrogateescape")
    return ArtifactSet(paths=paths, origin=origin, setup_overlap=overlap,
                       tracked_diff=diff_text, verify_overlap=verify_overlap)
