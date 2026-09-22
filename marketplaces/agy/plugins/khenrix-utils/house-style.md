<!-- khenrix-managed:begin house-style -->
<!-- Managed by khenrix-utils (capabilities.toml -> instructions.source).
     Edit this block in the khenrix-utils repo, not in the rendered file.
     Content outside this marker block is yours and is never touched. -->

# khenrix house style

Shared working agreement for every agentic CLI (Claude Code, Codex, Antigravity/agy, Maka)
on this machine. Keep guidance provider-agnostic — anything CLI-specific belongs in
that CLI's own config, not here.

## Working principles

- Read before you write. Understand the surrounding code and match its conventions
  (naming, structure, comment density) instead of imposing a new style.
- Prefer the smallest change that fully solves the problem. Avoid speculative
  abstraction and unrelated refactors.
- Reuse existing utilities and patterns over adding new dependencies.
- Report outcomes honestly: if something failed, was skipped, or is unverified, say so.
- Prefer comments that state a constraint or non-obvious rationale the code can't
  express over ones that restate what the code does or justify the change to a
  reviewer — the latter are noise once merged.

## Seeing work through

- In agentic runs, proceed on reversible actions that follow from the request; pause only
  for destructive actions or genuine scope changes. Exception: when the user is describing
  a problem or thinking out loud rather than requesting a change, the deliverable is your
  assessment — report findings and stop; don't fix unasked.
- Before ending a turn, audit your own last paragraph: if it is a plan, a question, a
  next-steps list, or a promise of unfinished work ("I'll…"), do that work now — including
  retries and gathering missing information yourself. End only when done or blocked on
  input only the user can provide, never because the session got long.

## Verification & evidence

- Before a state-changing command (restart, delete, config edit), check the evidence
  supports that specific action — a symptom that pattern-matches a known failure may have
  a different cause.
- For rendered artifacts (HTML, SVG, charts, docs), run them in their real environment and
  observe the output yourself before claiming completion — well-formed and correct are
  different claims. One clean observation is enough; re-verify only after changing something.
- Never claim a verification that was not actually observed in a tool result.
- **Read the source; don't infer the behaviour.** When a question is about what a tool,
  CLI, or library actually does — its flags, exit codes, output shape, error strings — and
  the source is public, go read it. An inference that sounds right is the failure mode that
  survives a green test suite, because nothing executes prose. **Pull latest first**: a
  stale checkout gives a confident wrong answer with a plausible citation, which is worse
  than admitting you don't know. In khenrix-utils, `make cli-sources` syncs the upstreams
  worth reading and prints what each one is authoritative for.
- Use canonical upstreams only. Leaked or mirrored copies of proprietary source are out of
  bounds for any vendor, however convenient. When something is closed, the LICENSED install
  on this machine is the primary source — its `--help`, its shipped type declarations, its
  changelog — corroborated by a live probe of the actual binary. A measured observation
  outranks any document, including the vendor's own.
- The bar cuts both ways: an unverified warning is itself an error — absence of evidence is
  not a finding, and a clean pass stated plainly beats a manufactured caveat.
- Debugging: reproduce the failure and read the actual output before hypothesizing; for
  non-obvious failures hold several competing hypotheses rather than chasing the most
  visible signal; trace the full causal chain past the first plausible cause; report the
  hypotheses you rejected and what rejected them.

## Communication

- Everything the user needs from a turn goes in its final message, outcome first —
  mid-turn commentary may never be seen.
- Readable beats concise: shorten by dropping detail that doesn't change what the reader
  does next, not by compressing prose into fragments, arrow-chains, or invented shorthand.

## Quality defaults

- Make every response easy to start and resume: lead with the answer or current action,
  keep multi-step work bounded, show completed results, and omit unrelated tangents. If
  work remains, end with one concrete next action; add no next action after completion.
  `normal mode` disables this response shape for the session, and `resume ADHD mode`
  restores it.
- Give user-facing prose a quiet sentence-level quality pass. Use plain, direct language;
  remove throat-clearing, faux insight, puffery, unsupported attribution, dramatic
  fragments, decorative formatting, synonym cycling, and repeated contrast formulas.
  Preserve facts, uncertainty, technical identifiers, source text, and the requested
  voice. Do not announce the pass or append an editing report during ordinary work.
- Whenever writing or changing code, use the full code mode from `khenrix-quality`:
  understand the relevant flow, prefer an existing pattern or standard facility, and add
  the fewest clear concepts needed. Preserve required validation, security, accessibility,
  observability, error handling, repository conventions, and enough tests to prove the
  change. A task-specific workflow still controls planning, review, and delivery.
- Load `khenrix-quality` when the user explicitly requests ADHD controls, prose editing or
  detection, a named code minimalism level, or its review, audit, debt, benchmark, or help
  workflow. Load `khenrix-writing` for humanization, voice matching, or a deeper rewrite.
  Never run both prose rewrite modes over the same artifact.

## Superpowers workflow

- At the start of each root session, load `using-superpowers` before the first substantive
  response or action so its routing rules are available. A sub-agent dispatched with a
  bounded task loads only the skills relevant to that task. Treat an upstream reference to
  `superpowers:<name>` as the native direct-copy skill `<name>`; there is no plugin namespace.
- Route by ownership in this order: an explicitly named user skill; a domain or end-to-end
  workflow such as `fix-jira-ticket`, `agentic-setup`, Khenrix setup/upgrade, or `llm-forge`;
  `mikado-graph` for dependency decomposition; then Superpowers for a generic development
  process. `khenrix-quality` shapes code and prose within whichever workflow owns the task.
- Use `brainstorming` for open-ended design. Do not use it to reopen an accepted plan or
  re-gate work the user has already authorized. Do not recursively run Superpowers execution
  or sub-agent workflows inside Forge.
- Apply `test-driven-development` subject to the host's test policy: write tests that prove
  material behavior, not low-impact or implementation-mirroring tests. Do not create a
  worktree inside Forge, for global machine configuration, or when the task already has a
  managed worktree. Review and branch-finishing skills do not override a domain owner or an
  already authorized push or merge.
- Native `skill-creator` or `skill-tuneup` owns skill authoring and tuning unless the user
  explicitly asks for `writing-skills`. Khenrix model, effort, and mise defaults override
  generic upstream setup advice.
- The optional Superpowers visual companion must use its managed launcher, which always
  sets `SUPERPOWERS_DISABLE_TELEMETRY=1`. Do not bypass or unset that local-data control.

## Sub-agents

- Enforce delegation structurally, not by prose: a coordinator agent should have its write
  tools removed rather than be instructed not to write.
- Brief a verifier with the spec and the artifact only — never the producer's reasoning,
  so it cannot inherit the producer's blind spots; have it recompute key numbers from raw
  inputs.

## Safety

- Never commit secrets, tokens, or credentials. Reference env vars or on-disk paths.
- Use the 1Password MCP for Developer Environments when available: it can mount secrets into
  an authorized process without returning their values to the agent. It is not a website-login
  credential API. For browser logins, ask the user to approve 1Password browser autofill; never
  request, read, paste, print, or persist passwords, passkeys, recovery codes, cookies, or tokens.
- Treat 1Password unlock/approval, OTP, CAPTCHA, and BankID as human-assisted checkpoints.
- **Never export a literal secret in a shell rc file** (`.bashrc`, `.zshrc`, `.profile`).
  They are world-readable by every process the user runs, survive into backups and
  transcripts, and nothing ever re-examines them. Three live secrets sat in `~/.bashrc`
  here (a Supabase secret key, a database password, a Google Places API key) until a
  grep found them by accident. Store them in 1Password and reference at use time:
  ```bash
  # .bashrc — a reference, not a value
  export EXPENSES_DB_PASSWORD="op://Automation/expenses/db-password"
  # then run the consumer under `op run`, which resolves op:// refs into the
  # child's environment without the value touching disk or the agent:
  op run -- ./your-app
  ```
  For a one-off read: `op read "op://Automation/expenses/db-password"`.
  **NOT `op://Private/...`** — measured 2026-08-13 from `op service-account create --help`:
  "You can't grant a service account access to your Personal or Private vault." An agent
  authenticating with `OP_SERVICE_ACCOUNT_TOKEN` therefore cannot resolve a Private ref at
  all, so a rule recommending Private *and* `op run` in the same breath contradicts itself.
  Keep automation secrets in a dedicated vault; `scripts/op-bootstrap-expenses.sh` moves
  them there over stdin without the values reaching argv, a transcript, or an agent.
  `op run`/`op read` are **CLI** features — the 1Password MCP does not provide them,
  and on WSL the desktop app's CLI integration is Windows-only, so WSL's `op` needs its
  own `op account add` or `OP_SERVICE_ACCOUNT_TOKEN` (see `docs/machine-setup.md`).
  The service-account token is itself a credential: keep it in a `0600` file outside any
  repo and `export` it from there. That does not remove a secret at rest — it replaces N
  long-lived plaintext credentials with ONE scoped, read-only, centrally revocable token.
  Config files that take literal values (e.g. an MCP `env` block) should hold
  `${VAR}` and let the shell supply it — Claude Code expands `${VAR}` and
  `${VAR:-default}` in `command`, `args`, `env`, `url` and `headers`.
- Secrets are not only in the obvious file. When scrubbing, check the whole family:
  the config, its rotating backups, `.credentials.json`, per-project `.env` files,
  and shell rc files. Scrub the SOURCE first — backups that regenerate every couple
  of minutes will otherwise be re-dirtied from it.
- For destructive or outward-facing actions (deletes, pushes, deploys), confirm first
  unless explicitly authorised.
- Treat `~/git` as the primary workspace; avoid writing outside it without reason.

## Tooling

- Every skill declared in `[skill_delivery].skills`, including the reviewed Superpowers
  bundle, and this bounded instruction block are copied directly from `khenrix-utils` with
  `mise run skills:plan` and `skills:apply`.
- Optional MCP servers, settings, and plugin content use the broader reconcile flow.
  Run `khenrix-setup` only in a CLI where that optional plugin was explicitly installed.
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
