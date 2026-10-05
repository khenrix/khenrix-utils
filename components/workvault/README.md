# WorkVault inbound sync

This runtime turns an explicitly configured GitHub Markdown knowledge source into
private, curated Obsidian notes. The repository, vault paths and model project live
in a machine-owned configuration, not this public component.

## Commands

Run through the checkout's pinned mise environment:

```bash
mise exec -- python -B components/workvault/sync.py inventory --config /absolute/private/sync.json
mise exec -- python -B components/workvault/sync.py sync --config /absolute/private/sync.json
mise exec -- python -B components/workvault/sync.py status --config /absolute/private/sync.json
mise exec -- python -B components/workvault/sync.py maintain --config /absolute/private/sync.json
mise exec -- python -B -m unittest discover -s components/workvault
```

The same deterministic suite runs in `make verify` and `make test`; use
`mise exec -- make workvault-test` to run only this component.

`inventory` reads the source schema, complete index and recursive Git tree. `sync`
pins the requested ref or default branch to an immutable commit, reads at most five
knowledge pages per model call, validates structured curation, and publishes a stable
note per page. `status` is offline. Both inventory and sync accept `--ref` for an
explicit commit or ref.

`maintain` is offline and report-only. It compares managed publications with the
last observed pinned inventory, reports missing publications, stale commit identities
and retained removed paths, and groups exact duplicate curated payloads among active
source paths. Duplicate comparison includes title, summary, details, date, draft and
uncertainty; it excludes source paths, links and cross-references. It emits metadata
only, preserves human notes, and does not create roots, refresh sources, call a model,
publish, snapshot or update state. `--ref` is refused. Concurrent changes detected
during the report refuse the result rather than claim a coherent generation.
Configured state and vault roots are securely reopened and matched by device/inode
before returning a report; replacement, disappearance or a previously missing vault
appearing refuses the result. These checks are not a transactional multi-file snapshot
and do not isolate an unrestricted same-user process.

The current card schema has unstructured prose, not comparable fact identities.
`conflict_check: unavailable_unstructured_cards` explicitly records that semantic
contradictions are not assessed. A successful report is not an all-clear or complete
Phase 3 acceptance, and stale means revision mismatch, not an inferred age threshold.

Configuration is an owner-only `0600` regular JSON file in an owner-only directory:

```json
{
  "schema_version": "workvault-sync-config-v1",
  "repository": "example/team-wiki",
  "vault": "/absolute/private/vault",
  "snapshots": "/absolute/private/snapshots",
  "state": "/absolute/private/state",
  "vertex": {
    "project": "example-project",
    "location": "eu",
    "model": "explicit-model-id"
  }
}
```

The three storage roots must be distinct and non-overlapping. Authentication uses
the existing GitHub CLI login and Google application-default credentials; this
configuration contains no credentials. The current collector is a tool-free
Anthropic-on-Vertex adapter, pinned to the configured model and EU endpoint, with
adaptive medium effort. It does not fall back to a different route or model.

## Coverage and recovery

Discovery includes every regular `knowledge/*.md` tree entry, even unindexed pages
and templates. Missing index entries and broken index references are reported, not
silently excluded. Raw page bodies and model reasoning stay in the collector's
process memory; neither is saved as an artifact or added to invoking-agent context.
Only validated curated notes, source metadata and content-free provider evidence
are persisted. Links are retained as provenance without fetching linked bodies.

A published page at the target commit is skipped without another model call.
A failed batch stops the campaign and leaves already published notes intact.
Confirmed settled failures may be retried by running sync again; uncertain provider
work fences implicit replay. Source disappearance is reported as retained removed
notes, never automatically erased. Authentication failure is not source removal.

Publication uses private storage, a cooperative lock, bounded validation and atomic
replacement. It preserves human notes and retains seven prior successful generations
outside the vault. A failed replacement can leave one pending snapshot until retry;
it does not discard successful history.

`cli.py publish` and `cli.py status` also support already-curated JSON batches.
They do not collect source pages or call a model.

This component does not enable Obsidian Sync, vault Git, community plugins, semantic
retrieval or automatic prompt/memory injection. Those require separate integration
and acceptance evidence. Same-user file-integrity checks and cooperative locks are
not isolation from a hostile process running as that user.
