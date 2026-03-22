from conftest import load_module


server_module = load_module(
    "test_server_module",
    "server/server.py",
    clear_modules=["db_manager", "logging_utils"],
)


class DummySocket:
    def __init__(self, *args, **kwargs):
        return None

    def setsockopt(self, *args, **kwargs):
        return None

    def close(self):
        return None


def build_server(monkeypatch):
    monkeypatch.setattr(server_module.socket, "socket", DummySocket)
    return server_module.ChatServer("node-test")


def teardown_server(server):
    server.running = False
    server.command_socket.close()
    server.ping_socket.close()


def test_server_validates_username_and_addresses(monkeypatch):
    server = build_server(monkeypatch)
    try:
        assert server.is_valid_username("alice_01") is True
        assert server.is_valid_username("ab") is False
        assert server.is_valid_username("alice-smith") is False

        assert server.is_valid_client_address("127.0.0.1", 12345) is True
        assert server.is_valid_client_address("999.0.0.1", 12345) is False
        assert server.is_valid_client_address("127.0.0.1", 70000) is False
    finally:
        teardown_server(server)


def test_server_rolling_hash_is_stable_and_in_range(monkeypatch):
    server = build_server(monkeypatch)
    try:
        hash_a = server.rolling_hash("alice")
        hash_b = server.rolling_hash("alice")

        assert hash_a == hash_b
        assert 0 <= hash_a < server_module.HASH_MOD
    finally:
        teardown_server(server)
