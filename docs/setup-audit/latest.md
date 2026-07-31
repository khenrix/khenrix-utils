# Setup audit — 2026-07-31T01:31:11Z

Status: complete-with-findings

Inventory 799 items (hash ece86074), 46 finding(s), 0 discovery error(s).
0 local waiver(s) active.

## Phase coverage

- checks: complete
- ecosystem: engine does not run discovery — SKILL.md Phase E
- inventory: complete
- probes: engine does not run probes — SKILL.md Phase D

## Findings (by severity)

### [53] b7.skill.<listing-budget>
- rule B7 · claude/user · silent-capability-loss · confidence high · id `90ff7bebe3fb` fp `977e8799`
- evidence: `{"budget": 2000, "estimator": "claude plugin details (count_tokens)", "over_by_pct": 304, "total_always_on": 8075, "biggest_payers": [["claude-obsidian", 3678], ["khenrix-utils", 3233], ["superpowers", 715], ["claude-md-management", 175], ["skill-creator", 112]], "instruction_file_estimates_chars4": {"khenrix-utils:CLAUDE.md": 1220, "obsidian-vault:CLAUDE.md": 1426, "obsidian-vault:AGENTS.md": 737`
- rung: disable the biggest-payer plugin you use least (rung 2)
- rung: shorten khenrix-utils descriptions (rung 1, arena-gated)

### [41] b6.skill.claude-obsidian-save--khenrix-utils-khenrix-wiki-add
- rule B6 · claude/user · wrong-tool-fires · confidence low · id `005e5aeabf76` fp `e236fd27`
- note: heuristic nomination — adjudicate before acting
- evidence: `{"corpus_size": 43, "cosine": 0.657, "nominated": 15, "same_plugin": false, "shared_phrases": ["add this to the wiki"], "shared_tokens": ["save", "add", "wiki", "keep"]}`
- rung: Phase C adjudication → Phase D arena/probes
- rung: rung 1 if DUPLICATE and one side is ours

### [41] b6.skill.superpowers-subagent-driven-development--superpowers-using-git-worktrees
- rule B6 · claude/user · wrong-tool-fires · confidence low · id `d6a9afcc80b9` fp `9815a7db`
- note: heuristic nomination — adjudicate before acting
- evidence: `{"corpus_size": 43, "cosine": 0.34, "nominated": 15, "same_plugin": true, "shared_phrases": [], "shared_tokens": ["executing", "plans", "current", "implementation"]}`
- rung: Phase C adjudication → Phase D arena/probes
- rung: rung 1 if DUPLICATE and one side is ours

### [41] b6.skill.claude-obsidian-obsidian-markdown--claude-obsidian-wiki-cli
- rule B6 · claude/user · wrong-tool-fires · confidence low · id `7ee064f7e2f9` fp `70cad473`
- note: heuristic nomination — adjudicate before acting
- evidence: `{"corpus_size": 43, "cosine": 0.299, "nominated": 15, "same_plugin": true, "shared_phrases": [], "shared_tokens": ["obsidian", "write", "note"]}`
- rung: Phase C adjudication → Phase D arena/probes
- rung: rung 1 if DUPLICATE and one side is ours

### [41] b6.skill.khenrix-utils-khenrix-upgrade--khenrix-utils-skill-tuneup
- rule B6 · claude/user · wrong-tool-fires · confidence low · id `200e65a2dd2e` fp `1ba59cf9`
- note: heuristic nomination — adjudicate before acting
- evidence: `{"corpus_size": 43, "cosine": 0.252, "nominated": 15, "same_plugin": true, "shared_phrases": [], "shared_tokens": ["tune", "modernize", "refresh", "improve"]}`
- rung: Phase C adjudication → Phase D arena/probes
- rung: rung 1 if DUPLICATE and one side is ours

### [41] b6.skill.superpowers-dispatching-parallel-agents--superpowers-subagent-driven-development
- rule B6 · claude/user · wrong-tool-fires · confidence low · id `fd5274803cd6` fp `636f62d1`
- note: heuristic nomination — adjudicate before acting
- evidence: `{"corpus_size": 43, "cosine": 0.229, "nominated": 15, "same_plugin": true, "shared_phrases": [], "shared_tokens": ["independent", "tasks"]}`
- rung: Phase C adjudication → Phase D arena/probes
- rung: rung 1 if DUPLICATE and one side is ours

### [41] b6.skill.claude-obsidian-wiki-lint--claude-obsidian-wiki-query
- rule B6 · claude/user · wrong-tool-fires · confidence low · id `15289bfafb6b` fp `b58c5d4b`
- note: heuristic nomination — adjudicate before acting
- evidence: `{"corpus_size": 43, "cosine": 0.227, "nominated": 15, "same_plugin": true, "shared_phrases": [], "shared_tokens": ["wiki", "find"]}`
- rung: Phase C adjudication → Phase D arena/probes
- rung: rung 1 if DUPLICATE and one side is ours

### [41] b6.skill.claude-obsidian-obsidian-bases--claude-obsidian-wiki
- rule B6 · claude/user · wrong-tool-fires · confidence low · id `d5e715dafd37` fp `f28ffb94`
- note: heuristic nomination — adjudicate before acting
- evidence: `{"corpus_size": 43, "cosine": 0.222, "nominated": 15, "same_plugin": true, "shared_phrases": [], "shared_tokens": ["base", "obsidian", "create"]}`
- rung: Phase C adjudication → Phase D arena/probes
- rung: rung 1 if DUPLICATE and one side is ours

### [41] b6.skill.claude-obsidian-wiki--claude-obsidian-wiki-mode
- rule B6 · claude/user · wrong-tool-fires · confidence low · id `67d009fcd88b` fp `35655685`
- note: heuristic nomination — adjudicate before acting
- evidence: `{"corpus_size": 43, "cosine": 0.216, "nominated": 15, "same_plugin": true, "shared_phrases": [], "shared_tokens": ["vault", "setup", "set", "wiki"]}`
- rung: Phase C adjudication → Phase D arena/probes
- rung: rung 1 if DUPLICATE and one side is ours

### [41] b6.skill.claude-md-management-claude-md-improver--khenrix-utils-skill-tuneup
- rule B6 · claude/user · wrong-tool-fires · confidence low · id `ed221268ccba` fp `db527201`
- note: heuristic nomination — adjudicate before acting
- evidence: `{"corpus_size": 43, "cosine": 0.201, "nominated": 15, "same_plugin": false, "shared_phrases": [], "shared_tokens": ["maintenance", "improve", "audit"]}`
- rung: Phase C adjudication → Phase D arena/probes
- rung: rung 1 if DUPLICATE and one side is ours

### [41] b6.skill.superpowers-executing-plans--superpowers-subagent-driven-development
- rule B6 · claude/user · wrong-tool-fires · confidence low · id `c9fcca014066` fp `4a81b776`
- note: heuristic nomination — adjudicate before acting
- evidence: `{"corpus_size": 43, "cosine": 0.197, "nominated": 15, "same_plugin": true, "shared_phrases": [], "shared_tokens": ["session", "implementation"]}`
- rung: Phase C adjudication → Phase D arena/probes
- rung: rung 1 if DUPLICATE and one side is ours

### [41] b6.skill.claude-md-management-claude-md-improver--claude-obsidian-wiki-lint
- rule B6 · claude/user · wrong-tool-fires · confidence low · id `915faa8305ef` fp `52972d9d`
- note: heuristic nomination — adjudicate before acting
- evidence: `{"corpus_size": 43, "cosine": 0.179, "nominated": 15, "same_plugin": false, "shared_phrases": [], "shared_tokens": ["maintenance", "audit"]}`
- rung: Phase C adjudication → Phase D arena/probes
- rung: rung 1 if DUPLICATE and one side is ours

### [41] b6.skill.superpowers-test-driven-development--superpowers-using-git-worktrees
- rule B6 · claude/user · wrong-tool-fires · confidence low · id `150ea7712f0c` fp `fc5f1329`
- note: heuristic nomination — adjudicate before acting
- evidence: `{"corpus_size": 43, "cosine": 0.174, "nominated": 15, "same_plugin": true, "shared_phrases": [], "shared_tokens": ["feature", "implementation"]}`
- rung: Phase C adjudication → Phase D arena/probes
- rung: rung 1 if DUPLICATE and one side is ours

### [41] b6.skill.claude-obsidian-wiki--claude-obsidian-wiki-cli
- rule B6 · claude/user · wrong-tool-fires · confidence low · id `62f475b8c62d` fp `dd060294`
- note: heuristic nomination — adjudicate before acting
- evidence: `{"corpus_size": 43, "cosine": 0.17, "nominated": 15, "same_plugin": true, "shared_phrases": [], "shared_tokens": ["obsidian", "vault", "create"]}`
- rung: Phase C adjudication → Phase D arena/probes
- rung: rung 1 if DUPLICATE and one side is ours

### [41] b6.skill.khenrix-utils-chunk-map--khenrix-utils-mikado-graph
- rule B6 · claude/user · wrong-tool-fires · confidence low · id `9ae827a923b5` fp `a1b8de07`
- note: heuristic nomination — adjudicate before acting
- evidence: `{"corpus_size": 43, "cosine": 0.168, "nominated": 15, "same_plugin": true, "shared_phrases": [], "shared_tokens": ["here", "break", "refactor", "big"]}`
- rung: Phase C adjudication → Phase D arena/probes
- rung: rung 1 if DUPLICATE and one side is ours

### [41] b6.skill.khenrix-utils-khenrix-audit--khenrix-utils-khenrix-setup
- rule B6 · claude/user · wrong-tool-fires · confidence low · id `c4d37de09277` fp `89796613`
- note: heuristic nomination — adjudicate before acting
- evidence: `{"corpus_size": 43, "cosine": 0.164, "nominated": 15, "same_plugin": true, "shared_phrases": [], "shared_tokens": ["servers", "mcp"]}`
- rung: Phase C adjudication → Phase D arena/probes
- rung: rung 1 if DUPLICATE and one side is ours

### [41] b6.skill.artifact-template-simple-dark-mode--artifact-template-simple-light-mode
- rule B6 · codex/user · wrong-tool-fires · confidence low · id `bc60e24e4f3b` fp `876480e5`
- note: heuristic nomination — adjudicate before acting
- evidence: `{"corpus_size": 37, "cosine": 0.657, "nominated": 15, "same_plugin": false, "shared_phrases": [], "shared_tokens": ["mode", "simple", "selects", "names"]}`
- rung: Phase C adjudication → Phase D arena/probes
- rung: rung 1 if DUPLICATE and one side is ours

### [41] b6.skill.artifact-template-operating-calendar--artifact-template-operating-review
- rule B6 · codex/user · wrong-tool-fires · confidence low · id `21b74ae0a70a` fp `d37fa1e6`
- note: heuristic nomination — adjudicate before acting
- evidence: `{"corpus_size": 37, "cosine": 0.601, "nominated": 15, "same_plugin": false, "shared_phrases": [], "shared_tokens": ["operating", "selects", "names"]}`
- rung: Phase C adjudication → Phase D arena/probes
- rung: rung 1 if DUPLICATE and one side is ours

### [41] b6.skill.artifact-template-design-report--artifact-template-system-design
- rule B6 · codex/user · wrong-tool-fires · confidence low · id `23996db1453e` fp `0e658965`
- note: heuristic nomination — adjudicate before acting
- evidence: `{"corpus_size": 37, "cosine": 0.582, "nominated": 15, "same_plugin": false, "shared_phrases": [], "shared_tokens": ["design", "selects", "names"]}`
- rung: Phase C adjudication → Phase D arena/probes
- rung: rung 1 if DUPLICATE and one side is ours

### [41] b6.skill.artifact-template-project-kickoff--artifact-template-project-tracker
- rule B6 · codex/user · wrong-tool-fires · confidence low · id `abefccaceda1` fp `ad07bd25`
- note: heuristic nomination — adjudicate before acting
- evidence: `{"corpus_size": 37, "cosine": 0.527, "nominated": 15, "same_plugin": false, "shared_phrases": [], "shared_tokens": ["project", "selects", "names"]}`
- rung: Phase C adjudication → Phase D arena/probes
- rung: rung 1 if DUPLICATE and one side is ours

### [41] b6.skill.artifact-template-legal-memorandum--artifact-template-strategy-memorandum
- rule B6 · codex/user · wrong-tool-fires · confidence low · id `6d0a332217d9` fp `ac697697`
- note: heuristic nomination — adjudicate before acting
- evidence: `{"corpus_size": 37, "cosine": 0.527, "nominated": 15, "same_plugin": false, "shared_phrases": [], "shared_tokens": ["memorandum", "selects", "names"]}`
- rung: Phase C adjudication → Phase D arena/probes
- rung: rung 1 if DUPLICATE and one side is ours

### [41] b6.skill.artifact-template-business-review--artifact-template-operating-review
- rule B6 · codex/user · wrong-tool-fires · confidence low · id `6946c757bb1e` fp `b59daec5`
- note: heuristic nomination — adjudicate before acting
- evidence: `{"corpus_size": 37, "cosine": 0.48, "nominated": 15, "same_plugin": false, "shared_phrases": [], "shared_tokens": ["review", "selects", "names"]}`
- rung: Phase C adjudication → Phase D arena/probes
- rung: rung 1 if DUPLICATE and one side is ours

### [41] b6.skill.artifact-template-design-report--artifact-template-market-trends-report
- rule B6 · codex/user · wrong-tool-fires · confidence low · id `9a3b596a59af` fp `5bd92e64`
- note: heuristic nomination — adjudicate before acting
- evidence: `{"corpus_size": 37, "cosine": 0.456, "nominated": 15, "same_plugin": false, "shared_phrases": [], "shared_tokens": ["report", "selects", "names"]}`
- rung: Phase C adjudication → Phase D arena/probes
- rung: rung 1 if DUPLICATE and one side is ours

### [41] b6.skill.khenrix-upgrade--skill-tuneup
- rule B6 · codex/user · wrong-tool-fires · confidence low · id `fd648348c801` fp `974bd959`
- note: heuristic nomination — adjudicate before acting
- evidence: `{"corpus_size": 37, "cosine": 0.183, "nominated": 15, "same_plugin": false, "shared_phrases": [], "shared_tokens": ["tune", "modernize", "refresh"]}`
- rung: Phase C adjudication → Phase D arena/probes
- rung: rung 1 if DUPLICATE and one side is ours

### [41] b6.skill.artifact-template-operating-review--artifact-template-system-design
- rule B6 · codex/user · wrong-tool-fires · confidence low · id `8ea0dc665859` fp `879e16f7`
- note: heuristic nomination — adjudicate before acting
- evidence: `{"corpus_size": 37, "cosine": 0.177, "nominated": 15, "same_plugin": false, "shared_phrases": [], "shared_tokens": ["selects", "names"]}`
- rung: Phase C adjudication → Phase D arena/probes
- rung: rung 1 if DUPLICATE and one side is ours

### [41] b6.skill.artifact-template-design-report--artifact-template-operating-review
- rule B6 · codex/user · wrong-tool-fires · confidence low · id `527739153d9f` fp `879e16f7`
- note: heuristic nomination — adjudicate before acting
- evidence: `{"corpus_size": 37, "cosine": 0.177, "nominated": 15, "same_plugin": false, "shared_phrases": [], "shared_tokens": ["selects", "names"]}`
- rung: Phase C adjudication → Phase D arena/probes
- rung: rung 1 if DUPLICATE and one side is ours

### [41] b6.skill.artifact-template-operating-review--artifact-template-strategy-memorandum
- rule B6 · codex/user · wrong-tool-fires · confidence low · id `f16df7f229ec` fp `e53ae984`
- note: heuristic nomination — adjudicate before acting
- evidence: `{"corpus_size": 37, "cosine": 0.169, "nominated": 15, "same_plugin": false, "shared_phrases": [], "shared_tokens": ["selects", "names"]}`
- rung: Phase C adjudication → Phase D arena/probes
- rung: rung 1 if DUPLICATE and one side is ours

### [41] b6.skill.artifact-template-operating-review--artifact-template-project-tracker
- rule B6 · codex/user · wrong-tool-fires · confidence low · id `76841757f5c8` fp `e53ae984`
- note: heuristic nomination — adjudicate before acting
- evidence: `{"corpus_size": 37, "cosine": 0.169, "nominated": 15, "same_plugin": false, "shared_phrases": [], "shared_tokens": ["selects", "names"]}`
- rung: Phase C adjudication → Phase D arena/probes
- rung: rung 1 if DUPLICATE and one side is ours

### [41] b6.skill.artifact-template-operating-review--artifact-template-project-kickoff
- rule B6 · codex/user · wrong-tool-fires · confidence low · id `c441ac034cc5` fp `e53ae984`
- note: heuristic nomination — adjudicate before acting
- evidence: `{"corpus_size": 37, "cosine": 0.169, "nominated": 15, "same_plugin": false, "shared_phrases": [], "shared_tokens": ["selects", "names"]}`
- rung: Phase C adjudication → Phase D arena/probes
- rung: rung 1 if DUPLICATE and one side is ours

### [41] b6.skill.artifact-template-legal-memorandum--artifact-template-operating-review
- rule B6 · codex/user · wrong-tool-fires · confidence low · id `e50de3c854ff` fp `e53ae984`
- note: heuristic nomination — adjudicate before acting
- evidence: `{"corpus_size": 37, "cosine": 0.169, "nominated": 15, "same_plugin": false, "shared_phrases": [], "shared_tokens": ["selects", "names"]}`
- rung: Phase C adjudication → Phase D arena/probes
- rung: rung 1 if DUPLICATE and one side is ours

### [41] b6.skill.artifact-template-experiment-analysis--artifact-template-operating-review
- rule B6 · codex/user · wrong-tool-fires · confidence low · id `6b1054fdeed4` fp `e53ae984`
- note: heuristic nomination — adjudicate before acting
- evidence: `{"corpus_size": 37, "cosine": 0.169, "nominated": 15, "same_plugin": false, "shared_phrases": [], "shared_tokens": ["selects", "names"]}`
- rung: Phase C adjudication → Phase D arena/probes
- rung: rung 1 if DUPLICATE and one side is ours

### [41] b6.skill.khenrix-utils-khenrix-upgrade--khenrix-utils-skill-tuneup
- rule B6 · agy/user · wrong-tool-fires · confidence low · id `1bea27f1f0ab` fp `349637d3`
- note: heuristic nomination — adjudicate before acting
- evidence: `{"corpus_size": 11, "cosine": 0.208, "nominated": 5, "same_plugin": true, "shared_phrases": [], "shared_tokens": ["tune", "modernize", "refresh", "improve"]}`
- rung: Phase C adjudication → Phase D arena/probes
- rung: rung 1 if DUPLICATE and one side is ours

### [41] b6.skill.khenrix-utils-chunk-map--khenrix-utils-mikado-graph
- rule B6 · agy/user · wrong-tool-fires · confidence low · id `412e122e485e` fp `fd8a4898`
- note: heuristic nomination — adjudicate before acting
- evidence: `{"corpus_size": 11, "cosine": 0.146, "nominated": 5, "same_plugin": true, "shared_phrases": [], "shared_tokens": ["here", "break", "refactor", "big"]}`
- rung: Phase C adjudication → Phase D arena/probes
- rung: rung 1 if DUPLICATE and one side is ours

### [41] b6.skill.khenrix-utils-khenrix-audit--khenrix-utils-khenrix-setup
- rule B6 · agy/user · wrong-tool-fires · confidence low · id `c56fc4f4fc84` fp `e426f2e7`
- note: heuristic nomination — adjudicate before acting
- evidence: `{"corpus_size": 11, "cosine": 0.136, "nominated": 5, "same_plugin": true, "shared_phrases": [], "shared_tokens": ["servers", "mcp"]}`
- rung: Phase C adjudication → Phase D arena/probes
- rung: rung 1 if DUPLICATE and one side is ours

### [41] b6.skill.khenrix-utils-khenrix-wiki-add--khenrix-utils-khenrix-wiki-sync
- rule B6 · agy/user · wrong-tool-fires · confidence low · id `e40e02eef87b` fp `9735452d`
- note: heuristic nomination — adjudicate before acting
- evidence: `{"corpus_size": 11, "cosine": 0.126, "nominated": 5, "same_plugin": true, "shared_phrases": [], "shared_tokens": ["wiki", "says"]}`
- rung: Phase C adjudication → Phase D arena/probes
- rung: rung 1 if DUPLICATE and one side is ours

### [41] b6.skill.khenrix-utils-khenrix-setup--khenrix-utils-khenrix-upgrade
- rule B6 · agy/user · wrong-tool-fires · confidence low · id `63d45e4a5b2e` fp `3a510cb7`
- note: heuristic nomination — adjudicate before acting
- evidence: `{"corpus_size": 11, "cosine": 0.09, "nominated": 5, "same_plugin": true, "shared_phrases": [], "shared_tokens": ["their", "agy"]}`
- rung: Phase C adjudication → Phase D arena/probes
- rung: rung 1 if DUPLICATE and one side is ours

### [33] b4.mcp.claude/google-drive
- rule B4 · claude/user · state-divergence · confidence high · id `081390b020c1` fp `843310c2`
- evidence: `{"direction": "managed-absent-but-live", "reason": "Removed from capabilities.toml 2026-07-20: redundant with the native claude.ai Drive connector. Still live in ~/.claude.json and ~/.codex/config.toml — must keep firing until removed there."}`
- rung: remove from live config (confirmed, restore bundle first)

### [33] b4.mcp.codex/google-drive
- rule B4 · codex/user · state-divergence · confidence high · id `9d35755aea83` fp `843310c2`
- evidence: `{"direction": "managed-absent-but-live", "reason": "Removed from capabilities.toml 2026-07-20: redundant with the native claude.ai Drive connector. Still live in ~/.claude.json and ~/.codex/config.toml — must keep firing until removed there."}`
- rung: remove from live config (confirmed, restore bundle first)

### [33] b4.mcp.agy/google-drive
- rule B4 · agy/user · state-divergence · confidence high · id `8c6fc4e2223f` fp `843310c2`
- evidence: `{"direction": "managed-absent-but-live", "reason": "Removed from capabilities.toml 2026-07-20: redundant with the native claude.ai Drive connector. Still live in ~/.claude.json and ~/.codex/config.toml — must keep firing until removed there."}`
- rung: remove from live config (confirmed, restore bundle first)

### [31] b4.mcp.codex/openaideveloperdocs
- rule B4 · codex/user · state-divergence · confidence low · id `8d305adb9806` fp `45750e7f`
- note: unmanaged extra — reported once, deliberately preserved
- evidence: `{"direction": "unmanaged"}`
- rung: declare it in capabilities.toml, or leave as machine-specific

### [12] b12.skill.superpowers-writing-skills
- rule B12 · claude/user · hygiene · confidence medium · id `477d1c50dd3c` fp `4054f579`
- evidence: `{"issues": ["body 689 lines (>=500)"]}`
- rung: fix frontmatter (repo constraint)

### [11] b8.hook.<settings.json>-stop--security-guidance-stop
- rule B8 · claude/user · hygiene · confidence low · id `cf6ede6145f1` fp `f3f3518c`
- note: shared event slot — usually intentional composition
- evidence: `{"duplicate_body": false, "event": "Stop", "matcher": "*"}`

### [11] b9.plugin.skill-creator
- rule B9 · claude/user · hygiene · confidence low · id `56cf09905aba` fp `8f3d6cf5`
- evidence: `{"issue": "version unknown"}`
- rung: reinstall pinned

### [11] b9.plugin.frontend-design
- rule B9 · claude/user · hygiene · confidence low · id `9521d0158d71` fp `8f3d6cf5`
- evidence: `{"issue": "version unknown"}`
- rung: reinstall pinned

### [11] b9.plugin.code-review
- rule B9 · claude/user · hygiene · confidence low · id `cc8c11071833` fp `8f3d6cf5`
- evidence: `{"issue": "version unknown"}`
- rung: reinstall pinned

### [11] b9.plugin.playwright
- rule B9 · claude/user · hygiene · confidence low · id `2a03d683f8b0` fp `8f3d6cf5`
- evidence: `{"issue": "version unknown"}`
- rung: reinstall pinned

## Waived (collapsed)

