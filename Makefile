# khenrix-utils — thin install targets.
#
# These targets ONLY register the marketplace and install the khenrix-utils
# plugin so the `khenrix-setup` reconcile skill becomes available inside each
# CLI. They write NO machine config themselves — all reconciliation happens
# interactively inside the CLI via the skill, which is non-destructive.

REPO := $(shell pwd)
PY   := python3

.DEFAULT_GOAL := help

.PHONY: help render setup-claude setup-codex setup-agy khenrix-refresh refresh verify precommit test council-test forge-test-slow doctor-test audit-test bats-test smoke-llm-council smoke-llm-forge eval eval-test status clean cli-sources cli-sources-status

LLM_COUNCIL := shared/skills/llm-council/scripts/fanout.py
EVAL := scripts/eval_harness.py
DOCTOR_TESTS := tests/test_doctor.py
AUDIT_TESTS := tests/test_setup_audit.py
COUNCIL_TESTS := tests/test_council_seat_validity.py tests/test_council_characterization.py \
                 tests/test_council_seams.py tests/test_council_facade.py \
                 tests/test_checks_secrets.py tests/test_mutate.py \
                 tests/test_eval_harness_receipt.py
# The forge suite, split by weight. The fast subset — schema, state machine,
# classification, journal parsing — is in `verify` and therefore in `precommit`. The clone-
# and process-heavy subset is NOT, because `make verify` is the obvious confirmed verify
# command for this very repository: a forge run would then spawn clone fleets four-plus
# times inside its own verifier clones, inflating wall clock and manufacturing FLAKY
# verdicts under contended execution.
FORGE_TESTS := tests/test_forge_storage.py tests/test_forge_inspect.py \
               tests/test_forge_screen.py tests/test_forge_packaging.py \
               tests/test_forge_snapshot.py tests/test_forge_seams.py \
               tests/test_forge_journal.py tests/test_forge_runstate.py \
               tests/test_forge_preflight.py tests/test_forge_gate.py \
               tests/test_forge_seat.py tests/test_forge_taskbundle.py \
               tests/test_forge_ledger.py tests/test_forge_coverage.py \
               tests/test_forge_fingerprint.py tests/test_forge_launch.py \
               tests/test_forge_seatrecord.py tests/test_forge_strategy.py \
               tests/test_forge_progress.py tests/test_forge_rubric.py \
               tests/test_forge_ultra.py tests/test_forge_handover.py \
               tests/test_forge_brief.py tests/test_forge_smoke.py

FORGE_SLOW_TESTS := tests/test_forge_baseline.py tests/test_forge_fleet.py \
                    tests/test_forge_harvest.py tests/test_forge_bundle.py \
                    tests/test_forge_verify.py tests/test_forge_runner.py \
                    tests/test_forge_review.py tests/test_forge_gc.py \
                    tests/test_forge_cli.py tests/test_forge_fix.py
BATS_RUNNER := tests/bats-fallback.sh
BATS_SUITES := tests/test_repo_sweep.bats tests/test_reconcile_apply.bats \
               tests/test_bootstrap_tier0.bats tests/test_bootstrap_machine.bats

# Run a pytest file via whichever runner exists. Failing loudly when neither does is
# deliberate: a green gate must never mean "the suite was skipped" — that is the exact
# defect doctor.py exists to catch, and silently exiting 0 would hide it in our own gate.
define RUN_PYTEST
	@if $(PY) -c "import pytest" >/dev/null 2>&1; then \
		$(PY) -m pytest -q $(1); \
	elif command -v uvx >/dev/null 2>&1; then \
		uvx --with pytest pytest -q $(1); \
	else \
		echo "  ✗ CANNOT RUN $(1) — it needs pytest, which is not importable by"; \
		echo "    '$(PY)', and 'uvx' is not on PATH either."; \
		echo "    Install uv (https://docs.astral.sh/uv/) or 'pip install pytest'."; \
		exit 1; \
	fi
endef

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

# council-test and eval-test are in the gate DELIBERATELY. Without them,
# `make verify` went green on a change that removed mcp_merge's same-name
# collision guard -- i.e. a change that silently overwrites the user's existing
# MCP definition. Verified: guard removed -> verify GREEN, eval-test RED. The
# suites guarding destructive behaviour must be inside the gate, not beside it.
verify: render doctor-test audit-test bats-test council-test eval-test ## Validate manifests and skills without touching any CLI
	$(PY) scripts/render.py --check
	@$(PY) -c "import sys; sys.path.insert(0,'scripts/lib'); import checks; [print('  ⚠',x) for x in checks.receipt_gate(checks.ROOT, advisory=True)]"

# THE SPLIT MOVES NINE SUITES OUT OF `verify`, AND `precommit` IS WHERE THEY LAND. `verify`
# is reached by `precommit` and also by whoever picks a confirmed verify command for THIS
# repository, which is the forge-inside-forge argument above the variables — a forge run
# would spawn clone fleets inside its own verifier clones. `precommit` is neither: nothing
# runs it inside a verifier. Without this dependency `baseline`, `fleet`, `harvest`,
# `bundle`, `verify`, `runner` and `review` sit in no commit-boundary gate at all.
precommit: verify forge-test-slow ## Commit-boundary gate: render in sync + every changed skill has a fresh eval receipt
	$(PY) scripts/render.py
	@git diff --quiet -- marketplaces/ || { echo "✗ render drift: regenerate + stage rendered output ('git add marketplaces/')"; exit 1; }
	@$(PY) -c "import sys; sys.path.insert(0,'scripts/lib'); import checks; p=checks.receipt_gate(checks.ROOT, advisory=False); [print('  ✗',x) for x in p]; sys.exit(1 if p else 0)"
	@echo "✅ precommit clean (render in sync + eval receipts fresh)"

test: council-test forge-test-slow ## Run the deterministic llm-council engine self-test + slow suites (no token cost)
	$(PY) $(LLM_COUNCIL) --self-test
	$(call RUN_PYTEST,-m slow tests/test_council_characterization.py tests/test_council_facade.py)

council-test: ## Council engine (seat/characterization/seam/facade) + fast forge suites, sans slow (no token cost)
	$(call RUN_PYTEST,-m "not slow" $(COUNCIL_TESTS) $(FORGE_TESTS))

# A HEAVY SUITE WITH NO TARGET IS A SUITE THAT ROTS, which is the failure `bats-test`'s own
# comment is about one gate over: the `-m slow` line in `test` names only the two council
# files, so a forge test given that marker would be run by nothing. `make test` runs this,
# the receipt gate runs a subset of it through DETERMINISTIC_GATED, and `make precommit`
# depends on it — three homes, and none of them is `make verify`.
forge-test-slow: ## Clone- and process-heavy forge suites (no token cost, slower)
	$(call RUN_PYTEST,$(FORGE_SLOW_TESTS))

# Wired into `verify` (and so into `precommit`) on purpose. A verifier whose own
# tests nothing ever runs decays into exactly the "claims a capability, never
# checks it" state doctor.py exists to prevent. Hermetic: fake $HOME/$PATH
# fixtures only — it never touches the real clipboard or the network.
doctor-test: ## Behavioural tests for scripts/doctor.py (no token cost)
	$(call RUN_PYTEST,$(DOCTOR_TESTS))

# Hermetic engine tests for the khenrix-audit skill — same stance as doctor-test:
# a verifier whose own tests never run decays into a false assurance.
audit-test: ## Hermetic tests for the khenrix-audit engine (no token cost)
	$(call RUN_PYTEST,$(AUDIT_TESTS))

# Wired into `verify` for the same reason doctor-test is: four suites (84 tests)
# that no target runs are suites that rot, exactly as tests/test_doctor.py
# was about to. ~10s total, no token cost, no network.
#
# A NON-ZERO SKIP COUNT IS A FAILURE HERE. The fallback runner reports skips
# loudly but still exits 0, so a suite whose fixtures all went missing prints
# "35 tests, 0 failures" and a naive gate goes green having verified nothing —
# the vacuous-green defect this whole plan exists to close, reintroduced at the
# make layer. Zero tests run and a missing summary line are failures for the same
# reason. This is the RUN_PYTEST stance: fail loudly rather than exit 0 on
# "could not actually run it".
#
# Parses the FALLBACK runner's summary, not real bats' (different format). bats
# cannot be installed here — see the header of tests/bats-fallback.sh — and the
# fallback runs assertions byte-identical to the ones bats would.
bats-test: ## Behavioural .bats suites: repo-sweep, reconcile --apply, Tier 0/1 (no token cost)
	@test -x $(BATS_RUNNER) || { \
		echo "  ✗ CANNOT RUN the .bats suites — $(BATS_RUNNER) is missing or"; \
		echo "    not executable, so 84 behavioural tests would silently not run."; \
		exit 1; \
	}
	@rc=0; \
	for suite in $(BATS_SUITES); do \
		out=$$(bash $(BATS_RUNNER) $$suite 2>&1); srun=$$?; \
		summary=$$(printf '%s\n' "$$out" | grep -E '^[0-9]+ tests, [0-9]+ failures' | tail -1); \
		ran=$$(printf '%s' "$$summary" | awk '{print $$1}'); \
		skipped=$$(printf '%s' "$$summary" | awk '{print $$5}'); \
		bad=0; \
		[ -n "$$summary" ] || { echo "  ✗ $$suite: no summary line — the runner died before finishing"; bad=1; }; \
		[ "$$srun" -eq 0 ] || { echo "  ✗ $$suite: reported failures"; bad=1; }; \
		[ "$${ran:-0}" -gt 0 ] || { echo "  ✗ $$suite: ran 0 tests"; bad=1; }; \
		[ "$${skipped:-1}" -eq 0 ] || { echo "  ✗ $$suite: SKIPPED $$skipped test(s) — a test that could not run is not a pass"; bad=1; }; \
		if [ "$$bad" -eq 0 ]; then echo "  ✓ $$suite — $$summary"; \
		else printf '%s\n' "$$out" | sed 's/^/    /'; rc=1; fi; \
	done; \
	[ "$$rc" -eq 0 ] || exit 1

smoke-llm-council: ## Live smoke test of the council vs one real provider (costs tokens, needs auth)
	$(PY) $(LLM_COUNCIL) --smoke --providers claude --timeout 60

# §18's live three-provider WRITE smoke. THE DELIBERATE MONEY EXCEPTION, and it is opt-in for
# that reason: it appears in no gate target, because `verify` and `precommit` run on every
# commit and must stay free. `smoke-llm-council` above is one provider and read-only, which
# proves nothing about three write-enabled seats — and `launch.py` and `runner.py` both say
# in words that nothing in the forge suite invokes a real provider.
# Three provider calls, roughly 15% of one default forge run.
smoke-llm-forge: ## Live 3-provider WRITE smoke through the real forge adapter (costs tokens, needs auth)
	$(PY) scripts/forge_smoke.py

eval-test: ## Hermetic eval-harness logic tests (no token cost)
	$(PY) $(EVAL) --self-test
	$(PY) scripts/lib/checks.py --self-test
	$(PY) scripts/lib/portability.py --self-test
	$(PY) scripts/cli_sources.py --self-test
	$(PY) scripts/lib/reconcile_test.py
	$(PY) scripts/claude_session_stats.py --self-test
	$(PY) scripts/session_report.py --self-test
	$(PY) scripts/eval_trigger.py --self-test
	$(PY) shared/skills/chunk-map/scripts/codebase_stats.py --self-test
	$(PY) shared/skills/mikado-graph/scripts/mikado.py --self-test
	$(PY) shared/skills/skill-tuneup/scripts/tuneup.py --self-test
	$(PY) scripts/env_inventory.py --self-test
	$(PY) scripts/lib/mcp_merge.py --self-test

eval: ## Run the skill-eval harness — SKILL=<name> [PROVIDERS=…] [MODE=normal|deep] [TIMEOUT=secs] [RETRIES=n] (costs tokens)
# TIMEOUT raises the per-attempt cap without MODE=deep, which would also change reasoning
# depth. Heavy eval prompts (a full skill body + a research-shaped task) can exceed the
# 300s normal-mode default even with nothing else running — that is an under-timed eval,
# not contention, and the harness now fails closed on it instead of scoring it 0.
	$(PY) $(EVAL) --skill $(SKILL) $(if $(PROVIDERS),--providers $(PROVIDERS),) $(if $(MODE),--mode $(MODE),) $(if $(TIMEOUT),--timeout $(TIMEOUT),) $(if $(RETRIES),--retries $(RETRIES),) $(if $(MODELCLAUDE),--model-claude "$(MODELCLAUDE)",) $(if $(MODELCODEX),--model-codex "$(MODELCODEX)",) $(if $(MODELAGY),--model-agy "$(MODELAGY)",)

status: ## Show what each CLI currently has vs the source of truth (read-only)
	$(PY) scripts/lib/reconcile.py --status --all

clean: ## Remove rendered skill copies (keeps per-CLI khenrix-setup)
	$(PY) scripts/render.py --clean

# Upstream sources we reason about, pulled fresh. Reading beats inferring; a STALE
# checkout is a confident wrong answer with a plausible citation, so this always fetches.
cli-sources:
	python3 scripts/cli_sources.py

cli-sources-status:
	python3 scripts/cli_sources.py --status
