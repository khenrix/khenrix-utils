"""§10: the claim ledger — what is currently claimed, with an identity that cannot drift."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

import json  # noqa: E402
import re  # noqa: E402
import pytest  # noqa: E402
from forge import ledger, storage  # noqa: E402


def _crit(**kw):
    base = dict(kind="prose", text="the thing works", path=None, symbol=None,
                node_id=None, sha256=None, query=None, trace=None)
    base.update(kw)
    return ledger.Criterion(**base)


def _row(requirement_id="R1", claim="records carry a monotonic seq", **kw):
    base = dict(
        id=ledger.row_id(requirement_id, claim),
        requirement_id=requirement_id,
        requirement_span="spec.md:10-12",
        requirement_sha256="0" * 64,
        kind="behavior", component="core", semantic_claim=claim, status="accepted",
        dependencies=(), seat_evidence=(), counterevidence="",
        acceptance_criteria=(_crit(),), synthesis_evidence=None,
        verification_receipt=None, risk="low", rationale="")
    base.update(kw)
    if "id" not in kw:
        base["id"] = ledger.row_id(base["requirement_id"], base["semantic_claim"])
    return ledger.Row(**base)


def _led(rows, **kw):
    base = dict(version=ledger.VERSION, rows=tuple(rows), union_diff_bytes=100,
                degrade_threshold_bytes=ledger.DEGRADE_UNION_DIFF_BYTES, degraded=False)
    base.update(kw)
    return ledger.Ledger(**base)


def test_the_id_frames_its_fields_so_a_split_claim_cannot_collide():
    """§10's own reason for content-derived ids is that a round SPLITS a claim — which is
    exactly the operation that manufactures a `||` collision."""
    assert ledger.row_id("ab", "c") != ledger.row_id("a", "bc")


def test_the_id_is_not_normalized_because_an_edited_claim_is_a_new_claim():
    assert ledger.row_id("R1", "x") != ledger.row_id("R1", " x")
    assert ledger.row_id("R1", "X") != ledger.row_id("R1", "x")


def test_the_id_is_the_twelve_hex_characters_10_asks_for():
    """§10 writes `sha256(...)[:12]`, and the width is a collision budget, not a formatting
    choice — 48 bits over the claims of one run. Nothing else in this suite could tell 12
    from 11: every id is produced BY this function, so a narrower one stays self-consistent
    and `_check`'s equality still holds. Measured — `[:11]` SURVIVED without this."""
    i = ledger.row_id("R1", "alpha")
    assert len(i) == 12
    assert set(i) <= set("0123456789abcdef")


def test_a_row_whose_id_does_not_hash_its_own_claim_is_refused(tmp_path):
    """The invariant lives in the VALUE — the id IS the hash — and this check is what keeps
    the value honest. Without it, an edit that leaves the id stale is invisible."""
    r = _row()
    stale = ledger.Row(**{**r.__dict__, "semantic_claim": "something else"})
    with pytest.raises(ledger.LedgerError, match="does not hash its own"):
        ledger.write_ledger(tmp_path, _led([stale]))


def test_two_rows_with_one_id_and_different_claims_are_refused(tmp_path):
    a = _row("R1", "alpha")
    b = ledger.Row(**{**_row("R2", "beta").__dict__, "id": a.id})
    with pytest.raises(ledger.LedgerError, match="does not hash its own"):
        ledger.write_ledger(tmp_path, _led([a, b]))


def test_a_duplicated_row_id_is_refused_rather_than_merged(tmp_path):
    """MATCHED ON `_check`'S OWN SENTENCE, not on "twice". `topological_order` refuses a
    duplicate id as well, and `_check` calls it — so a bare "twice" passes on either refusal
    and deleting the write-time one leaves this green. Measured, it did. The two say different
    things and this is the one that names the ledger consequence."""
    a = _row("R1", "alpha")
    with pytest.raises(ledger.LedgerError, match="Merging would make one row"):
        ledger.write_ledger(tmp_path, _led([a, a]))


def test_a_cycle_written_with_blocks_is_caught(tmp_path):
    """The defect this is written against: `requires` and `blocks` are ONE edge with two
    names, and a checker walking `requires` alone passes a cyclic graph written with
    `blocks`."""
    a = _row("R1", "alpha")
    b = _row("R2", "beta")
    a2 = ledger.Row(**{**a.__dict__,
                       "dependencies": (ledger.Dependency(b.id, "blocks"),)})
    b2 = ledger.Row(**{**b.__dict__,
                       "dependencies": (ledger.Dependency(a.id, "blocks"),)})
    with pytest.raises(ledger.LedgerError, match="cycle"):
        ledger.write_ledger(tmp_path, _led([a2, b2]))


def test_a_cycle_written_with_requires_is_caught(tmp_path):
    a, b = _row("R1", "alpha"), _row("R2", "beta")
    a2 = ledger.Row(**{**a.__dict__, "dependencies": (ledger.Dependency(b.id, "requires"),)})
    b2 = ledger.Row(**{**b.__dict__, "dependencies": (ledger.Dependency(a.id, "requires"),)})
    with pytest.raises(ledger.LedgerError, match="cycle"):
        ledger.write_ledger(tmp_path, _led([a2, b2]))


def test_a_cycle_mixing_the_two_spellings_is_caught(tmp_path):
    """Genuinely mixed, unlike its earlier draft, which used `requires` on both rows and so
    tested nothing the test above did not: one row that both REQUIRES and BLOCKS the same
    partner is a two-node cycle written in both vocabularies at once, and a checker walking
    either relation name alone sees only half of it."""
    a, b = _row("R1", "alpha"), _row("R2", "beta")
    a2 = ledger.Row(**{**a.__dict__,
                       "dependencies": (ledger.Dependency(b.id, "requires"),
                                        ledger.Dependency(b.id, "blocks"))})
    with pytest.raises(ledger.LedgerError, match="cycle"):
        ledger.write_ledger(tmp_path, _led([a2, b]))


def test_the_cycle_refusal_names_the_path_and_clips_the_claims(tmp_path):
    """A human reading this refusal cannot act on twelve hex characters — and a whole claim
    per node makes the message unreadable at four, so each quote is CLIPPED.

    THE CLIP IS EXERCISED HERE, NOT ONLY NAMED. An earlier draft asserted over two
    eleven-character claims, so the message was byte-identical with `_CLAIM_CLIP` removed and
    the test's own NAME was the only place the clipping existed. Measured — that mutation
    survived the suite.

    The path must also CLOSE. `_render_cycle` walks to the first repeat and appends it, which
    is what makes the message read as a cycle rather than a chain that happens to stop;
    dropping that append leaves both ids present and every other assertion here green."""
    long_claim = "beta claim " + "b" * 200
    a, b = _row("R1", "alpha claim"), _row("R2", long_claim)
    a2 = ledger.Row(**{**a.__dict__, "dependencies": (ledger.Dependency(b.id, "requires"),)})
    b2 = ledger.Row(**{**b.__dict__, "dependencies": (ledger.Dependency(a.id, "requires"),)})
    with pytest.raises(ledger.LedgerError) as e:
        ledger.write_ledger(tmp_path, _led([a2, b2]))
    msg = str(e.value)
    assert a.id in msg and b.id in msg
    assert "alpha claim" in msg
    assert long_claim[:ledger._CLAIM_CLIP] in msg
    assert long_claim not in msg
    # Two nodes rendered as a closed path is three slots, so one id appears twice and the
    # other once. Which one is whichever the backward walk lands on, so the counts are
    # compared as a sorted pair rather than pinned to a particular row.
    assert sorted((msg.count(a.id), msg.count(b.id))) == [1, 2], \
        "the rendered path closes the cycle it names"


def test_the_refusal_names_the_rows_in_the_cycle_not_a_row_merely_blocked_by_it():
    """THE DRAFT NAMED THE WRONG CLAIMS. `a1` and `b1` require each other; `c1` requires `a1`
    and is in no cycle — it is only stuck behind one. Kahn's leaves all three unemitted, so a
    render that walks FORWARD from the lowest remaining id starts at `c1`, finds no remaining
    successor, dead-ends, and prints `c1 -> c1`: a self-cycle asserted about the one row that
    has none, with the two rows that ARE the cycle unmentioned. Measured, exactly that.

    The claim strings are chosen so `c1`'s id sorts FIRST — the id is a hash, so a fixture
    that did not pin the order would exercise the dead end only by luck.
    """
    a, b, c = _row("R1", "a1"), _row("R1", "b1"), _row("R1", "c1")
    assert c.id < a.id and c.id < b.id, "the fixture must start the walk at the blocked row"
    a2 = ledger.Row(**{**a.__dict__, "dependencies": (ledger.Dependency(b.id, "requires"),)})
    b2 = ledger.Row(**{**b.__dict__, "dependencies": (ledger.Dependency(a.id, "requires"),)})
    c2 = ledger.Row(**{**c.__dict__, "dependencies": (ledger.Dependency(a.id, "requires"),)})
    with pytest.raises(ledger.LedgerError) as e:
        ledger.topological_order([a2, b2, c2])
    msg = str(e.value)
    assert a.id in msg and b.id in msg
    assert c.id not in msg, "a row merely blocked by a cycle is not in it"


def test_a_cycle_among_rows_that_mostly_sort_fine_names_only_the_cycle():
    """THE SHAPE A REAL LEDGER HAS, and the one every other cycle fixture here lacks: rows
    that order cleanly AND a cycle. In a graph where nothing sorts, the emitted set is empty
    and any confusion between "every row" and "the rows left over" is invisible — so a
    predecessor map built over the whole graph rather than over the leftovers behaves
    identically in all of them. Here `root2` is emitted before the cycle is even reached, and
    a walk that can step back onto it leaves the remainder entirely.

    `root2`'s id is pinned to sort FIRST so a wrong predecessor set would actually be
    followed; the ids are hashes, so an unchosen fixture would exercise this only by luck."""
    root, a, b = _row("R1", "root2"), _row("R1", "cyc-a2"), _row("R1", "cyc-b2")
    assert root.id < a.id and root.id < b.id
    a2 = ledger.Row(**{**a.__dict__,
                       "dependencies": (ledger.Dependency(b.id, "requires"),
                                        ledger.Dependency(root.id, "requires"))})
    b2 = ledger.Row(**{**b.__dict__, "dependencies": (ledger.Dependency(a.id, "requires"),)})
    with pytest.raises(ledger.LedgerError) as e:
        ledger.topological_order([root, a2, b2])
    msg = str(e.value)
    assert a.id in msg and b.id in msg
    assert root.id not in msg, "a row that ordered fine is not part of the cycle"


def test_the_rendered_cycle_runs_along_the_arrows_not_against_them():
    """AT TWO NODES THE TWO READINGS ARE THE SAME PATH — both arrows are edges — so every
    other cycle test in this file is blind to a path collected backward and never turned
    round. At three they are different paths and only one is made of edges that exist.
    `_render_cycle`'s arrow means "must be synthesized before", which is exactly what `edges`
    emits, so every consecutive pair in the rendered path must be one of them."""
    x, y, z = _row("R1", "x"), _row("R2", "y"), _row("R3", "z")
    x2 = ledger.Row(**{**x.__dict__, "dependencies": (ledger.Dependency(z.id, "requires"),)})
    y2 = ledger.Row(**{**y.__dict__, "dependencies": (ledger.Dependency(x.id, "requires"),)})
    z2 = ledger.Row(**{**z.__dict__, "dependencies": (ledger.Dependency(y.id, "requires"),)})
    rows = [x2, y2, z2]
    with pytest.raises(ledger.LedgerError) as e:
        ledger.topological_order(rows)
    path = re.findall(r"\b[0-9a-f]{12}\b", str(e.value))
    assert len(path) == 4, path
    real = set(ledger.edges(rows))
    assert all(p in real for p in zip(path, path[1:])), (path, sorted(real))


def test_a_self_edge_is_a_one_node_cycle(tmp_path):
    a = _row("R1", "alpha")
    a2 = ledger.Row(**{**a.__dict__, "dependencies": (ledger.Dependency(a.id, "requires"),)})
    with pytest.raises(ledger.LedgerError, match="cycle"):
        ledger.write_ledger(tmp_path, _led([a2]))


def test_a_dangling_dependency_is_refused_not_skipped(tmp_path):
    """Skipping it makes the sort succeed over a graph MISSING the constraint, and §12.2's
    'topological ordering is a precondition of partitioned synthesis' would then be
    satisfied over the wrong graph."""
    a = _row("R1", "alpha")
    a2 = ledger.Row(**{**a.__dict__,
                       "dependencies": (ledger.Dependency("ffffffffffff", "requires"),)})
    with pytest.raises(ledger.LedgerError, match="no row carries"):
        ledger.write_ledger(tmp_path, _led([a2]))


def test_a_dangling_conflict_is_refused_where_the_sort_could_never_see_it(tmp_path):
    """`conflicts` produces NO edge, so the guard inside `topological_order` — which exists for
    the two ORDERING relations — never meets it, and `_check`'s is the only one that can. This
    is the half `test_a_dangling_dependency_is_refused_not_skipped` cannot reach: deleting
    `_check`'s dangling guard leaves that test green, because the sort's own guard refuses the
    `requires` case anyway. Measured — that mutation SURVIVED the suite without this.

    It is also the shape §10 makes likely rather than exotic: a round that resolved the partner
    claim by deleting the row instead of recording the resolution leaves exactly this behind,
    and coverage would then be comparing a conflict against nothing.

    THE MATCH NAMES THE DANGLING REFUSAL AND NOT MERELY THE RELATION. The symmetry check below
    also refuses this row — a partner that does not exist declares nothing back — and its
    message opens with the same "declares a conflicts on". So `match="declares a conflicts on"`
    left this test green with the dangling guard deleted. Measured. The full phrase is what
    distinguishes "the partner is missing" from "the partner stayed silent"."""
    a = _row("R1", "alpha")
    a2 = ledger.Row(**{**a.__dict__,
                       "dependencies": (ledger.Dependency("ffffffffffff", "conflicts"),)})
    with pytest.raises(ledger.LedgerError,
                       match="declares a conflicts on .*which no row carries"):
        ledger.write_ledger(tmp_path, _led([a2]))


def test_a_dangling_dependency_reaching_the_sort_directly_is_a_ledger_error(tmp_path):
    """`topological_order` is PUBLIC, and §12.2's partitioned synthesis is its caller — so
    it is reachable with rows that never passed `_check`. Indexing `succ` with an id no row
    carries raises `KeyError`, which is an error escaping this module's declared class and
    so one no caller of it knows to catch. Same argument as `union_diff_bytes` below."""
    a = _row("R1", "alpha")
    a2 = ledger.Row(**{**a.__dict__,
                       "dependencies": (ledger.Dependency("ffffffffffff", "requires"),)})
    with pytest.raises(ledger.LedgerError, match="no row carries"):
        ledger.topological_order([a2])


def test_a_duplicate_row_id_reaching_the_sort_directly_is_a_ledger_error_too():
    """THE SAME PUBLIC-SURFACE ARGUMENT AS THE TEST ABOVE, applied to the raise beside it.
    `_check` refuses a duplicate id before any WRITTEN ledger reaches the sort, and §12.2's
    partitioned synthesis calls the sort directly over rows that never went through it. Both
    outcomes are wrong and neither is a `LedgerError`:

    `[a, a]` returned `(a.id, a.id)` — an ORDER NAMING ONE ROW TWICE, with no refusal, which
    §12.2 would then partition synthesis by. And `[b2, b2, a]`, where `b2` carries an edge,
    left `len(order) != len(ids)` with NOTHING unemitted, so `_render_cycle`'s
    `sorted(remaining)[0]` raised `IndexError` — an error escaping this module's declared
    class, reporting a cycle on a graph that has none. Measured, both.

    A duplicate id is not an ordering question; it is a malformed ledger."""
    a, b = _row("R1", "alpha"), _row("R2", "beta")
    with pytest.raises(ledger.LedgerError, match="appears twice"):
        ledger.topological_order([a, a])
    b2 = ledger.Row(**{**b.__dict__, "dependencies": (ledger.Dependency(a.id, "requires"),)})
    with pytest.raises(ledger.LedgerError, match="appears twice"):
        ledger.topological_order([b2, b2, a])


def test_a_generator_of_rows_is_not_ordered_over_the_empty_graph_it_leaves_behind():
    """`topological_order` reads `rows` THREE TIMES — the ids, the claims, and `edges` — so a
    generator is exhausted by the first pass and the last two see nothing. Every edge is
    dropped, every in-degree is zero, the length check passes, and the answer is a clean,
    fully-covering, SORTED order over a graph that declared constraints. Measured: the pair
    below came back in exactly the wrong order, with no refusal.

    The fixture pins `a2 requires b` with `a.id < b.id`, so the correct order is DESCENDING
    and the silent-drop answer is distinguishable from it. Materializing once is the fix; a
    generator is now ordered like the sequence it stands for."""
    a, b = _row("R1", "alpha"), _row("R2", "beta")
    assert a.id < b.id, "the correct order must differ from the sorted one"
    a2 = ledger.Row(**{**a.__dict__, "dependencies": (ledger.Dependency(b.id, "requires"),)})
    assert ledger.topological_order([a2, b]) == (b.id, a2.id)
    assert ledger.topological_order(x for x in (a2, b)) == (b.id, a2.id)


def test_the_rest_of_the_public_surface_refuses_in_this_modules_own_error_class():
    """THE SWEEP THE DANGLING-EDGE FIX DID NOT DO. `edges`, `topological_order`, `ledger_hash`
    and `row_id` are all public, all reachable without `_check`, and each one raised something
    outside `LedgerError` on an off-contract argument — an error no caller of this module
    knows to catch. Measured before the fix: `edges([{...}])` and `ledger_hash({...})` raised
    `AttributeError`, `row_id(object(), "x")` raised `TypeError` out of `json.dumps`.

    `row_id` is the one that could also fail SILENTLY: `json.dumps` serializes an int happily,
    so a non-str `requirement_id` used to hash cleanly into an id nothing would question."""
    with pytest.raises(ledger.LedgerError, match="a ledger row is a Row"):
        ledger.edges([{"id": "ffffffffffff"}])
    with pytest.raises(ledger.LedgerError, match="a ledger row is a Row"):
        ledger.topological_order(["not a row"])
    with pytest.raises(ledger.LedgerError, match="rows is a sequence"):
        ledger.edges(7)
    with pytest.raises(ledger.LedgerError, match="a Ledger is required"):
        ledger.ledger_hash({"version": 1, "rows": []})
    with pytest.raises(ledger.LedgerError, match="is a str"):
        ledger.row_id(object(), "alpha")
    with pytest.raises(ledger.LedgerError, match="is a str"):
        ledger.row_id("R1", 7)


@pytest.mark.parametrize("key,bad", [("dependencies", "x"), ("seat_evidence", {"a": 1}),
                                     ("acceptance_criteria", 7)])
def test_a_nested_list_holding_the_wrong_type_is_refused_on_the_write_side_too(tmp_path,
                                                                              key, bad):
    """THE SAME SWEEP AS THE TEST ABOVE, ONE BRANCH OVER, and it is the ORDINARY route rather
    than the exotic one. `_decode` builds these elements with `_sub`, so a ledger read from
    disk cannot hold the wrong type — but a `Ledger` built IN PROCESS never passes through the
    decoder, and §12.2's synthesis builds rows. `_check` typed the ROW and stopped there, so
    the element's real type was discovered by whoever first read a field off it. Measured, four
    ways out of this module's declared class: `d.relation` in `edges` and `topological_order`,
    `e.stance` and `c.kind` in `_check` (all `AttributeError`), and `dataclasses.asdict` in
    `ledger_hash` (`TypeError`)."""
    r = _row(**{key: (bad,)})
    with pytest.raises(ledger.LedgerError, match=f"{key}\\[0\\] is a"):
        ledger.write_ledger(tmp_path, _led([r]))
    with pytest.raises(ledger.LedgerError, match=f"{key}\\[0\\] is a"):
        ledger.ledger_hash(_led([r]))
    with pytest.raises(ledger.LedgerError, match=f"{key}\\[0\\] is a"):
        ledger.edges([r])
    with pytest.raises(ledger.LedgerError, match="is a sequence of"):
        ledger.write_ledger(tmp_path, _led([_row(**{key: None})]))
    not_a_sequence = ledger.Ledger(
        version=ledger.VERSION, rows=7, union_diff_bytes=100,
        degrade_threshold_bytes=ledger.DEGRADE_UNION_DIFF_BYTES, degraded=False)
    with pytest.raises(ledger.LedgerError, match="rows are a sequence"):
        ledger.write_ledger(tmp_path, not_a_sequence)


def test_a_criterion_field_that_is_not_text_is_refused_before_a_path_is_joined(tmp_path):
    """The required-field loop tests TRUTHINESS, and `7` is truthy — so an int `path` went
    straight through it into `bundle._assert_contained`, which raised `TypeError` out of
    `os.fspath`. That is the nested-list escape one level further down, on the one field this
    module hands to another module."""
    bad = _row(acceptance_criteria=(_crit(kind="hash", text="p is unchanged", path=7,
                                          sha256="0" * 64),))
    with pytest.raises(ledger.LedgerError, match="path is a str, not int"):
        ledger.write_ledger(tmp_path, _led([bad]))
    numeric = _row(acceptance_criteria=(_crit(kind="prose", text=7),))
    with pytest.raises(ledger.LedgerError, match="text is a str, not int"):
        ledger.write_ledger(tmp_path, _led([numeric]))


def test_conflicts_is_symmetric_and_not_an_ordering_edge(tmp_path):
    """A 'cycle' of conflicts is meaningless; two rows may conflict mutually and still be
    a legal ledger. Whether they may both be ACCEPTED is a coverage question, not a write
    refusal.

    THE SYMMETRY HALF USED TO BE THE TEST'S NAME AND NOTHING ELSE. Both fixtures below
    declared the relation, so the test passed whether or not anything enforced it — measured,
    a ONE-SIDED `conflicts` wrote and read back clean, and a passing test named for an
    unimplemented property is worse than no test because it retires the question. §12.2
    partitions synthesis by the topological order, so a partition holding only the row that
    stayed silent reads an unconstrained claim: the record has to carry the relation on both
    rows or it is not the symmetric relation §10 describes."""
    a, b = _row("R1", "alpha"), _row("R2", "beta")
    a2 = ledger.Row(**{**a.__dict__, "dependencies": (ledger.Dependency(b.id, "conflicts"),)})
    b2 = ledger.Row(**{**b.__dict__, "dependencies": (ledger.Dependency(a.id, "conflicts"),)})
    ledger.write_ledger(tmp_path, _led([a2, b2]))
    assert ledger.read_ledger(tmp_path).rows[0].id == a.id
    assert ledger.edges([a2, b2]) == ()
    with pytest.raises(ledger.LedgerError, match="does not declare it back"):
        ledger.write_ledger(tmp_path, _led([a2, b]))
    with pytest.raises(ledger.LedgerError, match="does not declare it back"):
        ledger.write_ledger(tmp_path, _led([a, b2]))
    assert ledger.read_ledger(tmp_path).rows[0].id == a.id, \
        "a refused round leaves the previous ledger readable"


def test_a_self_conflict_is_refused_as_nonsense(tmp_path):
    a = _row("R1", "alpha")
    a2 = ledger.Row(**{**a.__dict__, "dependencies": (ledger.Dependency(a.id, "conflicts"),)})
    with pytest.raises(ledger.LedgerError, match="conflicts with itself"):
        ledger.write_ledger(tmp_path, _led([a2]))


def test_the_order_covers_every_row_or_there_is_no_order():
    """Kahn's algorithm stops early on a cycle; a caller reading the emitted list without
    comparing its length to the row count synthesizes the partitions it happened to emit.

    THIS IS ALSO THE ONLY TEST THAT PINS THE EDGE DIRECTION. Every cycle test above passes
    with `edges` reversed, so if this one fails the fix is in `edges`, NEVER here: §12.2 makes
    the ordering a precondition of partitioned synthesis, and a required claim cannot be
    synthesized after the claim requiring it."""
    a, b = _row("R1", "alpha"), _row("R2", "beta")
    a2 = ledger.Row(**{**a.__dict__, "dependencies": (ledger.Dependency(b.id, "requires"),)})
    assert ledger.edges([a2, b]) == ((b.id, a2.id),), \
        "an edge runs dependency -> dependent; `a2 requires b` is `b -> a2`"
    order = ledger.topological_order([a2, b])
    assert order == (b.id, a2.id)
    b2 = ledger.Row(**{**b.__dict__, "dependencies": (ledger.Dependency(a.id, "requires"),)})
    with pytest.raises(ledger.LedgerError, match="cycle"):
        ledger.topological_order([a2, b2])


def test_the_order_is_a_function_of_the_graph_not_of_the_row_order_in_the_file():
    """§12.2 partitions synthesis by this order, and §13's loop rewrites the ledger every
    round — so a round that appended two independent rows the other way round must not
    repartition the synthesis. `edges` builds each node's successor list in ROW order, so
    without the queue re-sort the tie between two simultaneously-ready rows breaks on however
    the file happened to list them."""
    a = _row("R1", "root")
    b = _row("R2", "left")
    c = _row("R3", "right")
    b2 = ledger.Row(**{**b.__dict__, "dependencies": (ledger.Dependency(a.id, "requires"),)})
    c2 = ledger.Row(**{**c.__dict__, "dependencies": (ledger.Dependency(a.id, "requires"),)})
    assert ledger.topological_order([a, b2, c2]) == ledger.topological_order([a, c2, b2])


def test_blocks_and_requires_produce_the_same_edge_from_opposite_rows():
    """§10's two names for one constraint, pinned as one edge rather than argued in prose:
    "A blocks B" written on A and "B requires A" written on B must be indistinguishable."""
    a, b = _row("R1", "alpha"), _row("R2", "beta")
    from_a = ledger.edges([ledger.Row(**{**a.__dict__,
                                         "dependencies": (ledger.Dependency(b.id, "blocks"),)}),
                           b])
    from_b = ledger.edges([a,
                           ledger.Row(**{**b.__dict__,
                                         "dependencies": (ledger.Dependency(a.id, "requires"),)})])
    assert from_a == from_b == ((a.id, b.id),)


def test_a_relation_the_vocabulary_does_not_name_is_refused(tmp_path):
    a = _row("R1", "alpha")
    a2 = ledger.Row(**{**a.__dict__, "dependencies": (ledger.Dependency(a.id, "supersedes"),)})
    with pytest.raises(ledger.LedgerError, match="relation"):
        ledger.write_ledger(tmp_path, _led([a2]))


def test_a_relation_the_vocabulary_does_not_name_is_not_silently_dropped_from_the_graph():
    """`edges` matches `requires` and `blocks` by name and IGNORES anything else, so an
    unknown relation is an ordering constraint that vanishes. `_check` refuses it before the
    sort ever runs — this pins that the refusal, not the silent drop, is what a caller sees,
    because `edges` alone would answer `()` for a graph that says otherwise."""
    a, b = _row("R1", "alpha"), _row("R2", "beta")
    a2 = ledger.Row(**{**a.__dict__, "dependencies": (ledger.Dependency(b.id, "supersedes"),)})
    assert ledger.edges([a2, b]) == ()


def test_a_symbol_criterion_must_carry_structured_fields_not_a_sentence(tmp_path):
    """§10.1's own worked example is a symbol-presence check standing in for a behavioural
    claim while the property is false. A symbol predicate may only be attached to a
    criterion PHRASED as 'P defines S'."""
    bad = _row(acceptance_criteria=(_crit(kind="symbol",
                                          text="storage.atomic_write is crash-safe"),))
    with pytest.raises(ledger.LedgerError, match="path.*symbol"):
        ledger.write_ledger(tmp_path, _led([bad]))
    good = _row(acceptance_criteria=(_crit(kind="symbol",
                                           text="storage.py defines atomic_write",
                                           path="shared/lib/forge/storage.py",
                                           symbol="atomic_write"),))
    ledger.write_ledger(tmp_path, _led([good]))


def test_a_criterion_may_not_carry_another_evaluators_input(tmp_path):
    """A `node_id` under `kind="symbol"` is two evaluators' inputs in one record and nothing
    says which was meant. The required-field loop cannot see this — every field it names is
    present — so only the second loop refuses it."""
    bad = _row(acceptance_criteria=(_crit(kind="symbol", text="storage.py defines it",
                                          path="shared/lib/forge/storage.py",
                                          symbol="atomic_write",
                                          node_id="tests/t.py::test_x"),))
    with pytest.raises(ledger.LedgerError, match="may not carry"):
        ledger.write_ledger(tmp_path, _led([bad]))
    # `query` joined the record for `schema`, and the rule is one rule: it is refused on the
    # other three kinds exactly as `node_id` is here, rather than being a slot only the loop
    # above happens to name.
    stray = _row(acceptance_criteria=(_crit(kind="symbol", text="storage.py defines it",
                                            path="shared/lib/forge/storage.py",
                                            symbol="atomic_write",
                                            query="table users has column seq"),))
    with pytest.raises(ledger.LedgerError, match="may not carry 'query'"):
        ledger.write_ledger(tmp_path, _led([stray]))


def test_a_criterion_kind_the_vocabulary_does_not_name_is_refused(tmp_path):
    """Refused BEFORE `_CRITERION_FIELDS[c.kind]` is indexed, which is the whole reason the
    check comes first: an unknown kind reaching that lookup raises `KeyError`, an error
    escaping this module's declared class. Nothing else in this suite reaches it — every
    other criterion test names a kind the table holds. Measured — deleting the check
    SURVIVED, because no fixture had an unknown kind to hand it."""
    with pytest.raises(ledger.LedgerError, match="criterion kind is one of"):
        ledger.write_ledger(tmp_path, _led([_row(acceptance_criteria=(_crit(kind="vibes"),))]))


def test_a_criterion_with_no_human_sentence_stands_for_nothing(tmp_path):
    """§10.1's rule is that a mechanical predicate may only be attached to the sentence it
    proves. An empty `text` is that rule's degenerate case: a `node_id` with no claim beside
    it reports a green check against nothing a reader can compare it to."""
    bad = _crit(kind="test", text="", node_id="tests/t.py::test_x")
    with pytest.raises(ledger.LedgerError, match="human sentence"):
        ledger.write_ledger(tmp_path, _led([_row(acceptance_criteria=(bad,))]))


def test_a_trace_may_not_hang_on_a_mechanical_criterion(tmp_path):
    """A human note beside a predicate that could flip the method to
    `manual_trace_confirmed` is §10.1's manufactured green by a second route."""
    bad = _row(acceptance_criteria=(_crit(kind="test", node_id="tests/t.py::test_x",
                                          trace="I checked it by hand"),))
    with pytest.raises(ledger.LedgerError, match="trace"):
        ledger.write_ledger(tmp_path, _led([bad]))


def test_a_schema_criterion_carries_a_predicate_because_10_1_calls_it_mechanical(tmp_path):
    """§10.1 names FOUR mechanically-checkable kinds — test ID, SCHEMA QUERY, exact symbol,
    file/hash invariant. `schema` used to require no fields at all, so a schema criterion was
    a bare sentence: exactly what the rule above refuses for `symbol`, on a kind the spec puts
    in the same list. It carries the query and the schema it runs against, like the other
    three carry theirs.

    Every schema fixture in the earlier suite supplied a trace, so the no-predicate path was
    never exercised — measured, a `schema` criterion with no predicate inputs wrote and read
    back clean."""
    bare = _row(acceptance_criteria=(_crit(kind="schema",
                                           text="the users table carries a seq column"),))
    with pytest.raises(ledger.LedgerError, match="path.*query"):
        ledger.write_ledger(tmp_path, _led([bare]))
    half = _row(acceptance_criteria=(_crit(kind="schema",
                                           text="the users table carries a seq column",
                                           path="db/schema.sql"),))
    with pytest.raises(ledger.LedgerError, match="'query' is empty"):
        ledger.write_ledger(tmp_path, _led([half]))
    good = _row(acceptance_criteria=(_crit(kind="schema",
                                           text="the users table carries a seq column",
                                           path="db/schema.sql",
                                           query="table users has column seq"),))
    ledger.write_ledger(tmp_path, _led([good]))


def test_a_trace_may_not_stand_in_for_the_schema_evaluator_this_repo_does_not_have(tmp_path):
    """THE TWO VERDICTS ARE NOT INTERCHANGEABLE. There is no schema evaluator in this
    repository and none should be invented, so a `schema` criterion must answer `unresolved`
    downstream — *nobody looked*. `manual_trace_confirmed` says *a human did*. A trace hanging
    on a mechanical kind whose evaluator is missing is that substitution written down, and it
    is §10.1's manufactured green wearing the other verdict: measured, `kind="schema"` with
    `trace="I checked it by hand"` and no predicate inputs at all wrote and read back clean."""
    bad = _row(acceptance_criteria=(_crit(kind="schema",
                                          text="the users table carries a seq column",
                                          path="db/schema.sql",
                                          query="table users has column seq",
                                          trace="I checked it by hand"),))
    with pytest.raises(ledger.LedgerError, match="a trace may hang only on"):
        ledger.write_ledger(tmp_path, _led([bad]))
    assert ledger._TRACEABLE == ("prose",), \
        "prose is the one kind with no predicate, and the only one a trace may hang on"


def test_the_degradation_is_recorded_in_the_ledger_not_only_in_a_report(tmp_path):
    """§10 also says from-scratch synthesis 'reads only the ledger', so a degraded ledger
    silently becomes the INPUT to synthesis. The threshold is recorded too, so changing it
    later cannot reinterpret an old ledger."""
    big = _led([_row()], union_diff_bytes=ledger.DEGRADE_UNION_DIFF_BYTES + 1, degraded=True)
    ledger.write_ledger(tmp_path, big)
    back = ledger.read_ledger(tmp_path)
    assert back.degraded is True
    assert back.degrade_threshold_bytes == ledger.DEGRADE_UNION_DIFF_BYTES
    assert back.union_diff_bytes == ledger.DEGRADE_UNION_DIFF_BYTES + 1


def test_a_ledger_claiming_undegraded_over_the_threshold_is_refused(tmp_path):
    with pytest.raises(ledger.LedgerError, match="degrad"):
        ledger.write_ledger(tmp_path, _led([_row()],
                                           union_diff_bytes=ledger.DEGRADE_UNION_DIFF_BYTES + 1,
                                           degraded=False))


def test_a_ledger_claiming_degraded_under_the_threshold_is_refused_too(tmp_path):
    """The other half of the equality. A ledger that claims degradation it did not measure
    tells synthesis to distrust a spec that is whole — and a check written as a one-sided
    `>` implication would let it through."""
    with pytest.raises(ledger.LedgerError, match="degrad"):
        ledger.write_ledger(tmp_path, _led([_row()], union_diff_bytes=1, degraded=True))


def test_a_ledger_recording_a_threshold_this_engine_did_not_apply_is_refused(tmp_path):
    """The recorded threshold is what stops a later change to `DEGRADE_UNION_DIFF_BYTES`
    reinterpreting an old ledger, so it must be the one that was applied.

    THE FIXTURE IS CHOSEN SO ONLY THIS GUARD CAN FIRE. A threshold of 1 also puts the
    100-byte union diff over it, so the degradation-equality check below refuses that ledger
    too — with a message that says "threshold" — and the test passed with THIS check deleted.
    Measured: that mutation SURVIVED. A threshold well ABOVE the measurement leaves the
    degradation check satisfied and this one as the only refusal available."""
    with pytest.raises(ledger.LedgerError, match="engine applies"):
        ledger.write_ledger(tmp_path, _led([_row()],
                                           degrade_threshold_bytes=
                                           ledger.DEGRADE_UNION_DIFF_BYTES * 2))


def test_the_ledger_round_trips_and_hashes_stably(tmp_path):
    l = _led([_row("R1", "alpha"), _row("R2", "beta")])
    ledger.write_ledger(tmp_path, l)
    back = ledger.read_ledger(tmp_path)
    assert back == l
    assert ledger.ledger_hash(back) == ledger.ledger_hash(l)


def test_a_claim_outside_ascii_round_trips_and_keeps_its_id(tmp_path):
    """Claim text is authored by three seats from a user's prose, so non-ASCII is ordinary
    here rather than exotic. Two distinct failures are pinned at once. The id is taken over
    the FRAMED json text, and NFC/NFD are two spellings of one glyph that an id which
    quietly normalized would collapse onto a single hash — \u00e9 and e\u0301 print
    identically in every diff a reviewer reads, so the escapes below are written out rather
    than typed. And the payload is JSON with `ensure_ascii`, so the claim leaves as escapes
    and must come back as the same str, not as the escape text itself.
    """
    claim = "les tests \u00e9chouent \u2014 \u65e5\u672c\u8a9e \U0001f600"
    decomposed = "les tests e\u0301chouent \u2014 \u65e5\u672c\u8a9e \U0001f600"
    assert claim != decomposed
    assert ledger.row_id("R1", claim) != ledger.row_id("R1", decomposed)
    ledger.write_ledger(tmp_path, _led([_row("R1", claim), _row("R1", decomposed)]))
    back = ledger.read_ledger(tmp_path)
    assert [r.semantic_claim for r in back.rows] == [claim, decomposed]
    assert back.rows[0].id == ledger.row_id("R1", claim)


def test_the_hash_moves_when_a_status_moves(tmp_path):
    """§14.1 embeds this in every checkpoint commit message; a status revision that did not
    move the hash would make the message a record of nothing."""
    a = _row("R1", "alpha")
    before = ledger.ledger_hash(_led([a]))
    after = ledger.ledger_hash(_led([ledger.Row(**{**a.__dict__, "status": "rejected"})]))
    assert before != after


def test_the_hash_covers_the_degradation_because_a_degraded_ledger_is_a_different_spec():
    """§10 makes the ledger the INPUT to from-scratch synthesis, so two ledgers with the
    same rows and different `degraded` are two different specs. A hash taken over `rows`
    alone would report them identical in the checkpoint commit message."""
    r = _row()
    over = ledger.DEGRADE_UNION_DIFF_BYTES + 1
    assert (ledger.ledger_hash(_led([r], union_diff_bytes=over, degraded=True))
            != ledger.ledger_hash(_led([r], union_diff_bytes=over, degraded=False)))


def test_a_missing_ledger_raises_rather_than_reading_as_no_claims(tmp_path):
    """An empty ledger is a run with no claims, which the coverage check reports as fully
    covered."""
    with pytest.raises(ledger.LedgerError, match="does not exist"):
        ledger.read_ledger(tmp_path)


def test_a_present_but_empty_rows_list_is_refused_exactly_as_a_missing_file_is(tmp_path):
    """THE HALF THE ABSENCE CHECK DOES NOT COVER, and the one that actually ships: only a
    MISSING `rows` key raises through the missing-field list. A present `[]` used to decode
    cleanly — `topological_order(())` returns `()`, `_check` counts nothing — and the ledger
    came back with no rows, over which `coverage.check` produces zero results, zero
    `unsatisfied`, zero `unresolved` and zero contradictions. `taskbundle._decode` refuses the
    same shape for `entries`; these two must agree."""
    ledger.write_ledger(tmp_path, _led([_row()]))
    p = storage.ledger_path(tmp_path)
    raw = json.loads(p.read_text())
    raw["rows"] = []
    p.write_text(json.dumps(raw))
    with pytest.raises(ledger.LedgerError, match="non-empty"):
        ledger.read_ledger(tmp_path)


def test_an_empty_ledger_cannot_be_written_either_and_leaves_no_file(tmp_path):
    """The refusal above is on the READ side, and a producer holding an empty `rows` tuple
    never goes through it: `_check` iterates no rows, `topological_order(())` returns `()`,
    and every structural check passes over nothing. What stops it is `write_ledger`'s own
    round trip through `_decode`. Asserting the file is ABSENT is the load-bearing half —
    a refusal raised after `atomic_write` would still have published the empty ledger."""
    with pytest.raises(ledger.LedgerError, match="non-empty"):
        ledger.write_ledger(tmp_path, _led([]))
    assert not storage.ledger_path(tmp_path).exists()


@pytest.mark.parametrize("key", ["acceptance_criteria", "seat_evidence", "dependencies"])
def test_a_row_that_omits_a_nested_list_is_refused_not_read_as_empty(tmp_path, key):
    """Each of these three used to be assigned into the decode body UNCONDITIONALLY, via
    `r.get(key, [])`, BEFORE the missing-field check ran — so the check could never see them
    absent, and each absence fails open in its own direction: no `acceptance_criteria` is a row
    that reports fully covered with nothing checked; no `seat_evidence` makes the
    unanimous-rejection finding unreachable (`len(...) < 2` -> `continue`), dropping §10's most
    valuable signal on a missing key; no `dependencies` is an unconstrained node the cycle check
    then passes over, which is the other route into the graph
    `test_a_dangling_dependency_is_refused_not_skipped` exists to forbid. Absent must reach
    `_sub`."""
    ledger.write_ledger(tmp_path, _led([_row()]))
    p = storage.ledger_path(tmp_path)
    raw = json.loads(p.read_text())
    del raw["rows"][0][key]
    p.write_text(json.dumps(raw))
    with pytest.raises(ledger.LedgerError, match="missing"):
        ledger.read_ledger(tmp_path)


@pytest.mark.parametrize("key", ["acceptance_criteria", "seat_evidence", "dependencies"])
def test_a_nested_list_that_is_not_a_list_is_refused_before_it_is_iterated(tmp_path, key):
    """A JSON object here is iterable — `tuple(_sub(cls, x, ...) for x in {"id": 1})` walks
    the KEYS — so without the type check the refusal that arrives is `_sub`'s "expected an
    object, not str", which names the wrong defect at the wrong level."""
    ledger.write_ledger(tmp_path, _led([_row()]))
    p = storage.ledger_path(tmp_path)
    raw = json.loads(p.read_text())
    raw["rows"][0][key] = {"id": "ffffffffffff"}
    p.write_text(json.dumps(raw))
    with pytest.raises(ledger.LedgerError, match=f"{key} is a list"):
        ledger.read_ledger(tmp_path)


@pytest.mark.parametrize("key", ["acceptance_criteria", "seat_evidence", "dependencies"])
def test_a_nested_list_element_that_is_not_an_object_is_refused_as_one(tmp_path, key):
    """THE ELEMENT-LEVEL HALF, and it was never measured. The test above pins the LIST's type;
    this pins the type of what is IN it, and `_sub`'s guard is the only thing that reads it.
    Without that guard `_sub` runs its missing-field comprehension over the element, and
    Python's `in` means three different things across the shapes JSON can produce here:
    `"id" in "ffffffffffff"` is a SUBSTRING test, so a string element is refused for the wrong
    reason; `row["id"]` on a JSON ARRAY raises `TypeError: list indices must be integers`; and
    `"id" in 7` raises `TypeError` too. Two of the three escape this module's declared class.
    Measured — and the guard SURVIVED the suite, because the multi-line mutation pattern that
    was supposed to test it exited 2 without applying."""
    ledger.write_ledger(tmp_path, _led([_row()]))
    p = storage.ledger_path(tmp_path)
    for element in ("ffffffffffff", ["id", "relation"], 7):
        raw = json.loads(p.read_text())
        raw["rows"][0][key] = [element]
        p.write_text(json.dumps(raw))
        with pytest.raises(ledger.LedgerError, match="expected an object"):
            ledger.read_ledger(tmp_path)


def test_a_ledger_file_that_is_not_an_object_is_refused_as_one(tmp_path):
    """`json.loads` answers whatever the file holds, so a ledger that is a NUMBER or `null`
    reaches `_decode` as one. Without the guard the missing-field comprehension asks
    `"version" not in 5` and raises `TypeError: argument of type 'int' is not iterable` — an
    error escaping this module's declared class on the ordinary route, a truncated or
    hand-edited file. Measured; the guard SURVIVED the suite without this."""
    p = storage.ledger_path(tmp_path)
    for blob in (b"5", b"null", b'"a ledger"', b"[1, 2]"):
        p.write_bytes(blob)
        with pytest.raises(ledger.LedgerError, match="a ledger is an object"):
            ledger.read_ledger(tmp_path)


def test_a_rows_element_that_is_not_an_object_is_refused_before_it_is_copied(tmp_path):
    """`body = dict(r)` is where an unguarded row lands, and `dict` accepts more than it
    should: `dict(["id"])` is `{'i': 'd'}` — a two-character string read as a key/value PAIR —
    so a JSON array of short strings decodes into a dict nobody wrote and is then refused for
    its missing fields, naming the wrong defect. The other shapes escape the class outright:
    `dict("nope")` raises `ValueError`, `dict(5)` raises `TypeError`. Measured; the guard
    SURVIVED the suite without this."""
    ledger.write_ledger(tmp_path, _led([_row()]))
    p = storage.ledger_path(tmp_path)
    for element in ("nope", 5, ["id"]):
        raw = json.loads(p.read_text())
        raw["rows"] = [element]
        p.write_text(json.dumps(raw))
        with pytest.raises(ledger.LedgerError, match="expected an object"):
            ledger.read_ledger(tmp_path)


def test_a_value_json_cannot_serialize_is_a_ledger_error_and_publishes_nothing(tmp_path):
    """`synthesis_evidence` is §10's `{oid, path, hunk/symbol, test}` and this module types it
    as a bare `dict`, so what a producer puts INSIDE it is unvalidated by design — which makes
    the serializer the first thing that sees it. BOTH exception types are needed and one test
    cannot show it: a `set` raises `TypeError`, while a NaN under `allow_nan=False` raises
    `ValueError`, so a handler naming either alone lets the other escape as itself. Measured;
    the handler SURVIVED the suite without this.

    `allow_nan=False` is why the NaN case exists at all — json's default emits bare `NaN`,
    which is not JSON and which no other reader of this file would parse back."""
    for bad in ({"oid": {1, 2}}, {"lines": float("nan")}):
        with pytest.raises(ledger.LedgerError, match="json cannot serialize"):
            ledger.write_ledger(tmp_path, _led([_row(synthesis_evidence=bad)]))
        assert not storage.ledger_path(tmp_path).exists()


def test_a_criterion_path_that_leaves_the_tree_is_refused_at_write(tmp_path):
    """`coverage` joins this onto the candidate tree, and `Path(tree) / "/abs"` IS "/abs". A
    criterion naming a host file would be reported as a MECHANICAL check on something the
    ledger describes nothing about."""
    for escaping in ("../../etc/passwd", "/etc/passwd"):
        bad = _row(acceptance_criteria=(_crit(kind="hash", text="p is unchanged",
                                              path=escaping, sha256="0" * 64),))
        with pytest.raises(ledger.LedgerError, match="escapes"):
            ledger.write_ledger(tmp_path, _led([bad]))


def test_a_non_integer_union_diff_size_is_a_ledger_error_not_a_type_error(tmp_path):
    """An error escaping this module's declared class is one no caller of it knows to catch:
    a string here used to raise `TypeError` out of `_check`'s `>` comparison."""
    ledger.write_ledger(tmp_path, _led([_row()]))
    p = storage.ledger_path(tmp_path)
    raw = json.loads(p.read_text())
    raw["union_diff_bytes"] = "lots"
    p.write_text(json.dumps(raw))
    with pytest.raises(ledger.LedgerError, match="union_diff_bytes"):
        ledger.read_ledger(tmp_path)


def test_a_boolean_union_diff_size_is_refused_because_bool_is_an_int(tmp_path):
    """`isinstance(True, int)` is True, so the type check above admits `true` for a byte
    count and `True > 524288` is a legal comparison answering False. The ledger would then
    record a measurement nobody took as an undegraded run."""
    ledger.write_ledger(tmp_path, _led([_row()]))
    p = storage.ledger_path(tmp_path)
    raw = json.loads(p.read_text())
    raw["union_diff_bytes"] = True
    p.write_text(json.dumps(raw))
    with pytest.raises(ledger.LedgerError, match="union_diff_bytes"):
        ledger.read_ledger(tmp_path)


def test_a_string_degraded_flag_is_refused_because_bool_of_false_is_true(tmp_path):
    """`bool("false")` is True, so a coerced read would take a degraded ledger's own denial
    as an admission — and the equality check in `_check` would then refuse the honest
    ledger and accept nothing else."""
    ledger.write_ledger(tmp_path, _led([_row()]))
    p = storage.ledger_path(tmp_path)
    raw = json.loads(p.read_text())
    raw["degraded"] = "false"
    p.write_text(json.dumps(raw))
    with pytest.raises(ledger.LedgerError, match="degraded is a bool"):
        ledger.read_ledger(tmp_path)


def test_a_status_the_vocabulary_does_not_name_is_refused(tmp_path):
    """The measured `{"phse": "biulding"}` failure, on a record where it decides the
    deliverable: `acceptd` reads as neither accepted nor rejected and coverage skips it."""
    ledger.write_ledger(tmp_path, _led([_row()]))
    p = storage.ledger_path(tmp_path)
    p.write_text(p.read_text().replace('"accepted"', '"acceptd"'))
    with pytest.raises(ledger.LedgerError, match="status"):
        ledger.read_ledger(tmp_path)


def test_a_kind_the_vocabulary_does_not_name_is_refused(tmp_path):
    """`seam` is the one kind §12.2 freezes, so a misspelled kind is a row that silently
    loses its freeze."""
    ledger.write_ledger(tmp_path, _led([_row()]))
    p = storage.ledger_path(tmp_path)
    p.write_text(p.read_text().replace('"behavior"', '"behaviour"'))
    with pytest.raises(ledger.LedgerError, match="kind"):
        ledger.read_ledger(tmp_path)


def test_a_stance_the_vocabulary_does_not_name_is_refused(tmp_path):
    """§10's unanimous-rejection signal is counted over `contradicts`, so a stance spelled
    any other way is a seat whose objection is invisible rather than counted."""
    ev = ledger.SeatEvidence(seat="claude", stance="objects", evidence="it deadlocks",
                             prompt_sha256=None)
    with pytest.raises(ledger.LedgerError, match="stance"):
        ledger.write_ledger(tmp_path, _led([_row(seat_evidence=(ev,))]))


@pytest.mark.parametrize("key", ["version", "rows", "union_diff_bytes",
                                 "degrade_threshold_bytes", "degraded"])
def test_a_ledger_that_omits_a_top_level_field_is_refused(tmp_path, key):
    """The row-level parametrize above pins the same rule one level down; this is the level
    §14.1 hashes into every checkpoint commit message, where a field a writer stopped writing
    moves the hash with nothing saying why. Without the check each absence is a `KeyError` out
    of the type loop — an error escaping this module's declared class."""
    ledger.write_ledger(tmp_path, _led([_row()]))
    p = storage.ledger_path(tmp_path)
    raw = json.loads(p.read_text())
    del raw[key]
    p.write_text(json.dumps(raw))
    with pytest.raises(ledger.LedgerError, match=f"missing.*{key}"):
        ledger.read_ledger(tmp_path)


def test_an_unknown_top_level_field_is_refused_rather_than_ignored(tmp_path):
    """A ledger written by a newer engine carries fields this one would silently drop, and
    `read` then `write` would publish a QUIETLY LOSSY copy under a moved hash."""
    ledger.write_ledger(tmp_path, _led([_row()]))
    p = storage.ledger_path(tmp_path)
    raw = json.loads(p.read_text())
    raw["provenance"] = "a later engine's field"
    p.write_text(json.dumps(raw))
    with pytest.raises(ledger.LedgerError, match="does not know"):
        ledger.read_ledger(tmp_path)


def test_something_that_is_not_a_ledger_is_refused_as_one(tmp_path):
    """`write_ledger` is public and `Ledger` is a plain dataclass, so a caller handing over a
    dict gets `AttributeError` on `l.version` without this — the error class argument again,
    and the same for a `rows` tuple holding anything but a `Row`."""
    with pytest.raises(ledger.LedgerError, match="a Ledger is required"):
        ledger.write_ledger(tmp_path, {"version": 1, "rows": []})
    with pytest.raises(ledger.LedgerError, match="a ledger row is a Row"):
        ledger.write_ledger(tmp_path, _led([{"id": "ffffffffffff"}]))


def test_an_unknown_row_field_is_refused_on_the_way_back_in(tmp_path):
    ledger.write_ledger(tmp_path, _led([_row()]))
    p = storage.ledger_path(tmp_path)
    row = json.loads(p.read_text())
    row["rows"][0]["novel"] = 1
    p.write_text(json.dumps(row))
    with pytest.raises(ledger.LedgerError, match="does not know"):
        ledger.read_ledger(tmp_path)


def test_a_ledger_whose_tuples_are_lists_does_not_survive_its_own_round_trip(tmp_path):
    """JSON has one sequence type, so a producer that passed lists writes a file that reads
    back as tuples and compares UNEQUAL to what it holds. Refusing at write is what keeps
    `read_ledger(...) == the_ledger_written` true for every caller that relies on it."""
    with pytest.raises(ledger.LedgerError, match="round trip"):
        ledger.write_ledger(tmp_path, _led([_row(dependencies=[])]))
    assert not storage.ledger_path(tmp_path).exists()


def test_a_bad_ledger_never_replaces_the_good_one_already_published(tmp_path):
    """`atomic_write` is the last statement in `write_ledger` for this reason: §13's loop
    rewrites this file every review round, and a round that hands over a cyclic or stale-id
    ledger must leave the previous round's readable."""
    good = _led([_row("R1", "alpha")])
    ledger.write_ledger(tmp_path, good)
    a = _row("R1", "beta")
    a2 = ledger.Row(**{**a.__dict__, "dependencies": (ledger.Dependency(a.id, "requires"),)})
    with pytest.raises(ledger.LedgerError, match="cycle"):
        ledger.write_ledger(tmp_path, _led([a2]))
    assert ledger.read_ledger(tmp_path) == good


def test_a_ledger_of_a_version_this_engine_does_not_write_is_refused(tmp_path):
    with pytest.raises(ledger.LedgerError, match="version"):
        ledger.write_ledger(tmp_path, _led([_row()], version=ledger.VERSION + 1))


def test_a_file_that_is_not_json_is_a_ledger_error(tmp_path):
    storage.ledger_path(tmp_path).write_bytes(b"{not json")
    with pytest.raises(ledger.LedgerError, match="not readable as JSON"):
        ledger.read_ledger(tmp_path)
