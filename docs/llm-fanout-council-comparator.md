# Guarded Council protocol comparator

The guarded Council protocol quality baseline is `guarded-council-protocol-v1`, not an exact trace of
the currently installed `llm-council` skill's final answer. This is a fair
protocol-level comparison for the stated “as good or better, not the same words”
criterion only after the source and delivery boundaries below are certified.

`stage_guarded_council_bundle` copies `shared/skills/llm-council` and
`shared/lib/council` byte-for-byte into a private disposable layout. Its caller
must supply a **pre-registered** digest for every source file; the function checks
those digests and records the copied bundle's digest. It does not establish that
the caller's digest map was reviewed. The engine and facade are not edited or
installed into the default CLI state.

`capture_guarded_council_synthesis` reads the staged workdir's owner-only engine
manifest and full `*.result.txt` files, checks their hashes and exact
`summary.header`, and sends the full valid answers to a pinned Claude child with
all built-in tools, slash commands, and MCP servers disabled. The private
witness stores the actual child JSON stdout and digests of the prompt, full
seat results, engine manifest, executable, and final answer. It is marked
`provisional-native-isolation-unproven` and
`engine_origin=unattested-staged-workdir`: a manifest placed in that workdir
is **not yet proof** that the staged facade produced it. Neither this witness
nor a caller-supplied delivery dictionary is an installed Council final.

Before a paid quality campaign can use this comparator, an external native
sandbox must deny executor seats access to sealed assertions and oracles through
every checkout, shared Git directory, worktree, and hard link. An isolated
driver must then execute the staged unchanged facade, capture its child stdout,
bind that stdout to the manifest and result bytes, and pre-register the source
and synthesis route. The installed CLI's no-tools behavior also needs a real
boundary characterization. Until those proofs exist, `run_live_campaign`
remains fail-closed and no Council quality score is certified.
