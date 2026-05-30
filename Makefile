# khenrix-utils — thin install targets.
#
# These targets ONLY register the marketplace and install the khenrix-utils
# plugin so the `khenrix-setup` reconcile skill becomes available inside each
# CLI. They write NO machine config themselves — all reconciliation happens
# interactively inside the CLI via the skill, which is non-destructive.

REPO := $(shell pwd)
PY   := python3

.DEFAULT_GOAL := help

.PHONY: help render setup-claude setup-codex setup-agy khenrix-refresh refresh verify status clean

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
	codex plugin add khenrix-utils
	@echo "✅ Installed. Run the khenrix-setup skill inside Codex to reconcile config."

setup-agy: render ## Install the khenrix plugin into Antigravity (agy)
	agy plugin install $(REPO)/marketplaces/agy/plugins/khenrix-utils
	@echo "✅ Installed. Run the khenrix-setup skill inside agy to reconcile config."

khenrix-refresh: ## Re-render + push the latest plugin/skill/engine into all installed CLIs
	$(PY) scripts/refresh.py

refresh: khenrix-refresh ## Alias for khenrix-refresh

verify: render ## Validate manifests and skills without touching any CLI
	$(PY) scripts/render.py --check

status: ## Show what each CLI currently has vs the source of truth (read-only)
	$(PY) scripts/lib/reconcile.py --status --all

clean: ## Remove rendered skill copies (keeps per-CLI khenrix-setup)
	$(PY) scripts/render.py --clean
