# khenrix-utils — thin install targets.
#
# These targets ONLY register the marketplace and install the khenrix-utils
# plugin so the `khenrix-setup` reconcile skill becomes available inside each
# CLI. They write NO machine config themselves — all reconciliation happens
# interactively inside the CLI via the skill, which is non-destructive.

REPO := $(shell pwd)
PY   := python3

.DEFAULT_GOAL := help

.PHONY: help render setup-claude setup-codex setup-agy khenrix-refresh refresh verify precommit test smoke-llm-council eval eval-test status clean

LLM_COUNCIL := shared/skills/llm-council/scripts/fanout.py
EVAL := scripts/eval_harness.py

help: ## Show this help
	@echo "khenrix-utils — install targets (skills do the real setup):"
	@echo
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "After installing, run the khenrix-setup skill inside the CLI to reconcile config."

render: ## Regenerate per-CLI plugin skills from shared/ + capabilities.toml
	$(PY) scripts/render.py

setup-claude: render ## Install the khenrix marketplace + plugin into Claude Code
	claude plugin marketplace add $(REPO)/marketplaces/claude
	claude plugin install khenrix-utils@khenrix-claude-marketplace
	@echo "✅ Installed. Run /khenrix-setup inside Claude Code to reconcile config."

setup-codex: render ## Install the khenrix marketplace + plugin into Codex
	codex plugin marketplace add $(REPO)/marketplaces/codex
	codex plugin add khenrix-utils@khenrix-codex-marketplace
	@echo "✅ Installed. Run the khenrix-setup skill inside Codex to reconcile config."

setup-agy: render ## Install the khenrix plugin into Antigravity (agy)
	agy plugin install $(REPO)/marketplaces/agy/plugins/khenrix-utils
	@echo "✅ Installed. Run the khenrix-setup skill inside agy to reconcile config."

khenrix-refresh: ## Re-render + push the latest plugin/skill/engine into all installed CLIs
	$(PY) scripts/refresh.py

refresh: khenrix-refresh ## Alias for khenrix-refresh

verify: render ## Validate manifests and skills without touching any CLI
	$(PY) scripts/render.py --check
	@$(PY) -c "import sys; sys.path.insert(0,'scripts/lib'); import checks; [print('  ⚠',x) for x in checks.receipt_gate(checks.ROOT, advisory=True)]"

precommit: verify ## Commit-boundary gate: render in sync + every changed skill has a fresh eval receipt
	$(PY) scripts/render.py
	@git diff --quiet -- marketplaces/ || { echo "✗ render drift: regenerate + stage rendered output ('git add marketplaces/')"; exit 1; }
	@$(PY) -c "import sys; sys.path.insert(0,'scripts/lib'); import checks; p=checks.receipt_gate(checks.ROOT, advisory=False); [print('  ✗',x) for x in p]; sys.exit(1 if p else 0)"
	@echo "✅ precommit clean (render in sync + eval receipts fresh)"

test: ## Run the deterministic llm-council engine self-test (no token cost)
	$(PY) $(LLM_COUNCIL) --self-test

smoke-llm-council: ## Live smoke test of the council vs one real provider (costs tokens, needs auth)
	$(PY) $(LLM_COUNCIL) --smoke --providers claude --timeout 60

eval-test: ## Hermetic eval-harness logic tests (no token cost)
	$(PY) $(EVAL) --self-test
	$(PY) scripts/lib/checks.py --self-test
	$(PY) scripts/lib/reconcile_test.py
	$(PY) scripts/claude_session_stats.py --self-test
	$(PY) scripts/session_report.py --self-test
	$(PY) scripts/eval_trigger.py --self-test
	$(PY) shared/skills/chunk-map/scripts/codebase_stats.py --self-test
	$(PY) shared/skills/mikado-graph/scripts/mikado.py --self-test
	$(PY) shared/skills/skill-tuneup/scripts/tuneup.py --self-test

eval: ## Run the skill-eval harness — SKILL=<name> [PROVIDERS=claude,codex,agy] [MODE=normal|deep] (costs tokens)
	$(PY) $(EVAL) --skill $(SKILL) $(if $(PROVIDERS),--providers $(PROVIDERS),) $(if $(MODE),--mode $(MODE),)

status: ## Show what each CLI currently has vs the source of truth (read-only)
	$(PY) scripts/lib/reconcile.py --status --all

clean: ## Remove rendered skill copies (keeps per-CLI khenrix-setup)
	$(PY) scripts/render.py --clean
