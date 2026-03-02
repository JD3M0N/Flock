import os
import secrets

from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from flask_socketio import SocketIO, emit

import client


app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["SECRET_KEY"] = os.environ.get("FLOCK_SECRET_KEY", secrets.token_hex(32))
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = False
socketio = SocketIO(app, async_mode="threading")

chat = client.chat_client()


def on_new_message(sender, text):
    socketio.emit("new_message", {"sender": sender, "text": text})


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
    if not chat.send_message(contact, message):
        chat.add_to_pending_list(contact, message)
    emit("message_sent", {"contact": contact, "text": text})


@socketio.on("mark_seen")
def handle_mark_seen(data):
    if not require_authenticated_socket(data):
        return
    contact = data.get("contact", "").strip()
    if contact:
        chat.db.set_messages_as_seen(chat.username, contact)


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=False, allow_unsafe_werkzeug=True)
