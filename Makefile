# khenrix-utils — thin install targets.
#
# These targets ONLY register the marketplace and install the khenrix-utils
# plugin so the `khenrix-setup` reconcile skill becomes available inside each
# CLI. They write NO machine config themselves — all reconciliation happens
# interactively inside the CLI via the skill, which is non-destructive.

REPO := $(shell pwd)
PY   := python3

.DEFAULT_GOAL := help

.PHONY: workvault-test help render setup-claude setup-codex setup-agy khenrix-refresh refresh verify precommit test core-test fanout-test tier0-host-smoke doctor-test reconcile-defaults-test memory-runtime-test maka-component-test skill-delivery-test audit-test bats-test smoke-llm-fanout benchmark-llm-fanout eval eval-test status defaults-status defaults-status-json defaults-apply clean cli-sources cli-sources-status

EVAL := scripts/eval_harness.py
DOCTOR_TESTS := tests/test_doctor.py
RECONCILE_DEFAULTS_TESTS := tests/test_reconcile_defaults.py tests/test_model_policy.py \
                            tests/test_statusline.py tests/test_statusline_reconcile.py
MEMORY_RUNTIME_TESTS := tests/test_memory_runtime.py tests/test_memory_exchange.py tests/test_memory_ingress.py
MAKA_COMPONENT_TESTS := tests/test_maka_skill_smoke.py \
                        tests/test_maka_native_host.py tests/test_maka_read_aggregation.py \
                        tests/test_maka_colima_mount_migration.py
SKILL_DELIVERY_TESTS := tests/test_skill_delivery.py tests/test_skill_upstreams.py \
                        tests/test_refresh.py
AUDIT_TESTS := tests/test_setup_audit.py
CORE_TESTS := tests/test_checks_secrets.py tests/test_mutate.py \
              tests/test_eval_harness_receipt.py tests/test_eval_receipt_v3.py \
              tests/test_eval_native_consumers.py tests/test_eval_agy_read_policy.py \
              tests/test_eval_claude_scratch_policy.py \
              tests/test_eval_patch_acceptance.py \
              tests/test_render_packaging.py
FANOUT_TESTS := tests/test_fanout_artifacts.py \
                tests/test_fanout_benchmark.py \
                tests/test_fanout_bootstrap_packages.py \
                tests/test_fanout_branch_handover.py \
                tests/test_fanout_cold_recovery.py \
                tests/test_fanout_collaboration.py \
                tests/test_fanout_compiler.py \
                tests/test_fanout_context_view.py \
                tests/test_fanout_controller.py \
                tests/test_fanout_execute.py \
                tests/test_fanout_execute_skill.py \
                tests/test_fanout_lifecycle.py \
                tests/test_fanout_local_authority.py \
                tests/test_fanout_maka_native.py \
                tests/test_fanout_memory.py \
                tests/test_fanout_multirepo_integration.py \
                tests/test_fanout_native_profiles.py \
                tests/test_fanout_native_write_policy.py \
                tests/test_fanout_owner_actions.py \
                tests/test_fanout_owner_reconciliation.py \
                tests/test_fanout_plan_schema.py \
                tests/test_fanout_plan_skill.py \
                tests/test_fanout_process.py \
                tests/test_fanout_profiles.py \
                tests/test_fanout_providers.py \
                tests/test_fanout_recorded_cli.py \
                tests/test_fanout_repo.py \
                tests/test_fanout_runstate.py \
                tests/test_fanout_scheduler.py \
                tests/test_fanout_skills.py \
                tests/test_fanout_smoke.py \
                tests/test_fanout_storage.py \
                tests/test_fanout_targets.py \
                tests/test_fanout_verification.py \
                tests/test_eval_provider.py \
                tests/test_tuneup_fanout.py

# Only these audited executor-gate files may overlap; keep every other member serial.
FANOUT_PARALLEL_TESTS := tests/test_fanout_execute_skill.py tests/test_fanout_execute.py \
                         tests/test_fanout_cold_recovery.py tests/test_fanout_storage.py \
                         tests/test_fanout_memory.py tests/test_fanout_repo.py \
                         tests/test_fanout_targets.py tests/test_fanout_branch_handover.py \
                         tests/test_eval_provider.py tests/test_fanout_verification.py
FANOUT_SERIAL_TESTS := $(filter-out $(FANOUT_PARALLEL_TESTS),$(FANOUT_TESTS))
PARALLEL_PYTEST := uvx --python 3.12.14 --from pytest==9.1.1 \
                   --with pytest-xdist==3.8.0 --with execnet==2.1.2 \
                   pytest -q -n 4 --dist loadfile --max-worker-restart 0

BATS_RUNNER := tests/bats-fallback.sh
BATS_SUITES := tests/test_repo_sweep.bats tests/test_reconcile_apply.bats \
               tests/test_bootstrap_tier0.bats tests/test_bootstrap_machine.bats
TIER0_HOST_SMOKE_TESTS := tests/test_bootstrap_tier0_host_smoke.bats

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

# Guards for reconciliation, source inputs, secrets and receipt issuance belong in
# the deterministic gate alongside the native fanout engine.
verify: render doctor-test reconcile-defaults-test memory-runtime-test maka-component-test skill-delivery-test audit-test bats-test core-test fanout-test workvault-test eval-test ## Validate manifests and deterministic behavior without paid model calls
	$(PY) scripts/render.py --check
	@$(PY) -c "import sys; sys.path.insert(0,'scripts/lib'); import checks; [print('  ⚠',x) for x in checks.receipt_gate(checks.ROOT, advisory=True)]"

precommit: verify ## Commit-boundary gate: rendered output and evaluation evidence
	$(PY) scripts/render.py
	@git diff --quiet -- marketplaces/ || { echo "✗ render drift: regenerate + stage rendered output ('git add marketplaces/')"; exit 1; }
	@$(PY) -c "import sys; sys.path.insert(0,'scripts/lib'); import checks; p=checks.receipt_gate(checks.ROOT, advisory=False); [print('  ✗',x) for x in p]; sys.exit(1 if p else 0)"
	@echo "✅ precommit clean"

test: fanout-test core-test workvault-test ## Native fanout and shared safety/consumer regression tests

core-test: ## Shared secret, reconciliation, packaging and evaluation guards
	$(call RUN_PYTEST,$(CORE_TESTS))

fanout-test: ## Native fanout, recovery, owner, memory and evaluation regression tests
	$(PARALLEL_PYTEST) $(FANOUT_PARALLEL_TESTS)
	$(call RUN_PYTEST,$(FANOUT_SERIAL_TESTS))
	mise -C components/maka exec -- node --test ../../tests/test_fanout_maka_native_session.mjs

smoke-llm-fanout: ## Hermetic cold-recovery smoke; live work uses the public plan/execute commands
	$(PY) scripts/fanout_smoke.py --hermetic

benchmark-llm-fanout: ## Hermetic operation benchmark (no models or paid calls)
	$(PY) scripts/fanout_benchmark.py --hermetic

tier0-host-smoke: ## Optional WSL/Windows PowerShell-to-Chrome integration witness
	bash $(BATS_RUNNER) $(TIER0_HOST_SMOKE_TESTS)

# Wired into `verify` (and so into `precommit`) on purpose. A verifier whose own
# tests nothing ever runs decays into exactly the "claims a capability, never
# checks it" state doctor.py exists to prevent. Hermetic: fake $HOME/$PATH
# fixtures only — it never touches the real clipboard or the network.
doctor-test: ## Behavioural tests for scripts/doctor.py (no token cost)
	$(call RUN_PYTEST,$(DOCTOR_TESTS))

reconcile-defaults-test: ## Portable default and status-line reconciliation tests (no token cost)
	$(call RUN_PYTEST,$(RECONCILE_DEFAULTS_TESTS))

workvault-test: ## Curated vault collection, publication, coverage and recovery tests (no token cost)
	$(PY) -B -m unittest discover -s components/workvault

memory-runtime-test: ## Hermetic portable-memory tests (no network, credentials, or live writes)
	$(call RUN_PYTEST,$(MEMORY_RUNTIME_TESTS))

maka-component-test: ## Credential-free portable Maka component tests
	$(call RUN_PYTEST,$(MAKA_COMPONENT_TESTS))
	mise -C components/maka run maka:test-native

skill-delivery-test: ## Selective skill delivery, upstream provenance, and refresh tests
	$(call RUN_PYTEST,$(SKILL_DELIVERY_TESTS))

# Hermetic engine tests for the khenrix-audit skill — same stance as doctor-test:
# a verifier whose own tests never run decays into a false assurance.
audit-test: ## Hermetic tests for the khenrix-audit engine (no token cost)
	$(call RUN_PYTEST,$(AUDIT_TESTS))

# Wired into `verify` for the same reason doctor-test is: four suites (95 tests)
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
		echo "    not executable, so 95 behavioural tests would silently not run."; \
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

eval-test: ## Hermetic eval-harness logic tests (no token cost)
	$(PY) $(EVAL) --self-test
	$(PY) scripts/lib/checks.py --self-test
	$(PY) scripts/lib/portability.py --self-test
	$(PY) scripts/lib/charts.py --self-test
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

eval-trigger: ## Trigger/near-miss DESCRIPTION eval — SKILL=<name> [MODE=…] (costs tokens)
# The behaviour harness (`eval`) injects a skill body and assumes it already triggered.
# This covers the other axis: given ONLY name+description, would the agent pick it?
	$(PY) scripts/eval_trigger.py --skill $(SKILL) $(if $(MODE),--mode $(MODE),) $(if $(JUDGE),--judge $(JUDGE),)

eval-arena: ## Cross-skill ROUTING eval — SKILLS=a,b[,c] reads evals/<a>/arena.json (costs tokens)
	$(PY) scripts/eval_trigger.py --arena $(SKILLS) $(if $(MODE),--mode $(MODE),) $(if $(JUDGE),--judge $(JUDGE),)

status: ## Show what each CLI currently has vs the source of truth (read-only)
	$(PY) scripts/lib/reconcile.py --status --all

defaults-status: ## Show model/effort drift only; reads no MCP/plugin/skill state
	$(PY) scripts/lib/reconcile.py --status --all --defaults-only

defaults-status-json: ## Machine-readable model/effort drift without observed values
	$(PY) scripts/lib/reconcile.py --defaults-status-json

defaults-apply: ## Align only declared model/effort leaves; preserve all other config
	$(PY) scripts/lib/reconcile.py --apply --update-drift --all --defaults-only

clean: ## Remove rendered skill copies (keeps per-CLI khenrix-setup)
	$(PY) scripts/render.py --clean

# Upstream sources we reason about, pulled fresh. Reading beats inferring; a STALE
# checkout is a confident wrong answer with a plausible citation, so this always fetches.
cli-sources:
	python3 scripts/cli_sources.py

cli-sources-status:
	python3 scripts/cli_sources.py --status
