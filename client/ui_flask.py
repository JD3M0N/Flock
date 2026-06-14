import os
import secrets
import sys
import time
import importlib.util
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from flask_socketio import SocketIO, emit

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


app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["SECRET_KEY"] = os.environ.get("FLOCK_SECRET_KEY", secrets.token_hex(32))
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = False
socketio = SocketIO(app, async_mode="threading")

chat = client.chat_client()
recent_ui_events = []


def record_ui_event(kind, message, **details):
    event = {
        "kind": kind,
        "message": message,
        "time": time.strftime("%H:%M:%S"),
        "details": details,
    }
    recent_ui_events.insert(0, event)
    del recent_ui_events[30:]
    socketio.emit("diagnostic_event", event)


def on_new_message(sender, text):
    socketio.emit("new_message", {"sender": sender, "text": text})
    record_ui_event("message_received", f"Mensaje recibido de @{sender}", sender=sender)


chat.on_message_received = on_new_message


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


def authenticate_request(username, password):
    username_error = validate_username(username)
    if username_error:
        return False, username_error

    password_error = validate_password(password)
    if password_error:
        return False, password_error

    if not chat.server_address:
        return False, "Connect to a server before continuing."

    return chat.authenticate_user(username, password)


def is_authenticated():
    return bool(session.get("authenticated") and session.get("username") and chat.username == session.get("username"))


def build_client_diagnostics():
    server_ip = chat.server_address[0] if chat.server_address else None
    server_port = chat.server_address[1] if chat.server_address else None
    pending_summary = []
    pending_total = 0
    chat_count = 0
    unread_total = 0
    pending_messages = []

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

    return {
        "username": chat.username,
        "server": {
            "name": chat.server_name,
            "ip": server_ip,
            "port": server_port,
            "down": chat.server_down,
        },
        "client": {
            "message_port": chat.message_socket.getsockname()[1],
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
        "events": recent_ui_events[:10],
    }


def require_authenticated_socket(data):
    if not validate_csrf(data):
        return False
    if not is_authenticated():
        emit("auth_required", {"redirect": url_for("register")})
        return False
    return True


def validate_csrf(data):
    if not isinstance(data, dict) or data.get("csrf_token") != session.get("csrf_token"):
        emit("request_error", {"error": "Invalid session token. Refresh the page and try again."})
        return False
    return True


@app.route("/")
def index():
    ensure_csrf_token()
    if not chat.server_address:
        return redirect(url_for("servers"))
    if not is_authenticated():
        return redirect(url_for("register"))
    return redirect(url_for("chats"))


@app.route("/servers")
def servers():
    ensure_csrf_token()
    return render_template(
        "servers.html",
        connected_server=chat.server_name,
        connected_ip=chat.server_address[0] if chat.server_address else None,
        authenticated=is_authenticated(),
    )


@app.route("/register")
def register():
    ensure_csrf_token()
    suggested_username = request.args.get("username", "").strip()
    has_profile = bool(suggested_username and chat.has_local_profile(suggested_username))
    return render_template(
        "register.html",
        connected_server=chat.server_name,
        connected_ip=chat.server_address[0] if chat.server_address else None,
        suggested_username=suggested_username,
        has_profile=has_profile,
        local_profiles=chat.list_local_profiles(),
        authenticated=is_authenticated(),
    )


@app.route("/logout", methods=["POST"])
def logout():
    session.pop("authenticated", None)
    session.pop("username", None)
    ensure_csrf_token()
    return redirect(url_for("register"))


@app.route("/auth", methods=["POST"])
def auth():
    ensure_csrf_token()
    payload = request.get_json(silent=True) or {}

    if payload.get("csrf_token") != session.get("csrf_token"):
        return jsonify({"ok": False, "error": "Invalid session token. Refresh the page and try again."}), 400

    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))
    success, error = authenticate_request(username, password)

    if not success:
        return jsonify({"ok": False, "error": error or "Authentication failed."}), 400

    session["authenticated"] = True
    session["username"] = username
    return jsonify({"ok": True, "username": username})


@app.route("/chats")
def chats():
    if not is_authenticated():
        return redirect(url_for("register"))
    return render_template(
        "chats.html",
        username=chat.username,
        connected_server=chat.server_name,
        connected_ip=chat.server_address[0] if chat.server_address else None,
        authenticated=True,
    )


@app.route("/chat/<contact>")
def private_chat(contact):
    if not is_authenticated():
        return redirect(url_for("register"))
    return render_template(
        "chat.html",
        username=chat.username,
        contact=contact,
        connected_server=chat.server_name,
        connected_ip=chat.server_address[0] if chat.server_address else None,
        authenticated=True,
    )


@app.route("/diagnostics")
def diagnostics():
    if not is_authenticated():
        return redirect(url_for("register"))
    return render_template(
        "diagnostics.html",
        username=chat.username,
        connected_server=chat.server_name,
        connected_ip=chat.server_address[0] if chat.server_address else None,
        authenticated=True,
    )


@socketio.on("discover_servers")
def handle_discover(data):
    ensure_csrf_token()
    if not validate_csrf(data):
        return
    found = chat.discover_servers()
    emit("servers_found", [{"name": s[0], "ip": s[1]} for s in found])


@socketio.on("connect_server")
def handle_connect(data):
    ensure_csrf_token()
    if not validate_csrf(data):
        return
    if not isinstance(data, dict) or not data.get("name") or not data.get("ip"):
        emit("request_error", {"error": "Invalid server selection."})
        return
    chat.connect_to_server((data["name"], data["ip"]))
    record_ui_event("server_connected", f"Cliente conectado a {data['name']}", ip=data["ip"])
    emit("server_connected", {"name": data["name"]})


@socketio.on("load_chats")
def handle_load_chats(data):
    if not require_authenticated_socket(data):
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
    if not require_authenticated_socket(data):
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
    if not require_authenticated_socket(data):
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
        record_ui_event("message_queued", f"Mensaje para @{contact} en cola local", recipient=contact)
    else:
        record_ui_event("message_sent", f"Mensaje enviado a @{contact}", recipient=contact)
    emit("message_sent", {"contact": contact, "text": text, "queued": queued})


@socketio.on("mark_seen")
def handle_mark_seen(data):
    if not require_authenticated_socket(data):
        return
    contact = data.get("contact", "").strip()
    if contact:
        chat.db.set_messages_as_seen(chat.username, contact)


@socketio.on("load_diagnostics")
def handle_load_diagnostics(data):
    if not require_authenticated_socket(data):
        return
    emit("diagnostics_loaded", build_client_diagnostics())


@socketio.on("check_server")
def handle_check_server(data):
    if not require_authenticated_socket(data):
        return
    if not chat.server_address:
        emit("server_check_result", {"ok": False, "message": "No hay servidor activo."})
        return

    response = chat.send_command("PING")
    ok = response == "PONG"
    if ok:
        chat.server_down = False
        record_ui_event("server_ping", "El gestor activo respondio PONG")
        emit("server_check_result", {"ok": True, "message": "El gestor activo respondio PONG."})
    else:
        record_ui_event("server_ping_failed", "El gestor activo no respondio correctamente", response=response)
        emit("server_check_result", {"ok": False, "message": response})
    emit("diagnostics_loaded", build_client_diagnostics())


@socketio.on("retry_pending")
def handle_retry_pending(data):
    if not require_authenticated_socket(data):
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
        "pending_retry",
        f"Reintento manual: {delivered} entregado(s), {failed} pendiente(s)",
        delivered=delivered,
        failed=failed,
    )
    emit("pending_retry_result", {"delivered": delivered, "failed": failed})
    emit("diagnostics_loaded", build_client_diagnostics())


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=False, allow_unsafe_werkzeug=True)
