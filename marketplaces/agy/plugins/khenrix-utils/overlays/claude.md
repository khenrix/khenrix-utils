## Claude Code specifics

- Invoke khenrix skills via the Skill tool or their slash commands (`/khenrix-setup`,
  `/khenrix-upgrade`, `/llm-council`).
- After editing `khenrix-quality` or `khenrix-writing`, use `mise run skills:plan`
  followed by the reviewed `skills:apply`; these native copies never come from a plugin.
- Claude caches optional plugin content by version. After editing another bundled skill,
  run `make khenrix-refresh` so that installed plugin copy loads in a new session.
