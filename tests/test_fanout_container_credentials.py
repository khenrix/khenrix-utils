"""Host credential sources stay bound to owner-approved selectors and principals."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.lib.fanout.container_routes import RouteProfile  # noqa: E402
from shared.lib.fanout import container_credentials as credentials  # noqa: E402


def _route(family="openai-keychain"):
    return RouteProfile(
        route_id="codex-org", provider="codex", auth_family=family,
        api_family="openai-responses", origin="https://api.openai.com",
        project="fictional-project", location=None, requested_model="gpt-6.1-sol",
        wire_model="gpt-6.1-sol", effective_model="gpt-6.1-sol",
        credential_ref=("keychain:fixture-account" if family == "openai-keychain"
                        else "op://Automation/fictional/key"),
        max_data_classification="protected", execution_classes=("read-only", "repo-write"),
        transform_version=1,
    )


def _owned(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(0o600)


@pytest.fixture
def owner_root(tmp_path, monkeypatch):
    monkeypatch.setattr(credentials, "_OWNER_HOME", tmp_path)
    monkeypatch.setattr(credentials, "_GCLOUD_SYSTEM_PATHS", (), raising=False)
    _gcloud_executable(tmp_path)
    return tmp_path


def _gcloud_executable(root):
    executable = root / "google-cloud-sdk/bin/gcloud"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_bytes(b"#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    return executable


def _keychain_ready(root, account="fixture-account"):
    selector = root / ".config/khenrix-utils/maka/maka-openai-keychain-account"
    ready = root / ".config/khenrix-utils/maka/maka-api-key-relay-ready"
    raw = (account + "\n").encode()
    _owned(selector, raw)
    _owned(ready, b"maka-api-key-relay-ready-v2\nkeychain-account-sha256="
           + hashlib.sha256(raw).hexdigest().encode() + b"\n")
    return selector, ready


def test_keychain_uses_selected_account_and_only_openai_field(owner_root, monkeypatch):
    _keychain_ready(owner_root)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return b'{"OPENAI_API_KEY":"fake-key-one","api_key":"wrong"}'

    monkeypatch.setattr(credentials, "_run", fake_run)
    source = credentials.HostCredentialSource(_route())
    assert source.load() == "fake-key-one"
    assert calls[0][0] == ["/usr/bin/security", "find-generic-password", "-s",
                           "Codex Auth", "-a", "fixture-account", "-w"]
    assert calls[0][1]["timeout"] == 10
    assert calls[0][1]["env"] == {"PATH": "/usr/bin:/bin", "LANG": "C"}
    first_fingerprint = source.fingerprint
    monkeypatch.setattr(credentials, "_run", lambda argv, **kwargs:
                        b'{"OPENAI_API_KEY":"fake-key-two"}')
    assert source.load() == "fake-key-two"
    assert source.fingerprint == first_fingerprint


@pytest.mark.parametrize("change", ("selector", "ready", "mode", "symlink"))
def test_keychain_drift_refuses_before_lookup(owner_root, monkeypatch, change):
    selector, ready = _keychain_ready(owner_root)
    source = credentials.HostCredentialSource(_route())
    if change == "selector":
        selector.write_text("another-account\n")
    elif change == "ready":
        ready.write_text("unready\n")
    elif change == "mode":
        selector.chmod(0o644)
    else:
        selector.unlink()
        selector.symlink_to(ready)
    monkeypatch.setattr(credentials, "_run", lambda *_args, **_kwargs:
                        pytest.fail("drift reached Keychain"))
    with pytest.raises(credentials.RouteCredentialError):
        source.load()


@pytest.mark.parametrize("payload", (
    b'not json', b'{}', b'{"api_key":"fake-key"}',
    b'{"OPENAI_API_KEY":"fake\nkey"}',
    b'{"OPENAI_API_KEY":"one","OPENAI_API_KEY":"two"}',
    b'{"OPENAI_API_KEY":"\\ud800"}',
))
def test_keychain_rejects_invalid_document_without_secret_in_error(
    owner_root, monkeypatch, payload,
):
    _keychain_ready(owner_root)
    monkeypatch.setattr(credentials, "_run", lambda argv, **kwargs: payload)
    with pytest.raises(credentials.RouteCredentialError) as caught:
        credentials.HostCredentialSource(_route()).load()
    assert "fake-private-error" not in str(caught.value)
    assert "fake-key" not in str(caught.value)


def test_keychain_lookup_failure_has_no_environment_fallback(owner_root, monkeypatch):
    _keychain_ready(owner_root)
    monkeypatch.setenv("OPENAI_API_KEY", "fake-ambient-must-not-be-used")
    monkeypatch.setattr(credentials, "_run", lambda argv, **kwargs:
                        (_ for _ in ()).throw(credentials.RouteCredentialError(
                            "approved key source is unavailable")))
    with pytest.raises(credentials.RouteCredentialError):
        credentials.HostCredentialSource(_route()).load()


@pytest.mark.parametrize("change", ("selector", "ready"))
def test_keychain_change_during_lookup_refuses(owner_root, monkeypatch, change):
    selector, ready = _keychain_ready(owner_root)
    source = credentials.HostCredentialSource(_route())
    def changed_during_lookup(argv, **kwargs):
        _owned(selector if change == "selector" else ready, b"changed\n")
        return b'{"OPENAI_API_KEY":"fake-key"}'

    monkeypatch.setattr(credentials, "_run", changed_during_lookup)
    with pytest.raises(credentials.RouteCredentialError):
        source.load()
    assert source.fingerprint is None


def test_forged_provider_profile_cannot_use_keychain(owner_root, monkeypatch):
    _keychain_ready(owner_root)
    monkeypatch.setattr(credentials, "_run", lambda *_args, **_kwargs:
                        pytest.fail("forged route reached Keychain"))
    with pytest.raises(credentials.RouteCredentialError):
        credentials.HostCredentialSource(replace(_route(), provider="claude"))


def test_onepassword_reads_exact_reference_with_approved_account(owner_root, monkeypatch):
    _owned(owner_root / ".config/khenrix-utils/fanout/op-account", b"fixture-op-account\n")
    observed = []

    def fake_run(argv, **kwargs):
        observed.append(argv)
        return b"fake-op-key\n"

    monkeypatch.setattr(credentials, "_run", fake_run)
    source = credentials.HostCredentialSource(_route("openai-1password"))
    assert source.load() == "fake-op-key"
    assert observed == [["op", "read", "-n", "op://Automation/fictional/key",
                         "--account", "fixture-op-account"]]


def test_onepassword_change_during_lookup_refuses(owner_root, monkeypatch):
    selector = owner_root / ".config/khenrix-utils/fanout/op-account"
    _owned(selector, b"fixture-op-account\n")
    source = credentials.HostCredentialSource(_route("openai-1password"))

    def changed_during_lookup(argv, **kwargs):
        _owned(selector, b"other-op-account\n")
        return b"fake-op-key\n"

    monkeypatch.setattr(credentials, "_run", changed_during_lookup)
    with pytest.raises(credentials.RouteCredentialError):
        source.load()
    assert source.fingerprint is None


def _vertex_route():
    return RouteProfile(
        route_id="claude-vertex", provider="claude", auth_family="vertex-adc",
        api_family="anthropic-vertex-messages",
        origin="https://europe-west1-aiplatform.googleapis.com",
        project="fictional-project", location="europe-west1",
        requested_model="claude-opus-5-5", wire_model="claude-opus-5-5",
        effective_model="claude-opus-5-5", credential_ref="gcloud-adc",
        max_data_classification="protected", execution_classes=("read-only", "repo-write"),
        transform_version=1,
    )


def _vertex_files(root, *, account="fixture@example.invalid", quota="fictional-project"):
    approval = root / ".config/khenrix-utils/fanout/vertex-adc-source.json"
    adc = root / ".config/gcloud/application_default_credentials.json"
    _owned(approval, json.dumps({"account": account, "quota_project": quota,
                                 "source_ref": "gcloud-adc"},
                                sort_keys=True, separators=(",", ":")).encode() + b"\n")
    _owned(adc, json.dumps({"type": "authorized_user", "quota_project_id": quota,
                           "refresh_token": "fake-refresh-never-record"}).encode())
    return approval, adc


class TokenInfoResponse:
    status = 200

    def __init__(self, account="fixture@example.invalid", expires=3600,
                 mime="application/json", encoding=None):
        self.body = json.dumps({"email": account, "expires_in": expires}).encode()
        self.headers = {"content-type": mime, "content-encoding": encoding}

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def read(self, amount):
        result, self.body = self.body[:amount], self.body[amount:]
        return result

    def close(self):
        pass


class TokenInfoConnection:
    made = []

    def __init__(self, host, port, *, timeout, context, response, calls):
        self.host, self.port, self.response, self.calls = host, port, response, calls
        self.sock = None
        self.debuglevel = 99
        self.made.append(self)

    def connect(self):
        self.sock = self

    def settimeout(self, _timeout):
        pass

    def set_debuglevel(self, level):
        self.debuglevel = level

    def request(self, method, path, body=None, headers=None):
        self.calls.append((self.host, method, path, body, headers))

    def getresponse(self):
        return self.response

    def close(self):
        self.sock = None


def _stub_vertex(monkeypatch, *, account="fixture@example.invalid", expires=3600,
                 mime="application/json", encoding=None):
    requests = []
    calls = []
    TokenInfoConnection.made = []
    monkeypatch.setattr(credentials, "_run", lambda argv, **kwargs:
                        (calls.append((argv, kwargs)) or b"fake-adc-token\n"))
    monkeypatch.setattr(credentials.http.client, "HTTPSConnection",
        lambda host, port, *, timeout, context: TokenInfoConnection(
            host, port, timeout=timeout, context=context,
            response=TokenInfoResponse(account, expires, mime, encoding), calls=requests))
    return calls, requests


@pytest.mark.parametrize("family", ("openai-keychain", "openai-1password", "vertex-adc"))
def test_oversized_lookup_output_is_rejected_without_tokeninfo(
    owner_root, monkeypatch, family,
):
    if family == "openai-keychain":
        _keychain_ready(owner_root)
        route = _route()
    elif family == "openai-1password":
        _owned(owner_root / ".config/khenrix-utils/fanout/op-account",
               b"fixture-op-account\n")
        route = _route(family)
    else:
        _vertex_files(owner_root)
        route = _vertex_route()
    original_popen = subprocess.Popen
    children = []

    def fake_popen(_argv, **kwargs):
        child = original_popen(
            [sys.executable, "-c", "import os; os.write(1, b'x' * 70000)"], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(credentials.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(credentials.http.client, "HTTPSConnection",
                        lambda *_args, **_kwargs: pytest.fail("oversized token reached HTTPS"))
    source = credentials.HostCredentialSource(route)
    with pytest.raises(credentials.RouteCredentialError) as caught:
        source.load()
    assert "x" * 100 not in str(caught.value)
    assert source.fingerprint is None
    assert len(children) == 1
    assert children[0].poll() is not None


def test_lookup_capture_has_hard_cap_and_reaps_oversized_child(monkeypatch):
    original_popen = subprocess.Popen
    children = []

    def tracked_popen(argv, **kwargs):
        child = original_popen(argv, **kwargs)
        children.append(child)
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["stderr"] == subprocess.DEVNULL
        assert kwargs["stdout"] == subprocess.PIPE
        return child

    monkeypatch.setattr(credentials.subprocess, "Popen", tracked_popen)
    argv = [sys.executable, "-c", "import os; os.write(1, b'x' * 70000)"]
    with pytest.raises(credentials.RouteCredentialError):
        credentials._run(argv, timeout=2, env={"PATH": "/usr/bin:/bin"})
    assert len(children) == 1
    assert children[0].poll() is not None


def test_lookup_oversize_kills_child_before_deadline(monkeypatch):
    original_popen = subprocess.Popen
    children = []

    def tracked_popen(argv, **kwargs):
        child = original_popen(argv, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(credentials.subprocess, "Popen", tracked_popen)
    argv = [sys.executable, "-c",
            "import os,time; os.write(1, b'x' * 70000); time.sleep(30)"]
    started = time.monotonic()
    with pytest.raises(credentials.RouteCredentialError):
        credentials._run(argv, timeout=2, env={"PATH": "/usr/bin:/bin"})
    assert time.monotonic() - started < 1.5
    assert len(children) == 1
    assert children[0].poll() is not None


def test_lookup_deadline_reaps_silent_child(monkeypatch):
    original_popen = subprocess.Popen
    children = []

    def tracked_popen(argv, **kwargs):
        child = original_popen(argv, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(credentials.subprocess, "Popen", tracked_popen)
    started = time.monotonic()
    with pytest.raises(credentials.RouteCredentialError):
        credentials._run([sys.executable, "-c", "import time; time.sleep(30)"],
                         timeout=0.2, env={"PATH": "/usr/bin:/bin"})
    assert time.monotonic() - started < 1.5
    assert len(children) == 1
    assert children[0].poll() is not None


def test_lookup_failure_suppresses_child_output():
    argv = [sys.executable, "-c",
            "import os,sys; os.write(1, b'fake-private-stdout'); "
            "os.write(2, b'fake-private-stderr'); sys.exit(9)"]
    with pytest.raises(credentials.RouteCredentialError) as caught:
        credentials._run(argv, timeout=2, env={"PATH": "/usr/bin:/bin"})
    assert "fake-private" not in str(caught.value)


def test_vertex_verifies_account_and_expiry_with_bounded_post(owner_root, monkeypatch):
    _vertex_files(owner_root)
    calls, requests = _stub_vertex(monkeypatch)
    source = credentials.HostCredentialSource(_vertex_route())
    assert source.load() == "fake-adc-token"
    assert calls[0][0] == [str(owner_root / "google-cloud-sdk/bin/gcloud"),
                           "auth", "application-default", "print-access-token"]
    assert calls[0][1]["timeout"] == 10
    assert calls[0][1]["env"] == {
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin", "LANG": "C",
        "HOME": str(owner_root), "CLOUDSDK_CORE_DISABLE_PROMPTS": "1",
    }
    assert requests[0][:3] == ("www.googleapis.com", "POST", "/oauth2/v1/tokeninfo")
    assert requests[0][3] == b"access_token=fake-adc-token"
    assert "fake-adc-token" not in requests[0][2]
    assert TokenInfoConnection.made[0].debuglevel == 0
    first_fingerprint = source.fingerprint
    monkeypatch.setattr(credentials, "_run", lambda argv, **kwargs:
                        b"fake-rotated-token\n")
    assert source.load() == "fake-rotated-token"
    assert requests[1][3] == b"access_token=fake-rotated-token"
    assert source.fingerprint == first_fingerprint
    assert "fake-adc-token" not in first_fingerprint


def test_vertex_accepts_documented_decimal_string_expiry(owner_root, monkeypatch):
    _vertex_files(owner_root)
    _stub_vertex(monkeypatch, expires="3568")
    assert credentials.HostCredentialSource(_vertex_route()).load() == "fake-adc-token"


def test_vertex_tokeninfo_absolute_deadline_refuses_trickled_reply(
    owner_root, monkeypatch,
):
    _vertex_files(owner_root)
    _stub_vertex(monkeypatch)
    monkeypatch.setattr(credentials, "_TOKENINFO_DEADLINE_SECONDS", 0.05, raising=False)
    original_read = TokenInfoResponse.read

    def slow_read(response, amount):
        time.sleep(0.08)
        return original_read(response, amount)

    monkeypatch.setattr(TokenInfoResponse, "read", slow_read)
    with pytest.raises(credentials.RouteCredentialError):
        credentials.HostCredentialSource(_vertex_route()).load()


@pytest.mark.parametrize("change", ("account", "quota", "approval", "expired"))
def test_vertex_principal_quota_or_expiry_drift_refuses(owner_root, monkeypatch, change):
    approval, adc = _vertex_files(owner_root)
    source = credentials.HostCredentialSource(_vertex_route())
    _stub_vertex(monkeypatch, account=("other@example.invalid" if change == "account"
                                      else "fixture@example.invalid"),
                 expires=(0 if change == "expired" else 3600))
    if change == "quota":
        _owned(adc, json.dumps({"type": "authorized_user", "quota_project_id": "other-project",
                                "refresh_token": "fake-refresh"}).encode())
    if change == "approval":
        _owned(approval, json.dumps({"account": "other@example.invalid",
                                      "quota_project": "fictional-project",
                                      "source_ref": "gcloud-adc"}).encode())
    with pytest.raises(credentials.RouteCredentialError):
        source.load()


@pytest.mark.parametrize("change", ("quota", "approval"))
def test_vertex_source_change_during_mint_refuses_before_model_send(
    owner_root, monkeypatch, change,
):
    approval, adc = _vertex_files(owner_root)
    _stub_vertex(monkeypatch)

    def changed_during_mint(argv, **kwargs):
        if change == "quota":
            _owned(adc, json.dumps({"type": "authorized_user",
                                    "quota_project_id": "other-project",
                                    "refresh_token": "fake-rotated"}).encode())
        else:
            _owned(approval, json.dumps({"account": "other@example.invalid",
                                         "quota_project": "fictional-project",
                                         "source_ref": "gcloud-adc"}).encode())
        return b"fake-adc-token\n"

    monkeypatch.setattr(credentials, "_run", changed_during_mint)
    with pytest.raises(credentials.RouteCredentialError):
        credentials.HostCredentialSource(_vertex_route()).load()


def test_subscription_has_no_direct_key_source(owner_root, monkeypatch):
    monkeypatch.setattr(credentials, "_run", lambda *_args, **_kwargs:
                        pytest.fail("subscription reached a direct-key lookup"))
    route = replace(_route(), auth_family="codex-subscription",
                    api_family="codex-subscription", project=None,
                    credential_ref="adapter:codex")
    with pytest.raises(credentials.RouteCredentialError):
        credentials.HostCredentialSource(route).load()


@pytest.mark.parametrize("account", ["cli|fixture", "Fixture@Example.invalid", "A0._+|:-"])
def test_keychain_bounded_account_is_exact_list_argv_and_fingerprint(owner_root, monkeypatch, account):
    _keychain_ready(owner_root, account)
    calls = []
    monkeypatch.setattr(credentials, "_run", lambda argv, **kwargs:
                        (calls.append(argv) or b'{"OPENAI_API_KEY":"fake-fixture-key"}'))
    route = replace(_route(), credential_ref="keychain:" + account)
    source = credentials.HostCredentialSource(route)
    assert source.load() == "fake-fixture-key"
    assert calls == [["/usr/bin/security", "find-generic-password", "-s", "Codex Auth",
                      "-a", account, "-w"]]
    expected = json.dumps({"auth_family": "openai-keychain", "source_ref": "keychain:" + account,
                           "principal": account, "project": "fictional-project"},
                          sort_keys=True, separators=(",", ":")).encode() + b"\n"
    assert source.fingerprint == hashlib.sha256(expected).hexdigest()


@pytest.mark.parametrize("defect", ["selector", "ready"])
def test_pipe_keychain_account_requires_exact_selector_and_ready_marker(owner_root, monkeypatch, defect):
    selector, ready = _keychain_ready(owner_root, "cli|fixture")
    if defect == "selector":
        _owned(selector, b"cli|other\n")
    else:
        _owned(ready, b"maka-api-key-relay-ready-v2\nkeychain-account-sha256=" + b"0" * 64 + b"\n")
    monkeypatch.setattr(credentials, "_run", lambda *_args, **_kwargs:
                        pytest.fail("unapproved pipe account reached Keychain"))
    with pytest.raises(credentials.RouteCredentialError, match="selector"):
        credentials.HostCredentialSource(replace(_route(), credential_ref="keychain:cli|fixture"))


@pytest.mark.parametrize("mime", [
    "application/json; charset=UTF-8", "application/json;charset=utf-8",
    'application/json; charset="UTF-8"', "Application/JSON; Charset=UTF-8",
])
def test_vertex_accepts_normal_utf8_json_charset(owner_root, monkeypatch, mime):
    _vertex_files(owner_root)
    _stub_vertex(monkeypatch, mime=mime)
    assert credentials.HostCredentialSource(_vertex_route()).load() == "fake-adc-token"


@pytest.mark.parametrize("mime", [
    None, "text/plain", "application/problem+json", "application/jsonx",
    "application/json; charset=latin1", "application/json; charset=UTF-16",
    "application/json; charset", "application/json; charset=", "application/json;",
    "application/json; charset=UTF-8; charset=UTF-8", "application/json; foo=bar",
    "application/json; charset=UTF-8; foo=bar", 'application/json; charset="UTF-8',
    "application/json; charset=UTF-8\r\n", "application/json, application/json",
    "application/json; " + " " * 129 + "charset=UTF-8",
])
def test_vertex_refuses_unsupported_or_malformed_json_media_type(owner_root, monkeypatch, mime):
    _vertex_files(owner_root)
    _stub_vertex(monkeypatch, mime=mime)
    source = credentials.HostCredentialSource(_vertex_route())
    with pytest.raises(credentials.RouteCredentialError):
        source.load()
    assert source.fingerprint is None


@pytest.mark.parametrize("defect", ["status", "encoding", "oversize", "identity", "expiry"])
def test_utf8_json_charset_does_not_bypass_tokeninfo_checks(owner_root, monkeypatch, defect):
    _vertex_files(owner_root)
    _stub_vertex(monkeypatch, mime="application/json; charset=UTF-8",
                 account="other@example.invalid" if defect == "identity" else "fixture@example.invalid",
                 expires=60 if defect == "expiry" else 3600,
                 encoding="gzip" if defect == "encoding" else None)
    if defect == "status":
        monkeypatch.setattr(TokenInfoResponse, "status", 403)
    elif defect == "oversize":
        monkeypatch.setattr(credentials, "_MAX_TOKENINFO_BYTES", 1)
    with pytest.raises(credentials.RouteCredentialError):
        credentials.HostCredentialSource(_vertex_route()).load()


@pytest.mark.parametrize("defect", ["missing", "directory", "not-executable", "writable-file",
                                     "writable-parent", "symlink", "symlink-parent", "wrong-owner"])
def test_gcloud_resolver_refuses_missing_or_unsafe_fixed_executable(owner_root, monkeypatch, defect):
    executable = owner_root / "google-cloud-sdk/bin/gcloud"
    if defect == "missing":
        executable.unlink()
    elif defect == "directory":
        executable.unlink()
        executable.mkdir()
    elif defect == "not-executable":
        executable.chmod(0o600)
    elif defect == "writable-file":
        executable.chmod(0o720)
    elif defect == "writable-parent":
        executable.parent.chmod(0o777)
    elif defect == "symlink":
        target = executable.with_name("other")
        executable.rename(target)
        executable.symlink_to(target)
    elif defect == "symlink-parent":
        parent = executable.parent
        target = parent.with_name("alternate-bin")
        parent.rename(target)
        parent.symlink_to(target, target_is_directory=True)
    else:
        original_lstat = Path.lstat

        def wrong_owner(path, *args, **kwargs):
            metadata = original_lstat(path, *args, **kwargs)
            if path == executable:
                fields = list(metadata)
                fields[4] = os.getuid() + 12345
                return os.stat_result(fields)
            return metadata

        monkeypatch.setattr(Path, "lstat", wrong_owner)
    with pytest.raises(credentials.RouteCredentialError):
        credentials._resolve_gcloud()


def test_gcloud_ignores_ambient_path_and_does_not_lookup_when_fixed_files_missing(owner_root, monkeypatch):
    _vertex_files(owner_root)
    (owner_root / "google-cloud-sdk/bin/gcloud").unlink()
    ambient = owner_root / "ambient"
    _gcloud_executable(ambient)
    monkeypatch.setenv("PATH", str(ambient / "google-cloud-sdk/bin"))
    monkeypatch.setattr(credentials, "_run", lambda *_args, **_kwargs:
                        pytest.fail("unadmitted ambient executable reached lookup"))
    with pytest.raises(credentials.RouteCredentialError):
        credentials.HostCredentialSource(_vertex_route()).load()


def test_gcloud_resolver_uses_only_admitted_system_fixture(owner_root, monkeypatch):
    (owner_root / "google-cloud-sdk/bin/gcloud").unlink()
    system = _gcloud_executable(owner_root / "fictional-system")
    monkeypatch.setattr(credentials, "_GCLOUD_SYSTEM_PATHS", (system,), raising=False)
    assert credentials._resolve_gcloud() == system


def test_account_sdk_fixture_executes_with_sanitized_environment(owner_root, monkeypatch):
    _vertex_files(owner_root)
    executable = owner_root / "google-cloud-sdk/bin/gcloud"
    executable.write_bytes(b"#!/bin/sh\nprintf '%s\\n' fake-adc-token\n")
    real_run = credentials._run
    _stub_vertex(monkeypatch, mime="application/json; charset=UTF-8")
    monkeypatch.setattr(credentials, "_run", real_run)
    monkeypatch.setenv("OPENAI_API_KEY", "fake-ambient-must-not-be-used")
    source = credentials.HostCredentialSource(_vertex_route())
    assert source.load() == "fake-adc-token"
    assert source.fingerprint is not None
