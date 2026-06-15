from conftest import load_module


client_module = load_module(
    "test_client_module",
    "client/client.py",
    clear_modules=["db_manager", "logging_utils", "crypto_manager"],
)


class DummySocket:
    def __init__(self, *args, **kwargs):
        self.bound_address = ("127.0.0.1", 43210)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def settimeout(self, timeout):
        return None

    def setsockopt(self, *args, **kwargs):
        return None

    def bind(self, address):
        self.bound_address = address

    def getsockname(self):
        return self.bound_address

    def connect(self, address):
        return None

    def close(self):
        return None


class DummyCrypto:
    def __init__(self):
        self.keys = {}

    def store_peer_key(self, username, public_key):
        self.keys[username] = public_key


def build_client(tmp_path, monkeypatch):
    monkeypatch.setattr(client_module.socket, "socket", DummySocket)
    app_client = client_module.chat_client()
    app_client.auth_directory = str(tmp_path / "auth")
    app_client.db.db_directory = str(tmp_path / "chats")
    return app_client


def teardown_client(app_client):
    app_client.running = False
    app_client.client_socket.close()
    app_client.message_socket.close()


def test_local_profile_authentication(tmp_path, monkeypatch):
    app_client = build_client(tmp_path, monkeypatch)
    try:
        app_client.create_local_profile("alice", "StrongPass1")

        assert app_client.authenticate_local_profile("alice", "StrongPass1") is True
        assert app_client.authenticate_local_profile("alice", "wrong-pass") is False
    finally:
        teardown_client(app_client)


def test_add_to_pending_list_groups_messages_per_recipient(tmp_path, monkeypatch):
    app_client = build_client(tmp_path, monkeypatch)
    try:
        app_client.db.set_db("alice")
        app_client.add_to_pending_list("bob", "MESSAGE alice hi")
        app_client.add_to_pending_list("bob", "MESSAGE alice second")

        assert app_client.pending_list["bob"] == [
            "MESSAGE alice hi",
            "MESSAGE alice second",
        ]
    finally:
        teardown_client(app_client)


def test_send_message_to_self_is_stored_locally(tmp_path, monkeypatch):
    app_client = build_client(tmp_path, monkeypatch)
    try:
        app_client.username = "alice"
        app_client.db.set_db("alice")

        assert app_client.send_message("alice", "MESSAGE alice hello self") is True

        history = app_client.db.get_previous_chat("alice", "alice")
        assert [message[3] for message in history] == ["hello self"]
    finally:
        teardown_client(app_client)


def test_resolve_user_caches_successful_lookup(tmp_path, monkeypatch):
    app_client = build_client(tmp_path, monkeypatch)
    try:
        app_client.crypto = DummyCrypto()
        app_client.send_command = lambda command, operation_id=None: "OK 127.0.0.1 5555 public-key 9"

        assert app_client.resolve_user("bob") == ("127.0.0.1", 5555)
        assert app_client.contact_list["bob"] == ("127.0.0.1", 5555)
        assert app_client.crypto.keys["bob"] == "public-key"
    finally:
        teardown_client(app_client)


def test_get_ip_prefers_explicit_public_ip(tmp_path, monkeypatch):
    app_client = build_client(tmp_path, monkeypatch)
    try:
        monkeypatch.setenv("FLOCK_PUBLIC_IP", "192.0.2.10")

        assert app_client.get_ip("10.0.0.2") == "192.0.2.10"
        assert app_client.last_advertised_ip == "192.0.2.10"
    finally:
        teardown_client(app_client)


def test_get_ip_uses_route_to_selected_server(tmp_path, monkeypatch):
    class RoutedSocket(DummySocket):
        def connect(self, address):
            self.bound_address = ("192.168.50.7", 55123)

    monkeypatch.setattr(client_module.socket, "socket", RoutedSocket)
    app_client = client_module.chat_client()
    app_client.auth_directory = str(tmp_path / "auth")
    app_client.db.db_directory = str(tmp_path / "chats")
    try:
        assert app_client.get_ip("10.1.1.20") == "192.168.50.7"
    finally:
        teardown_client(app_client)


def test_send_message_records_resolve_failure_reason(tmp_path, monkeypatch):
    app_client = build_client(tmp_path, monkeypatch)
    try:
        app_client.username = "alice"
        app_client.db.set_db("alice")
        app_client.resolve_user = lambda recipient, operation_id=None: None

        assert app_client.send_message("bob", "MESSAGE alice hello") is False
        assert app_client.last_delivery["status"] == "queued"
        assert app_client.last_delivery["reason"] == "resolve_failed"
        assert app_client.delivery_events[0]["kind"] == "message_queued"
    finally:
        teardown_client(app_client)
