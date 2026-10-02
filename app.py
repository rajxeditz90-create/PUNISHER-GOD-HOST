#!/usr/bin/env python3
"""telegram-hoster -- Flask web backend, auth & per-user process manager.

Model:
  * ANYONE can self-register and sign in.
  * The OWNER (credentials set in this script) is the only one who can upload
    the shared bot script, `custom_script.py`.
  * EVERY signed-in user -- owner included -- can add their OWN bots (each with
    its own token and owner id), up to MAX_BOTS_PER_USER, and start/stop each
    one and watch its logs. Bots run the shared script, in their own child
    processes, each with its own log file. Users cannot see or affect another
    user's bots.

SECURITY: the owner uploads a script that everyone runs. Keep this panel
password-protected and behind HTTPS; do not expose it to the public internet.

Environment variables:
    PANEL_SECRET       - Flask session secret (random per start if unset)
    PANEL_PORT         - default 8080
    OWNER_USERNAME / OWNER_PASSWORD - optional; override the owner credentials
                                      hard-coded below
    ALLOW_REGISTRATION - set to "0" to close sign-ups
    MAX_BOTS_PER_USER  - how many bots each user may have (default 3)
    DATA_DIR           - where auth.sqlite3, logs/ live
    SCRIPT_PATH        - the shared bot script (default: ./custom_script.py)
"""

import atexit
import hashlib
import os
import re
import secrets
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from functools import wraps

from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)
SCRIPT_PATH = os.environ.get("SCRIPT_PATH", os.path.join(BASE_DIR, "custom_script.py"))
LOG_DIR = os.path.join(DATA_DIR, "logs")
AUTH_DB = os.path.join(DATA_DIR, "auth.sqlite3")

# Respect PANEL_PORT if set, otherwise the platform-provided PORT (Render,
# Heroku, etc.), otherwise 8080.
PANEL_PORT = int(os.environ.get("PANEL_PORT") or os.environ.get("PORT") or "8080")
PANEL_HOST = os.environ.get("PANEL_HOST", "0.0.0.0")
ALLOW_REGISTRATION = os.environ.get("ALLOW_REGISTRATION", "1") != "0"
# How many bots a single user may have (and run at once).
MAX_BOTS_PER_USER = int(os.environ.get("MAX_BOTS_PER_USER", "3"))

# ==========================================================================
#  OWNER CREDENTIALS  --  edit these two lines.
#
#  The owner account is created automatically when the app starts, using the
#  values below, and its password is re-synced on every startup. The owner is
#  the only account that can upload the shared bot script.
#
#  (You can also override these with the OWNER_USERNAME / OWNER_PASSWORD
#   environment variables, which take priority over the values here.)
# ==========================================================================
OWNER_USERNAME = os.environ.get("OWNER_USERNAME") or "admin"
OWNER_PASSWORD = os.environ.get("OWNER_PASSWORD") or "changeme123"
# ==========================================================================

app = Flask(__name__)
app.secret_key = os.environ.get("PANEL_SECRET") or secrets.token_hex(32)

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,32}$")
_auth_lock = threading.Lock()


# ------------------------------------------------------------- auth store ----
def db():
    conn = sqlite3.connect(AUTH_DB, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_auth_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role          TEXT NOT NULL DEFAULT 'user',
                created_ts    REAL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bots (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                name       TEXT NOT NULL,
                bot_token  TEXT,
                owner_id   TEXT,
                created_ts REAL
            )
            """
        )
    try:
        os.chmod(AUTH_DB, 0o600)
    except OSError:
        pass


def create_user(username, password):
    with _auth_lock, db() as conn:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, role, created_ts) VALUES (?, ?, 'user', ?)",
            (username, generate_password_hash(password), time.time()),
        )
        return cur.lastrowid, "user"


def seed_owner():
    with _auth_lock, db() as conn:
        row = conn.execute("SELECT id FROM users WHERE username = ?", (OWNER_USERNAME,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO users (username, password_hash, role, created_ts) VALUES (?, ?, 'owner', ?)",
                (OWNER_USERNAME, generate_password_hash(OWNER_PASSWORD), time.time()),
            )
        else:
            conn.execute(
                "UPDATE users SET password_hash = ?, role = 'owner' WHERE id = ?",
                (generate_password_hash(OWNER_PASSWORD), row["id"]),
            )


def verify_user(username, password):
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if row and check_password_hash(row["password_hash"], password):
        return dict(row)
    return None


def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    with db() as conn:
        row = conn.execute("SELECT id, username, role FROM users WHERE id = ?", (uid,)).fetchone()
    return dict(row) if row else None


# ------------------------------------------------------------ bot records ----
def list_bots(user_id):
    with db() as conn:
        rows = conn.execute("SELECT * FROM bots WHERE user_id = ? ORDER BY id", (user_id,)).fetchall()
    return [dict(r) for r in rows]


def get_bot(bot_id):
    with db() as conn:
        row = conn.execute("SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()
    return dict(row) if row else None


def count_bots(user_id):
    with db() as conn:
        return conn.execute("SELECT COUNT(*) FROM bots WHERE user_id = ?", (user_id,)).fetchone()[0]


def create_bot(user_id, name, bot_token, owner_id):
    with _auth_lock, db() as conn:
        cur = conn.execute(
            "INSERT INTO bots (user_id, name, bot_token, owner_id, created_ts) VALUES (?, ?, ?, ?, ?)",
            (user_id, name, bot_token, owner_id, time.time()),
        )
        return cur.lastrowid


def update_bot(bot_id, bot_token=None, owner_id=None):
    with _auth_lock, db() as conn:
        conn.execute(
            """
            UPDATE bots SET
                bot_token = COALESCE(?, bot_token),
                owner_id  = COALESCE(?, owner_id)
            WHERE id = ?
            """,
            (bot_token, owner_id, bot_id),
        )


def delete_bot(bot_id):
    with _auth_lock, db() as conn:
        conn.execute("DELETE FROM bots WHERE id = ?", (bot_id,))


def mask(token):
    if not token:
        return ""
    return "****" + token[-4:]


def log_path(bot_id):
    return os.path.join(LOG_DIR, f"{bot_id}.log")


def tail(path, n):
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            block = min(size, 200_000)
            fh.seek(size - block)
            data = fh.read().decode("utf-8", "replace")
        return data.splitlines()[-n:]
    except FileNotFoundError:
        return []


def script_version():
    try:
        with open(SCRIPT_PATH, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()[:8]
    except FileNotFoundError:
        return "none"


# ------------------------------------------------ per-bot process manager ----
class ProcessManager:
    """One child process per bot, each with its own log file."""

    def __init__(self):
        self._procs = {}      # bot_id -> Popen
        self._started = {}    # bot_id -> float
        self._versions = {}   # bot_id -> script version it started with
        self._lock = threading.Lock()

    def is_running(self, bid):
        proc = self._procs.get(bid)
        return proc is not None and proc.poll() is None

    def count_running_for_user(self, user_id):
        n = 0
        for bid in list(self._procs):
            if self.is_running(bid):
                bot = get_bot(bid)
                if bot and bot["user_id"] == user_id:
                    n += 1
        return n

    def start(self, bid):
        with self._lock:
            if self.is_running(bid):
                return False, "already running"

            bot = get_bot(bid)
            if not bot:
                return False, "bot not found"
            if not bot.get("bot_token"):
                return False, "bot token is not set"
            if self.count_running_for_user(bot["user_id"]) >= MAX_BOTS_PER_USER:
                return False, f"you can run at most {MAX_BOTS_PER_USER} bots at once"

            os.makedirs(LOG_DIR, exist_ok=True)
            env = dict(os.environ)
            env["BOT_TOKEN"] = bot["bot_token"]
            env["OWNER_ID"] = str(bot.get("owner_id") or "")
            env["PYTHONUNBUFFERED"] = "1"

            version = script_version()
            logf = open(log_path(bid), "a", encoding="utf-8")
            logf.write(
                f"\n===== starting {time.strftime('%Y-%m-%d %H:%M:%S')} "
                f"(shared script {version}) =====\n"
            )
            logf.flush()
            try:
                self._procs[bid] = subprocess.Popen(
                    [sys.executable, "-u", SCRIPT_PATH],
                    cwd=BASE_DIR,
                    env=env,
                    stdout=logf,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except Exception as exc:  # noqa: BLE001
                logf.write(f"failed to start: {exc}\n")
                logf.close()
                return False, str(exc)
            finally:
                logf.close()

            self._started[bid] = time.time()
            self._versions[bid] = version
            return True, "started"

    def stop(self, bid):
        with self._lock:
            proc = self._procs.get(bid)
            if proc is None or proc.poll() is not None:
                self._procs.pop(bid, None)
                self._started.pop(bid, None)
                self._versions.pop(bid, None)
                return False, "not running"

            pid = proc.pid
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=5)

            self._procs.pop(bid, None)
            self._started.pop(bid, None)
            self._versions.pop(bid, None)
            return True, "stopped"

    def status(self, bid):
        running = self.is_running(bid)
        return {
            "running": running,
            "pid": self._procs[bid].pid if running else None,
            "uptime": int(time.time() - self._started[bid]) if running and bid in self._started else 0,
            "running_version": self._versions.get(bid) if running else None,
            "current_version": script_version(),
        }

    def stop_all(self):
        for bid in list(self._procs):
            self.stop(bid)


manager = ProcessManager()


# --------------------------------------------------------------- guards ------
def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            if request.path.startswith("/api/"):
                return jsonify({"error": "unauthorized"}), 401
            return redirect(url_for("index"))
        return view(*args, **kwargs)

    return wrapped


def owner_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user:
            return jsonify({"error": "unauthorized"}), 401
        if user["role"] != "owner":
            return jsonify({"error": "owner only"}), 403
        return view(*args, **kwargs)

    return wrapped


def owned_bot(bot_id):
    """Return the bot if it belongs to the signed-in user, else None."""
    user = current_user()
    bot = get_bot(bot_id)
    if not bot or bot["user_id"] != user["id"]:
        return None
    return bot


# ---------------------------------------------------------------- routes -----
@app.route("/")
def index():
    user = current_user()
    bots = []
    if user:
        for b in list_bots(user["id"]):
            bots.append(
                {
                    "id": b["id"],
                    "name": b["name"],
                    "masked_token": mask(b["bot_token"]),
                    "owner_id": b["owner_id"] or "",
                    "running": manager.is_running(b["id"]),
                }
            )
    return render_template(
        "index.html",
        user=user,
        allow_registration=ALLOW_REGISTRATION,
        bots=bots,
        max_bots=MAX_BOTS_PER_USER,
        script_path=os.path.relpath(SCRIPT_PATH, BASE_DIR),
        script_version=script_version(),
    )


@app.route("/healthz")
def healthz():
    return "ok", 200


@app.route("/register", methods=["POST"])
def register():
    if not ALLOW_REGISTRATION:
        flash("Registration is closed.")
        return redirect(url_for("index"))

    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""

    if not USERNAME_RE.match(username):
        flash("Username must be 3-32 characters: letters, numbers, underscore.")
        return redirect(url_for("index"))
    if len(password) < 6:
        flash("Password must be at least 6 characters.")
        return redirect(url_for("index"))

    try:
        uid, _ = create_user(username, password)
    except sqlite3.IntegrityError:
        flash("That username is already taken.")
        return redirect(url_for("index"))

    session["uid"] = uid
    return redirect(url_for("index"))


@app.route("/login", methods=["POST"])
def login():
    user = verify_user((request.form.get("username") or "").strip(), request.form.get("password") or "")
    if user:
        session["uid"] = user["id"]
    else:
        flash("Wrong username or password.")
    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


# -- manage your own bots --
@app.route("/api/bots", methods=["POST"])
@login_required
def api_create_bot():
    user = current_user()
    if count_bots(user["id"]) >= MAX_BOTS_PER_USER:
        return jsonify({"ok": False, "error": f"limit reached: {MAX_BOTS_PER_USER} bots per user"}), 400

    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip() or "My bot"
    token = (data.get("bot_token") or "").strip()
    owner_id = str(data.get("owner_id") or "").strip()
    if not token:
        return jsonify({"ok": False, "error": "bot token is required"}), 400

    bid = create_bot(user["id"], name[:40], token, owner_id)
    return jsonify({"ok": True, "id": bid})


@app.route("/api/bots/<int:bid>/config", methods=["POST"])
@login_required
def api_bot_config(bid):
    bot = owned_bot(bid)
    if not bot:
        return jsonify({"error": "not found"}), 404

    data = request.get_json(silent=True) or {}
    token = (data.get("bot_token") or "").strip()
    if token and token.startswith("****"):
        token = None
    owner_id = str(data.get("owner_id") or "").strip() if "owner_id" in data else None
    update_bot(bid, bot_token=token, owner_id=owner_id)

    fresh = get_bot(bid)
    return jsonify({"ok": True, "masked_token": mask(fresh.get("bot_token", "")), "owner_id": fresh.get("owner_id") or ""})


@app.route("/api/bots/<int:bid>/delete", methods=["POST"])
@login_required
def api_bot_delete(bid):
    if not owned_bot(bid):
        return jsonify({"error": "not found"}), 404
    manager.stop(bid)
    delete_bot(bid)
    try:
        os.remove(log_path(bid))
    except OSError:
        pass
    return jsonify({"ok": True})


@app.route("/api/bots/<int:bid>/start", methods=["POST"])
@login_required
def api_bot_start(bid):
    if not owned_bot(bid):
        return jsonify({"error": "not found"}), 404
    ok, msg = manager.start(bid)
    return jsonify({"ok": ok, "message": msg, "status": manager.status(bid)})


@app.route("/api/bots/<int:bid>/stop", methods=["POST"])
@login_required
def api_bot_stop(bid):
    if not owned_bot(bid):
        return jsonify({"error": "not found"}), 404
    ok, msg = manager.stop(bid)
    return jsonify({"ok": ok, "message": msg, "status": manager.status(bid)})


@app.route("/api/bots/<int:bid>/status")
@login_required
def api_bot_status(bid):
    if not owned_bot(bid):
        return jsonify({"error": "not found"}), 404
    return jsonify(manager.status(bid))


@app.route("/api/bots/<int:bid>/logs")
@login_required
def api_bot_logs(bid):
    if not owned_bot(bid):
        return jsonify({"error": "not found"}), 404
    lines = int(request.args.get("lines", "200"))
    return jsonify({"lines": tail(log_path(bid), lines)})


# -- the shared script: readable by all, uploadable by the owner only --
@app.route("/api/script", methods=["GET"])
@login_required
def api_script_get():
    try:
        with open(SCRIPT_PATH, encoding="utf-8") as fh:
            return jsonify({"script": fh.read()})
    except FileNotFoundError:
        return jsonify({"script": ""})


@app.route("/api/script", methods=["POST"])
@owner_required
def api_script_save():
    data = request.get_json(silent=True) or {}
    code = data.get("script", "")
    try:
        compile(code, SCRIPT_PATH, "exec")
    except SyntaxError as exc:
        return jsonify({"ok": False, "error": f"Syntax error on line {exc.lineno}: {exc.msg}"}), 400

    tmp = SCRIPT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(code)
    os.replace(tmp, SCRIPT_PATH)
    return jsonify({"ok": True})


# ------------------------------------------------------------------ main -----
def _shutdown(*_):
    manager.stop_all()
    sys.exit(0)


def main():
    init_auth_db()
    seed_owner()
    atexit.register(manager.stop_all)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _shutdown)

    print(f"telegram-hoster listening on http://{PANEL_HOST}:{PANEL_PORT}")
    print(f"Owner account: '{OWNER_USERNAME}' (uploads the shared script).")
    print(f"Max bots per user: {MAX_BOTS_PER_USER}.")
    if OWNER_PASSWORD == "changeme123":
        print("WARNING: OWNER_PASSWORD is still the default -- change it in app.py.")
    app.run(host=PANEL_HOST, port=PANEL_PORT, threaded=True)


if __name__ == "__main__":
    main()
