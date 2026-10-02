# Prepare the historical comparison subject

The historical Council/Forge subject is source-only comparison data. Its original
revision is `f063e2f4dc6c976b3dd314685642678d8c8ab7bc`. The unchanged
`evals/llm-fanout/baseline-lock.json` names 59 original blobs; its SHA-256 is
`b8b02b98daace4e7d898cd669dcda17f724e3be525088065616f73eac22e397d`.

The public exact-file package at `tests/fixtures/llm-fanout-historical-original/`
contains those blobs and that lock, preserving their original relative paths.
It is outside active skill and library delivery. Its old receipts are historical
source artifacts, not fresh live certifications.

From the checkout, prepare a private snapshot using the package explicitly:

```sh
mise exec -- python scripts/fanout_baseline.py prepare-historical \
  --root /absolute/path/to/khenrix-utils \
  --historical-source /absolute/path/to/khenrix-utils/tests/fixtures/llm-fanout-historical-original \
  --historical-root /absolute/path/to/owner-selected-private-parent/snapshot
```

Choose real absolute paths with no symlink ancestors or parent traversal. The
snapshot must be fresh and outside the checkout. Its immediate parent may be new
(with an existing grandparent), or an existing owner-only directory with mode
`0700`. A pre-existing `provenance.json` in that parent refuses preparation.

The explicit source is validated before any destination creation. Missing,
extra, changed, unreadable, symlink or nonregular inputs refuse. Ordinary public
input permissions are accepted. Preparation retains the verified bytes, writes
private `0700` directories and `0600` single-link files, and writes separate
`provenance.json` beside the snapshot. It then uses the existing historical
checker and original lock checker. Explicit source preparation reads no Git or
network and never falls back.

To retain the original offline Git behavior, omit `--historical-source`:

```sh
mise exec -- python scripts/fanout_baseline.py prepare-historical \
  --root /absolute/path/to/khenrix-utils \
  --historical-root /absolute/path/to/owner-selected-private-parent/snapshot
```

This mode requests only the exact original revision and locked relative paths
from the local Git object store. If that revision is unavailable, preparation
refuses; it does not download it or select the file package implicitly. Both
modes require the original revision, including when `--revision` is supplied.

Recheck the private snapshot with:

```sh
mise exec -- python scripts/fanout_baseline.py check-historical \
  --root /absolute/path/to/khenrix-utils \
  --historical-root /absolute/path/to/owner-selected-private-parent/snapshot
```

Consumers select this prepared subject with `--historical-root` or
`KHENRIX_HISTORICAL_BASELINE_ROOT`. `--historical-source` applies only to
preparation; it is not a consumer selector or a live validation result.
