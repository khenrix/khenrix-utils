"""What the run agreed to do, and the repository state it is judged against (§9, §14.2).

Both facts live here because they are recorded at the same instant and are worth nothing
apart. The manifest says what the run committed to — repo path, `base_commit`, the baseline's
identity, the selected paths, the confirmed argv. The snapshot says what the user's repository
looked like when it committed to that. A manifest without the snapshot describes a deliverable
with no way to ask whether it still applies; a snapshot without the manifest is a photograph
of nothing in particular.

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

WHAT THE SNAPSHOT IS. §9 protects the user's current branch ref, `HEAD`, the index hash, the
checkout, and every non-forge ref, and whitelists forge's own refs by exact name AND the exact
OID recorded here — a namespace whitelist would let a seat write into forge's own namespace
invisibly. So `protected_refs` is name-to-OID for every ref that is not forge's, and
`status_digest` covers the three parts of the checkout state that move independently:

  * the porcelain, for the working tree and the index;
  * `HEAD`, because a clean tree, an edit and a commit leaves the porcelain byte for byte
    what it was — measured — so on the porcelain alone an entire commit of the user's work
    reads as no drift at all;
  * the branch `HEAD` points at, because switching between two branches at one commit moves
    the porcelain, `HEAD`'s OID and every ref not at all, and handover targets the branch that
    was current.

It does not cover ignored paths. `git status` needs `--ignored` for those, and admitting them
was rejected on the other direction: a repository with a watcher or a dev server rewrites
ignored build output continuously, so the digest would move during every run and the drift
report would fire on all of them. What bounds it is that the untracked set preflight offers
comes from `--untracked-files=all`, which does not list ignored paths.
"""
import dataclasses
import hashlib
import json
from dataclasses import dataclass

from . import gitcmd, storage


class ManifestError(RuntimeError):
    """A run's identity that cannot be recorded, cannot be recovered, or would be rewritten."""


# §9's two explicitly-allowed namespaces. Prefixes rather than a substring test: a user's
# `refs/heads/forgery-experiments` is theirs, and dropping it here would leave a moved ref
# with nothing to compare against — the fail-OPEN direction of the same decision.
_FORGE_REF_PREFIXES = ("refs/khenrix-forge/", "refs/heads/forge/")


@dataclass(frozen=True)
class Manifest:
    """The run's identity, as agreed at the §5 gate.

    `setup` and `verify` are sequences of argv sequences, never strings: §5.1 rejects shell
    metacharacter syntax rather than reinterpreting it, and a manifest holding
    `"cd frontend && npm ci"` would hand a resume a command it has to re-parse — which is
    re-detection under another name, at the one moment nobody is watching.

    `protected_refs` and `status_digest` are the t0 snapshot: what the user's repository
    looked like at the moment of agreement, kept so a later check can ask whether it still
    does.
    """
    run_id: str
    repo_path: str
    base_commit: str
    baseline_ref: str
    baseline_commit: str
    tracked_tree_oid: str
    selected_paths: tuple[str, ...]
    generator_contract: dict
    setup: tuple[tuple[str, ...], ...]
    verify: tuple[tuple[str, ...], ...]
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


def snapshot_refs(repo) -> tuple[dict, str]:
    """`(protected_refs, status_digest)` for `repo` as it is right now.

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
    return refs, _status_digest(repo, head)


def _head(repo) -> str:
    """HEAD's OID, or "" for an unborn HEAD — an ordinary state of a fresh repository, which
    preflight rejects with a sentence a user can act on. Raising here would replace that
    sentence with a git stderr dump."""
    r = gitcmd.git(repo, "rev-parse", "--verify", "HEAD", env_extra=gitcmd.READONLY,
                   check=False)
    return r.stdout.strip() if r.returncode == 0 else ""


def _status_digest(repo, head: str) -> str:
    """sha256 over the three parts of the checkout state that move independently.

    Read as BYTES. Under `-z` a path is git's raw bytes with no quoting, and a repository is
    allowed to hold a path that is not valid UTF-8; decoding to compute a digest would raise
    on the one repository that most needs a drift check to work.

    Each part is fed in with its name and its LENGTH, and that framing is defence in depth
    rather than load-bearing: a porcelain is empty or NUL-terminated, no path may contain a
    NUL, and `head` is fixed-width hex or empty, so the three parts are already unambiguous
    end to end and no pair of checkouts can be made to digest the same without it. What the
    framing buys is that the argument stops depending on the value domain of these particular
    three — a fourth part that is neither NUL-free nor fixed-width would otherwise reopen it
    silently.
    """
    porcelain = gitcmd.git(repo, *gitcmd.NO_DAEMON_CACHE, "status", "--porcelain=v1", "-z",
                           "--untracked-files=all", "--no-renames",
                           env_extra=gitcmd.READONLY, binary=True).stdout
    # `--untracked-files=all` is required, not tuning: git's default collapses an untracked
    # directory to a single `?? dir/` record, so a file appearing inside one leaves the
    # porcelain identical — measured — and untracked paths are selectable into the baseline.
    #
    # `--no-renames` is required here for a DIFFERENT reason than the one inspect.repo_facts
    # gives, which is about parsing the records and this does not parse them. `status.renames`
    # is repo-local config, so it survives the /dev/null pins on the global and system files;
    # measured, flipping it rewrote an unchanged staged rename from `R renamed\0big` to
    # `D big\0A renamed`. Inheriting it would let a display preference the user changed report
    # as drift in their work.
    branch = gitcmd.git(repo, "symbolic-ref", "-q", "HEAD", env_extra=gitcmd.READONLY,
                        check=False).stdout.strip()   # "" when HEAD is detached
    h = hashlib.sha256()
    for label, value in (("porcelain", porcelain), ("head", head.encode()),
                         ("branch", branch.encode())):
        h.update(f"{label}:{len(value)}:".encode("utf-8"))
        h.update(value)
    return h.hexdigest()


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


def _argv(name, value, source):
    if not isinstance(value, list):
        raise ManifestError(f"{source}: {name} must be a list of argv lists, not {value!r}")
    for cmd in value:
        if not isinstance(cmd, list) or not all(isinstance(w, str) for w in cmd):
            raise ManifestError(
                f"{source}: {name} holds {cmd!r}, which is not an argv list. A command is "
                "the words it is made of — a string here would be re-parsed by whoever runs "
                "it, and §5.1 rejects shell syntax rather than reinterpreting it.")
    return tuple(tuple(cmd) for cmd in value)


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
    "setup": _argv,
    "verify": _argv,
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
