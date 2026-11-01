"""Offline OAuth boundary tests; every Google request is mocked."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import base64
import hashlib
from http.client import IncompleteRead
import io
import json
import multiprocessing
import os
from pathlib import Path
import stat
import threading
import time
from types import SimpleNamespace
from urllib import error, parse, request

import pytest

from invoice_optimizer import gmail
from invoice_optimizer.gmail import GmailConnection, GmailError, SCOPES, SEND_SCOPE


CLIENT_SECRET = "test-client-secret-never-return"
ACCESS = "test-access-token-never-return"
REFRESH = "test-refresh-token-never-return"
EMAIL = "operator@example.test"
BINDING = "browser-bound-nonce-" + "x" * 32


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("No external network is allowed in Gmail tests")
    monkeypatch.setattr(gmail.request, "build_opener", blocked)


@pytest.fixture
def settings(tmp_path):
    return SimpleNamespace(data_dir=tmp_path / "var", public_base_url="http://127.0.0.1:8000", gmail_expected_email="")


def client_json(connection, **overrides):
    values = {"client_id": "123456-test.apps.googleusercontent.com", "client_secret": CLIENT_SECRET,
              "redirect_uris": [connection.callback_url], "auth_uri": "https://accounts.google.com/o/oauth2/auth",
              "token_uri": gmail.TOKEN_URL, "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs"}
    values.update(overrides)
    return json.dumps({"web": values}).encode()


@pytest.fixture
def connection(settings):
    result = GmailConnection(settings)
    result.configure(client_json(result))
    return result


def token_response(**overrides):
    result = {"access_token": ACCESS, "refresh_token": REFRESH, "token_type": "Bearer",
              "expires_in": 3600, "scope": " ".join(SCOPES)}
    result.update(overrides)
    return result


def identity(**overrides):
    return {"email": EMAIL, "email_verified": True, "sub": "123456789", **overrides}


def mock_exchange(monkeypatch, connection, token=None, user=None):
    calls = []
    def fake(url, **kwargs):
        calls.append((url, kwargs))
        return deepcopy(token_response() if token is None else token) if url == gmail.TOKEN_URL else deepcopy(identity() if user is None else user)
    monkeypatch.setattr(connection, "_request_json", fake)
    return calls


def connect(monkeypatch, connection):
    calls = mock_exchange(monkeypatch, connection)
    flow = connection.begin(BINDING, EMAIL)
    connection.finish("auth-code", flow["state"], BINDING)
    return calls


def expire(connection):
    with connection._locked():
        tokens = connection._read("tokens.json")
        tokens["expires_at"] = time.time() - 1
        connection._write("tokens.json", tokens)


def test_pkce_state_offline_scopes_and_private_metadata(connection, monkeypatch, caplog):
    start = connection.begin(BINDING, EMAIL.upper())
    query = parse.parse_qs(parse.urlsplit(start["authorization_url"]).query)
    pending = json.loads((connection.directory / "pending.json").read_text())
    challenge = base64.urlsafe_b64encode(hashlib.sha256(pending["verifier"].encode()).digest()).decode().rstrip("=")
    assert query["code_challenge"] == [challenge]
    assert query["code_challenge_method"] == ["S256"]
    assert query["access_type"] == ["offline"]
    assert query["include_granted_scopes"] == ["false"]
    assert set(query["scope"][0].split()) == set(SCOPES)
    assert query["login_hint"] == [EMAIL]
    assert query["redirect_uri"] == [connection.callback_url]
    assert len(start["state"]) >= 43 and 43 <= len(pending["verifier"]) <= 128
    assert BINDING not in json.dumps(pending) and start["state"] not in json.dumps(pending)
    assert CLIENT_SECRET not in start["authorization_url"]
    calls = mock_exchange(monkeypatch, connection)
    result = connection.finish("auth-code", start["state"], BINDING)
    assert result == {"connected": True, "email": EMAIL, "scopes": sorted(SCOPES)}
    assert calls[0][1]["data"]["code_verifier"] == pending["verifier"]
    assert calls[0][1]["data"]["redirect_uri"] == connection.callback_url
    assert calls[1] == (gmail.USERINFO_URL, {"access_token": ACCESS})
    assert not (connection.directory / "pending.json").exists()
    assert stat.S_IMODE(connection.directory.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in connection.directory.iterdir())
    status = connection.status()
    assert status["connected"] is True and status["reason"] == "connected"
    assert set(status) == {"client_configured", "connected", "email", "scopes", "callback_url", "reason"}
    for secret in (CLIENT_SECRET, ACCESS, REFRESH, pending["verifier"], BINDING, "auth-code"):
        assert secret not in json.dumps(status) + caplog.text
    assert connection.get_access_token() == ACCESS
    assert len(calls) == 2  # Still-valid access never causes refresh.


@pytest.mark.parametrize("overrides", [
    {"client_id": "attacker.example"}, {"client_id": "x.apps.googleusercontent.com.evil.test"},
    {"client_secret": ""}, {"client_secret": "secret\nheader"},
    {"redirect_uris": ["http://127.0.0.1:8000/other"]}, {"redirect_uris": "not-a-list"},
    {"auth_uri": "https://evil.test/auth"}, {"token_uri": "http://oauth2.googleapis.com/token"},
    {"auth_provider_x509_cert_url": "https://evil.test/certs"},
])
def test_config_rejects_invalid_or_foreign_endpoints(settings, overrides):
    connection = GmailConnection(settings)
    with pytest.raises(GmailError, match="valid Google") as caught:
        connection.configure(client_json(connection, **overrides))
    assert caught.value.code == "invalid_config"
    assert not connection.status()["client_configured"]


@pytest.mark.parametrize("raw", [b"not-json", b"[]", b'{"installed":{}}', b'{"web":{},"web":{}}', b"x" * (gmail.MAX_CONFIG_BYTES + 1)])
def test_config_size_shape_and_duplicate_keys(settings, raw):
    with pytest.raises(GmailError) as caught:
        GmailConnection(settings).configure(raw)
    assert caught.value.code == "invalid_config"


@pytest.mark.parametrize("url", ["", "http://billing.example.test", "ftp://localhost", "https://user:password@example.test", "https://example.test/?redirect=x", "https://example.test/#fragment", "https://example.test\\evil", "https://example.test:99999"])
def test_public_base_url_validation(settings, url):
    settings.public_base_url = url
    with pytest.raises(GmailError) as caught:
        GmailConnection(settings)
    assert caught.value.code == "invalid_base_url"


def test_state_binding_expiry_and_replay(connection, monkeypatch):
    calls = mock_exchange(monkeypatch, connection)
    start = connection.begin(BINDING)
    for state, binding in [("wrong-state", BINDING), (start["state"], "other-browser")]:
        with pytest.raises(GmailError) as caught:
            connection.finish("auth-code", state, binding)
        assert caught.value.code == "invalid_state"
        assert (connection.directory / "pending.json").exists()
    assert not calls
    with connection._locked():
        pending = connection._read("pending.json")
        pending["expires_at"] = time.time()
        connection._write("pending.json", pending)
    with pytest.raises(GmailError) as caught:
        connection.finish("auth-code", start["state"], BINDING)
    assert caught.value.code == "expired_state"
    assert not calls
    assert not (connection.directory / "pending.json").exists()
    start = connection.begin(BINDING)
    connection.finish("auth-code", start["state"], BINDING)
    with pytest.raises(GmailError) as caught:
        connection.finish("auth-code", start["state"], BINDING)
    assert caught.value.code == "invalid_state"
    assert len(calls) == 2


@pytest.mark.parametrize("code,state,binding", [(None, "a", BINDING), ("x", [], BINDING), ("x", "a", ""), ("x\n", "a", BINDING)])
def test_invalid_callback_inputs(connection, code, state, binding):
    with pytest.raises(GmailError) as caught:
        connection.finish(code, state, binding)
    assert caught.value.code == "invalid_request"


@pytest.mark.parametrize("user,expected", [
    (identity(email_verified=False), "unverified_email"), (identity(email_verified="true"), "unverified_email"),
    (identity(email_verified=1), "unverified_email"), (identity(sub=""), "unverified_email"),
    (identity(email="another@example.test"), "unexpected_email"),
])
def test_identity_verification_failure_never_saves_tokens(connection, monkeypatch, user, expected):
    mock_exchange(monkeypatch, connection, user=user)
    start = connection.begin(BINDING, EMAIL)
    with pytest.raises(GmailError) as caught:
        connection.finish("auth-code", start["state"], BINDING)
    assert caught.value.code == expected
    assert not (connection.directory / "tokens.json").exists()
    assert not (connection.directory / "pending.json").exists()


@pytest.mark.parametrize("overrides,expected", [
    ({"scope": "openid email"}, "missing_scope"),
    ({"scope": SEND_SCOPE + ".evil openid email"}, "missing_scope"),
    ({"scope": " ".join(SCOPES) + " profile"}, "unexpected_scope"),
    ({"refresh_token": None}, "missing_refresh_token"),
    ({"access_token": "line\nbreak"}, "invalid_response"),
    ({"token_type": "not-Bearer"}, "invalid_response"),
    ({"expires_in": 0}, "invalid_response"), ({"expires_in": -1}, "invalid_response"),
    ({"expires_in": True}, "invalid_response"), ({"expires_in": float("nan")}, "invalid_response"),
    ({"expires_in": 86401}, "invalid_response"),
    ({"expires_in": 10 ** 1000}, "invalid_response"),
])
def test_invalid_token_permissions_and_expiry(connection, monkeypatch, overrides, expected):
    calls = mock_exchange(monkeypatch, connection, token=token_response(**overrides))
    start = connection.begin(BINDING)
    with pytest.raises(GmailError) as caught:
        connection.finish("auth-code", start["state"], BINDING)
    assert caught.value.code == expected
    assert not connection.status()["connected"] and len(calls) == 1


def test_email_scope_alias_and_configured_expected_email(settings, monkeypatch):
    settings.gmail_expected_email = EMAIL
    connection = GmailConnection(settings)
    connection.configure(client_json(connection))
    with pytest.raises(GmailError) as caught:
        connection.begin(BINDING, "other@example.test")
    assert caught.value.code == "unexpected_email"
    mock_exchange(monkeypatch, connection, token=token_response(scope=SEND_SCOPE + " openid https://www.googleapis.com/auth/userinfo.email"))
    start = connection.begin(BINDING)
    assert connection.finish("auth-code", start["state"], BINDING)["email"] == EMAIL


def test_reconfigure_disconnect_clear_pending_and_keep_client(connection, monkeypatch):
    first = connection.begin(BINDING)
    connection.configure(client_json(connection))
    with pytest.raises(GmailError) as caught:
        connection.finish("auth-code", first["state"], BINDING)
    assert caught.value.code == "invalid_state"
    connect(monkeypatch, connection)
    with pytest.raises(GmailError) as caught:
        connection.configure(client_json(connection))
    assert caught.value.code == "already_connected"
    status = connection.disconnect()
    assert status["client_configured"] and not status["connected"]
    assert status["email"] is None and not (connection.directory / "tokens.json").exists()
    assert sorted(path.name for path in connection.directory.iterdir()) == [".lock", "client.json"]


def test_refresh_retains_refresh_token_and_safe_revocation(connection, monkeypatch):
    connect(monkeypatch, connection)
    expire(connection)
    def refresh(url, **kwargs):
        assert url == gmail.TOKEN_URL and kwargs["data"]["grant_type"] == "refresh_token"
        return {"access_token": "new-access", "token_type": "Bearer", "expires_in": 3600}
    monkeypatch.setattr(connection, "_request_json", refresh)
    assert connection.get_access_token() == "new-access"
    assert json.loads((connection.directory / "tokens.json").read_text())["refresh_token"] == REFRESH
    expire(connection)
    def revoked(*args, **kwargs):
        raise GmailError("reconnect_required")
    monkeypatch.setattr(connection, "_request_json", revoked)
    with pytest.raises(GmailError) as caught:
        connection.get_access_token()
    assert caught.value.code == "reconnect_required"
    assert connection.status()["reason"] == "reconnect_required"
    assert not connection.status()["connected"]
    assert not (connection.directory / "tokens.json").exists()


def test_refresh_expiry_without_network(connection, monkeypatch):
    connect(monkeypatch, connection)
    with connection._locked():
        tokens = connection._read("tokens.json")
        tokens["refresh_expires_at"] = time.time() - 1
        connection._write("tokens.json", tokens)
    assert connection.status()["connected"] is False
    with pytest.raises(GmailError) as caught:
        connection.get_access_token()
    assert caught.value.code == "reconnect_required"


def _process_access(settings, queue):
    try:
        queue.put(("ok", GmailConnection(settings).get_access_token()))
    except Exception as exc:
        queue.put(("error", type(exc).__name__))


def test_refresh_is_serialized_across_processes(settings, connection, monkeypatch):
    connect(monkeypatch, connection)
    expire(connection)
    context = multiprocessing.get_context("fork")
    count = context.Value("i", 0)
    queue = context.Queue()
    def refresh(self, url, **kwargs):
        with count.get_lock():
            count.value += 1
        time.sleep(0.08)
        return {"access_token": "refreshed-once", "token_type": "Bearer", "expires_in": 3600}
    monkeypatch.setattr(GmailConnection, "_request_json", refresh)
    children = [context.Process(target=_process_access, args=(settings, queue)) for _ in range(3)]
    try:
        for child in children:
            child.start()
        answers = [queue.get(timeout=5) for _ in children]
        for child in children:
            child.join(timeout=5)
        assert answers == [("ok", "refreshed-once")] * 3
        assert all(child.exitcode == 0 for child in children)
        assert count.value == 1
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
            child.join(timeout=5)
        queue.close()


def test_concurrent_callback_is_one_use(connection, monkeypatch):
    calls = mock_exchange(monkeypatch, connection)
    flow = connection.begin(BINDING)
    def finish():
        try:
            return connection.finish("auth-code", flow["state"], BINDING)["connected"]
        except GmailError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: finish(), range(2)))
    assert sorted(str(value) for value in results) == ["True", "invalid_state"]
    assert len(calls) == 2


def test_disconnect_waits_for_refresh_then_removes_tokens(connection, settings, monkeypatch):
    connect(monkeypatch, connection)
    expire(connection)
    entered, release = threading.Event(), threading.Event()
    def refresh(url, **kwargs):
        entered.set()
        assert release.wait(timeout=3)
        return {"access_token": "refreshed", "token_type": "Bearer", "expires_in": 3600}
    monkeypatch.setattr(connection, "_request_json", refresh)
    with ThreadPoolExecutor(max_workers=2) as executor:
        get = executor.submit(connection.get_access_token)
        assert entered.wait(timeout=3)
        disconnect = executor.submit(GmailConnection(settings).disconnect)
        release.set()
        assert get.result(timeout=3) == "refreshed"
        assert disconnect.result(timeout=3)["connected"] is False
    assert not (connection.directory / "tokens.json").exists()


class FakeResponse:
    def __init__(self, raw, status=200):
        self.raw, self.status = raw, status
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self, limit): return self.raw[:limit]


def test_http_transport_fixed_urls_timeout_and_authorization(connection, monkeypatch):
    captured = []
    class Opener:
        def open(self, req, timeout):
            captured.append((req, timeout))
            return FakeResponse(b'{"ok":true}')
    def build(handler):
        assert isinstance(handler, gmail._NoRedirect)
        return Opener()
    monkeypatch.setattr(gmail.request, "build_opener", build)
    assert connection._request_json(gmail.TOKEN_URL, data={"secret": "a+b"}) == {"ok": True}
    connection._request_json(gmail.USERINFO_URL, access_token=ACCESS)
    assert captured[0][0].data == b"secret=a%2Bb" and captured[0][0].method == "POST"
    assert captured[1][0].get_header("Authorization") == "Bearer " + ACCESS
    assert all(timeout == gmail.HTTP_TIMEOUT for _, timeout in captured)
    with pytest.raises(GmailError):
        connection._request_json("https://evil.test")
    with pytest.raises(GmailError) as caught:
        gmail._NoRedirect().redirect_request(request.Request(gmail.TOKEN_URL), None, 302, "", {}, "https://evil.test")
    assert caught.value.code == "redirect_rejected"


@pytest.mark.parametrize("body", [b"not-json", b"[]", b'{"a":NaN}', b'{"a":1,"a":2}', b" " * (gmail.MAX_JSON_BYTES + 1)])
def test_http_json_bounded_and_strict(connection, monkeypatch, body):
    monkeypatch.setattr(gmail.request, "build_opener", lambda *args: SimpleNamespace(open=lambda *args, **kwargs: FakeResponse(body)))
    with pytest.raises(GmailError) as caught:
        connection._request_json(gmail.TOKEN_URL, data={})
    assert caught.value.code == "invalid_response"


@pytest.mark.parametrize("upstream,expected", [
    (error.HTTPError(gmail.TOKEN_URL, 400, CLIENT_SECRET, {}, io.BytesIO(b'{"error":"invalid_grant","error_description":"secret-response"}')), "reconnect_required"),
    (error.HTTPError(gmail.TOKEN_URL, 302, CLIENT_SECRET, {}, io.BytesIO(b"secret-response")), "redirect_rejected"),
    (error.HTTPError(gmail.TOKEN_URL, 500, CLIENT_SECRET, {}, io.BytesIO(b"secret-response")), "google_rejected"),
    (error.URLError(CLIENT_SECRET), "google_unavailable"), (TimeoutError(CLIENT_SECRET), "google_unavailable"),
    (IncompleteRead(CLIENT_SECRET.encode()), "google_unavailable"),
])
def test_http_failure_messages_never_expose_credentials(connection, monkeypatch, upstream, expected, caplog):
    def fail(*args, **kwargs):
        raise upstream
    monkeypatch.setattr(gmail.request, "build_opener", lambda *args: SimpleNamespace(open=fail))
    with pytest.raises(GmailError) as caught:
        connection._request_json(gmail.TOKEN_URL, data={"client_secret": CLIENT_SECRET})
    assert caught.value.code == expected
    assert CLIENT_SECRET not in str(caught.value) + caplog.text
    assert "secret-response" not in str(caught.value) + caplog.text


def test_symlink_store_and_files_are_rejected(settings, tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    directory = settings.data_dir / "gmail"
    directory.parent.mkdir()
    directory.symlink_to(target, target_is_directory=True)
    with pytest.raises(GmailError) as caught:
        GmailConnection(settings).status()
    assert caught.value.code == "storage_error"
    directory.unlink()
    directory.mkdir()
    file = target / "secret.json"
    file.write_text('{}')
    (directory / "client.json").symlink_to(file)
    with pytest.raises(GmailError) as caught:
        GmailConnection(settings).status()
    assert caught.value.code == "storage_error"


def test_private_mode_repaired_and_atomic_no_temporary_files(connection):
    os.chmod(connection.directory, 0o755)
    os.chmod(connection.directory / "client.json", 0o644)
    connection.status()
    assert stat.S_IMODE(connection.directory.stat().st_mode) == 0o700
    assert stat.S_IMODE((connection.directory / "client.json").stat().st_mode) == 0o600
    connection.begin(BINDING)
    assert not list(connection.directory.glob(".tmp-*"))


def test_disconnect_removes_orphan_atomic_token_copy(connection):
    temporary = connection.directory / (".tmp-" + "a" * 32)
    temporary.write_text(REFRESH)
    os.chmod(temporary, 0o600)
    connection.disconnect()
    assert not temporary.exists()


def test_interrupted_http_error_body_is_safe(connection, monkeypatch):
    class InterruptedBody(io.BytesIO):
        def read(self, size):
            raise IncompleteRead(REFRESH.encode())
    def fail(*args, **kwargs):
        raise error.HTTPError(gmail.TOKEN_URL, 400, CLIENT_SECRET, {}, InterruptedBody())
    monkeypatch.setattr(gmail.request, "build_opener", lambda *args: SimpleNamespace(open=fail))
    with pytest.raises(GmailError) as caught:
        connection._request_json(gmail.TOKEN_URL, data={})
    assert caught.value.code == "google_unavailable"
    assert REFRESH not in str(caught.value)
