"""The real provider adapter: `runner`'s injected `launch` contract, satisfied for real.

NOTHING IN THIS PACKAGE'S SUITE CALLS A PROVIDER. `run_provider` and the fingerprint probe are
both parameters with real defaults, and every test overrides them. §5.2 prices a real fleet in
provider calls and a suite that spends them is one nobody runs — so the first real invocation
is the skill's own eval, one plan away. What that costs is that NOTHING HERE PROVES THE REAL
PROVIDER PATH WORKS; the signature is kept narrow enough that the shape is obviously the same.

WHY THIS FILE EXISTS AT ALL, beyond convenience: `seat.forge_spec` has had NO PRODUCTION
CALLER since Plan H, so §8.1's validator has never run outside the suite. Whatever wires the
real adapter must wire `forge_spec` with it, or seats run under the council's own validity
policy — a builder seat that worked for forty minutes and signed off in one line would be
RE-RUN on top of itself, which is the defect `_forge_validator` exists to close.

THE PROMPT IS COMPOSED BY `engine.apply_sentinel`, AND THAT IS A SEAM, NOT A STYLE CHOICE.
`fingerprint.without_engine_text` — which is both §8's rationale floor and §11's nonce-stripped
semantic hash — removes `SENTINEL_NOTE.format(token=token)` and then the token, which is
exactly what `apply_sentinel` writes. A launcher that composed the prompt any other way would
make that strip a no-op, and both measurements would silently start counting engine text as
the seat's own.

WHY THE LAUNCHER OWNS THE FINGERPRINT. `runner.run_seat` never sees the prompt: it mints the
token and calls `launch(name=, seat_path=, token=, env=)`. For codex the prompt is
`ProviderSpec.stdin` and for claude and agy it is buried inside `argv`, so recovering "the
prompt" from a spec is per-provider guesswork. The only party that knows it is this one.

`retries=0` BY DEFAULT, EXPLICITLY. `gate.quote` prices one attempt per seat per round and
`run_provider` takes `retries` as a required positional — an inherited default here would
re-price the run without anybody choosing to.
"""
from pathlib import Path

from council import engine

from . import fingerprint, seat as seatmod


class LaunchError(RuntimeError):
    """This provider could not be launched, or answered something that cannot be recorded."""


def make_launcher(*, prompt: str, timeout: int, cfg=None, bundle_sha256=None,
                  retries: int = 0, backoff: float = 0.0,
                  run_provider=engine.run_provider,
                  probe=fingerprint.build):
    """A callable satisfying `runner`'s contract: `launch(*, name, seat_path, token, env)`.

    `env` IS `fleet.forge_child_env`'s scrubbed environment AND IT IS PASSED THROUGH. Accepting
    it and dropping it — which an earlier draft did, with a docstring calling the drop a
    contract nicety — undoes three defences a previous plan paid for, in the one direction
    nothing downstream can see, because the record comes back identical either way:

      1. `LLM_FORGE_DEPTH` is never set. Without a forge guard, a seat that reaches for
         /llm-forge spawns three more write-enabled seats, each of which can spawn three more.
         `LLM_COUNCIL_DEPTH` guards the council and bars nothing about /llm-forge.
      2. `gitcmd.HOSTILE_ENV` is not stripped. An inherited
         `GIT_CONFIG_COUNT=1 / KEY_0=core.hooksPath` re-enables hooks for every git the seat
         runs, undoing the empty `--template=` `clone_seat` spends to keep them out; an ambient
         absolute `GIT_DIR` puts a write-enabled agent on a repository that is not its clone.
      3. `gitcmd.NO_USER_CONFIG` is not pinned.

    `engine.run_provider` grew a keyword-only `env=` for exactly this; it applies the council's
    own depth guard ON TOP of what it is given rather than instead of it.

    The adapter owns the model and the TIMEOUT: §19 forbids a second timeout mechanism, so
    there is deliberately no timeout parameter on the returned callable.

    NO PRODUCTION CALLER YET. Nothing in this plan calls `make_launcher`; `runner.run(...,
    launch=)` is injected and the CLI is a later plan. So `seat.forge_spec`'s "production
    caller" is itself uncalled in production until then, and `bundle_sha256` is `None` for
    every seat a caller does not supply one for.

    WHAT THAT `None` DOES NOT DO, stated because this docstring said the opposite and the
    opposite was measured false: it does NOT make `fingerprint.agreement_label` answer
    `not-comparable`. That label is what a `None` produces only when nothing else DIFFERS, and
    `cli_version` differs across a real fleet — three seats are three different CLIs — so
    `agreement_label` returns `differently-prompted` the moment that compared field differs,
    ahead of any absence. `differently-prompted` is §11's "labelled weaker", which is the
    correct reading of a real fleet; the unhashed bundle is a measurement this run did not
    take, and it is not what decides the label.

    `model_requested` IS NOT PART OF THAT ARGUMENT, and the correction above used to claim it
    was — "three CLIs on three models", which measurement does not support. `spec.model` is
    `build_real_spec`'s reading of `cfg`, and with no `cfg` it is `None` for all three seats
    (measured: claude, codex and agy each answer `None`). `None` beside `None` is an absence
    on both sides, never a difference, so it contributes nothing to the label. It differs only
    when a caller supplies a `cfg` naming a DIFFERENT model per provider — one naming the same
    model for all three makes all three equal (also measured) — and no production caller
    supplies one at all yet, per the paragraph above.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise LaunchError("a seat is launched with a task, and this prompt is empty")
    cfg = cfg or {}

    def launch(*, name, seat_path, token, env):
        seat_prompt = engine.apply_sentinel(prompt, token)
        spec = seatmod.forge_spec(name, seat_prompt, timeout, cfg=cfg,
                                  workdir=Path(seat_path))
        # `spec.sentinel = token` USED TO BE HERE, described as provenance. It was not
        # provenance and it was not anything else: `_forge_validator` hands `evaluate` a
        # `replace(spec, sentinel=None)` copy, which is the only reader of the field
        # (`council/engine.py:1109`), and `run_provider`'s returned record carries no
        # `sentinel` key — so the assignment had no reader at all. The token's real provenance
        # is `runner._record`, which writes it as the attempt's `sentinel`, and §8's
        # `proven_read` reads it separately through `seat.read_proof`.
        spec.cwd = str(seat_path)
        record = run_provider(spec, retries, timeout, backoff, Path(seat_path), env=env)
        if not isinstance(record, dict):
            raise LaunchError(
                f"{name}: a provider record is a mapping, not {type(record).__name__}; §8's "
                "`process` dimension is read off it and there is no third value for "
                "'unreadable'")
        if "prompt_identity" in record:
            raise LaunchError(
                f"{name}: the provider record already carries `prompt_identity`. This adapter "
                "is the only party that knows the prompt, so a second one would mean two "
                "answers to §11's question with nothing saying which was measured.")
        # `model_reported=None`, ALWAYS, AND IT IS A MEASUREMENT RATHER THAN A GAP. This used
        # to read `record.get("model")` through a `_reported` helper whose docstring said it
        # was "the model the PROVIDER named" and that `None` was "honest for codex, whose
        # record carries no model field". Both halves are false: `engine.run_provider` builds
        # its record with `"model": spec.model` (`council/engine.py:1267`) for EVERY provider,
        # which is the model this adapter REQUESTED. So the helper returned either `None` (no
        # `cfg`, so `spec.model` is None) or exactly `model_requested` — never an observation.
        # `fingerprint.PromptIdentity` keeps the two fields apart precisely so a request is not
        # recorded as an observation; filling the second from the first is that collapse
        # performed one field later. No provider envelope this package reads names a model, so
        # nobody measured one, and `None` is §11's only spelling of that.
        pi = probe(prompt=seat_prompt, token=token, cli=name,
                   bundle_sha256=bundle_sha256,
                   model_requested=spec.model,
                   model_reported=None)
        return {**record, "prompt_identity": fingerprint.as_row(pi)}

    return launch
