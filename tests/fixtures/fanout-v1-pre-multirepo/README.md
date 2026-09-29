# Frozen v1 fanout run records

These bytes were produced before Task 4 changed the runtime. The disposable
repository and run lived under `/private/tmp`; the frozen descriptor, inputs,
owner verifier, authenticated event journal, authenticated journal snapshot,
and authenticated scheduler metadata and snapshot are retained here. The journal contains an accepted amendment and a completed
file-handover transaction. `manifest.json` pins the exact file bytes.

Generation commands, from the repository root, with the pre-upgrade source
checked out into a disposable directory:

```sh
git archive dfb76a5a53882c8dacce443bda309813c09f8ee7 | tar -x -C /private/tmp/fanout-pre-v1.sIOq1i
FANOUT_FIXTURE_SOURCE_ROOT=/private/tmp/fanout-pre-v1.sIOq1i uvx --offline --python 3.12 --with pytest python tests/fixtures/fanout-v1-pre-multirepo/generate.py
```

The generator uses only `github.com/example/*` or synthetic question input,
disposable Git repositories, a provider that raises if called, and fixed
test-only owner material. It must not be rerun to update this fixture under a
later runtime: the point is to retain pre-upgrade bytes.

Pre-upgrade runtime SHA-256: `fa5b0823a016f854ab9cb7bbdcb53599fe38b5e1e92e5d79d81611c95eb78bfd`.
Pre-upgrade compiler SHA-256: `6012e5badb8748b56720816552a7d08fce8274a7e274a987cb6410004803c36c`.

The original disposable roots no longer exist. This fixture is for authenticated
read-only historical inspection of frozen records, not for resuming work. A
pre-upgrade run needs its exact original runtime restored to resume.
