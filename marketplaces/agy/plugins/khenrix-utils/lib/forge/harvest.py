"""The four-phase artifact set (spec §6.1, §7.1).

Origin is PROVENANCE, not eligibility. Four inventories are taken — F0 (baseline
checkout), Fsetup (after the engine's setup), Fwork (after the agent exits), Fverify
(after the engine's verify) — and every changed path carries the phase that produced it.

The artifact PATH set is Fsetup -> Fwork: setup's output (node_modules, .venv) is not the
agent's work, and verify's output is recorded separately because §7.2 may admit it as a
required deliverable under a declared generator contract — that decision belongs to a
later plan, and discarding it here would foreclose it.

The artifact CONTENT is `git diff <B> <final>` over those paths, against the PINNED
baseline commit — never the seat's own HEAD, which a seat that commits would leave empty.

Empty directories are invisible throughout: `snapshot.Entry` never carries kind "dir", so
a directory an agent creates or removes reaches none of these sets unless its contents do.
"""
from dataclasses import dataclass, field

from . import gitcmd, snapshot


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

    The two ways `snapshot.take` can decline are handled differently on purpose:

    - a quota breach returns `({}, [breach])`, and that empty dict is exactly what `diff`
      would read as "the agent deleted the whole tree". Dropping the breach line here is
      therefore not a lost warning, it is a fabricated result — so it becomes a raise.
    - `SnapshotError` (an unwalkable root or subtree) PROPAGATES UNWRAPPED. It already
      names the path that could not be read and is already a RuntimeError, so wrapping it
      would only put this module's name in front of the tree's.
    """
    entries, breaches = snapshot.take(seat_path, quota=quota)
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


def artifact_set(phases: Phases, seat_path, baseline_commit: str) -> ArtifactSet:
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
        # check=True: git exits 0 for a pathspec matching nothing tracked (measured), so
        # a nonzero exit is a real failure — most likely a pinned B absent from this
        # clone's object store. Under check=False that failure and a legitimately empty
        # diff are the same empty string, and the candidate hands over empty in silence.
        diff_text = gitcmd.git(
            seat_path, "diff", baseline_commit, "--", *(_literal(p) for p in paths),
            env_extra=gitcmd.READONLY).stdout
    return ArtifactSet(paths=paths, origin=origin, setup_overlap=overlap,
                       tracked_diff=diff_text, verify_overlap=verify_overlap)
