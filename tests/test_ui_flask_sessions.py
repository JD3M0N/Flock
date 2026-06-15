from conftest import load_module


ui_module = load_module(
    "test_ui_flask_module",
    "client/ui_flask.py",
    clear_modules=["client", "logging_utils"],
)


class DummySocket:
    def getsockname(self):
        return ("0.0.0.0", 41000)

    def close(self):
        return None


class DummyDb:
    def get_pending_resume(self):
        return []

    def get_pending_messages(self):
        return []

    def get_chat_previews(self, username):
        return []

    def get_unseen_resume(self, username):
        return []

    def set_messages_as_seen(self, username, contact):
        return None

    def get_previous_chat(self, username, contact):
        return []


class DummyChat:
    instances = []

    def __init__(self):
        self.server_address = None
        self.server_name = None
        self.username = None
        self.running = True
        self.server_down = False
        self.background_started = False
        self.contact_list = {}
        self.on_message_received = None
        self.message_socket = DummySocket()
        self.client_socket = DummySocket()
        self.db = DummyDb()
        self.session_id = None
        DummyChat.instances.append(self)

    def set_session_id(self, session_id):
        self.session_id = session_id

    def list_local_profiles(self):
        return []

    def has_local_profile(self, username):
        return False

    def authenticate_user(self, username, password):
        self.username = username
        return True, None

    def get_ip(self, target_ip=None):
        return "192.168.1.20"

    def delivery_diagnostics(self):
        return {}


def reset_ui(monkeypatch):
    DummyChat.instances = []
    ui_module.chat_clients.clear()
    ui_module.recent_ui_events.clear()
    ui_module.app.config["TESTING"] = True
    monkeypatch.setattr(ui_module.client, "chat_client", DummyChat)


def prepare_browser(browser, server_name):
    browser.get("/servers")
    with browser.session_transaction() as flask_session:
        client_id = flask_session["client_id"]
        csrf_token = flask_session["csrf_token"]
    chat = ui_module.chat_clients[client_id]
    chat.server_name = server_name
    chat.server_address = ("10.0.0.1", 12345)
    return csrf_token, chat


def test_flask_sessions_keep_independent_chat_clients(monkeypatch):
    reset_ui(monkeypatch)
    alice_browser = ui_module.app.test_client()
    bob_browser = ui_module.app.test_client()

    alice_csrf, alice_chat = prepare_browser(alice_browser, "node-a")
    bob_csrf, bob_chat = prepare_browser(bob_browser, "node-b")

    alice_response = alice_browser.post(
        "/auth",
        json={"username": "alice", "password": "StrongPass1", "csrf_token": alice_csrf},
    )
    bob_response = bob_browser.post(
        "/auth",
        json={"username": "bob", "password": "StrongPass1", "csrf_token": bob_csrf},
    )

    assert alice_response.status_code == 200
    assert bob_response.status_code == 200
    assert alice_chat.username == "alice"
    assert bob_chat.username == "bob"
    assert alice_chat is not bob_chat

    alice_page = alice_browser.get("/chats")
    bob_page = bob_browser.get("/chats")

    assert alice_page.status_code == 200
    assert bob_page.status_code == 200
    assert b"alice" in alice_page.data
    assert b"bob" in bob_page.data
    assert b"bob" not in alice_page.data


def test_flask_session_survives_refresh(monkeypatch):
    reset_ui(monkeypatch)
    browser = ui_module.app.test_client()
    csrf_token, _chat = prepare_browser(browser, "node-a")

    response = browser.post(
        "/auth",
        json={"username": "alice", "password": "StrongPass1", "csrf_token": csrf_token},
    )
    assert response.status_code == 200

    first = browser.get("/chats")
    second = browser.get("/chats")

    assert first.status_code == 200
    assert second.status_code == 200
    assert b"alice" in first.data
    assert b"alice" in second.data
