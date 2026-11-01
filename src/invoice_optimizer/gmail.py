"""Local Gmail OAuth connection and the Gmail API send call.

Only ``send_raw`` submits mail, and only the delivery worker calls it, after the
outbox has applied the mandatory test-recipient redirect. Uses Google's documented code flow and discovery endpoints, with PKCE, a
browser-bound one-use state and a private, process-locked local token store.
https://developers.google.com/identity/protocols/oauth2/web-server
https://accounts.google.com/.well-known/openid-configuration
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import base64
import fcntl
import hashlib
import hmac
from http.client import HTTPException
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import time
from urllib import error, parse, request


AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
SCOPES = (SEND_SCOPE, "openid", "email")
SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
# The non-upload messages.send endpoint is for small messages; keep a margin.
MAX_RAW_BYTES = 4 * 1024 * 1024
MAX_CONFIG_BYTES = 32 * 1024
MAX_JSON_BYTES = 64 * 1024
FLOW_SECONDS = 600
HTTP_TIMEOUT = 15

_MESSAGES = {
    "invalid_config": "Upload a valid Google Web application OAuth client JSON with the exact callback URL.",
    "invalid_base_url": "Configure a valid public application URL; HTTP is supported only on localhost.",
    "not_configured": "Configure the Google OAuth client first.",
    "already_connected": "Disconnect the current Gmail account before changing this connection.",
    "not_connected": "Connect a Gmail account first.",
    "invalid_request": "The Gmail connection request is invalid. Start the connection again.",
    "invalid_state": "The Gmail connection could not be matched to this browser. Start again.",
    "expired_state": "The Gmail connection request expired. Start again.",
    "unverified_email": "Google did not confirm a verified email address for this account.",
    "unexpected_email": "The selected Google account does not match the expected email address.",
    "missing_scope": "Google did not grant the required Gmail sending and email identity permissions.",
    "unexpected_scope": "Google returned additional permissions. Review this application's Google grant before reconnecting.",
    "missing_refresh_token": "Google did not grant offline access. Reconnect and grant the requested consent.",
    "invalid_response": "Google returned an invalid connection response. Start again.",
    "google_unavailable": "Google could not be reached. Try again later.",
    "google_rejected": "Google rejected the connection request. Check the OAuth setup and try again.",
    "redirect_rejected": "An unexpected Google endpoint redirect was rejected.",
    "reconnect_required": "Google access expired or was revoked. Reconnect the Gmail account.",
    "storage_error": "The private Gmail connection store could not be accessed safely.",
    "config_changed": "The Google client or callback changed. Disconnect and reconnect Gmail.",
}


class GmailError(ValueError):
    """Only constant codes/messages may cross the API boundary."""
    def __init__(self, code: str):
        self.code = code if code in _MESSAGES else "invalid_response"
        super().__init__(_MESSAGES[self.code])


class GmailSendError(Exception):
    """``definite`` means Google refused the message, so nothing was sent.

    Otherwise (timeout, 5xx, lost connection) the outcome is unknown and the
    outbox must not resubmit automatically.
    """
    def __init__(self, definite: bool, detail: str):
        self.definite = definite
        super().__init__(detail[:500])


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise GmailError("redirect_rejected")


def _json(raw: bytes, limit: int, error_code="invalid_response") -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result
    try:
        if not isinstance(raw, bytes) or len(raw) > limit:
            raise ValueError("size")
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("constant")))
        if not isinstance(result, dict):
            raise ValueError("object")
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise GmailError(error_code) from None


def _opaque(value, maximum=4096):
    return isinstance(value, str) and 0 < len(value) <= maximum and not any(ord(c) < 33 or ord(c) > 126 for c in value)


def _email(value):
    if not isinstance(value, str) or len(value) > 320 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
        raise GmailError("unexpected_email")
    return value.casefold()


def _hash(value: str):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _scope_list(value):
    if not isinstance(value, str) or len(value) > 4096:
        raise GmailError("missing_scope")
    scopes = {"email" if scope == "https://www.googleapis.com/auth/userinfo.email" else scope for scope in value.split()}
    if not set(SCOPES).issubset(scopes):
        raise GmailError("missing_scope")
    if scopes != set(SCOPES):
        raise GmailError("unexpected_scope")
    return sorted(scopes)


def _lifetime(value, maximum=86400):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= maximum or not math.isfinite(value):
        raise GmailError("invalid_response")
    return float(value)


class GmailConnection:
    def __init__(self, settings):
        base = getattr(settings, "public_base_url", "")
        try:
            if not isinstance(base, str) or any(ord(c) < 33 for c in base) or "\\" in base:
                raise ValueError()
            parts = parse.urlsplit(base)
            if not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
                raise ValueError()
            if parts.scheme != "https" and not (parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost", "::1")):
                raise ValueError()
            if parts.port is not None and not 1 <= parts.port <= 65535:
                raise ValueError()
        except ValueError:
            raise GmailError("invalid_base_url") from None
        self.callback_url = base.rstrip("/") + "/api/mail/gmail/callback"
        self.expected_email = getattr(settings, "gmail_expected_email", "") or None
        if self.expected_email is not None:
            self.expected_email = _email(self.expected_email)
        self.directory = Path(settings.data_dir) / "gmail"

    @contextmanager
    def _locked(self):
        descriptor = None
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = self.directory.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                raise GmailError("storage_error")
            os.chmod(self.directory, 0o700)
            descriptor = os.open(self.directory / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise GmailError("storage_error")
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        except OSError:
            raise GmailError("storage_error") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _read(self, name):
        try:
            descriptor = os.open(self.directory / name, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise GmailError("storage_error")
            os.fchmod(stream.fileno(), 0o600)
            return _json(stream.read(MAX_JSON_BYTES + 1), MAX_JSON_BYTES, "storage_error")

    def _write(self, name, value):
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(raw) > MAX_JSON_BYTES:
            raise GmailError("storage_error")
        temporary = self.directory / (".tmp-" + secrets.token_hex(16))
        try:
            descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.directory / name)
            self._sync_directory()
        finally:
            temporary.unlink(missing_ok=True)

    def _remove(self, *names):
        for name in names:
            (self.directory / name).unlink(missing_ok=True)
        self._sync_directory()

    def _sync_directory(self):
        descriptor = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _client(self):
        client = self._read("client.json")
        if client is None:
            raise GmailError("not_configured")
        if client.get("callback_url") != self.callback_url:
            raise GmailError("config_changed")
        return client

    def _status(self):
        client = self._read("client.json")
        tokens = self._read("tokens.json")
        problem = self._read("problem.json") or {}
        reason = "not_configured" if client is None else "not_connected"
        connected = bool(client and tokens)
        if connected:
            reason = "connected"
            if client.get("callback_url") != self.callback_url or tokens.get("client_id") != client.get("client_id"):
                connected, reason = False, "config_changed"
            elif self.expected_email and tokens.get("email") != self.expected_email:
                connected, reason = False, "unexpected_email"
            elif tokens.get("refresh_expires_at") is not None and time.time() >= tokens["refresh_expires_at"]:
                connected, reason = False, "reconnect_required"
        elif client and problem.get("reason") == "reconnect_required":
            reason = "reconnect_required"
        return {"client_configured": client is not None, "connected": connected,
                "email": tokens.get("email") if tokens else None,
                "scopes": [scope for scope in tokens.get("scopes", []) if scope in SCOPES] if tokens else [],
                "callback_url": self.callback_url, "reason": reason}

    def status(self):
        """Local metadata only; never refreshes or returns any credentials."""
        with self._locked():
            return self._status()

    def configure(self, raw_json: bytes):
        document = _json(raw_json, MAX_CONFIG_BYTES, "invalid_config")
        client = document.get("web")
        if not isinstance(client, dict) or "installed" in document:
            raise GmailError("invalid_config")
        client_id, secret = client.get("client_id"), client.get("client_secret")
        if not isinstance(client_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,240}\.apps\.googleusercontent\.com", client_id) or not _opaque(secret):
            raise GmailError("invalid_config")
        redirects = client.get("redirect_uris")
        if not isinstance(redirects, list) or self.callback_url not in redirects or not all(isinstance(uri, str) for uri in redirects):
            raise GmailError("invalid_config")
        allowed_endpoints = {"auth_uri": {AUTH_URL, "https://accounts.google.com/o/oauth2/auth"},
                             "token_uri": {TOKEN_URL},
                             "auth_provider_x509_cert_url": {"https://www.googleapis.com/oauth2/v1/certs"}}
        for key, allowed in allowed_endpoints.items():
            if key in client and (not isinstance(client[key], str) or client[key] not in allowed):
                raise GmailError("invalid_config")
        with self._locked():
            if self._read("tokens.json") is not None:
                raise GmailError("already_connected")
            self._write("client.json", {"client_id": client_id, "client_secret": secret, "callback_url": self.callback_url})
            self._remove("pending.json", "problem.json")
            return self._status()

    def begin(self, session_binding: str, expected_email: str | None = None):
        if not _opaque(session_binding):
            raise GmailError("invalid_request")
        expected = _email(expected_email) if expected_email else self.expected_email
        if self.expected_email and expected != self.expected_email:
            raise GmailError("unexpected_email")
        with self._locked():
            client = self._client()
            if self._read("tokens.json") is not None:
                raise GmailError("already_connected")
            state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
            expires = time.time() + FLOW_SECONDS
            self._write("pending.json", {"state_hash": _hash(state), "binding_hash": _hash(session_binding),
                        "verifier": verifier, "expires_at": expires, "expected_email": expected,
                        "client_id": client["client_id"], "callback_url": self.callback_url})
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
            query = {"client_id": client["client_id"], "redirect_uri": self.callback_url,
                     "response_type": "code", "scope": " ".join(SCOPES), "state": state,
                     "code_challenge": challenge, "code_challenge_method": "S256",
                     "access_type": "offline", "prompt": "consent select_account",
                     "include_granted_scopes": "false"}
            if expected:
                query["login_hint"] = expected
            return {"authorization_url": AUTH_URL + "?" + parse.urlencode(query), "state": state,
                    "expires_at": datetime.fromtimestamp(expires, timezone.utc).isoformat()}

    def _request_json(self, url, *, data=None, access_token=None):
        if url not in (TOKEN_URL, USERINFO_URL):
            raise GmailError("invalid_request")
        headers = {"Accept": "application/json"}
        body = None
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            body = parse.urlencode(data).encode("ascii")
        if access_token is not None:
            headers["Authorization"] = "Bearer " + access_token
        req = request.Request(url, data=body, headers=headers, method="POST" if body is not None else "GET")
        opener = request.build_opener(_NoRedirect())
        try:
            with opener.open(req, timeout=HTTP_TIMEOUT) as response:
                if response.status != 200:
                    raise GmailError("google_rejected")
                return _json(response.read(MAX_JSON_BYTES + 1), MAX_JSON_BYTES)
        except error.HTTPError as exc:
            try:
                try:
                    raw = exc.read(MAX_JSON_BYTES + 1)
                except (HTTPException, OSError):
                    raise GmailError("google_unavailable") from None
            finally:
                exc.close()
            if exc.code in (301, 302, 303, 307, 308):
                raise GmailError("redirect_rejected") from None
            try:
                error_code = _json(raw, MAX_JSON_BYTES).get("error")
            except GmailError:
                error_code = None
            raise GmailError("reconnect_required" if error_code == "invalid_grant" else "google_rejected") from None
        except (error.URLError, HTTPException, TimeoutError, OSError):
            raise GmailError("google_unavailable") from None

    def _token_values(self, response, previous=None):
        if response.get("token_type") != "Bearer" or not _opaque(response.get("access_token"), 16384):
            raise GmailError("invalid_response")
        scopes = _scope_list(response["scope"]) if "scope" in response else (previous["scopes"] if previous else _scope_list(None))
        refresh = response.get("refresh_token", previous.get("refresh_token") if previous else None)
        if not _opaque(refresh, 16384):
            raise GmailError("missing_refresh_token")
        now = time.time()
        values = {"access_token": response["access_token"], "refresh_token": refresh,
                  "scopes": scopes, "expires_at": now + _lifetime(response.get("expires_in"))}
        if "refresh_token_expires_in" in response:
            values["refresh_expires_at"] = now + _lifetime(response["refresh_token_expires_in"], 10 * 366 * 86400)
        elif previous and "refresh_expires_at" in previous:
            values["refresh_expires_at"] = previous["refresh_expires_at"]
        return values

    def finish(self, code: str, state: str, session_binding: str):
        if not _opaque(code, 8192) or not _opaque(state, 256) or not _opaque(session_binding):
            raise GmailError("invalid_request")
        with self._locked():
            pending = self._read("pending.json")
            if not pending or not hmac.compare_digest(pending.get("state_hash", ""), _hash(state)) or not hmac.compare_digest(pending.get("binding_hash", ""), _hash(session_binding)):
                raise GmailError("invalid_state")
            self._remove("pending.json")  # Durable one-use consumption precedes network calls.
            if time.time() >= pending["expires_at"]:
                raise GmailError("expired_state")
            client = self._client()
            if pending["client_id"] != client["client_id"] or pending["callback_url"] != self.callback_url:
                raise GmailError("config_changed")
            response = self._request_json(TOKEN_URL, data={"grant_type": "authorization_code", "code": code,
                        "client_id": client["client_id"], "client_secret": client["client_secret"],
                        "redirect_uri": self.callback_url, "code_verifier": pending["verifier"]})
            tokens = self._token_values(response)
            identity = self._request_json(USERINFO_URL, access_token=tokens["access_token"])
            if identity.get("email_verified") is not True or not _opaque(identity.get("sub"), 255):
                raise GmailError("unverified_email")
            email = _email(identity.get("email"))
            expected = self.expected_email or pending.get("expected_email")
            if expected and email != expected:
                raise GmailError("unexpected_email")
            tokens.update({"email": email, "subject": identity["sub"], "client_id": client["client_id"],
                           "connected_at": datetime.now(timezone.utc).isoformat()})
            self._write("tokens.json", tokens)
            self._remove("problem.json")
            return {"email": email, "scopes": tokens["scopes"], "connected": True}

    def get_access_token(self):
        """Server-side use only; refresh serialization spans the Google request."""
        with self._locked():
            client = self._client()
            tokens = self._read("tokens.json")
            if tokens is None:
                problem = self._read("problem.json") or {}
                raise GmailError("reconnect_required" if problem.get("reason") == "reconnect_required" else "not_connected")
            if tokens.get("client_id") != client["client_id"]:
                raise GmailError("config_changed")
            if self.expected_email and tokens.get("email") != self.expected_email:
                raise GmailError("unexpected_email")
            if tokens.get("refresh_expires_at") is not None and time.time() >= tokens["refresh_expires_at"]:
                self._forget_revoked()
                raise GmailError("reconnect_required")
            if time.time() + 60 < tokens["expires_at"]:
                return tokens["access_token"]
            try:
                response = self._request_json(TOKEN_URL, data={"grant_type": "refresh_token",
                            "refresh_token": tokens["refresh_token"], "client_id": client["client_id"],
                            "client_secret": client["client_secret"]})
                updated = {**tokens, **self._token_values(response, previous=tokens)}
            except GmailError as exc:
                if exc.code == "reconnect_required":
                    self._forget_revoked()
                raise
            self._write("tokens.json", updated)
            return updated["access_token"]

    def send_raw(self, raw: bytes, access_token: str) -> str:
        """Submit one RFC 5322 message; returns Google's message id."""
        if not isinstance(raw, bytes) or not raw:
            raise GmailSendError(True, "Empty message")
        if len(raw) > MAX_RAW_BYTES:
            raise GmailSendError(True, "Message exceeds the Gmail API simple-send size limit")
        if not _opaque(access_token, 16384):
            raise GmailSendError(True, "Missing Gmail access token")
        body = json.dumps({"raw": base64.urlsafe_b64encode(raw).decode("ascii")}).encode("ascii")
        req = request.Request(SEND_URL, data=body, method="POST", headers={
            "Accept": "application/json", "Content-Type": "application/json",
            "Authorization": "Bearer " + access_token})
        opener = request.build_opener(_NoRedirect())
        try:
            with opener.open(req, timeout=HTTP_TIMEOUT) as response:
                payload = response.read(MAX_JSON_BYTES + 1)
                status = response.status
        except error.HTTPError as exc:
            exc.close()
            if 400 <= exc.code < 500 and exc.code not in (408,):
                raise GmailSendError(True, f"Gmail rejected the message (HTTP {exc.code})") from None
            raise GmailSendError(False, f"Gmail returned HTTP {exc.code}; the message may have been accepted") from None
        except GmailError:
            raise GmailSendError(False, "Unexpected Gmail redirect; outcome unknown") from None
        except (error.URLError, HTTPException, TimeoutError, OSError):
            raise GmailSendError(False, "Gmail could not be reached or did not answer; outcome unknown") from None
        if status != 200:
            raise GmailSendError(False, f"Unexpected Gmail status {status}; outcome unknown")
        try:
            message_id = _json(payload, MAX_JSON_BYTES).get("id")
        except GmailError:
            message_id = None
        if not _opaque(message_id, 128):
            raise GmailSendError(False, "Gmail accepted the request without a message id; outcome unknown")
        return message_id

    def _forget_revoked(self):
        self._remove("tokens.json", "pending.json")
        self._write("problem.json", {"reason": "reconnect_required"})

    def disconnect(self):
        """Delete local credentials/state only; does not revoke Google's grant."""
        with self._locked():
            self._remove("tokens.json", "pending.json", "problem.json")
            # A killed process can leave its private pre-replace temporary file.
            # No active writer can own one while this process holds the lock.
            for path in self.directory.glob(".tmp-*"):
                if re.fullmatch(r"\.tmp-[a-f0-9]{32}", path.name):
                    info = path.lstat()
                    if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1:
                        path.unlink()
            self._sync_directory()
            return self._status()
