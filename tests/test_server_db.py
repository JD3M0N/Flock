from conftest import load_module


server_db_manager = load_module(
    "test_server_db_manager",
    "server/db_manager.py",
)


def test_server_db_registers_and_updates_users(tmp_path):
    database = server_db_manager.server_db()
    database.db_directory = str(tmp_path / "server_db")
    database.set_db("node1")

    assert database.register_user("alice", "127.0.0.1", 5000, public_key="pub-a", version=1) is True
    assert database.register_user("alice", "127.0.0.2", 5001, public_key="pub-a", version=2) is True

    assert database.resolve_user("alice") == ("127.0.0.2", 5001, "pub-a", 2)
    assert database.resolve_user("missing") is None


def test_server_db_rejects_stale_user_versions(tmp_path):
    database = server_db_manager.server_db()
    database.db_directory = str(tmp_path / "server_db")
    database.set_db("node1")

    assert database.register_user("alice", "127.0.0.1", 5000, public_key="pub-a", version=5) is True
    assert database.register_user("alice", "127.0.0.2", 5001, public_key="pub-a", version=4) is False

    assert database.resolve_user("alice") == ("127.0.0.1", 5000, "pub-a", 5)


def test_server_db_rejects_same_version_identity_conflict(tmp_path):
    database = server_db_manager.server_db()
    database.db_directory = str(tmp_path / "server_db")
    database.set_db("node1")

    assert database.register_user("alice", "127.0.0.1", 5000, public_key="pub-a", version=5) is True
    assert database.register_user("alice", "127.0.0.2", 5001, public_key="pub-b", version=5) is False

    assert database.resolve_user("alice") == ("127.0.0.1", 5000, "pub-a", 5)


def test_server_db_allows_higher_version_to_change_identity_key(tmp_path):
    database = server_db_manager.server_db()
    database.db_directory = str(tmp_path / "server_db")
    database.set_db("node1")

    assert database.register_user("alice", "127.0.0.1", 5000, public_key="pub-a", version=5) is True
    assert database.register_user("alice", "127.0.0.2", 5001, public_key="pub-b", version=6) is True

    assert database.resolve_user("alice") == ("127.0.0.2", 5001, "pub-b", 6)


def test_server_db_accepts_same_version_same_identity_as_idempotent(tmp_path):
    database = server_db_manager.server_db()
    database.db_directory = str(tmp_path / "server_db")
    database.set_db("node1")

    assert database.register_user("alice", "127.0.0.1", 5000, public_key="pub-a", version=5) is True
    assert database.register_user("alice", "127.0.0.2", 5001, public_key="pub-a", version=5) is True

    assert database.resolve_user("alice") == ("127.0.0.1", 5000, "pub-a", 5)


def test_server_db_manages_replicas_by_owner(tmp_path):
    database = server_db_manager.server_db()
    database.db_directory = str(tmp_path / "server_db")
    database.set_db("node1")

    database.register_replic_user("alice", "10.0.0.1", 6000, public_key="pub-a", version=1, owner="node-a")
    database.register_replic_user("bob", "10.0.0.2", 6001, public_key="pub-b", version=1, owner="node-a")

    assert {row[0] for row in database.get_replics("node-a")} == {"alice", "bob"}

    database.drop_replics("node-a")
    assert database.get_replics("node-a") == []


def test_server_db_applies_version_rules_to_replicas(tmp_path):
    database = server_db_manager.server_db()
    database.db_directory = str(tmp_path / "server_db")
    database.set_db("node1")

    assert database.register_replic_user("alice", "10.0.0.1", 6000, public_key="pub-a", version=5, owner="node-a") is True
    assert database.register_replic_user("alice", "10.0.0.2", 6001, public_key="pub-a", version=4, owner="node-a") is False
    assert database.register_replic_user("alice", "10.0.0.3", 6002, public_key="pub-b", version=5, owner="node-a") is False
    assert database.register_replic_user("alice", "10.0.0.4", 6003, public_key="pub-a", version=6, owner="node-a") is True

    assert database.get_replics("node-a") == [("alice", "10.0.0.4", 6003, "pub-a", 6)]
