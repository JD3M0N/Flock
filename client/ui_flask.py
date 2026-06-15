import importlib.util
import json
import os
import secrets
import sys
import time
from datetime import timedelta
from pathlib import Path
from threading import RLock

from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from flask_socketio import SocketIO, emit, join_room

CLIENT_DIR = Path(__file__).resolve().parent
if str(CLIENT_DIR) not in sys.path:
    sys.path.insert(0, str(CLIENT_DIR))

try:
    import client
    if not hasattr(client, "chat_client"):
        raise ImportError("Loaded client package instead of client.py")
except ImportError:
    spec = importlib.util.spec_from_file_location("flock_client_core", CLIENT_DIR / "client.py")
    client = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(client)

from logging_utils import configure_logger, log_event


AUTH_DIR = CLIENT_DIR / "auth"
SESSION_SECRET_PATH = AUTH_DIR / "flask_session.key"
ADMIN_COMMANDS = {"STATUS", "SNAPSHOT", "CHECKSUM"}
MAX_EVENTS_PER_SESSION = 40


def load_secret_key():
    configured = os.environ.get("FLOCK_SECRET_KEY")
    if configured:
        return configured

    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    if SESSION_SECRET_PATH.exists():
        return SESSION_SECRET_PATH.read_text(encoding="utf-8").strip()

    secret = secrets.token_hex(32)
    SESSION_SECRET_PATH.write_text(secret + "\n", encoding="utf-8")
    return secret


def session_lifetime():
    try:
        hours = float(os.environ.get("FLOCK_SESSION_TTL_HOURS", "12"))
    except ValueError:
        hours = 12.0
    return timedelta(hours=max(hours, 1.0))


app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["SECRET_KEY"] = load_secret_key()
app.config["PERMANENT_SESSION_LIFETIME"] = session_lifetime()
app.config["SESSION_REFRESH_EACH_REQUEST"] = True
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = False
socketio = SocketIO(app, async_mode="threading")
logger = configure_logger("flock.ui", "client.log")

chat_clients = {}
recent_ui_events = {}
clients_lock = RLock()


@app.before_request
def keep_session_permanent():
    session.permanent = True


def get_client_id(create=True):
    client_id = session.get("client_id")
    if not client_id and create:
        client_id = secrets.token_urlsafe(18)
        session["client_id"] = client_id
        session.permanent = True
    return client_id


def make_message_callback(client_id):
    def on_new_message(sender, text):
        socketio.emit("new_message", {"sender": sender, "text": text}, room=client_id)
        record_ui_event(client_id, "message_received", f"Mensaje recibido de @{sender}", sender=sender)

    return on_new_message


def get_chat():
    client_id = get_client_id()
    with clients_lock:
        chat = chat_clients.get(client_id)
        if chat is None:
            chat = client.chat_client()
            if hasattr(chat, "set_session_id"):
                chat.set_session_id(client_id)
            chat.on_message_received = make_message_callback(client_id)
            chat_clients[client_id] = chat
            recent_ui_events.setdefault(client_id, [])
            log_event(logger, "INFO", "web_session_started", session_id=client_id, result="client_created")
        return chat


def close_chat(client_id):
    with clients_lock:
        chat = chat_clients.pop(client_id, None)
        recent_ui_events.pop(client_id, None)
    if not chat:
        return
    chat.running = False
    for sock in (getattr(chat, "client_socket", None), getattr(chat, "message_socket", None)):
        try:
            sock.close()
        except Exception:
            pass
    log_event(logger, "INFO", "web_session_closed", session_id=client_id, username=getattr(chat, "username", None))


def record_ui_event(client_id, kind, message, **details):
    event = {
        "kind": kind,
        "message": message,
        "time": time.strftime("%H:%M:%S"),
        "details": details,
    }
    with clients_lock:
        events = recent_ui_events.setdefault(client_id, [])
        events.insert(0, event)
        del events[MAX_EVENTS_PER_SESSION:]
    log_event(logger, "INFO", "ui_event", session_id=client_id, phase=kind, result=details, reason=message)
    socketio.emit("diagnostic_event", event, room=client_id)


def ensure_csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(24)
        session["csrf_token"] = token
    return token


def validate_username(username):
    if not username:
        return "Username is required."
    if len(username) < 3 or len(username) > 20:
        return "Username must contain between 3 and 20 characters."
    if any(char.isspace() for char in username) or "-" in username:
        return "Username cannot contain spaces or hyphens."
    if not username.replace("_", "").isalnum():
        return "Username can only contain letters, numbers and underscores."
    return None


def validate_password(password):
    if len(password) < 8:
        return "Password must contain at least 8 characters."
    if password.lower() == password or password.upper() == password or not any(char.isdigit() for char in password):
        return "Use a stronger password with upper, lower and numeric characters."
    return None


def authenticate_request(chat, username, password):
    username_error = validate_username(username)
    if username_error:
        return False, username_error

    password_error = validate_password(password)
    if password_error:
        return False, password_error

    if not chat.server_address:
        return False, "Connect to a server before continuing."

    return chat.authenticate_user(username, password)


def is_authenticated(chat=None):
    chat = chat or get_chat()
    return bool(
        session.get("authenticated")
        and session.get("username")
        and getattr(chat, "username", None) == session.get("username")
    )


def socket_address(sock):
    try:
        ip, port = sock.getsockname()
        return {"ip": ip, "port": port}
    except Exception:
        return {"ip": None, "port": None}


def template_context(chat, authenticated=None):
    server_ip = chat.server_address[0] if chat.server_address else None
    return {
        "connected_server": chat.server_name,
        "connected_ip": server_ip,
        "authenticated": is_authenticated(chat) if authenticated is None else authenticated,
    }


def build_client_diagnostics(chat):
    server_ip = chat.server_address[0] if chat.server_address else None
    server_port = chat.server_address[1] if chat.server_address else None
    pending_summary = []
    pending_total = 0
    chat_count = 0
    unread_total = 0
    pending_messages = []
    client_id = get_client_id(create=False)

    if chat.username:
        pending_summary = [
            {"recipient": recipient, "count": count}
            for recipient, count in chat.db.get_pending_resume()
        ]
        pending_total = sum(item["count"] for item in pending_summary)
        pending_messages = [
            {"id": message_id, "recipient": recipient}
            for message_id, recipient, _payload in chat.db.get_pending_messages()
        ]
        chat_count = len(chat.db.get_chat_previews(chat.username))
        unread_total = sum(count for _author, count in chat.db.get_unseen_resume(chat.username))

    advertised_ip = None
    if server_ip:
        advertised_ip = chat.get_ip(server_ip)

    network = chat.delivery_diagnostics() if hasattr(chat, "delivery_diagnostics") else {}
    return {
        "session": {
            "id": client_id,
            "authenticated": is_authenticated(chat),
        },
        "username": chat.username,
        "server": {
            "name": chat.server_name,
            "ip": server_ip,
            "port": server_port,
            "down": chat.server_down,
        },
        "client": {
            "message_socket": socket_address(chat.message_socket),
            "advertised_ip": advertised_ip,
            "background_workers": chat.background_started,
            "contacts_cached": len(chat.contact_list),
            "chat_count": chat_count,
            "unread_total": unread_total,
        },
        "pending": {
            "total": pending_total,
            "by_recipient": pending_summary,
            "messages": pending_messages,
        },
        "network": network,
        "events": recent_ui_events.get(client_id, [])[:10],
    }


def validate_csrf(data):
    if not isinstance(data, dict) or data.get("csrf_token") != session.get("csrf_token"):
        emit("request_error", {"error": "Invalid session token. Refresh the page and try again."})
        return False
    return True


def require_authenticated_socket(data):
    if not validate_csrf(data):
        return None
    chat = get_chat()
    if not is_authenticated(chat):
        emit("auth_required", {"redirect": url_for("register")})
        return None
    return chat


def parse_admin_response(response):
    if response.startswith("OK "):
        raw_payload = response[3:]
        try:
            return True, json.loads(raw_payload)
        except json.JSONDecodeError:
            return True, raw_payload
    return False, response


@app.route("/")
def index():
    ensure_csrf_token()
    chat = get_chat()
    if not chat.server_address:
        return redirect(url_for("servers"))
    if not is_authenticated(chat):
        return redirect(url_for("register"))
    return redirect(url_for("chats"))


@app.route("/servers")
def servers():
    ensure_csrf_token()
    chat = get_chat()
    return render_template("servers.html", **template_context(chat))


@app.route("/register")
def register():
    ensure_csrf_token()
    chat = get_chat()
    suggested_username = request.args.get("username", "").strip()
    has_profile = bool(suggested_username and chat.has_local_profile(suggested_username))
    return render_template(
        "register.html",
        suggested_username=suggested_username,
        has_profile=has_profile,
        local_profiles=chat.list_local_profiles(),
        **template_context(chat),
    )


@app.route("/logout", methods=["POST"])
def logout():
    if request.form.get("csrf_token") != session.get("csrf_token"):
        return redirect(url_for("register"))
    client_id = session.get("client_id")
    if client_id:
        close_chat(client_id)
    session.pop("authenticated", None)
    session.pop("username", None)
    session.pop("client_id", None)
    ensure_csrf_token()
    return redirect(url_for("register"))


@app.route("/auth", methods=["POST"])
def auth():
    ensure_csrf_token()
    chat = get_chat()
    payload = request.get_json(silent=True) or {}

    if payload.get("csrf_token") != session.get("csrf_token"):
        return jsonify({"ok": False, "error": "Invalid session token. Refresh the page and try again."}), 400

    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))
    success, error = authenticate_request(chat, username, password)

    if not success:
        return jsonify({"ok": False, "error": error or "Authentication failed."}), 400

    session["authenticated"] = True
    session["username"] = username
    record_ui_event(get_client_id(), "auth", f"Sesion iniciada como @{username}", username=username)
    return jsonify({"ok": True, "username": username})


@app.route("/chats")
def chats():
    chat = get_chat()
    if not is_authenticated(chat):
        return redirect(url_for("register"))
    return render_template(
        "chats.html",
        username=chat.username,
        csrf_token=session.get("csrf_token"),
        **template_context(chat, authenticated=True),
    )


@app.route("/chat/<contact>")
def private_chat(contact):
    chat = get_chat()
    if not is_authenticated(chat):
        return redirect(url_for("register"))
    return render_template(
        "chat.html",
        username=chat.username,
        contact=contact,
        **template_context(chat, authenticated=True),
    )


@app.route("/diagnostics")
def diagnostics():
    chat = get_chat()
    if not is_authenticated(chat):
        return redirect(url_for("register"))
    return render_template(
        "diagnostics.html",
        username=chat.username,
        **template_context(chat, authenticated=True),
    )


@socketio.on("connect")
def handle_socket_connect():
    client_id = session.get("client_id")
    if client_id:
        join_room(client_id)


@socketio.on("discover_servers")
def handle_discover(data):
    ensure_csrf_token()
    if not validate_csrf(data):
        return
    chat = get_chat()
    found = chat.discover_servers()
    record_ui_event(get_client_id(), "discover", f"{len(found)} nodo(s) detectado(s)", count=len(found))
    emit("servers_found", [{"name": s[0], "ip": s[1]} for s in found])


@socketio.on("connect_server")
def handle_connect(data):
    ensure_csrf_token()
    if not validate_csrf(data):
        return
    if not isinstance(data, dict) or not data.get("name") or not data.get("ip"):
        emit("request_error", {"error": "Invalid server selection."})
        return
    chat = get_chat()
    chat.connect_to_server((data["name"], data["ip"]))
    record_ui_event(get_client_id(), "server_connected", f"Nodo activo: {data['name']}", ip=data["ip"])
    emit("server_connected", {"name": data["name"]})


@socketio.on("load_chats")
def handle_load_chats(data):
    chat = require_authenticated_socket(data)
    if not chat:
        return
    previews = chat.db.get_chat_previews(chat.username)
    unread = chat.db.get_unseen_resume(chat.username)
    unread_dict = {u: c for u, c in unread}
    result = []
    for partner, last_msg in previews:
        result.append(
            {
                "contact": partner,
                "last_message": last_msg,
                "unread": unread_dict.get(partner, 0),
            }
        )
    emit("chats_loaded", result)


@socketio.on("load_chat_history")
def handle_load_history(data):
    chat = require_authenticated_socket(data)
    if not chat:
        return
    contact = data.get("contact", "").strip()
    if not contact:
        emit("request_error", {"error": "Contact is required."})
        return
    chat.db.set_messages_as_seen(chat.username, contact)
    messages = chat.load_chat(contact)
    result = [
        {"id": m[0], "author": m[1], "receiver": m[2], "text": m[3], "date": m[4]}
        for m in messages
    ]
    emit("chat_history", result)


@socketio.on("send_message")
def handle_send(data):
    chat = require_authenticated_socket(data)
    if not chat:
        return
    contact = data.get("contact", "").strip()
    text = data.get("text", "").strip()
    if not contact:
        emit("request_error", {"error": "Choose a valid contact."})
        return
    if not text:
        emit("request_error", {"error": "Write a message before sending."})
        return
    if len(text) > 4000:
        emit("request_error", {"error": "Message exceeds the 4000 character limit."})
        return
    message = f"MESSAGE {chat.username} {text}"
    queued = False
    if not chat.send_message(contact, message):
        chat.add_to_pending_list(contact, message)
        queued = True
        record_ui_event(get_client_id(), "message_queued", f"Mensaje para @{contact} en cola local", recipient=contact)
    else:
        record_ui_event(get_client_id(), "message_sent", f"Mensaje enviado a @{contact}", recipient=contact)
    emit("message_sent", {"contact": contact, "text": text, "queued": queued})
    emit("delivery_diagnostics", build_client_diagnostics(chat))


@socketio.on("mark_seen")
def handle_mark_seen(data):
    chat = require_authenticated_socket(data)
    if not chat:
        return
    contact = data.get("contact", "").strip()
    if contact:
        chat.db.set_messages_as_seen(chat.username, contact)


@socketio.on("load_diagnostics")
def handle_load_diagnostics(data):
    chat = require_authenticated_socket(data)
    if not chat:
        return
    emit("diagnostics_loaded", build_client_diagnostics(chat))


@socketio.on("check_server")
def handle_check_server(data):
    chat = require_authenticated_socket(data)
    if not chat:
        return
    if not chat.server_address:
        emit("server_check_result", {"ok": False, "message": "No hay servidor activo."})
        return

    response = chat.send_command("PING")
    ok = response == "PONG"
    if ok:
        chat.server_down = False
        record_ui_event(get_client_id(), "server_ping", "El gestor activo respondio PONG")
        emit("server_check_result", {"ok": True, "message": "El gestor activo respondio PONG."})
    else:
        record_ui_event(get_client_id(), "server_ping_failed", "El gestor activo no respondio", response=response)
        emit("server_check_result", {"ok": False, "message": response})
    emit("diagnostics_loaded", build_client_diagnostics(chat))


@socketio.on("admin_command")
def handle_admin_command(data):
    chat = require_authenticated_socket(data)
    if not chat:
        return
    command = str(data.get("command", "")).strip().upper()
    if command not in ADMIN_COMMANDS:
        emit("request_error", {"error": "Unsupported admin command."})
        return
    response = chat.send_command(command)
    ok, payload = parse_admin_response(response)
    record_ui_event(
        get_client_id(),
        "admin_command",
        f"{command}: {'OK' if ok else 'ERROR'}",
        command=command,
    )
    emit("admin_command_result", {"command": command, "ok": ok, "payload": payload})
    emit("diagnostics_loaded", build_client_diagnostics(chat))


@socketio.on("retry_pending")
def handle_retry_pending(data):
    chat = require_authenticated_socket(data)
    if not chat:
        return

    delivered = 0
    failed = 0
    for message_id, recipient, payload in chat.db.get_pending_messages():
        if chat.send_message(recipient, payload):
            chat.db.delete_pending_message(message_id)
            chat._remove_pending_cache_item(recipient, payload)
            delivered += 1
        else:
            failed += 1

    record_ui_event(
        get_client_id(),
        "pending_retry",
        f"Reintento manual: {delivered} entregado(s), {failed} pendiente(s)",
        delivered=delivered,
        failed=failed,
    )
    emit("pending_retry_result", {"delivered": delivered, "failed": failed})
    emit("diagnostics_loaded", build_client_diagnostics(chat))


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=False, allow_unsafe_werkzeug=True)
