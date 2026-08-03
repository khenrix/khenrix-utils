"""The real provider adapter — wired to §8.1's validator, and never invoked by this suite."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

import hashlib  # noqa: E402
import pytest  # noqa: E402
from council import engine  # noqa: E402
from forge import fingerprint, launch, seat  # noqa: E402


def _fake_provider(seen):
    # The signature MIRRORS `engine.run_provider`, `env=` included. A fake that omitted it
    # would let a launcher that drops `env` pass this whole file.
    def run_provider(spec, retries, timeout, backoff, workdir, *, env=None):
        seen.append(spec)
        return {"status": "ok", "reason": "", "valid": True, "structured": True,
                "exit_code": 0, "duration_sec": 1.0, "attempts": 1,
                "result_text": "done"}
    return run_provider


def _probe(**kw):
    def build(**called):
        called.update(kw)
        return fingerprint.PromptIdentity(
            *fingerprint.prompt_hashes(called["prompt"], called["token"]),
            called.get("bundle_sha256"), "/usr/bin/x", "1.0",
            called.get("model_requested"), called.get("model_reported"), "d" * 64)
    return build


def test_the_launcher_runs_the_forge_validator_not_the_councils(tmp_path):
    """§8.1's validator has had NO production caller since Plan H. Whatever wires the real
    adapter must wire `forge_spec` with it, or seats run under council's own validity policy
    — the defect that module exists to close."""
    seen = []
    fn = launch.make_launcher(prompt="do it", timeout=60,
                              run_provider=_fake_provider(seen), probe=_probe())
    fn(name="claude", seat_path=tmp_path, token="SENTINEL-abc", env={})
    assert seen[0].validator is seat._forge_validator
    assert seen[0].min_chars == 0


def test_the_seat_runs_from_its_own_clone(tmp_path):
    """§18's 'no seat launches with cwd=None' is satisfiable only if somebody sets it."""
    seen = []
    fn = launch.make_launcher(prompt="do it", timeout=60,
                              run_provider=_fake_provider(seen), probe=_probe())
    fn(name="claude", seat_path=tmp_path, token="SENTINEL-abc", env={})
    assert seen[0].cwd == str(tmp_path)


def test_the_prompt_is_composed_by_apply_sentinel(tmp_path):
    """THE CROSS-MODULE SEAM: `fingerprint.without_engine_text` strips exactly
    `apply_sentinel`'s output. A launcher composing the prompt any other way makes the strip a
    no-op, and §8's rationale floor and §11's semantic hash both start measuring engine text."""
    seen = []
    fn = launch.make_launcher(prompt="do it", timeout=60,
                              run_provider=_fake_provider(seen), probe=_probe())
    r = fn(name="claude", seat_path=tmp_path, token="SENTINEL-abc", env={})
    expected = engine.apply_sentinel("do it", "SENTINEL-abc")
    assert r["prompt_identity"]["prompt_sha256"] == \
        hashlib.sha256(expected.encode()).hexdigest()
    assert fingerprint.without_engine_text(expected, "SENTINEL-abc").strip() == "do it"


def test_the_prompt_the_provider_is_GIVEN_is_the_one_that_was_fingerprinted(tmp_path):
    """NON-VACUITY for the test above, which reads the fingerprint on both sides and so is
    green for a launcher that hashes `apply_sentinel`'s output and hands the provider
    something else. §11's whole claim is about the text the CLI actually received."""
    seen = []
    fn = launch.make_launcher(prompt="do it", timeout=60,
                              run_provider=_fake_provider(seen), probe=_probe())
    r = fn(name="codex", seat_path=tmp_path, token="SENTINEL-abc", env={})
    # codex takes its prompt on stdin, which is the one provider where the text is readable
    # off the spec without per-provider argv guesswork.
    assert seen[0].stdin == engine.apply_sentinel("do it", "SENTINEL-abc")
    assert r["prompt_identity"]["prompt_sha256"] == \
        hashlib.sha256(seen[0].stdin.encode()).hexdigest()


def test_the_record_carries_the_fingerprint_beside_the_provider_record(tmp_path):
    fn = launch.make_launcher(prompt="do it", timeout=60, bundle_sha256="c" * 64,
                              run_provider=_fake_provider([]), probe=_probe())
    r = fn(name="claude", seat_path=tmp_path, token="SENTINEL-abc", env={})
    assert r["valid"] is True and r["result_text"] == "done"
    assert fingerprint.from_row(r["prompt_identity"]).bundle_sha256 == "c" * 64


def test_the_launcher_never_overwrites_a_provider_field(tmp_path):
    """A provider that one day returns its own `prompt_identity` must not be silently
    replaced — nor silently win."""
    def run_provider(spec, retries, timeout, backoff, workdir, *, env=None):
        return {"valid": True, "result_text": "x", "prompt_identity": {"rogue": 1}}
    fn = launch.make_launcher(prompt="p", timeout=60,
                              run_provider=run_provider, probe=_probe())
    with pytest.raises(launch.LaunchError, match="prompt_identity"):
        fn(name="claude", seat_path=tmp_path, token="SENTINEL-abc", env={})


def test_retries_default_to_zero_because_the_gate_priced_them_that_way(tmp_path):
    seen = {}

    def run_provider(spec, retries, timeout, backoff, workdir, *, env=None):
        seen["retries"] = retries
        return {"valid": True, "result_text": "x"}
    launch.make_launcher(prompt="p", timeout=60, run_provider=run_provider,
                         probe=_probe())(name="claude", seat_path=tmp_path,
                                         token="SENTINEL-abc", env={})
    assert seen["retries"] == 0


def test_a_provider_record_that_is_not_a_mapping_is_refused(tmp_path):
    def run_provider(spec, retries, timeout, backoff, workdir, *, env=None):
        return "ok"
    fn = launch.make_launcher(prompt="p", timeout=60, run_provider=run_provider,
                              probe=_probe())
    with pytest.raises(launch.LaunchError, match="mapping"):
        fn(name="claude", seat_path=tmp_path, token="SENTINEL-abc", env={})


def test_an_empty_task_is_refused_when_the_launcher_is_built(tmp_path):
    """Before a clone exists and before anything is priced. A seat launched with no task
    would spend a provider call to be told nothing."""
    with pytest.raises(launch.LaunchError, match="empty"):
        launch.make_launcher(prompt="   ", timeout=60,
                             run_provider=_fake_provider([]), probe=_probe())


def test_the_scrubbed_environment_reaches_the_provider(tmp_path):
    """`fleet.forge_child_env` sets LLM_FORGE_DEPTH, strips `gitcmd.HOSTILE_ENV` and pins
    `NO_USER_CONFIG`. A launcher that ACCEPTED `env` and dropped it would undo all three for
    every real seat, and nothing downstream could see it: the record would look identical."""
    seen = {}

    def run_provider(spec, retries, timeout, backoff, workdir, *, env=None):
        seen["env"] = env
        return {"valid": True, "result_text": "x"}
    scrubbed = {"LLM_FORGE_DEPTH": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}
    launch.make_launcher(prompt="p", timeout=60, run_provider=run_provider,
                         probe=_probe())(name="claude", seat_path=tmp_path,
                                         token="SENTINEL-abc", env=scrubbed)
    assert seen["env"] == scrubbed, \
        "the seat's scrubbed environment is what the provider must run under"


def test_the_launcher_calls_a_provider_that_can_take_an_environment(tmp_path):
    """NON-VACUITY for the test above, and the reason the council seam exists: the real
    `engine.run_provider` must ACCEPT `env=`. Without this, a fake with `env=` in its
    signature would keep the suite green over a production call that raises TypeError.

    That the real one also USES it is pinned one module over, in
    `tests/test_council_seams.py`, against `run_member` rather than against a signature —
    accepting a parameter and dropping it is the same defect this file's own launcher is
    tested for, and a signature check cannot see it.
    """
    import inspect as _inspect  # noqa: PLC0415
    from council import engine as eng  # noqa: PLC0415
    assert "env" in _inspect.signature(eng.run_provider).parameters, \
        "the council-engine seam is not in place"


def test_the_model_the_provider_named_is_recorded_apart_from_the_one_requested(tmp_path):
    """§11 keeps `model_requested` and `model_reported` apart because collapsing them records
    a request as an observation. `None` is honest for a provider whose envelope names none."""
    seen = []

    def run_provider(spec, retries, timeout, backoff, workdir, *, env=None):
        seen.append(spec)
        return {"valid": True, "result_text": "x", "model": "opus-5-actual"}
    fn = launch.make_launcher(prompt="p", timeout=60,
                              cfg={"claude": {"model": "opus-5-asked"}},
                              run_provider=run_provider, probe=_probe())
    pi = fingerprint.from_row(
        fn(name="claude", seat_path=tmp_path, token="SENTINEL-abc", env={})["prompt_identity"])
    assert pi.model_requested == "opus-5-asked" and pi.model_reported == "opus-5-actual"
    assert seen[0].model == "opus-5-asked", "and the cfg reached the real spec builder"
