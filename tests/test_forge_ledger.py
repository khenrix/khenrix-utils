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
    a = _row("R1", "alpha")
    with pytest.raises(ledger.LedgerError, match="twice"):
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
    and coverage would then be comparing a conflict against nothing."""
    a = _row("R1", "alpha")
    a2 = ledger.Row(**{**a.__dict__,
                       "dependencies": (ledger.Dependency("ffffffffffff", "conflicts"),)})
    with pytest.raises(ledger.LedgerError, match="declares a conflicts on"):
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


def test_conflicts_is_symmetric_and_not_an_ordering_edge(tmp_path):
    """A 'cycle' of conflicts is meaningless; two rows may conflict mutually and still be
    a legal ledger. Whether they may both be ACCEPTED is a coverage question, not a write
    refusal."""
    a, b = _row("R1", "alpha"), _row("R2", "beta")
    a2 = ledger.Row(**{**a.__dict__, "dependencies": (ledger.Dependency(b.id, "conflicts"),)})
    b2 = ledger.Row(**{**b.__dict__, "dependencies": (ledger.Dependency(a.id, "conflicts"),)})
    ledger.write_ledger(tmp_path, _led([a2, b2]))
    assert ledger.read_ledger(tmp_path).rows[0].id == a.id
    assert ledger.edges([a2, b2]) == ()


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


def test_a_criterion_field_that_is_not_text_is_refused_before_a_path_is_joined(tmp_path):
    """The required-field loop tests TRUTHINESS, and `7` is truthy — so an int `path` went
    straight through it into `bundle._assert_contained`, which raised `TypeError` out of
    `os.fspath`: an error escaping this module's declared class, on the one field it hands to
    another module."""
    bad = _row(acceptance_criteria=(_crit(kind="hash", text="p is unchanged", path=7,
                                          sha256="0" * 64),))
    with pytest.raises(ledger.LedgerError, match="path is a str, not int"):
        ledger.write_ledger(tmp_path, _led([bad]))
    numeric = _row(acceptance_criteria=(_crit(kind="prose", text=7),))
    with pytest.raises(ledger.LedgerError, match="text is a str, not int"):
        ledger.write_ledger(tmp_path, _led([numeric]))


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
