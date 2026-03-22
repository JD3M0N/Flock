from conftest import load_module


console_module = load_module(
    "test_console_module",
    "client/ui_console.py",
    clear_modules=["client", "db_manager", "logging_utils", "crypto_manager"],
)


class DummyThread:
    def __init__(self, target=None, args=None, daemon=None):
        self.target = target
        self.args = args or ()
        self.daemon = daemon

    def start(self):
        return None


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


def teardown_console(app):
    app.chat_client.running = False
    app.chat_client.client_socket.close()
    app.chat_client.message_socket.close()


def test_prompt_server_selection_recovers_from_invalid_input(monkeypatch):
    monkeypatch.setattr(console_module.client.socket, "socket", DummySocket)
    app = console_module.console_app()
    try:
        servers = [("main", "127.0.0.1"), ("backup", "127.0.0.2")]
        answers = iter(["oops", "3", "2"])
        monkeypatch.setattr("builtins.input", lambda _: next(answers))

        assert app.prompt_server_selection(servers) == ("backup", "127.0.0.2")
    finally:
        teardown_console(app)


def test_private_chat_back_command_resets_flags(monkeypatch):
    monkeypatch.setattr(console_module.client.socket, "socket", DummySocket)
    app = console_module.console_app()
    try:
        app.chat_client.username = "alice"
        app.interlocutor = "bob"
        monkeypatch.setattr(console_module.threading, "Thread", DummyThread)
        monkeypatch.setattr(app, "print_header", lambda _: None)
        monkeypatch.setattr(app, "print_status", lambda: None)
        monkeypatch.setattr(app, "print_chat", lambda _: None)
        monkeypatch.setattr("builtins.input", lambda _: "/back")

        assert app.private_chat_ui() == "MAIN"
        assert app.interlocutor is None
        assert app.update_chat_flag is False
    finally:
        teardown_console(app)
