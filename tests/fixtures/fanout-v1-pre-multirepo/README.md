# Public synthetic v1 status fixture

This deterministic public derivative preserves the original pre-upgrade v1
persisted record shapes and their runtime/compiler format bindings. It is not
byte-exact original evidence, a live run, or an originating public commit.

All absolute roots are fictional `/srv/fanout-public-synthetic-v1` paths.
Device/inode identities, run and scheduler identities, timestamps, salt and
owner material are fixed synthetic values. The owner material is test-only and
grants no authority over any real run. Descriptor envelopes, owner and event
MACs, journal checksums, snapshot bindings, scheduler MACs/checksums and the
manifest were recomputed for these actual derivative bytes. The CLI fixture
trust pin identifies this derivative alone.

The runtime binding `fa5b0823a016f854ab9cb7bbdcb53599fe38b5e1e92e5d79d81611c95eb78bfd`
and compiler binding `6012e5badb8748b56720816552a7d08fce8274a7e274a987cb6410004803c36c`
remain descriptions of the original v1 format, not certifications of current
code. Status authenticates the saved amendments and handovers without current
compilation. Resume requires the original runtime and refuses this synthetic
fixture. Ordinary tampering and a forged scheduler with a rehashed adjacent
manifest remain refusals.

`generate.py` deterministically reseals these public synthetic records; it does
not rerun an old or current provider. An intentional record change requires
reviewing the newly computed manifest and corresponding CLI fixture trust pin.
