from conftest import load_module


client_module = load_module(
    "test_client_module",
    "client/client.py",
    clear_modules=["db_manager", "logging_utils", "crypto_manager"],
)


class DummySocket:
    def __init__(self, *args, **kwargs):
        self.bound_address = ("127.0.0.1", 43210)

    def settimeout(self, timeout):
        return None

    def setsockopt(self, *args, **kwargs):
        return None

    def bind(self, address):
        self.bound_address = address

    def getsockname(self):
        return self.bound_address

    def close(self):
        return None


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
        app_client.send_command = lambda command: "OK 127.0.0.1 5555"

        assert app_client.resolve_user("bob") == ("127.0.0.1", 5555)
        assert app_client.contact_list["bob"] == ("127.0.0.1", 5555)
    finally:
        teardown_client(app_client)
