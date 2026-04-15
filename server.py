"""Web UI for Finance Manager — setup wizard + streaming run."""
import asyncio
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from dotenv import load_dotenv, set_key
from flask import Flask, Response, jsonify, request, send_file, stream_with_context

BASE_DIR = Path(__file__).parent
ENV_FILE = BASE_DIR / ".env"

app = Flask(__name__)
app.secret_key = os.urandom(24)

# Mutable shared state (single-user local tool, no locking needed)
_monarch_pending: dict = {}          # email/password held during MFA flow
_google_auth_status: dict = {"done": False, "error": None}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _reload_env() -> None:
    load_dotenv(str(ENV_FILE), override=True)


def _save_key(key: str, value: str) -> None:
    if not ENV_FILE.exists():
        ENV_FILE.write_text("")
    set_key(str(ENV_FILE), key, value)


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_file(str(BASE_DIR / "server.html"))


@app.route("/api/status")
def api_status():
    _reload_env()
    return jsonify({
        "monarch_email_value":   os.environ.get("MONARCH_EMAIL", ""),
        "monarch_session":       (BASE_DIR / ".monarch_session").exists(),
        "anthropic_key_value":   os.environ.get("ANTHROPIC_API_KEY", ""),
        "google_credentials":    (BASE_DIR / "credentials.json").exists(),
        "google_token":          (BASE_DIR / ".gsheets_token.json").exists(),
        "spreadsheet_id_value":  os.environ.get("SPREADSHEET_ID", ""),
    })


@app.route("/api/save", methods=["POST"])
def api_save():
    data = request.json
    _save_key(data["key"], data["value"])
    return jsonify({"status": "ok"})


@app.route("/api/monarch/login", methods=["POST"])
def monarch_login():
    from monarchmoney import MonarchMoney, RequireMFAException  # type: ignore

    data = request.json
    email    = (data.get("email") or "").strip()
    password = (data.get("password") or "").strip()
    if not email or not password:
        return jsonify({"status": "error", "message": "Email and password are required."})

    _save_key("MONARCH_EMAIL", email)
    _save_key("MONARCH_PASSWORD", password)

    async def _try():
        mm = MonarchMoney()
        try:
            await mm.login(email, password)
            mm.save_session(str(BASE_DIR / ".monarch_session"))
            return {"status": "ok"}
        except RequireMFAException:
            _monarch_pending["email"]    = email
            _monarch_pending["password"] = password
            return {"status": "mfa_required"}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    return jsonify(asyncio.run(_try()))


@app.route("/api/monarch/mfa", methods=["POST"])
def monarch_mfa():
    from monarchmoney import MonarchMoney  # type: ignore

    code     = (request.json.get("code") or "").strip()
    email    = _monarch_pending.get("email")
    password = _monarch_pending.get("password")
    if not email or not code:
        return jsonify({"status": "error", "message": "No pending MFA session."})

    async def _submit():
        mm = MonarchMoney()
        try:
            await mm.multi_factor_authenticate(email, password, code)
            mm.save_session(str(BASE_DIR / ".monarch_session"))
            _monarch_pending.clear()
            return {"status": "ok"}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    return jsonify(asyncio.run(_submit()))


@app.route("/api/google/credentials", methods=["POST"])
def google_credentials():
    if "file" not in request.files:
        return jsonify({"status": "error", "message": "No file received."})
    request.files["file"].save(str(BASE_DIR / "credentials.json"))
    return jsonify({"status": "ok"})


@app.route("/api/google/auth", methods=["POST"])
def google_auth():
    global _google_auth_status
    _google_auth_status = {"done": False, "error": None}

    def _run_oauth():
        try:
            from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore
            flow = InstalledAppFlow.from_client_secrets_file(
                str(BASE_DIR / "credentials.json"),
                scopes=["https://www.googleapis.com/auth/spreadsheets"],
            )
            creds = flow.run_local_server(port=0)
            (BASE_DIR / ".gsheets_token.json").write_text(creds.to_json())
            _google_auth_status["done"] = True
        except Exception as exc:
            _google_auth_status["error"] = str(exc)

    threading.Thread(target=_run_oauth, daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/api/google/auth/status")
def google_auth_status():
    return jsonify(_google_auth_status)


@app.route("/api/run")
def api_run():
    _reload_env()
    env = os.environ.copy()

    def _generate():
        proc = subprocess.Popen(
            [sys.executable, "-u", str(BASE_DIR / "main.py")],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=str(BASE_DIR),
            env=env,
        )

        for line in iter(proc.stdout.readline, ""):
            yield f"data: {json.dumps({'type': 'line', 'text': line.rstrip()})}\n\n"

        proc.wait()
        sid = env.get("SPREADSHEET_ID", "")
        if proc.returncode == 0:
            url = f"https://docs.google.com/spreadsheets/d/{sid}"
            yield f"data: {json.dumps({'type': 'done', 'url': url})}\n\n"
        else:
            yield f"data: {json.dumps({'type': 'error', 'text': f'Process exited with code {proc.returncode}'})}\n\n"

    return Response(
        stream_with_context(_generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    print("Finance Manager running at \033[1mhttp://localhost:5000\033[0m")
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
