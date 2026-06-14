import json
import logging

from conftest import load_module


server_module = load_module(
    "test_server_module",
    "server/server.py",
    clear_modules=["db_manager", "logging_utils"],
)


class DummySocket:
    sent = []

    def __init__(self, *args, **kwargs):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def setsockopt(self, *args, **kwargs):
        return None

    def settimeout(self, *args, **kwargs):
        return None

    def sendto(self, data, address):
        self.sent.append((data, address))

    def close(self):
        return None


def build_server(monkeypatch):
    DummySocket.sent = []
    monkeypatch.setattr(server_module.socket, "socket", DummySocket)
    server = server_module.ChatServer("node-test")
    monkeypatch.setattr(server, "get_ip", lambda: "127.0.0.10")
    return server


def init_db(server, tmp_path):
    server.db_manager.db_directory = str(tmp_path / "server_db")
    server.db_manager.set_db(server.name)


def teardown_server(server):
    server.running = False
    server.command_socket.close()
    server.ping_socket.close()


class CaptureHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.events = []

    def emit(self, record):
        self.events.append(getattr(record, "flock", {}).get("event"))


def capture_server_events():
    handler = CaptureHandler()
    server_module.logger.addHandler(handler)
    return handler


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


def test_server_status_payload_contains_admin_topology(monkeypatch):
    server = build_server(monkeypatch)
    try:
        server.lower_bound = 10
        server.upper_bound = 20
        server.predecessor = "127.0.0.1"
        server.successor = "127.0.0.2"
        server.successors = ["127.0.0.2", "127.0.0.3"]
        server.replics = ["127.0.0.4"]
        server.replicants = ["127.0.0.5"]

        payload = server.status_payload()

        assert payload["name"] == "node-test"
        assert payload["ip"] == "127.0.0.10"
        assert payload["range"] == {"lower": 10, "upper": 20}
        assert payload["predecessor"] == "127.0.0.1"
        assert payload["successor"] == "127.0.0.2"
        assert payload["successors"] == ["127.0.0.2", "127.0.0.3"]
        assert payload["replicas"] == ["127.0.0.4"]
        assert payload["replics"] == ["127.0.0.4"]
        assert payload["replicants"] == ["127.0.0.5"]
    finally:
        teardown_server(server)


def test_server_snapshot_is_deterministic_and_hides_public_keys(monkeypatch, tmp_path):
    server = build_server(monkeypatch)
    init_db(server, tmp_path)
    try:
        server.db_manager.register_user("bob", "10.0.0.2", 5002, public_key="pub-b", version=2)
        server.db_manager.register_user("alice", "10.0.0.1", 5001, public_key="pub-a", version=1)
        server.db_manager.register_replic_user("carol", "10.0.0.3", 5003, public_key="pub-c", version=3, owner="node-c")

        first = server.snapshot_payload()
        second = server.snapshot_payload()

        assert first == second
        assert [item["username"] for item in first["owned"]] == ["alice", "bob"]
        assert first["replicas"] == [{
            "username": "carol",
            "version": 3,
            "owner": "node-c",
            "hash": first["replicas"][0]["hash"],
        }]
        assert "public_key" not in json.dumps(first)
    finally:
        teardown_server(server)


def test_server_checksum_is_stable_and_changes_with_state(monkeypatch, tmp_path):
    server = build_server(monkeypatch)
    init_db(server, tmp_path)
    events = capture_server_events()
    try:
        server.db_manager.register_user("alice", "10.0.0.1", 5001, public_key="pub-a", version=1)

        first = server.checksum_payload()
        second = server.checksum_payload()
        server.db_manager.register_user("alice", "10.0.0.2", 5002, public_key="pub-a", version=2)
        third = server.checksum_payload()

        assert first == second
        assert third["checksum"] != first["checksum"]
        assert third["records"] == 1
        assert "checksum_generated" in events.events
    finally:
        server_module.logger.removeHandler(events)
        teardown_server(server)


def test_server_sync_from_owner_assimilates_only_that_owner(monkeypatch, tmp_path):
    server = build_server(monkeypatch)
    init_db(server, tmp_path)
    events = capture_server_events()
    try:
        server.db_manager.register_replic_user("alice", "10.0.0.1", 5001, public_key="pub-a", version=1, owner="node-a")
        server.db_manager.register_replic_user("bob", "10.0.0.2", 5002, public_key="pub-b", version=1, owner="node-b")

        result = server.sync_from_owner("node-a")

        assert result == {"owner": "node-a", "seen": 1, "applied": 1, "forwarded": 0, "rejected": 0}
        assert server.db_manager.resolve_user("alice") == ("10.0.0.1", 5001, "pub-a", 1)
        assert server.db_manager.resolve_user("bob") is None
        assert "sync_completed" in events.events
    finally:
        server_module.logger.removeHandler(events)
        teardown_server(server)


def test_server_takeover_rejects_stale_updates(monkeypatch, tmp_path):
    server = build_server(monkeypatch)
    init_db(server, tmp_path)
    try:
        server.db_manager.register_user("alice", "10.0.0.1", 5001, public_key="pub-a", version=5)

        result = server.place_user_record("alice", "10.0.0.2", 5002, "pub-a", 4)

        assert result == server_module.db_manager.STALE
        assert server.db_manager.resolve_user("alice") == ("10.0.0.1", 5001, "pub-a", 5)
    finally:
        teardown_server(server)


def test_server_replica_rejects_stale_updates(monkeypatch, tmp_path):
    server = build_server(monkeypatch)
    init_db(server, tmp_path)
    try:
        server.db_manager.register_replic_user("alice", "10.0.0.1", 5001, public_key="pub-a", version=5, owner="node-a")

        resolution, stored = server.db_manager.upsert_replic_user(
            "alice",
            "10.0.0.2",
            5002,
            public_key="pub-a",
            version=4,
            owner="node-a",
        )

        assert (resolution, stored) == (server_module.db_manager.STALE, False)
        assert server.db_manager.get_replics("node-a") == [("alice", "10.0.0.1", 5001, "pub-a", 5)]
    finally:
        teardown_server(server)


def test_server_assimilated_records_are_re_replicated(monkeypatch, tmp_path):
    server = build_server(monkeypatch)
    init_db(server, tmp_path)
    events = capture_server_events()
    try:
        server.replicants = ["node-a"]
        server.replics = ["127.0.0.20"]
        server.db_manager.register_replic_user("alice", "10.0.0.1", 5001, public_key="pub-a", version=1, owner="node-a")
        monkeypatch.setattr(server, "ping", lambda _ip: False)

        server.replicants_manager()

        assert server.db_manager.resolve_user("alice") == ("10.0.0.1", 5001, "pub-a", 1)
        assert server.db_manager.get_replics("node-a") == []
        assert DummySocket.sent == [(b"REPLIC alice 10.0.0.1 5001 1 pub-a", ("127.0.0.20", 12345))]
        assert "replica_assimilated" in events.events
        assert "replica_written" in events.events
    finally:
        server_module.logger.removeHandler(events)
        teardown_server(server)
