from conftest import load_module


server_db_manager = load_module(
    "test_server_db_manager",
    "server/db_manager.py",
)


def test_server_db_registers_and_updates_users(tmp_path):
    database = server_db_manager.server_db()
    database.db_directory = str(tmp_path / "server_db")
    database.set_db("node1")

    database.register_user("alice", "127.0.0.1", 5000)
    database.register_user("alice", "127.0.0.2", 5001)

    assert database.resolve_user("alice") == ("127.0.0.2", 5001)
    assert database.resolve_user("missing") is None


def test_server_db_manages_replicas_by_owner(tmp_path):
    database = server_db_manager.server_db()
    database.db_directory = str(tmp_path / "server_db")
    database.set_db("node1")

    database.register_replic_user("alice", "10.0.0.1", 6000, "node-a")
    database.register_replic_user("bob", "10.0.0.2", 6001, "node-a")

    assert {row[0] for row in database.get_replics("node-a")} == {"alice", "bob"}

    database.drop_replics("node-a")
    assert database.get_replics("node-a") == []
