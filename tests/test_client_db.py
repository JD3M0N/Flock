from conftest import load_module


client_db_manager = load_module(
    "test_client_db_manager",
    "client/db_manager.py",
)


def test_user_db_stores_and_loads_chat_history(tmp_path):
    database = client_db_manager.user_db()
    database.db_directory = str(tmp_path / "chats")
    database.set_db("alice")

    database.insert_new_message("alice", "bob", "first", True)
    database.insert_new_message("bob", "alice", "second", False)

    history = database.get_previous_chat("alice", "bob")

    assert [message[3] for message in history] == ["first", "second"]


def test_user_db_tracks_unseen_and_marks_seen(tmp_path):
    database = client_db_manager.user_db()
    database.db_directory = str(tmp_path / "chats")
    database.set_db("alice")

    database.insert_new_message("bob", "alice", "hello", False)
    database.insert_new_message("bob", "alice", "still unseen", False)
    database.insert_new_message("charlie", "alice", "yo", False)

    resume = database.get_unseen_resume("alice")

    assert ("bob", 2) in resume
    assert ("charlie", 1) in resume

    unseen = database.get_unseen_messages("alice", "bob")
    assert len(unseen) == 2

    database.set_messages_as_seen("alice", "bob")
    assert database.get_unseen_messages("alice", "bob") == []
