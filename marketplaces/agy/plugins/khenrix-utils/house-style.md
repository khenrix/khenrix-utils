<!-- khenrix-managed:begin house-style -->
<!-- Managed by khenrix-utils (capabilities.yaml -> instructions.source).
     Edit this block in the khenrix-utils repo, not in the rendered file.
     Content outside this marker block is yours and is never touched. -->

# khenrix house style

Shared working agreement for every agentic CLI (Claude Code, Codex, Antigravity/agy)
on this machine. Keep guidance provider-agnostic — anything CLI-specific belongs in
that CLI's own config, not here.

## Working principles

- Read before you write. Understand the surrounding code and match its conventions
  (naming, structure, comment density) instead of imposing a new style.
- Prefer the smallest change that fully solves the problem. Avoid speculative
  abstraction and unrelated refactors.
- Reuse existing utilities and patterns over adding new dependencies.
- Report outcomes honestly: if something failed, was skipped, or is unverified, say so.

## Safety

- Never commit secrets, tokens, or credentials. Reference env vars or on-disk paths.
- For destructive or outward-facing actions (deletes, pushes, deploys), confirm first
  unless explicitly authorised.
- Treat `~/git` as the primary workspace; avoid writing outside it without reason.

## Tooling

- These CLIs share a managed set of MCP servers and skills via `khenrix-utils`.
  Run the `khenrix-setup` skill to reconcile a CLI's config with the source of truth.
- MCP servers and settings added outside `khenrix-utils` are intentionally preserved —
  do not remove machine-specific configuration.
- To get a second opinion, a sibling CLI can be run headlessly with permissions bypassed
  (non-interactive print/exec mode) — see `headless-invocation.md`. Useful for
  cross-reviewing a plan or diff before acting.

## Skill & command hygiene

- In skills that declare `allowed-tools`, keep each Bash command a single command —
  do NOT chain with `&&`, `||`, or `;`; chaining defeats allow-list matching and forces
  a permission prompt. Run separate steps instead.
- Read env vars with `printenv VAR` and check the exit code, not `${VAR}` expansion —
  some CLIs treat `${VAR}` as a prompt-worthy security concern even when allow-listed.
- Interpret `test`/`command -v` exit codes directly; don't `echo` a result and re-parse it.

<!-- khenrix-managed:end house-style -->
