"""§10: the claim ledger — what is CURRENTLY CLAIMED about the fused result.

A COMPACTION-SURVIVABLE SPEC AND AUDIT TRAIL, claimed as nothing more. Writing it requires
reading all three artifact sets, so peak context is unchanged; it is not a context-budget
control and must not be sold as one.

TWO FILES, TWO DISCIPLINES. `events.jsonl` is append-only because every line is a fact that
HAPPENED. A ledger row's `status` CHANGES — §13's loop revises it across review rounds — so an
append-only ledger would force every reader to fold the log to learn the current row, and two
folds written in two places eventually disagree. The journal covers what happened; this covers
what is claimed. Published through `storage.atomic_write`, so a reader arriving mid-write sees
the previous ledger whole.

NOT `exclusive_write`. The manifest is write-once because §14.2 makes the run's identity agreed
once and never rewritten; this is the opposite, and write-once would make review round 2 a crash.

A STRICT DECODER, AND `write_seat`'S SILENCE IS NOT A PRECEDENT FOR IT. That module argues its
own case: §14.2 assigns the seat record's fields to the ORCHESTRATOR, so `runstate` refuses to
become the authority on a record another module owns. None of that transfers. §10 enumerates
these fields exactly; §14.1 hashes this file into EVERY checkpoint commit message, so a field a
writer stopped writing silently changes the hash with nothing saying why; and §10's own text
says from-scratch synthesis "reads only the ledger", so an unvalidated row is directly a wrong
deliverable rather than a confusing record. Measured on that module: `{"phse": "biulding"}`
round-trips with no complaint. Here, `status: "acceptd"` would read as neither accepted nor
rejected and the coverage check would silently skip it.

WHERE THE BYTES ARE, AND WHERE THEY ARE NOT. The run directory, at 0700, under
`${XDG_STATE_HOME}/khenrix-forge/<hash>-<run-id>/`. NO LEDGER BYTES EXIST UNDER ANY CLONE ROOT —
not a seat clone, not the synthesis checkout, not a verifier clone, and above all not inside the
§20 task bundle, which reviewers ARE given. §13 sets every reviewer's cwd to the synthesis
checkout and a reviewer has a shell, so the guarantee must be STRUCTURAL (the bytes are not in
the tree), never textual (an instruction not to read them). §14.1 embeds the ledger HASH in the
checkpoint commit message and reviewers run `git log` by design — a hash is safe, a summary line
quoting a claim is the ledger handed over through git. Say hash; never summary.

WHAT THIS MODULE REFUSES AT WRITE AND WHAT IT LEAVES TO COVERAGE. Structure is refused here,
where the producer is still present and can fix it (`write_manifest`'s round-trip refusal makes
the same argument): a stale id, a duplicate id, an unknown vocabulary word, a dangling
dependency, an ordering cycle, a self-conflict, a one-sided conflict, a criterion whose kind
and fields disagree.
SEMANTICS are `coverage`'s: whether two conflicting rows are both accepted, and whether an
accepted row contradicts a unanimous rejection, are findings a report carries — §12.4 makes the
coverage check "a fallback trigger AND a report line", which a write refusal cannot be.
"""
import dataclasses
import hashlib
import json
from dataclasses import dataclass, fields

from . import bundle as bundlemod
from . import storage

VERSION = 1

KINDS = ("behavior", "api", "schema", "migration", "security", "test", "architecture", "seam")
STATUSES = ("accepted", "rejected", "deferred", "unresolved")
# ONE ORDERING RELATION WITH TWO NAMES, plus a symmetric one that is not an ordering at all.
RELATIONS = ("requires", "conflicts", "blocks")
STANCES = ("supports", "contradicts", "silent")
CRITERION_KINDS = ("test", "symbol", "hash", "schema", "prose")

# §10 says "above N KB union diff, drop to per-file summaries and say so in the report" and
# never gives N. An unspecified threshold is one two implementers set differently, so it is a
# named constant here AND a recorded field on every ledger: changing this value later must not
# silently reinterpret a ledger written under the old one.
DEGRADE_UNION_DIFF_BYTES = 512 * 1024

# How much of a claim a refusal quotes. A human reading a cycle refusal cannot act on twelve
# hex characters, and a whole claim per node makes the message unreadable at four nodes.
_CLAIM_CLIP = 80


class LedgerError(RuntimeError):
    """This ledger cannot be recorded honestly, or cannot be read back as what was written."""


@dataclass(frozen=True)
class Dependency:
    """One relation this row declares. VALUE FORM, not key form.

    §10 writes `[{id, requires|conflicts|blocks}]`, which does not say whether the relation is
    a key (`{"id": X, "requires": true}`) or a value (`{"id": X, "relation": "requires"}`). Two
    implementers read it two ways and write two records. The value form is chosen and the
    decoder refuses any other key and any other relation string.
    """
    id: str
    relation: str


@dataclass(frozen=True)
class SeatEvidence:
    """One seat's stance on one claim. §10: a NESTED LIST, never flattened columns.

    Prompt-identity conditioning (§11) is per seat and cannot be recorded any other way.
    `prompt_sha256` is `str | None`, and `None` means the seat's identity was never captured —
    it must never compare equal to another `None` for "the same prompt".
    """
    seat: str
    stance: str
    evidence: str
    prompt_sha256: str | None


@dataclass(frozen=True)
class Criterion:
    """One acceptance criterion, with its evaluator's inputs as STRUCTURED FIELDS.

    THE RULE THAT MAKES §10.1 ENFORCEABLE. §10.1's worked example is a symbol-presence check
    (`os.replace` appears) standing in for a behavioural claim (crash-safe atomic update) while
    the property is false. So a mechanical criterion may only be attached to a criterion PHRASED
    as the thing the predicate proves: `kind="symbol"` must carry `path` and `symbol` as fields,
    and a sentence with a symbol name in it is refused. The decoder is where that lives, because
    a rule stated only in prose is one the next author will not meet.

    §10.1 NAMES FOUR MECHANICAL KINDS — test ID, SCHEMA QUERY, exact symbol, file/hash
    invariant — so `schema` carries structured inputs like the other three: the query and the
    schema it runs against. Requiring none of them made a schema criterion a bare sentence,
    which is what the paragraph above refuses for `symbol`, on a kind the spec puts in the
    same list.

    `prose` is therefore the ONE kind with no predicate, and the only one `trace` — a human's
    record of having traced the claim — may hang on. On a mechanical kind the predicate IS the
    evidence, and a human note beside it that could flip the method to `manual_trace_confirmed`
    is §10.1's manufactured green arriving by a second route.

    THAT THIS REPOSITORY HAS NO SCHEMA EVALUATOR IS WHY `trace` IS REFUSED HERE, NOT WHY IT
    WOULD BE ALLOWED. A `schema` criterion answers `unresolved` downstream — nobody looked —
    and `manual_trace_confirmed` says a human did. Those are different verdicts, and letting
    the missing evaluator become a human's word is the same manufactured green wearing the
    other one.
    """
    kind: str
    text: str
    path: str | None
    symbol: str | None
    node_id: str | None
    sha256: str | None
    query: str | None
    trace: str | None


@dataclass(frozen=True)
class Row:
    """§10's row. `requirement_id` is split into three fields because §10 asks for it "+ source
    span/hash" and one string cannot carry three facts a reader has to compare separately.

    `rejected` IS FIRST-CLASS. If all three seats considered and rejected a cache layer, that is
    the most valuable signal in the run — and from-scratch synthesis, which reads only this file,
    would otherwise add it straight back.
    """
    id: str
    requirement_id: str
    requirement_span: str
    requirement_sha256: str
    kind: str
    component: str
    semantic_claim: str
    status: str
    dependencies: tuple
    seat_evidence: tuple
    counterevidence: str
    acceptance_criteria: tuple
    synthesis_evidence: dict | None
    verification_receipt: str | None
    risk: str
    rationale: str


@dataclass(frozen=True)
class Ledger:
    """The rows plus the degradation record §10 asks for.

    THE DEGRADATION IS IN THE LEDGER, NOT ONLY IN THE REPORT. §10 says to "say so in the
    report", but it also says from-scratch synthesis "reads only the ledger" — so a degraded
    ledger silently becomes the INPUT to synthesis while §12.2's seam claims assume it is a
    spec. Both the measured size and the threshold applied are recorded, so a later change to
    `DEGRADE_UNION_DIFF_BYTES` cannot reinterpret a ledger written under the old one.
    """
    version: int
    rows: tuple
    union_diff_bytes: int
    degrade_threshold_bytes: int
    degraded: bool


# The nested lists on a Row, and the type each element holds. ONE PAIRING, READ BY BOTH SIDES:
# `_decode` builds elements of these classes with `_sub`, and `_check` requires a Ledger built
# IN PROCESS to already hold them. Named rather than inlined so the decode loop can also tell
# "this key is absent" from "this key is an empty list", which are different records.
_ROW_LISTS = (("dependencies", Dependency), ("seat_evidence", SeatEvidence),
              ("acceptance_criteria", Criterion))


def row_id(requirement_id: str, semantic_claim: str) -> str:
    """§10's content-derived id: `sha256(requirement_id || semantic_claim)[:12]`, FRAMED.

    A bare `||` is ambiguous — `("ab","c")` and `("a","bc")` produce the same bytes — and that
    is not a theoretical collision here: §10's stated reason for content-derived ids is that "a
    round splits or inserts a claim", and splitting a claim is exactly the operation that
    manufactures such a pair. A JSON array is injective because quoting and escaping delimit the
    fields, and it is this repository's existing spelling for a content hash
    (`checks.source_hash`).

    `json.dumps(...).encode()` rather than hand-rolled `.encode("utf-8")` on the raw strings:
    a claim carrying a lone surrogate (possible if it was ever read from a filesystem name)
    raises `UnicodeEncodeError` out of the id function, while `json.dumps` escapes it.

    NO NORMALIZATION. No strip, no casefold, no whitespace collapse, no NFC. A normalization
    rule is a second predicate every implementer and every language reading this file has to
    spell identically — and §10 WANTS an edited claim to be a different claim, because coverage
    compares across review rounds and a shifting id makes the check compare stale identity. A
    round that rephrases a claim INSERTS the new row and resolves the old one (`deferred` or
    `rejected`, with `rationale` naming the successor id); it never mutates text in place.

    `.strip()` is the tempting one, and it is jointly wrong with the equality check in
    `write_ledger`: normalization makes two visibly different claims share a row, and the check
    then passes because both sides normalize.

    BOTH ARGUMENTS ARE `str` OR THIS REFUSES, because `json.dumps` fails two different ways on
    anything else and neither is this module's error class. An unserializable value raised
    `TypeError`; an int or a list serialized QUIETLY, so a row whose `requirement_id` was a
    number hashed into an id nothing downstream would question.
    """
    for name, v in (("requirement_id", requirement_id), ("semantic_claim", semantic_claim)):
        if not isinstance(v, str):
            raise LedgerError(f"a row id is taken over two strings; {name} is a str, not "
                              f"{type(v).__name__}")
    canonical = json.dumps([requirement_id, semantic_claim], sort_keys=True).encode()
    return hashlib.sha256(canonical).hexdigest()[:12]


def _as_rows(rows) -> tuple:
    """Materialize ONCE, and refuse anything that is not a `Row`.

    ITERATED ONCE, BY EVERY CALLER. `topological_order` reads its argument three times — the
    ids, the claims, and `edges` — so a GENERATOR is exhausted by the first pass and the last
    two see an empty graph: every edge silently dropped, every in-degree zero, the length check
    satisfied, and a clean SORTED order returned for a graph that declared constraints. §12.2
    partitions synthesis by that order. Measured: a two-row `requires` came back reversed.

    The type check is the same public-surface argument the dangling-edge refusal makes.
    `edges` and `topological_order` are called by §12.2 over rows that need never have been
    through a write, and a dict or a bare string there raised `AttributeError` on
    `r.dependencies` — an error escaping this module's declared class, so one no caller of it
    knows to catch. A `str` is the ordinary way to reach it, because iterating one yields
    characters rather than failing.
    """
    try:
        out = tuple(rows)
    except TypeError as e:
        raise LedgerError(
            f"rows is a sequence of Row, not {type(rows).__name__}") from e
    _check_rows(out)
    return out


def _check_rows(rows) -> None:
    """A sequence of `Row`, each holding its declared element types. THE SHAPE, NOT THE VALUES.

    Separate from `_check` because three entry points meet a `Row` with no decoder ahead of
    them and none of the three runs the rest: `_as_rows`, for the two public graph functions;
    `_payload`, for `ledger_hash`, which §14.1 calls without ever going through `_check`; and
    `_check` itself, before it starts reading fields whose meaning presumes the shape.

    ONE RULE IN ONE PLACE, which is why `edges` is refused a row whose `acceptance_criteria`
    are malformed although it never reads them. The narrower alternative is a second spelling
    per caller, and two spellings of one rule drifting apart is this project's standing defect.
    """
    if not isinstance(rows, (tuple, list)):
        raise LedgerError(f"a ledger's rows are a sequence of Row, not {type(rows).__name__}")
    for r in rows:
        if not isinstance(r, Row):
            raise LedgerError(f"a ledger row is a Row, not {type(r).__name__}")
        _check_row_lists(r)


def _check_row_lists(r: Row) -> None:
    """The three nested lists hold what `_ROW_LISTS` says they hold — ON THE WRITE SIDE.

    `_decode` builds every element with `_sub`, so a ledger READ from disk cannot hold the
    wrong type here. A `Ledger` built IN PROCESS never goes through it, and §12.2's synthesis
    builds rows — so this is the ordinary route into the module, not an exotic one. It is the
    dangling-edge argument one branch over: `_check` typed the ROW and said nothing about what
    its lists carry, and the element's real type was then discovered by whoever first read a
    field off it. Measured, all four escape this module's declared class — `d.relation` in
    `edges` and `topological_order`, `e.stance` and `c.kind` in `_check`, all `AttributeError`;
    `dataclasses.asdict` in `ledger_hash`, `TypeError`.

    A `list` is accepted here because refusing it is `write_ledger`'s round-trip check, which
    reports the right defect — JSON has one sequence type — where this would report a type
    error about a sequence that is in fact the right shape.
    """
    for name, cls in _ROW_LISTS:
        v = getattr(r, name)
        if not isinstance(v, (tuple, list)):
            raise LedgerError(f"row {r.id}: {name} is a sequence of {cls.__name__}, "
                              f"not {type(v).__name__}")
        for i, x in enumerate(v):
            if not isinstance(x, cls):
                raise LedgerError(f"row {r.id}: {name}[{i}] is a {cls.__name__}, "
                                  f"not {type(x).__name__}")


def edges(rows) -> tuple:
    """The ONE directed edge set, with both spellings normalized into it.

    §10's `requires` and `blocks` are THE SAME EDGE IN OPPOSITE DIRECTIONS: "A blocks B" and
    "B requires A" describe one ordering constraint. That is the first real defect in §10's row
    spec, and it is dangerous rather than cosmetic — a cycle checker that walks only `requires`
    misses every cycle a writer expressed with `blocks`, and PASSES.

    AN EDGE HERE READS "THE TAIL MUST BE SYNTHESIZED BEFORE THE HEAD", which is the direction
    Kahn's algorithm below needs and the ONLY one §12.2 admits: "topological ordering is a
    precondition of partitioned synthesis", and no claim can be synthesized before the claim it
    requires. So `requires` on row X naming Y is `Y -> X`, and `blocks` on row A naming B is
    `A -> B` — the same edge `{"id": A, "relation": "requires"}` on row B would produce.

    REVERSING THIS IS SILENTLY CORRECT-LOOKING, which is why the direction is argued here rather
    than assumed. Cycle DETECTION is direction-agnostic: every cycle test in this module's suite
    passes under either arrow, `_check` calls `topological_order` only for its raise, and the
    sole signal is `test_the_order_covers_every_row_or_there_is_no_order`. An implementer who
    "fixes" that test instead of this function locks an inverted synthesis order in under a
    green suite, and Plan I2's partitioned synthesis is its first consumer.

    A RELATION THIS DOES NOT NAME PRODUCES NO EDGE, which is only safe because `_check` refuses
    one before any sort runs. Read on its own this function answers `()` for a graph that
    declares an ordering, so the refusal there is what makes the silence here honest.

    `conflicts` IS NOT HERE. It is symmetric — `_check` refuses a one-sided one — and not an
    ordering; a "cycle" of conflicts is meaningless. Whether two conflicting rows may both be
    accepted is a coverage assertion, not a sort.
    """
    rows = _as_rows(rows)
    out = []
    for r in rows:
        for d in r.dependencies:
            if d.relation == "requires":
                out.append((d.id, r.id))
            elif d.relation == "blocks":
                out.append((r.id, d.id))
    return tuple(out)


def topological_order(rows) -> tuple:
    """Kahn's algorithm over `edges`, or a `LedgerError` naming the full cycle as a path.

    ITERATIVE, NEVER A RECURSIVE DFS. Catching `RecursionError` and reporting "no cycle" would
    give a deep chain and a cycle the same outcome.

    THE LENGTH CHECK IS THE POINT. Kahn's produces a PARTIAL order and stops early on a cycle;
    a caller that reads the emitted list without comparing its length to the row count gets a
    silently truncated ordering and synthesizes the partitions it happened to emit — with
    §12.2's "topological ordering is a precondition of partitioned synthesis" satisfied over a
    graph that is not the one written down.
    """
    rows = _as_rows(rows)
    ids = [r.id for r in rows]
    # A DUPLICATE ID IS NOT AN ORDERING QUESTION, IT IS A MALFORMED LEDGER — and this is the
    # same public-surface argument the dangling-edge refusal below makes, applied to the raise
    # beside it. `_check` refuses a duplicate before any WRITTEN ledger reaches here. Reached
    # directly, both outcomes are wrong and neither was a `LedgerError`: `[a, a]` returned an
    # order naming one row TWICE, and a duplicate carrying an edge left `len(order)` short of
    # `len(ids)` with NOTHING unemitted, so the cycle renderer's `sorted(remaining)[0]` raised
    # `IndexError` about a graph with no cycle in it. Refusing here is also what makes
    # `_render_cycle`'s `remaining` provably non-empty.
    dedup = set()
    for i in ids:
        if i in dedup:
            raise LedgerError(
                f"row id {i!r} appears twice, so these rows are not a graph to order: an "
                "order naming one row twice is not an ordering, and §10's ids are "
                "content-derived, so two rows under one id are two claims merged into one.")
        dedup.add(i)
    claims = {r.id: r.semantic_claim for r in rows}
    succ = {i: [] for i in ids}
    indeg = {i: 0 for i in ids}
    for a, b in edges(rows):
        # A DANGLING EDGE IS A `LedgerError`, NOT A `KeyError`. `_check` refuses one before any
        # written ledger can reach here, but this function is PUBLIC and §12.2's partitioned
        # synthesis is its caller, over rows that need never have been through a write. `succ[a]`
        # on an id no row carries raises `KeyError`, which escapes this module's declared class
        # and so is an error no caller of it knows to catch — the same argument `_decode` makes
        # for typing `union_diff_bytes` on the way in.
        for n in (a, b):
            if n not in indeg:
                raise LedgerError(
                    f"a dependency names {n!r}, which no row carries, so no ordering over "
                    "these rows can honour it")
        succ[a].append(b)
        indeg[b] += 1
    queue = sorted(i for i in ids if indeg[i] == 0)
    order = []
    while queue:
        n = queue.pop(0)
        order.append(n)
        for m in succ[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                queue.append(m)
        queue.sort()
    if len(order) != len(ids):
        raise LedgerError(
            "these claims form a dependency cycle, so no synthesis order exists: "
            + _render_cycle(succ, {i for i in ids if i not in set(order)}, claims))
    return tuple(order)


def _render_cycle(succ, remaining, claims) -> str:
    """One concrete cycle as `a1b2c3 (claim…) -> d4e5f6 (claim…) -> a1b2c3`.

    `succ` is `edges`' own adjacency, so the arrow reads "MUST BE SYNTHESIZED BEFORE" — not
    "requires". Reversing `edges` without re-reading this renders the path backwards while
    every cycle test stays green, which is the same blind spot argued there.

    WALKED BACKWARD ALONG PREDECESSORS, and a forward walk is wrong rather than merely
    different. `remaining` holds exactly the rows whose in-degree never reached zero, so every
    one of them has a predecessor that is ALSO remaining — while a remaining row need not have
    a remaining SUCCESSOR, because a row can be left unemitted purely by being BLOCKED behind
    a cycle it is not in. Walking forward from such a row dead-ends, and the dead end renders
    as a cycle over the wrong claims: measured on `a1 -> b1 -> a1` plus `a1 -> c1`, the forward
    walk started at `c1` (the lowest id) and printed `c1 -> c1`, naming a self-cycle on the one
    row in no cycle at all, while the two rows that ARE the cycle went unmentioned. A reader
    acting on that message deletes the wrong edge and the cycle survives. Backward the walk
    cannot dead-end, so the first repeat closes a cycle that is really there — and the prefix
    before that repeat is trimmed off, because the rows leading INTO a cycle are not in it.

    `remaining` IS NON-EMPTY, and that is a fact about the CALLER rather than about this
    function. Each id is emitted at most once, so `len(order) < len(ids)` leaves something
    over — unless `ids` repeats an id, which is the one way the count falls short with nothing
    unemitted at all. `topological_order` refuses a duplicate id before it sorts; without that
    refusal `sorted(remaining)[0]` raises `IndexError` here, and it did.
    """
    pred = {n: [] for n in remaining}
    for tail in remaining:
        for head in succ[tail]:
            # `head` is necessarily remaining too, so this cannot escape `pred`'s keys: a row
            # is emitted only once its in-degree reaches zero, which requires every predecessor
            # to have been emitted — and `tail` is a predecessor that was not.
            pred[head].append(tail)
    node, seen, walk = sorted(remaining)[0], set(), []
    while node not in seen:
        seen.add(node)
        walk.append(node)
        node = sorted(pred[node])[0]
    cycle = walk[walk.index(node):]
    cycle.reverse()          # collected against the arrows; rendered along them
    cycle.append(cycle[0])
    return " -> ".join(f"{n} ({claims.get(n, '')[:_CLAIM_CLIP]})" for n in cycle)


# Which structured fields each criterion kind REQUIRES. Anything not listed for a kind must be
# None: a criterion carrying a `node_id` under `kind="symbol"` is two evaluators' inputs in one
# record and nothing says which one was meant.
#
# `schema` IS IN §10.1'S MECHANICAL LIST — "test ID, schema query, exact symbol, file/hash
# invariant" — so it takes inputs like the other three: WHICH schema, and WHAT query. Naming
# only the query would leave the target to the sentence, which is the failure `symbol` is
# refused for; naming only the schema is a target with nothing asked of it.
_CRITERION_FIELDS = {
    "test": ("node_id",),
    "symbol": ("path", "symbol"),
    "hash": ("path", "sha256"),
    "schema": ("path", "query"),
    "prose": (),
}
_CRITERION_OPTIONAL = ("path", "symbol", "node_id", "sha256", "query")
# `prose` is the ONE kind with no predicate, and so the only one a human trace may hang on.
# That no schema evaluator exists here does not put `schema` back on this list: see `Criterion`.
_TRACEABLE = ("prose",)


def _check_criterion(c: Criterion, where: str) -> None:
    if c.kind not in CRITERION_KINDS:
        raise LedgerError(f"{where}: criterion kind is one of {list(CRITERION_KINDS)}, "
                          f"not {c.kind!r}")
    # EVERY OTHER FIELD IS TEXT OR ABSENT. The required-field loop below tests truthiness and
    # `7` is truthy, so an int `path` reached `_assert_contained` and raised `TypeError` out of
    # `os.fspath` — the same escape as the nested lists, one level further down.
    for name in ("text", "trace") + _CRITERION_OPTIONAL:
        v = getattr(c, name)
        if v is not None and not isinstance(v, str):
            raise LedgerError(f"{where}: {name} is a str, not {type(v).__name__}")
    required = _CRITERION_FIELDS[c.kind]
    for name in required:
        if not getattr(c, name):
            raise LedgerError(
                f"{where}: a {c.kind!r} criterion carries {list(required)} as STRUCTURED "
                f"fields and {name!r} is empty. §10.1's worked example is a symbol-presence "
                "check standing in for a behavioural claim while the property is false, so a "
                "criterion that is only a sentence with a symbol name in it is refused.")
    for name in _CRITERION_OPTIONAL:
        if name not in required and getattr(c, name) is not None:
            raise LedgerError(
                f"{where}: a {c.kind!r} criterion may not carry {name!r}; it is another "
                "evaluator's input and nothing would say which one was meant.")
    if c.trace is not None and c.kind not in _TRACEABLE:
        raise LedgerError(
            f"{where}: a trace may hang only on {list(_TRACEABLE)}, not on {c.kind!r}. On a "
            "mechanical kind the predicate is the evidence, and a human note beside it that "
            "could flip the method to `manual_trace_confirmed` is §10.1's manufactured green.")
    if c.path is not None:
        # A CRITERION PATH NAMES A FILE IN THE TREE THE LEDGER DESCRIBES, OR IT NAMES NOTHING
        # THIS ENGINE WILL LOOK AT. `coverage` joins it onto the candidate tree, and
        # `Path(tree) / "/etc/passwd"` IS `/etc/passwd` while `../../` walks straight out — so
        # an unchecked path turns a MECHANICAL check into a report about a host file the ledger
        # claims nothing about. Ledger rows are authored from three fallible seats' claims, so
        # this is in reach rather than theoretical. One spelling of the rule, imported: this is
        # `taskbundle._check_rel`'s guard, wrapped the same way, because `_assert_contained`
        # raises a `BundleError` this module's contract does not promise. Re-inlining the rule
        # instead is the divergence route on this project's open-defect list — `bundle._rediff`
        # took it with `harvest._literal`'s pathspec prefix. `coverage` guards again at the
        # join, because a `Ledger` built in-process never passed through here.
        try:
            bundlemod._assert_contained(c.path, f"{where}: a criterion path")
        except bundlemod.BundleError as e:
            raise LedgerError(str(e)) from e
    if not c.text:
        raise LedgerError(f"{where}: a criterion carries the human sentence it stands for")


def _check(l: Ledger) -> None:
    """Every structural refusal, at WRITE time, where the producer is present to fix it.

    A read-time refusal lands hours later on a resume with nobody able to say which round
    introduced the edge — `write_manifest`'s round-trip refusal makes the same argument.
    """
    if not isinstance(l, Ledger):
        raise LedgerError(f"a Ledger is required, not {type(l).__name__}")
    _check_rows(l.rows)
    if l.version != VERSION:
        raise LedgerError(f"this engine writes ledger version {VERSION}, not {l.version!r}")
    if l.degrade_threshold_bytes != DEGRADE_UNION_DIFF_BYTES:
        raise LedgerError(
            f"the recorded degradation threshold is {l.degrade_threshold_bytes} and this "
            f"engine applies {DEGRADE_UNION_DIFF_BYTES}; record the one that was applied")
    if (l.union_diff_bytes > l.degrade_threshold_bytes) != bool(l.degraded):
        raise LedgerError(
            f"this ledger measured a {l.union_diff_bytes}-byte union diff against a "
            f"{l.degrade_threshold_bytes}-byte threshold and records degraded={l.degraded}. "
            "§10 says a degraded ledger must say so — and it becomes the INPUT to from-scratch "
            "synthesis, so a ledger that hides its own degradation misdescribes the spec.")
    seen = {}
    for r in l.rows:
        if r.kind not in KINDS:
            raise LedgerError(f"row {r.id}: kind is one of {list(KINDS)}, not {r.kind!r}")
        if r.status not in STATUSES:
            raise LedgerError(f"row {r.id}: status is one of {list(STATUSES)}, "
                              f"not {r.status!r}")
        # THE CHECK THAT KEEPS THE VALUE HONEST. The id IS the hash; without this an editor
        # that changes the claim text and leaves the id is exactly the failure §10 names, and
        # it is invisible — the ledger stays well-formed while coverage keeps comparing a stale
        # identity under a stable-looking key.
        if r.id != row_id(r.requirement_id, r.semantic_claim):
            raise LedgerError(
                f"row {r.id!r} does not hash its own (requirement_id, semantic_claim). §10's "
                "ids are content-derived so coverage can compare across review rounds; a round "
                "that rephrases a claim inserts a NEW row and resolves the old one, never "
                "edits text under a stable key.")
        if r.id in seen:
            raise LedgerError(
                f"row id {r.id!r} appears twice. Merging would make one row where two claims "
                "were required, and coverage would report one covered.")
        seen[r.id] = r
        for e in r.seat_evidence:
            if e.stance not in STANCES:
                raise LedgerError(f"row {r.id}: stance is one of {list(STANCES)}, "
                                  f"not {e.stance!r}")
        for i, c in enumerate(r.acceptance_criteria):
            _check_criterion(c, f"row {r.id} criterion {i}")
    conflicts = set()
    for r in l.rows:
        for d in r.dependencies:
            if d.relation not in RELATIONS:
                raise LedgerError(f"row {r.id}: relation is one of {list(RELATIONS)}, "
                                  f"not {d.relation!r}")
            if d.id not in seen:
                raise LedgerError(
                    f"row {r.id} declares a {d.relation} on {d.id!r}, which no row carries. "
                    "Skipping it would make the sort succeed over a graph missing the "
                    "constraint, and §12.2's precondition would hold over the wrong graph.")
            if d.relation == "conflicts" and d.id == r.id:
                raise LedgerError(f"row {r.id} conflicts with itself, which is not a claim")
            if d.relation == "conflicts":
                conflicts.add((r.id, d.id))
    # SYMMETRY IS A PROPERTY OF THE RELATION, SO IT HAS TO BE IN THE RECORD. §10 makes
    # `conflicts` symmetric, and this file stores dependencies PER ROW — so a one-sided
    # declaration leaves the partner row reading as unconstrained to anything that meets it
    # alone, which §12.2's partitioned synthesis does by construction. Refused at write, where
    # the producer is present, for the reason the module docstring gives; it is the same
    # malformed-relation class as a self-conflict, one branch over.
    for a, b in sorted(conflicts):
        if (b, a) not in conflicts:
            raise LedgerError(
                f"row {a} declares a conflicts on {b} and {b} does not declare it back. §10's "
                "`conflicts` is symmetric, and a reader holding only the silent row — which "
                "is every partition that does not contain both — would read an unconstrained "
                "claim.")
    topological_order(l.rows)


def _crit_row(c: Criterion) -> dict:
    return dataclasses.asdict(c)


def _row_row(r: Row) -> dict:
    d = dataclasses.asdict(r)
    d["dependencies"] = [dataclasses.asdict(x) for x in r.dependencies]
    d["seat_evidence"] = [dataclasses.asdict(x) for x in r.seat_evidence]
    d["acceptance_criteria"] = [_crit_row(x) for x in r.acceptance_criteria]
    return d


def _payload(l: Ledger) -> dict:
    # `ledger_hash` reaches here WITHOUT `_check` — §14.1 hashes a ledger it already holds —
    # and `dataclasses.asdict` answers `TypeError` for anything that is not a dataclass. The
    # shape check is in the one function that serializes, so both callers get it.
    _check_rows(l.rows)
    return {"version": l.version, "union_diff_bytes": l.union_diff_bytes,
            "degrade_threshold_bytes": l.degrade_threshold_bytes,
            "degraded": bool(l.degraded), "rows": [_row_row(r) for r in l.rows]}


def _sub(cls, row, where):
    names = [f.name for f in fields(cls)]
    if not isinstance(row, dict):
        raise LedgerError(f"{where}: expected an object, not {type(row).__name__}")
    missing = [n for n in names if n not in row]
    unknown = sorted(set(row) - set(names))
    if missing:
        raise LedgerError(f"{where} is missing {missing}")
    if unknown:
        raise LedgerError(f"{where} carries fields this engine does not know: {unknown}")
    return cls(**{n: row[n] for n in names})


def _decode(raw, source) -> Ledger:
    """Missing refused, unknown refused, vocabulary refused — `runstate._decode`'s precedent.

    `rows` IS NEITHER DEFAULTED NOR ALLOWED TO BE EMPTY, and the second half is the one that
    was missing. An absent `rows` raises through the `missing` list; a PRESENT-but-empty one
    used to sail through — `for i, r in enumerate([])` yields nothing, `_check` counts no rows,
    `topological_order(())` returns `()` cleanly, and `read_ledger` handed back a `Ledger` over
    which `coverage.check` produces zero results, zero `unsatisfied`, zero `unresolved` and zero
    contradictions: a run reported as fully covered having checked nothing. That is §10.1's own
    failure shape one level up. `taskbundle._decode` refuses the equivalent `entries` case and
    this is the same refusal, spelled the same way. It is also what stops `write_ledger`
    publishing an empty ledger, since every check in `_check` passes over no rows at all.

    THE THREE NESTED LISTS ARE ONLY SUBSTITUTED WHEN THEY ARE PRESENT. Assigning them into
    `body` unconditionally — `r.get("dependencies", [])` — makes `_sub`'s missing-field check
    structurally unreachable for exactly the three fields whose absence is most dangerous:
    a row with no `acceptance_criteria` decodes as "no criteria" and `coverage.check` produces
    zero results for it, so the row reports as fully covered with nothing checked; a row with no
    `seat_evidence` makes `_contradictions`' unanimous-rejection finding unreachable
    (`len(...) < 2` -> `continue`), dropping §10's "most valuable signal in the run" on a missing
    key rather than on a measurement; and a row with no `dependencies` is an unconstrained node
    the cycle check then passes over — precisely what
    `test_a_dangling_dependency_is_refused_not_skipped` forbids on the other route. Absent stays
    absent here, so `_sub` refuses it.
    """
    if not isinstance(raw, dict):
        raise LedgerError(f"{source}: a ledger is an object, not {type(raw).__name__}")
    names = [f.name for f in fields(Ledger)]
    missing = [n for n in names if n not in raw]
    unknown = sorted(set(raw) - set(names))
    if missing:
        raise LedgerError(f"{source} is missing {missing}")
    if unknown:
        raise LedgerError(f"{source} carries fields this engine does not know: {unknown}")
    # Typed on the way in, so a string `union_diff_bytes` becomes a LedgerError rather than a
    # TypeError out of `_check`'s `>` comparison — an error escaping this module's declared
    # class is one no caller of it knows to catch. `taskbundle._decode` type-checks its ints.
    # `bool` is excluded because `isinstance(True, int)` is True and `True > 524288` is a legal
    # comparison answering False, so a `true` byte count would record a measurement nobody took.
    for n in ("version", "union_diff_bytes", "degrade_threshold_bytes"):
        if not isinstance(raw[n], int) or isinstance(raw[n], bool):
            raise LedgerError(f"{source}: {n} is an int, not {raw[n]!r}")
    if not isinstance(raw["degraded"], bool):
        raise LedgerError(
            f"{source}: degraded is a bool, not {raw['degraded']!r}. `bool(\"false\")` is True, "
            "so a string here would read a degraded ledger's own denial as an admission.")
    if not isinstance(raw["rows"], list) or not raw["rows"]:
        raise LedgerError(
            f"{source}: rows is a non-empty list. An empty one reads as a run with no claims, "
            "which the coverage check reports as fully covered having checked nothing.")
    rows = []
    for i, r in enumerate(raw["rows"]):
        where = f"{source}: row {i}"
        if not isinstance(r, dict):
            raise LedgerError(f"{where}: expected an object, not {type(r).__name__}")
        body = dict(r)
        for key, cls in _ROW_LISTS:
            if key not in r:
                continue          # left ABSENT so `_sub`'s missing-field check can refuse it
            # A JSON object here is ITERABLE — the comprehension below would walk its keys and
            # refuse them one level too deep, as "expected an object, not str".
            if not isinstance(r[key], list):
                raise LedgerError(
                    f"{where}: {key} is a list, not {type(r[key]).__name__}")
            body[key] = tuple(_sub(cls, x, f"{where} {key} {j}")
                              for j, x in enumerate(r[key]))
        rows.append(_sub(Row, body, where))
    l = Ledger(raw["version"], tuple(rows), raw["union_diff_bytes"],
               raw["degrade_threshold_bytes"], raw["degraded"])
    _check(l)
    return l


def ledger_hash(l: Ledger) -> str:
    """The value §14.1 embeds in every checkpoint commit message. A HASH, NEVER A SUMMARY.

    Reviewers run `git log` in the synthesis checkout by design (§13), so a message carrying row
    text would be the ledger handed to a blind reviewer through git. A hash is not content; a
    line quoting a claim is. The whole payload is hashed — including `degraded`, because a
    degraded ledger IS a different spec.

    A NON-`Ledger` IS REFUSED HERE AND NOT ONLY IN `_check`. This function does not run the
    structural checks — §14.1 calls it on a value it already holds — so a dict handed over
    raised `AttributeError` on `l.version`, which is the error class argument `_check`'s own
    first line makes.
    """
    if not isinstance(l, Ledger):
        raise LedgerError(f"a Ledger is required, not {type(l).__name__}")
    return hashlib.sha256(
        json.dumps(_payload(l), sort_keys=True).encode()).hexdigest()


def write_ledger(run_dir, l: Ledger) -> None:
    """Publish the ledger, refusing anything structurally dishonest first.

    `atomic_write` is the LAST statement, and every refusal above it is one that leaves the
    previous round's ledger in place. §13's loop rewrites this file every review round, so a
    round handing over a cyclic or stale-id ledger must not also destroy the readable one.
    """
    _check(l)
    path = storage.ledger_path(run_dir)
    try:
        blob = json.dumps(_payload(l), sort_keys=True, indent=2,
                          allow_nan=False).encode("utf-8") + b"\n"
    except (TypeError, ValueError) as e:
        raise LedgerError(f"this ledger carries a value json cannot serialize: {e}") from e
    restored = _decode(json.loads(blob), path)
    if restored != l:
        differing = [f.name for f in fields(Ledger)
                     if getattr(restored, f.name) != getattr(l, f.name)]
        raise LedgerError(
            f"this ledger does not survive its own round trip; {differing} come back as a "
            "different type. JSON has one sequence type: pass the declared tuples as tuples.")
    storage.atomic_write(path, blob)


def read_ledger(run_dir) -> Ledger:
    """What is currently claimed.

    Raises if absent AND if present-but-empty — never an empty ledger by either route. Both
    refusals are `_decode`'s argument: a ledger with no rows is a run the coverage check
    reports as fully covered having checked nothing.
    """
    path = storage.ledger_path(run_dir)
    try:
        raw = path.read_bytes()
    except FileNotFoundError as e:
        raise LedgerError(
            f"{path} does not exist: this run has recorded no claims. An empty ledger is a run "
            "with no claims, which the coverage check reports as fully covered.") from e
    try:
        return _decode(json.loads(raw), path)
    except ValueError as e:
        raise LedgerError(f"{path} is not readable as JSON: {e}") from e
