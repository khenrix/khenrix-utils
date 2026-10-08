# Context efficiency corpus

Twenty deterministic fictional cases for source fidelity and exact expansion.
All people, seats, operations and repositories here are invented. No workplace
content, account identifiers, credentials or provider routes are included.

The model input packet is exactly `model-input/`: its manifest contains questions,
citations and hash-pinned original artifacts under `sources/`. Copy only that
directory into a model session. `gold.json` and the tests are evaluator inputs;
they must not be included in a model input packet. This directory separation is
an input contract, not operating-system confinement of a trusted native CLI.

Source identities include their immutable revision. Artifact provenance is
embedded as a first-line comment, or the `_provenance` object for JSON. Times
are fixed fictional UTC event times. Line locators are inclusive and one-based,
including the provenance line; JSON paths use ordered object keys and zero-based
array indices; heading locators include the heading and stop before the next
heading at the same or a higher level. Full artifact hashes include provenance.

Case coverage: code/line refs (01,12,17), JSON paths (02,11,15,16), overlapping
logs (03), heading boundaries (04,14), Swedish decisions (05,19), negations and
exceptions (06,07,18,19), late correction (08), old/new contradictions (09,15),
human decision/agent claim/failing test (10), equal numbers from three authors
(11), two repositories (12), duplicate summaries with one original source (13),
and a passing test that predates a later unverified correction (20).

Gold facts are hand-derived. Gold evidence values preserve exact cited text or
JSON values so each locator can be checked without a model. The test file keeps
its own literal expected facts and corpus pins; update pins only after reviewing
an intentional fixture revision. No generator, model invoker, index or live
source adapter is part of this corpus.

Verify with the existing cached environment:

```sh
mise exec -- uvx --offline --with pytest pytest -q tests/test_context_efficiency_corpus.py
```
