"""Comprehensive tests for all server.py routes."""
import io
import json
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import server
from server import app, _enc


@pytest.fixture
def client():
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "test-secret"
    with app.test_client() as c:
        yield c


# ── GET / ──────────────────────────────────────────────────────────────────────

def test_index_returns_html(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Finance Manager" in resp.data


# ── GET /api/status ────────────────────────────────────────────────────────────

def test_status_empty_session(client):
    resp = client.get("/api/status")
    data = resp.get_json()
    assert data["monarch_session"] is False
    assert data["has_csv"] is False
    assert data["google_token"] is False
    assert data["spreadsheet_id"] == ""
    assert data["monarch_email"] == ""


def test_status_reflects_session(client):
    with client.session_transaction() as sess:
        sess["monarch_email"] = "test@example.com"
        sess["monarch_session"] = _enc("fake-session")
        sess["spreadsheet_id"] = "sheet123"
    resp = client.get("/api/status")
    data = resp.get_json()
    assert data["monarch_email"] == "test@example.com"
    assert data["monarch_session"] is True
    assert data["spreadsheet_id"] == "sheet123"


# ── POST /api/monarch/login ────────────────────────────────────────────────────

def test_monarch_login_empty_credentials(client):
    resp = client.post("/api/monarch/login",
                       json={"email": "", "password": ""})
    data = resp.get_json()
    assert data["status"] == "error"
    assert "required" in data["message"].lower()


def test_monarch_login_missing_password(client):
    resp = client.post("/api/monarch/login",
                       json={"email": "user@example.com", "password": ""})
    data = resp.get_json()
    assert data["status"] == "error"


def test_monarch_login_bad_credentials(client):
    mock_mm = MagicMock()
    mock_mm.login = AsyncMock(side_effect=Exception("Invalid credentials"))
    with patch("monarchmoney.MonarchMoney", return_value=mock_mm):
        resp = client.post("/api/monarch/login",
                           json={"email": "user@example.com", "password": "badpass"})
    data = resp.get_json()
    assert data["status"] == "error"
    assert "Invalid credentials" in data["message"]


def test_monarch_login_mfa_required(client):
    from monarchmoney import RequireMFAException
    mock_mm = MagicMock()
    mock_mm.login = AsyncMock(side_effect=RequireMFAException())
    with patch("monarchmoney.MonarchMoney", return_value=mock_mm):
        resp = client.post("/api/monarch/login",
                           json={"email": "user@example.com", "password": "pass"})
    data = resp.get_json()
    assert data["status"] == "mfa_required"


def test_monarch_login_success_stores_session(client):
    mock_mm = MagicMock()
    mock_mm.login = AsyncMock(return_value=None)
    with patch("monarchmoney.MonarchMoney", return_value=mock_mm), \
         patch("server._get_monarch_session_bytes", return_value="base64session"):
        resp = client.post("/api/monarch/login",
                           json={"email": "user@example.com", "password": "pass"})
    data = resp.get_json()
    assert data["status"] == "ok"
    with client.session_transaction() as sess:
        assert sess.get("monarch_email") == "user@example.com"
        assert sess.get("monarch_session")


def test_monarch_login_with_mfa_upfront_calls_multi_factor_authenticate(client):
    """When the client sends MFA with the initial login, the server should skip
    mm.login() and call mm.multi_factor_authenticate() directly."""
    mock_mm = MagicMock()
    mock_mm.login = AsyncMock(return_value=None)
    mock_mm.multi_factor_authenticate = AsyncMock(return_value=None)
    with patch("monarchmoney.MonarchMoney", return_value=mock_mm), \
         patch("server._get_monarch_session_bytes", return_value="base64session"):
        resp = client.post("/api/monarch/login",
                           json={"email": "user@example.com", "password": "pass", "mfa": "123456"})
    data = resp.get_json()
    assert data["status"] == "ok"
    mock_mm.multi_factor_authenticate.assert_awaited_once_with("user@example.com", "pass", "123456")
    mock_mm.login.assert_not_awaited()


def test_monarch_login_with_mfa_upfront_error(client):
    """MFA failures in the upfront path return an error, not a 500."""
    mock_mm = MagicMock()
    mock_mm.multi_factor_authenticate = AsyncMock(side_effect=Exception("Invalid MFA code"))
    with patch("monarchmoney.MonarchMoney", return_value=mock_mm):
        resp = client.post("/api/monarch/login",
                           json={"email": "user@example.com", "password": "pass", "mfa": "000000"})
    data = resp.get_json()
    assert data["status"] == "error"
    assert "Invalid MFA code" in data["message"]


# ── POST /api/monarch/validate ─────────────────────────────────────────────────

def test_monarch_validate_no_session(client):
    resp = client.post("/api/monarch/validate")
    data = resp.get_json()
    assert data["status"] == "error"
    assert "No session" in data["message"]


def test_monarch_validate_expired_session_clears_cookie(client):
    with client.session_transaction() as sess:
        sess["monarch_session"] = _enc("dGVzdA==")  # base64 "test"
        sess["monarch_email"] = "user@example.com"
    mock_mm = MagicMock()
    mock_mm.get_accounts = AsyncMock(side_effect=Exception("Session expired"))
    with patch("monarchmoney.MonarchMoney", return_value=mock_mm), \
         patch("tempfile.NamedTemporaryFile"), \
         patch("os.unlink"):
        # Patch load_session to be a no-op
        mock_mm.load_session = MagicMock()
        resp = client.post("/api/monarch/validate")
    data = resp.get_json()
    assert data["status"] == "error"
    with client.session_transaction() as sess:
        assert "monarch_session" not in sess
        assert "monarch_email" not in sess


# ── POST /api/monarch/mode ─────────────────────────────────────────────────────

def test_monarch_mode_invalid(client):
    resp = client.post("/api/monarch/mode", json={"mode": "invalid"})
    data = resp.get_json()
    assert data["status"] == "error"
    assert "mode must be" in data["message"]


def test_monarch_mode_csv(client):
    resp = client.post("/api/monarch/mode", json={"mode": "csv"})
    data = resp.get_json()
    assert data["status"] == "ok"
    with client.session_transaction() as sess:
        assert sess["monarch_mode"] == "csv"


def test_monarch_mode_login(client):
    resp = client.post("/api/monarch/mode", json={"mode": "login"})
    data = resp.get_json()
    assert data["status"] == "ok"
    with client.session_transaction() as sess:
        assert sess["monarch_mode"] == "login"


# ── POST /api/sheets/save ──────────────────────────────────────────────────────

def test_sheets_save_empty_id(client):
    resp = client.post("/api/sheets/save", json={"spreadsheet_id": ""})
    data = resp.get_json()
    assert data["status"] == "error"
    assert "required" in data["message"].lower()


def test_sheets_save_valid_id(client):
    resp = client.post("/api/sheets/save", json={"spreadsheet_id": "abc123"})
    data = resp.get_json()
    assert data["status"] == "ok"
    with client.session_transaction() as sess:
        assert sess["spreadsheet_id"] == "abc123"


# ── POST /api/sheets/create ────────────────────────────────────────────────────

def test_sheets_create_no_google_auth(client):
    resp = client.post("/api/sheets/create")
    data = resp.get_json()
    assert data["status"] == "error"
    assert "Not authenticated" in data["message"]


def test_sheets_create_success(client):
    with client.session_transaction() as sess:
        sess["google_token"] = _enc('{"token": "fake"}')
    mock_sheet = MagicMock()
    mock_sheet.id = "new_sheet_id"
    mock_gc = MagicMock()
    mock_gc.create.return_value = mock_sheet
    mock_creds = MagicMock()
    mock_creds.to_json.return_value = '{"token": "refreshed"}'
    with patch("gspread.authorize", return_value=mock_gc), \
         patch("manager.login.Google.get_credentials", return_value=mock_creds):
        resp = client.post("/api/sheets/create")
    data = resp.get_json()
    assert data["status"] == "ok"
    assert data["spreadsheet_id"] == "new_sheet_id"
    with client.session_transaction() as sess:
        assert sess["spreadsheet_id"] == "new_sheet_id"


# ── POST /api/google/auth ──────────────────────────────────────────────────────

def test_google_auth_no_credentials_no_file(client, tmp_path, monkeypatch):
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    # Patch MANAGER_DIR so credentials.json is not found
    monkeypatch.setattr(server, "MANAGER_DIR", tmp_path)
    resp = client.post("/api/google/auth")
    data = resp.get_json()
    assert data["status"] == "error"
    assert "GOOGLE_CLIENT_ID" in data["message"] or "credentials.json" in data["message"]


def test_google_auth_cloud_returns_redirect(client, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client_id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client_secret")
    mock_flow = MagicMock()
    mock_flow.authorization_url.return_value = ("https://accounts.google.com/auth", "state123")
    with patch("google_auth_oauthlib.flow.Flow.from_client_config", return_value=mock_flow):
        resp = client.post("/api/google/auth")
    data = resp.get_json()
    assert data["status"] == "redirect"
    assert "url" in data


# ── GET /api/google/auth/status ───────────────────────────────────────────────

def test_google_auth_status_not_done(client):
    server._local_oauth_result.update({"token_json": None, "error": None, "done": False})
    resp = client.get("/api/google/auth/status")
    data = resp.get_json()
    assert data["done"] is False
    assert data["error"] is None


def test_google_auth_status_done_with_token(client):
    server._local_oauth_result.update({"token_json": '{"token":"abc"}', "error": None, "done": True})
    resp = client.get("/api/google/auth/status")
    data = resp.get_json()
    assert data["done"] is True
    assert data["error"] is None
    with client.session_transaction() as sess:
        assert sess.get("google_token")
    # Result should be reset after consumption
    assert server._local_oauth_result["done"] is False


def test_google_auth_status_done_with_error(client):
    server._local_oauth_result.update({"token_json": None, "error": "Access denied", "done": True})
    resp = client.get("/api/google/auth/status")
    data = resp.get_json()
    assert data["done"] is True
    assert data["error"] == "Access denied"


# ── GET /api/google/callback ───────────────────────────────────────────────────

def test_google_callback_missing_state(client):
    resp = client.get("/api/google/callback?code=authcode&state=xyz")
    assert resp.status_code == 400


# ── GET /api/run ───────────────────────────────────────────────────────────────

def test_api_run_streams_sse(client, tmp_path):
    """api/run should return an SSE stream with at least one data event."""
    mock_proc = MagicMock()
    mock_proc.stdout = io.StringIO("All done\n")
    mock_proc.returncode = 0
    mock_proc.wait = MagicMock()
    with patch("subprocess.Popen", return_value=mock_proc):
        resp = client.get("/api/run")
    assert "text/event-stream" in resp.content_type


def test_api_run_subprocess_failure_streams_error(client):
    """If subprocess fails to start, SSE stream should contain an error event."""
    with patch("subprocess.Popen", side_effect=OSError("not found")):
        resp = client.get("/api/run")
        data = resp.data.decode()
    assert "error" in data
    assert "Failed to start process" in data


# ── POST /api/stop ─────────────────────────────────────────────────────────────

def test_api_stop_no_running_process(client):
    """api/stop returns ok even when no process is running."""
    server._run_proc = None
    resp = client.post("/api/stop")
    assert resp.get_json()["status"] == "ok"


def test_api_stop_kills_running_process(client):
    """api/stop terminates the running process and sets the stopped flag."""
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None  # process is running
    server._run_proc = mock_proc
    server._run_stopped = False

    resp = client.post("/api/stop")

    assert resp.get_json()["status"] == "ok"
    mock_proc.terminate.assert_called_once()
    assert server._run_stopped is True


def test_api_stop_ignores_already_finished_process(client):
    """api/stop does not terminate a process that has already exited."""
    mock_proc = MagicMock()
    mock_proc.poll.return_value = 0  # process already finished
    server._run_proc = mock_proc
    server._run_stopped = False

    resp = client.post("/api/stop")

    assert resp.get_json()["status"] == "ok"
    mock_proc.terminate.assert_not_called()
    assert server._run_stopped is False


def test_api_run_emits_stopped_event_when_terminated(client):
    """When _run_stopped is set, api/run SSE stream emits a 'stopped' event."""
    mock_proc = MagicMock()
    mock_proc.stdout = io.StringIO("")  # empty but has readline()
    mock_proc.returncode = 1
    mock_proc.wait = MagicMock()

    def fake_popen(*args, **kwargs):
        server._run_stopped = True  # simulate stop being called during the run
        return mock_proc

    with patch("subprocess.Popen", side_effect=fake_popen):
        resp = client.get("/api/run")
        data = resp.data.decode()

    assert "stopped" in data
    assert "done" not in data


# ── Persistent async event loop ────────────────────────────────────────────────

def test_run_async_reuses_same_loop():
    """run_async should use a single persistent loop, not create a new one per call."""
    import asyncio as _asyncio

    async def get_loop():
        return _asyncio.get_running_loop()

    loop1 = server.run_async(get_loop())
    loop2 = server.run_async(get_loop())
    assert loop1 is loop2, "run_async must reuse the same event loop across calls"


def test_run_async_loop_survives_multiple_calls():
    """run_async should remain functional after multiple consecutive calls."""
    async def add(a, b):
        return a + b

    results = [server.run_async(add(i, i)) for i in range(5)]
    assert results == [0, 2, 4, 6, 8]
