## Claude Code specifics

- Invoke khenrix skills via the Skill tool or their slash commands (`/khenrix-setup`,
  `/khenrix-upgrade`, `/llm-council`).
- Claude caches plugins by version — after editing a khenrix skill, run
  `make khenrix-refresh` (not just `make render`) so the change loads in a new session.
