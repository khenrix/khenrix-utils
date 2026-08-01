"""What the run agreed to do, where it got to, and the repository state it is judged against
(§9, §14, §14.1, §14.2).

The manifest and the snapshot live here together because they are recorded at the same instant
and are worth nothing apart. The manifest says what the run committed to — repo path,
`base_commit`, the baseline's identity, the selected paths, the confirmed steps. The snapshot
says what the user's repository looked like when it committed to that. A manifest without the
snapshot describes a deliverable with no way to ask whether it still applies; a snapshot
without the manifest is a photograph of nothing in particular.

The other two facts here are the ones a RESUME needs and neither of those carries: §14's phase
tuple, which says where the run had got to, and the per-seat record, which says what each seat
last managed to write down. Both are described at the end of this docstring and implemented at
the end of the file.

WRITTEN ONCE. §14.2 requires the manifest be written at `confirmed` and never rewritten, so
commands are never re-detected. That is not a style rule about callers: `--collect` resuming
from disk and a run continuing in memory have to be the same code path, and a second write
would make them differ exactly when they must not — after a crash, where re-detection would
quietly substitute today's answer for the one the user approved. So the refusal is the file
system's, not a check this module performs: `storage.exclusive_write` links the name into
place and the kernel refuses a name that already exists.

READ BACK AS WHAT WAS WRITTEN. JSON has one sequence type, so every tuple returns as a list
and every argv would arrive as something that compares unequal to what was confirmed. Each
field is decoded explicitly, and a field added to `Manifest` without a decoder makes the next
read fail rather than arrive as whatever JSON happened to make of it.

WHAT THE SNAPSHOT IS. §9's protected list is the user's current branch ref, `HEAD`, the index
hash, the checkout files, all non-forge refs, REMOTES, and CONFIGURATION; forge's own refs are
whitelisted by exact name AND the exact OID recorded here, because a namespace whitelist would
let a seat write into forge's own namespace invisibly. The last two of those seven are NOT
recorded: nothing here reads `remote.*` or any other config key, so a seat that adds a remote
or sets a repo-local value leaves no t0 fact to be judged against, and §9's drift check cannot
speak for either. What is recorded is `protected_refs`, name-to-OID for every ref that is not
forge's, and `status_digest`, over the four parts of the checkout state that move
independently:

  * the porcelain, which is a PATH/STATUS LISTING — it says which paths differ from `HEAD`
    and in which direction, and carries none of their content;
  * `HEAD`, because a clean tree, an edit and a commit leaves the porcelain byte for byte
    what it was — measured — so on the porcelain alone an entire commit of the user's work
    reads as no drift at all;
  * the branch `HEAD` points at, because switching between two branches at one commit moves
    the porcelain, `HEAD`'s OID and every ref not at all, and handover targets the branch that
    was current;
  * the CONTENT of the paths the run is carrying, because the listing above cannot see an
    edit to a path it already lists. Measured: a tree with `seed.txt` modified and `notes.txt`
    untracked-and-selected, rewritten in both, leaves the porcelain, the refs and the first
    three parts identical — and that is §9's own worked example, "you have since changed 4 of
    the files it touches", on the tree shape forge treats as normal.

WHAT THE RUN CARRIES, exactly, is what that fourth part ranges over: the tracked entries the
porcelain names, plus `selected_paths`. That is `baseline.materialize`'s domain read back —
`add -u -- :/` then `add -f` over the selection. Recomputing the tree OID is the other way to
ask the same question, and the two were measured against each other over thirteen cases: they
agree on twelve, and the one they differ on is a branch switch at one commit, which the tree
OID cannot see at all. What the recomputation costs is a write into the USER's object store on
every drift check — measured at +21 loose objects per call on a twenty-file dirty tree, one
blob each plus the tree, unreachable until git's two-week gc grace expires. Reading the paths
back writes nothing, so this is the read-only half of the same answer.

Two boundaries follow from that domain rather than being chosen. A tracked path whose content
moves while the porcelain does NOT list it is one git itself calls clean, so `add -u` would
skip it on the same stat comparison — neither method sees it, and neither can. An untracked
path nobody selected is deliberately outside it: no tree forge writes contains it, so no merge
of forge's work can revert it, and hashing it would put every stray editor swap file and test
log into a drift report whose only value is that it means something.

Ignored paths follow from that same rule rather than being a separate gap. `git status` does
not list them without `--ignored`, which stays off: a repository with a watcher or a dev
server rewrites ignored build output continuously, and a digest that moved on all of it would
make the one drift report that means something unreadable. An ignored path the run IS carrying
is covered anyway, because selection is what puts a path in the content set — so the case that
was open here, a caller hand-selecting an ignored path, is closed by the selection it made.

WHERE THE RUN GOT TO is five dimensions and not one. §14: `(phase, round, attempt,
verified_checkpoint, deliverable_checkpoint)`, "separate dimensions, with the
`reviewing → synthesizing` back-edge declared. A single enum cannot represent 'fixing after
review round 2.'" The phase reads `synthesizing` for a first synthesis and again for a fix
after the second review round; what tells those apart is a counter the phase does not move.
So `advance` moves the phase and NOTHING else, and each of the other four has its own reason
for not riding along: a `round` incremented on the back-edge would spend a council round §14.2
says is never spent automatically, an `attempt` reset would lose the retry count on the one
operation being retried, and either checkpoint cleared on the way back to `synthesizing` would
drop a commit §14.2 depends on having — the last verified one is what it keeps as the
deliverable when a resumed fix fails to verify.

WHAT A SEAT LAST SAID has one rule, and §14.1 states it as the thing the whole discipline is
for: a SIGTERM landing mid-rewrite of `seat-codex.json` "must not leave truncated JSON
indistinguishable from a seat that never wrote". Distinguishability is what §14.1 puts in
place of an exactly-once it opens by conceding is not deliverable, so `read_seat` answers None
for exactly ONE condition — no such file — and raises for every other way a record can fail to
be one. Three routes reach None, or an answer a caller cannot tell from it, without damaging
a byte — and they are the reason the rule is written as a whitelist: a JSON `null` parses cleanly and hands back the same object a
missing file would, an unreadable record is an OSError that a blanket `except OSError` would
answer with the same silence, and an empty record is falsy exactly as None is — so the
shortest question a caller can ask, `if read_seat(...)`, gets the wrong answer from a seat
that did write. The last of those is refused at BOTH ends, because the reader meets records
this writer did not write.
"""
import dataclasses
import hashlib
import json
import os
import stat
from dataclasses import dataclass

from . import gitcmd, storage
from .verify import Step, VerifyError


class ManifestError(RuntimeError):
    """A run's identity that cannot be recorded, cannot be recovered, or would be rewritten."""


class StateError(RuntimeError):
    """A seat's record that is damaged, unreadable, or written in a shape its reader could not
    tell from a seat that never wrote at all."""


class TransitionError(RuntimeError):
    """A move §14's graph does not declare."""


# §9's two explicitly-allowed namespaces. Prefixes rather than a substring test: a user's
# `refs/heads/forgery-experiments` is theirs, and dropping it here would leave a moved ref
# with nothing to compare against — the fail-OPEN direction of the same decision.
_FORGE_REF_PREFIXES = ("refs/khenrix-forge/", "refs/heads/forge/")

# WHAT THIS GRAPH CANNOT EXPRESS, recorded because the next reader reads the code and not a
# report. §14's diagram reaches every terminal only from `reviewing`, and two other sections
# need endings it cannot draw: §5's confirmed calibration-failure policy has an `abort`
# branch, and calibration runs inside `setting_up`; §9 says a checkout or protected branch
# moving "during the run" transitions to `source_diverged`, at whatever phase the run is in.
# The edges are NOT invented here. `advance` refusing an undeclared move names the legal
# successors and stops at the first caller that tries one, which is the fail-CLOSED
# direction; a graph quietly wider than the spec is the other one, and no test could tell an
# invented edge from a declared one. The amendment is a spec question, and it belongs to the
# whole class rather than to whichever single edge a caller hits first.


@dataclass(frozen=True)
class Manifest:
    """The run's identity, as agreed at the §5 gate.

    `setup` and `verify` are sequences of `verify.Step`, which is §5.1's step record whole —
    `{argv, cwd, env, timeout}`. All four, not argv alone: §5.1's motivating sentence is that
    "real monorepos need several steps with different cwds", so a manifest that recorded the
    argv and left the rest to a resume would hand `--collect` a `cwd` of "" and a timeout of
    600 SUPPLIED BY THE READER rather than agreed by the user — this file's own failure mode,
    three fields per step instead of one field per manifest. The type is `verify`'s own rather
    than a record this module invents, so there is no conversion between what is written and
    what the gate runs for a field to go missing in.

    Never a string, at any level: §5.1 rejects shell metacharacter syntax rather than
    reinterpreting it, and a manifest holding `"cd frontend && npm ci"` would hand a resume a
    command it has to re-parse — which is re-detection under another name, at the one moment
    nobody is watching.

    `protected_refs` and `status_digest` are the t0 snapshot: what the user's repository
    looked like at the moment of agreement, kept so a later check can ask whether it still
    does. `selected_paths` is an input to the second of those as well as a record — see
    `snapshot_refs`.
    """
    run_id: str
    repo_path: str
    base_commit: str
    baseline_ref: str
    baseline_commit: str
    tracked_tree_oid: str
    selected_paths: tuple[str, ...]
    generator_contract: dict
    setup: tuple[Step, ...]
    verify: tuple[Step, ...]
    protected_refs: dict
    status_digest: str
    created_at: str


def write_manifest(run_dir, manifest: Manifest) -> None:
    """Record the run's identity, once. Raises ManifestError if one is already recorded."""
    if not isinstance(manifest, Manifest):
        raise ManifestError(f"a manifest is required, not {type(manifest).__name__}")
    path = storage.manifest_path(run_dir)
    row = dataclasses.asdict(manifest)
    try:
        # sort_keys so one run identity has one spelling on disk, whatever order the fields
        # were declared in; indented because the first reader of this file is usually a human
        # working out what a crashed run had agreed to.
        blob = json.dumps(row, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    except (TypeError, ValueError) as e:
        raise ManifestError(f"the manifest carries a value json cannot serialize: {e}") from e
    restored = _decode(json.loads(blob), path)
    if restored != manifest:
        # The check JSON's own types cannot make for the caller. A tuple nested inside the
        # free-form `generator_contract` serializes happily and reads back a list, as does a
        # sequence field handed in as a list where the type says tuple — after which the
        # manifest on disk stops equalling the one in memory, and nothing says so until a
        # resume hours later compares them. The differing fields are named because "it does
        # not round trip" sends the caller looking through every field there is.
        differing = [f.name for f in dataclasses.fields(Manifest)
                     if getattr(restored, f.name) != getattr(manifest, f.name)]
        raise ManifestError(
            f"this manifest does not survive its own round trip; {differing} come back as a "
            "different type. JSON has one sequence type: pass the declared tuples as tuples, "
            "and store nothing but lists, strings and numbers inside generator_contract.")
    try:
        storage.exclusive_write(path, blob)
    except FileExistsError as e:
        raise ManifestError(
            f"{path} already records this run's identity and is never rewritten (§14.2): "
            "the commands, the baseline and the selected paths were agreed once, and "
            "re-recording them would let a resume proceed on facts nobody approved.") from e


def read_manifest(run_dir) -> Manifest:
    """The run's identity as recorded. Raises ManifestError if it is absent or unreadable."""
    path = storage.manifest_path(run_dir)
    try:
        raw = path.read_bytes()
    except FileNotFoundError as e:
        raise ManifestError(
            f"{path} does not exist: this run never reached `confirmed`, so there is no "
            "record of what it agreed to do and nothing to resume from.") from e
    try:
        row = json.loads(raw)
    except ValueError as e:
        raise ManifestError(f"{path} is not readable as JSON: {e}") from e
    return _decode(row, path)


def snapshot_refs(repo, selected_paths) -> tuple[dict, str]:
    """`(protected_refs, status_digest)` for `repo` as it is right now.

    `selected_paths` is REQUIRED rather than defaulted, on `verify.build_verifier`'s argument
    for its `contract`: a default here is a policy, and the policy is the one a human
    confirmed at the §5 gate. A caller that has selected nothing passes `()`, which is a
    statement that the run carries no untracked path — and a caller that HAS selected
    something and forgets is the fail-open case the fourth digest part exists to close.

    Every call is read-only against the USER's repository, which is why `READONLY` is on all
    of them rather than on the status call alone: measured on git 2.53, a plain `git status`
    over a tracked file whose stat data had gone stale rewrote `.git/index`, and the same
    command under `GIT_OPTIONAL_LOCKS=0` left it byte for byte alone. §9 protects the index
    hash, so a snapshot that refreshed it would be forge moving the very thing it is here to
    watch — once at t0 and again at every drift check.
    """
    refs = {}
    # `git show-ref` exits 1 in a repository that has no refs at all, which is a fact about
    # the repository rather than a failure of the call.
    listed = gitcmd.git(repo, "show-ref", env_extra=gitcmd.READONLY, check=False)
    for line in listed.stdout.splitlines():
        oid, _, name = line.partition(" ")
        if name and not name.startswith(_FORGE_REF_PREFIXES):
            refs[name] = oid
    head = _head(repo)
    if head:
        refs["HEAD"] = head
    return refs, _status_digest(repo, head, selected_paths)


def _head(repo) -> str:
    """HEAD's OID, or "" for an unborn HEAD — an ordinary state of a fresh repository, which
    preflight rejects with a sentence a user can act on. Raising here would replace that
    sentence with a git stderr dump."""
    r = gitcmd.git(repo, "rev-parse", "--verify", "HEAD", env_extra=gitcmd.READONLY,
                   check=False)
    return r.stdout.strip() if r.returncode == 0 else ""


def _status_digest(repo, head: str, selected_paths) -> str:
    """sha256 over the four parts of the checkout state that move independently.

    Read as BYTES. Under `-z` a path is git's raw bytes with no quoting, and a repository is
    allowed to hold a path that is not valid UTF-8; decoding to compute a digest would raise
    on the one repository that most needs a drift check to work.

    Each part is fed in with its name and its LENGTH, and that framing is still defence in
    depth rather than load-bearing: the porcelain is the only part that may contain a NUL and
    is empty or NUL-terminated, `head` and the content digest are fixed-width hex or empty, and
    a ref name is empty or begins `refs/`, so the boundaries stay recoverable from the ends
    inward without it. What the framing buys is that the argument stops having to be re-derived
    over the value domains of whichever parts happen to be here — it took three steps for three
    parts and four for four, and a fifth with none of those properties would reopen it silently.
    """
    # The porcelain's paths are REPOSITORY-ROOT-relative even when git runs in a subdirectory
    # — measured — while `repo` only has to name the repository. Joining content paths onto
    # the argument would read them from the wrong directory whenever a caller passes a
    # subdirectory, which is the same slip `baseline.materialize` resolves `facts.root` for.
    # Binary, because a repository is allowed to live under a path that is not valid UTF-8 and
    # a text-mode read of it raises before any digest exists.
    root = gitcmd.git(repo, "rev-parse", "--show-toplevel", env_extra=gitcmd.READONLY,
                      binary=True).stdout.strip()
    porcelain = gitcmd.git(repo, *gitcmd.NO_DAEMON_CACHE, "status", "--porcelain=v1", "-z",
                           "--untracked-files=all", "--no-renames",
                           env_extra=gitcmd.READONLY, binary=True).stdout
    # `--untracked-files=all` is required, not tuning: git's default collapses an untracked
    # directory to a single `?? dir/` record, so a file appearing inside one leaves the
    # porcelain identical — measured — and untracked paths are selectable into the baseline.
    #
    # `--no-renames` carries BOTH of the reasons this package has for it. `status.renames` is
    # repo-local config, so it survives the /dev/null pins on the global and system files;
    # measured, flipping it rewrote an unchanged staged rename from `R renamed\0big` to
    # `D big\0A renamed`, and inheriting it would let a display preference the user changed
    # report as drift in their work. And `_carried_digest` PARSES these records — which this
    # call did not do before it existed, and which is `inspect.repo_facts`'s reason — so a
    # rename record's second NUL-separated path would be read as a status code plus a path.
    branch = gitcmd.git(repo, "symbolic-ref", "-q", "HEAD", env_extra=gitcmd.READONLY,
                        check=False).stdout.strip()   # "" when HEAD is detached
    h = hashlib.sha256()
    for label, value in (("porcelain", porcelain), ("head", head.encode()),
                         ("branch", branch.encode()),
                         ("carried", _carried_digest(root, porcelain, selected_paths))):
        h.update(f"{label}:{len(value)}:".encode("utf-8"))
        h.update(value)
    return h.hexdigest()


def _carried_digest(root: bytes, porcelain: bytes, selected_paths) -> bytes:
    """The content identity of every path the run is carrying, as one fixed-width digest.

    The set is the porcelain's TRACKED entries plus the selection, which is
    `baseline.materialize`'s `add -u -- :/` plus its `add -f` over `selected_untracked` read
    back — see the module docstring for why that is the right domain and what a recomputed
    tree OID would cost instead. `??` is dropped because forge carries no unselected untracked
    path; `!!` cannot appear without `--ignored` and is dropped for the same reason it would
    be if it could.

    Paths stay BYTES from git's output to `os.lstat`, never decoded: a repository may hold a
    path that is not valid UTF-8, and a drift check that raises is a drift check that does not
    run. `selected_paths` are `str` on both call paths — the caller's at the confirmation
    gate, and a manifest's, where JSON has only text — so `os.fsencode` puts them in the same
    alphabet.

    The length framing here is LOAD-BEARING, unlike `_status_digest`'s over the four parts. A
    path may contain anything but NUL, including the text of another entry's digest, so
    unframed `"a" + digest(a) + "b" + digest(b)` is byte for byte what one path literally named
    `a<digest(a)>b` produces when its own content digests to `digest(b)` — constructed, not
    hypothesised, and pinned by a test. Framing the digest as one value outside cannot help:
    the collision has already happened by the time that framing is applied.
    """
    paths = {rec[3:] for rec in porcelain.split(b"\0")
             if rec and rec[:2] not in (b"??", b"!!")}
    paths |= {os.fsencode(p) for p in selected_paths}
    h = hashlib.sha256()
    for rel in sorted(paths):
        value = _path_digest(os.path.join(root, rel))
        for part in (rel, value):
            h.update(b"%d:" % len(part))
            h.update(part)
    return h.hexdigest().encode("ascii")


def _path_digest(path: bytes) -> bytes:
    """One path's content identity — total over every shape a path can have, and raising for
    none of them.

    NEVER READ THROUGH A LINK, and never OPEN what is not a regular file. A symlink is its
    target TEXT on `baseline._sha256_link`'s argument: following it would put content from
    outside the tree into a measurement that claims to describe the tree. A FIFO is its file
    TYPE on `verify._surface_state`'s: a read-open on one blocks until a writer appears, and
    there is no timeout anywhere in this call path.

    The executable BIT rides along with a regular file's bytes because that is the only part
    of the mode git records, so a `chmod +x` on a carried script is work a merge could revert.
    The rest of the mode is left out deliberately: git ignores it, so hashing it would report
    a umask difference as drift in the user's work.

    An OSError is an ANSWER, not an exception: this runs at t0 and again at every drift check,
    and one raised here would take the whole check with it — on the repository least able to
    spare it. Every one contributes its ERRNO rather than a shared "missing" value, so a
    carried file the user deleted (ENOENT) and one they made unreadable (EACCES) stay different
    answers; collapsing them would let either become the other without moving the digest.
    """
    try:
        st = os.lstat(path)
    except OSError as e:
        return b"error:%d" % e.errno
    try:
        if stat.S_ISLNK(st.st_mode):
            return b"link:" + hashlib.sha256(os.readlink(path)).hexdigest().encode("ascii")
        if stat.S_ISDIR(st.st_mode):
            return _dir_digest(path)
        if stat.S_ISREG(st.st_mode):
            h = hashlib.sha256()
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 16), b""):
                    h.update(chunk)
            return b"file:%d:" % bool(st.st_mode & 0o111) + h.hexdigest().encode("ascii")
    except OSError as e:
        return b"error:%d" % e.errno
    return b"special:%d" % stat.S_IFMT(st.st_mode)


def _dir_digest(path: bytes) -> bytes:
    """A selected DIRECTORY's contents. §2.2 contemplates selecting one explicitly, and
    `baseline.materialize`'s literal pathspec sweeps its whole contents into B — so a digest
    that stopped at the directory itself would be blind to every file the run is carrying
    inside it, which was measured against a recomputed tree OID and is why this exists.

    `baseline._walk_selected`'s rules, for its reasons: `.git` is pruned rather than
    post-filtered, and a linked directory is reported by its own target text rather than
    descended into. A directory that cannot be listed contributes its errno rather than
    nothing, because os.walk's default is to swallow the error and yield an empty tree — which
    would make an unreadable directory digest the same as an empty one.
    """
    h = hashlib.sha256()
    failures = []
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False,
                                                onerror=failures.append):
        dirnames[:] = sorted(n for n in dirnames if n != b".git")
        leaves = [n for n in list(dirnames) if os.path.islink(os.path.join(dirpath, n))]
        for n in leaves:
            dirnames.remove(n)
        for name in leaves + sorted(filenames):
            leaf = os.path.join(dirpath, name)
            rel = os.path.relpath(leaf, path)
            for part in (rel, _path_digest(leaf)):
                h.update(b"%d:" % len(part))
                h.update(part)
    for e in failures:
        h.update(b"error:%d:" % (e.errno or 0))
    return b"dir:" + h.hexdigest().encode("ascii")


def _text(name, value, source):
    if not isinstance(value, str):
        raise ManifestError(f"{source}: {name} must be a string, not {value!r}")
    return value


def _texts(name, value, source):
    # The list check is not redundant with the element check below: a string iterates into
    # its characters, so `tuple("scratch")` yields a seven-element path list without a word
    # of complaint from anything downstream.
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ManifestError(f"{source}: {name} must be a list of strings, not {value!r}")
    return tuple(value)


# §5.1's step record, whole. Named here rather than derived from `verify.Step`'s fields so a
# field added there cannot start arriving in manifests this reader silently accepts — the same
# argument the unknown-field refusal below makes one level up.
_STEP_FIELDS = ("argv", "cwd", "env", "timeout")


def _steps(name, value, source):
    if not isinstance(value, list):
        raise ManifestError(f"{source}: {name} must be a list of step records, not {value!r}")
    steps = []
    for i, row in enumerate(value):
        where = f"{source}: {name}[{i}]"
        if not isinstance(row, dict):
            raise ManifestError(
                f"{where} is {row!r}, not a step record. §5.1's step is "
                "{argv, cwd, env, timeout} — an argv on its own leaves the cwd, the "
                "environment and the timeout for whoever resumes to supply, and a field the "
                "reader supplies is a fact the run never agreed to.")
        missing = [f for f in _STEP_FIELDS if f not in row]
        if missing:
            raise ManifestError(f"{where} is missing {missing}")
        unknown = sorted(set(row) - set(_STEP_FIELDS))
        if unknown:
            raise ManifestError(f"{where} carries fields this engine does not know: {unknown}")
        argv = row["argv"]
        if not isinstance(argv, list) or not all(isinstance(w, str) for w in argv):
            raise ManifestError(
                f"{where}: argv is {argv!r}, which is not a list of words. A command is the "
                "words it is made of — a string here would be re-parsed by whoever runs it, "
                "and §5.1 rejects shell syntax rather than reinterpreting it.")
        env = row["env"]
        if not isinstance(env, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
            raise ManifestError(
                f"{where}: env must be an object of strings, not {env!r}; it is spliced into "
                "a process environment, where anything else raises at spawn time")
        timeout = row["timeout"]
        # `isinstance(True, int)` — a JSON `true` here would run the step under a one-second
        # budget rather than being refused.
        if isinstance(timeout, bool) or not isinstance(timeout, int):
            raise ManifestError(
                f"{where}: timeout must be a whole number of seconds, not {timeout!r}")
        try:
            steps.append(Step(argv=tuple(argv), cwd=_text("cwd", row["cwd"], where),
                              env=env, timeout=timeout))
        except VerifyError as e:
            # `Step` refuses an empty argv and a program name only a shell could resolve.
            # Re-raised in this module's vocabulary rather than propagated: `read_manifest`'s
            # documented contract is that a record it will not stand behind arrives as a
            # ManifestError, and the resume that catches it has no other name for this.
            raise ManifestError(f"{where}: {e}") from e
    return tuple(steps)


def _mapping(name, value, source):
    if not isinstance(value, dict):
        raise ManifestError(f"{source}: {name} must be an object, not {value!r}")
    return value


# One decoder per field, by name. A field added to `Manifest` and not named here makes the
# next read fail loudly, which is the point: the alternative is that it arrives as whatever
# JSON made of it and compares unequal to what was written, silently, on a resume.
_DECODERS = {
    "run_id": _text,
    "repo_path": _text,
    "base_commit": _text,
    "baseline_ref": _text,
    "baseline_commit": _text,
    "tracked_tree_oid": _text,
    "selected_paths": _texts,
    "generator_contract": _mapping,
    "setup": _steps,
    "verify": _steps,
    "protected_refs": _mapping,
    "status_digest": _text,
    "created_at": _text,
}


def _decode(row, source) -> Manifest:
    if not isinstance(row, dict):
        raise ManifestError(f"{source}: a manifest is an object, not {type(row).__name__}")
    names = [f.name for f in dataclasses.fields(Manifest)]
    missing = [n for n in names if n not in row]
    if missing:
        # Not defaulted. A field the reader supplies is a fact the run never agreed to, and
        # `base_commit` or `verify` invented at resume time is the whole failure this file
        # exists to prevent.
        raise ManifestError(f"{source} is missing {missing}")
    unknown = sorted(set(row) - set(names))
    if unknown:
        # A recorder that once wrote a fact this reader drops would let a resume answer
        # questions about the run out of a manifest it only partly understands.
        raise ManifestError(f"{source} carries fields this engine does not know: {unknown}")
    fields = {}
    for name in names:
        decode = _DECODERS.get(name)
        if decode is None:
            raise ManifestError(f"no decoder is declared for the manifest field {name!r}")
        fields[name] = decode(name, row[name], source)
    return Manifest(**fields)


# §14's graph, as the successors of each phase — the one place it is written down. Read as a
# mapping rather than a list of pairs so a refusal can say what IS legal from where the run
# stands, which is the difference between a message a resume can act on and one that only
# says no. The chain is §14's wrapped line straightened out: `comparing → synthesizing` is
# the joint the wrap hides, `synthesizing ⇄ verifying` is two edges, and `reviewing →
# synthesizing` is the back-edge §14 declares in prose beside the diagram.
_EDGES = {
    "created": frozenset({"confirmed"}),
    "confirmed": frozenset({"setting_up"}),
    "setting_up": frozenset({"building"}),
    "building": frozenset({"harvested"}),
    "harvested": frozenset({"comparing"}),
    "comparing": frozenset({"synthesizing"}),
    "synthesizing": frozenset({"verifying"}),
    "verifying": frozenset({"synthesizing", "reviewing"}),
    "reviewing": frozenset({"synthesizing", "ready", "degraded", "review_blocked",
                            "source_diverged", "failed"}),
    "ready": frozenset(),
    "degraded": frozenset(),
    "review_blocked": frozenset(),
    "source_diverged": frozenset(),
    "failed": frozenset(),
}

# Both views are DERIVED from that one declaration. A separately spelled terminal list is a
# second place to be right about which phases end a run, and the two are then free to
# disagree — a phase listed terminal while holding a successor is a run that ends and then
# carries on. What derivation costs is that a phase whose successors were forgotten reads as
# terminal instead of failing; `test_every_terminal_the_spec_names_exists` is what stands
# there, by naming the five §14 declares rather than counting them.
PHASES = tuple(_EDGES)
TERMINAL = frozenset(name for name, successors in _EDGES.items() if not successors)

# §14.1's name for an operation that started, left no receipt and has no surviving process:
# "It is never silently retried." A SEAT's outcome, not a phase of the run — §14's graph has
# no such ending, so it is deliberately absent from PHASES and `advance` refuses it like any
# other name the graph does not declare.
OUTCOME_UNKNOWN = "outcome_unknown"


@dataclass(frozen=True)
class State:
    """§14's five dimensions, which are separate because one enum cannot hold them.

    No defaults, on `snapshot_refs`'s argument: a run at `created` with round 0 and a run
    resuming a fix after round 2 are both ordinary, and a dimension the constructor supplies
    is a fact nobody recorded.

    `phase` is NOT validated here. A state read back from a foreign or damaged record has to
    be constructible for a resume to report what it found, so the refusal lives at `advance`,
    where the question "what happens next" is actually being asked.
    """
    phase: str
    round: int
    attempt: int
    verified_checkpoint: str | None
    deliverable_checkpoint: str | None


def advance(state: State, phase: str) -> State:
    """`state` moved to `phase`, or TransitionError because §14 declares no such edge.

    ONLY the phase moves — see the module docstring for what each of the other four would cost
    if it rode along. A caller that means to spend a round or a retry says so itself, which is
    what "separate dimensions" buys and what a resume reads back to tell one fix from the next.
    """
    if not isinstance(state, State):
        raise TransitionError(f"a State is required, not {type(state).__name__}")
    if not isinstance(phase, str):
        # Checked rather than left to the lookup: an unhashable target raises TypeError out of
        # `in`, which no caller of this module is catching.
        raise TransitionError(f"a phase is one of §14's names, not {phase!r}")
    successors = _EDGES.get(state.phase)
    if successors is None:
        raise TransitionError(
            f"this run is in {state.phase!r}, which §14 does not declare, so what follows it "
            f"is not a question this graph can answer. The declared phases are {list(PHASES)}.")
    if phase not in successors:
        if not successors:
            raise TransitionError(
                f"{state.phase!r} ends the run; §14 declares no phase after a terminal, and a "
                f"run that left one would have reported an outcome it then went on to change.")
        unknown = "" if phase in _EDGES else " — which is not a declared phase at all"
        raise TransitionError(
            f"§14 declares no edge {state.phase!r} → {phase!r}{unknown}. From {state.phase!r} "
            f"a run may go to {sorted(successors)}.")
    return dataclasses.replace(state, phase=phase)


def write_seat(run_dir, name: str, payload: dict) -> None:
    """Record what seat `name` last knew, replacing whatever it last said.

    REWRITTEN, unlike the manifest: a seat's status moves the whole length of its run, and
    write-once here would make its second update a crash. `atomic_write` is what makes the
    rewrite safe to be interrupted — the record is published by a rename, so a reader arriving
    mid-write sees the previous record whole rather than a prefix of this one, and the killed
    writer of §14.1 leaves the seat saying something older rather than something torn.

    NOT THE SIGNAL PATH. §14.1 requires the handler write "only pre-formatted bytes it already
    holds, never serializes", and this call serializes; a handler owing a record hands the
    bytes it is already holding to `storage.atomic_write` itself.

    Refused BEFORE anything is published, on all four counts below, so a rejected record
    leaves the seat's last good one exactly where a resume will look for it.
    """
    path = storage.seat_state_path(run_dir, name)
    if not isinstance(payload, dict):
        raise StateError(
            f"{path}: a seat record is an object of facts about the seat, not "
            f"{type(payload).__name__}")
    if not payload:
        raise StateError(
            f"{path}: a seat record may not be empty. `{{}}` is falsy exactly as the None a "
            "seat that never wrote reads back as, and §14.1 requires those two stay apart.")
    try:
        # sort_keys so one seat state has one spelling on disk whatever order the caller built
        # it in; indented because the first reader of this file is usually a human working out
        # what a crashed seat had got to.
        # allow_nan=False because the default publishes `Infinity`/`NaN`, which THIS reader
        # accepts and no strict JSON reader does — a record only forge can read is one a
        # `--collect` in another language cannot. NaN was already refused, but by accident:
        # `nan != nan` trips the round-trip check, so it was reported as a shape failure
        # rather than as a value JSON cannot carry.
        blob = json.dumps(payload, sort_keys=True, indent=2,
                          allow_nan=False).encode("utf-8") + b"\n"
    except (TypeError, ValueError) as e:
        raise StateError(
            f"{path}: this record carries a value json cannot serialize: {e}") from e
    restored = json.loads(blob)
    if restored != payload:
        # `write_manifest`'s check, one file over and for the same reason: JSON has one
        # sequence type and one key type, so a tuple or an int key is accepted here and comes
        # back as something that compares unequal — with nothing saying so until a resume
        # compares them hours later. Keys are reported through repr() because the mismatched
        # pair is usually `1` and `"1"`, which do NOT sort against each other — without repr() the
        # diagnostic path raises TypeError while building the error it was reporting.
        differing = sorted(repr(k) for k in set(payload) | set(restored)
                           if payload.get(k) != restored.get(k))
        raise StateError(
            f"{path}: this record does not survive its own round trip; {differing} come back "
            "as something else. Store lists, strings and numbers under string keys.")
    storage.atomic_write(path, blob)


def read_seat(run_dir, name: str) -> dict | None:
    """What seat `name` last recorded, or None because it never recorded anything.

    None means ONE thing — no such file. Every other way a record can fail to be one raises,
    because §14.1's requirement is that a damaged seat and a silent one stay different
    answers; see the module docstring for the three routes that reach None — or an answer a
    caller cannot tell from it — without damaging a byte, which is why this is a whitelist
    rather than a fallback.
    """
    path = storage.seat_state_path(run_dir, name)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        # The one absence this call reports. A run directory that does not exist at all is
        # `read_manifest`'s refusal, which every resume passes through before it asks a seat
        # anything.
        return None
    except OSError as e:
        raise StateError(f"{path} exists and cannot be read: {e}") from e
    try:
        row = json.loads(raw)
    except ValueError as e:
        raise StateError(
            f"{path} is not readable as JSON: {e}. A record cut short by a killed writer "
            "lands here, and §14.1 requires that it read as a damaged seat and not a silent "
            "one.") from e
    if not isinstance(row, dict):
        raise StateError(
            f"{path} holds a {type(row).__name__}, which is not a seat record. A JSON `null` "
            "is the one that matters: it parses cleanly and reads back as the same None a "
            "missing file gives, so a reader trusting the parse would report a seat that "
            "recorded something as one that never ran.")
    if not row:
        raise StateError(
            f"{path} holds an empty record, which is falsy exactly as the None a seat that "
            "never wrote reads back as. `write_seat` refuses to produce one; this arrived by "
            "another route.")
    return row
