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

An optional topic selects an owner-declared path set, not a model-generated category:

```bash
mise exec -- python -B components/workvault/sync.py sync shipping --config /absolute/private/sync.json --topic-map /absolute/private/topics.json
```

The explicit topic map is an owner-only `0600` regular file in an owner-only directory:

```json
{
  "schema_version": "workvault-sync-topics-v1",
  "repository": "example/team-wiki",
  "topics": {"shipping": ["knowledge/example.md"], "empty": []}
}
```

Its repository must equal the already configured source, and every mapped path must
belong to the complete inventory at the pinned source commit. A map never admits a
new source or linked document. Topic names are exact and case-sensitive; undeclared
names, missing maps, unsafe paths, duplicate fields/paths and stale inventory mappings
refuse collection. An explicitly empty topic returns `topic_selection.outcome: no_match`
and exit1 without page reads, publication or source-state replacement. It never falls
back to all pages. No real topics or path labels are supplied by this component.

Topic sync retains full-source discovery, uncovered/tracked/failed-path accounting,
nonselected cards/checkpoints and uncertain-work fencing. `topic_selection` separately
reports the requested commit, selected paths, uncovered paths and topic completion;
topic success exits0 even if `full_source_coverage` remains false. Offline status still
reports source-wide coverage, not topic success. An empty selection reports against its
newly inspected commit while retaining the prior source state, if any. Repeating a
successful topic at the same commit reads no pages and spends no model calls. Omitting
the topic and map preserves the original full-source sync. Topic/map arguments are
not accepted by status, inventory or maintain. The library accepts the immutable
`SyncTopics` returned by `parse_topics(bytes)` as `sync_all(..., topic=..., topics=...)`.
Machine skill delivery must explicitly adopt this contract; existing installed helpers
do not gain topic support merely because the component source changes.

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
A failed batch stops the campaign and preserves that requested batch's entire
last-good notes and their embedded checkpoints. Earlier successful batches remain
published independently.
Confirmed settled failures may be retried by running sync again; uncertain provider
work fences implicit replay. Source disappearance is reported as retained removed
notes, never automatically erased. Authentication failure is not source removal.

Sync stages at most five validated curated notes, backs up their exact prior bytes,
and completes every pre-sync snapshot before replacing a note. A bounded private
curated-only journal uses the existing vault lock to serialize publication and
recovery. An ordinary publication failure restores the whole requested batch;
interrupted publication is recovered by the next explicitly requested sync, before
any source read. A recorded committed batch finishes cleanup without reverting or
recurating it. Recovery validates owned names, identities, permissions and content;
operator edits or ambiguous journal state refuse recovery without overwriting them.
Uncertain provider work still fences implicit replay after recovery.

Cooperative status and maintenance readers take the vault's shared lock and refuse
an in-progress or recovery-pending batch. Individual note renames are atomic, but
several renames are not instantly atomic: external readers that ignore the lock can
observe a mixed generation until recovery. Human notes remain untouched and seven
available prior generations remain outside the vault; one additional pending
snapshot can remain after a failed replacement, without discarding prior history.
An interruption while preparing backups, before the complete journal is durable,
can leave an ambiguous private backup directory; sync and reports refuse it for
operator inspection. This is bounded local recovery, not disk-failure protection or
isolation from an unrestricted same-user process.

`cli.py publish` and `cli.py status` also support already-curated JSON batches.
They do not collect source pages or call a model.

## Pure directory reports

`directory.py` exposes `directory_report` for already-curated routing records.
`generated_directory.py` exposes `generated_directory_report` for supplied
generated JSON and health metadata bytes. These are library functions; existing
commands and source checkpoints are unchanged.

The generated envelope contains `generated_at` (a date), `count`, and
`knowledge_bases`. Each entry contains `team`, `repo`, `use_when`, `caretaker`,
and `access`; optional fields are `slack`, `status`, `last_commit`, and
`review_by`. This enriched envelope is distinct from a repository's registration
document. For example:

```json
{
  "generated_at": "2026-10-06",
  "count": 1,
  "knowledge_bases": [{
    "team": "Example area",
    "repo": "example/team-kb",
    "use_when": "You need context on this team's shared services, boundaries and ownership.",
    "caretaker": "operator@example.invalid",
    "access": [{"kind": "repo", "path": "docs", "entrypoint": "index.md"}]
  }]
}
```

The first `kind: repo` access determines the declared path; defaults are `.`
and `README.md`. MCP entries remain inert. Repository identities ignore casing,
while declared paths preserve it. Unknown fields, conflicting duplicates, unsafe
paths, oversized metadata, and inconsistent counts, dates, or directory pins
refuse the report.

Callers explicitly select identities and provide independent target commit pins.
All supplied metadata must already be admitted for the source and consumer.
Selection controls card relevance, not access: unselected identities and the
existing repository's routes remain in the report.
`last_commit` is a date and never supplies a target revision. Missing dates remain
explicit; review dates before generation and commit ages greater than 90 days
receive separate flags. Status values are draft, stable (default), or deprecated.
The existing repository appears in `existing_routes`, without a duplicate card;
its absence retains the preserve-existing-source action. Tagged health metadata,
including a no-findings result, does not establish complete inventory or source
coverage.

Both functions operate only on supplied metadata. They perform no collection,
MCP connection, publication, state update, or retrieval authorization. Directory
pin assertions alone do not prove an authenticated read or approved access.

The internal `batch.read_curated_cards` primitive reads an explicit source-path
selection under one required shared publication lock. It preserves stable note IDs,
each card's exact source revision, draft status and uncertainty; a topic refresh may
legitimately leave different cards at different revisions. It refuses partial or
recovering publications and does not enumerate human notes. It is not exposed by
the CLI or MCP and establishes no session or consuming-route authority. Its existing
limits are 4,096 selected paths and 4 MiB per publication, not an aggregate recall
budget; the separate two-summary/500-token injection limit remains unimplemented.

This component does not enable Obsidian Sync, vault Git, community plugins, semantic
retrieval or automatic prompt/memory injection. Those require separate integration
and acceptance evidence. Same-user file-integrity checks and cooperative locks are
not isolation from a hostile process running as that user.
