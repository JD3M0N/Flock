import getpass
import os
import threading
import time

import client
from logging_utils import configure_logger


logger = configure_logger("flock.console", "console.log")


class console_app:
    """Console-based UI for interacting with a `chat_client` instance."""

    def __init__(self):
        self.running = True
        self.interlocutor = None
        self.update_chat_flag = False
        self.chat_client = client.chat_client()

    def clear_screen(self):
        os.system("cls" if os.name == "nt" else "clear")

    def print_header(self, title):
        self.clear_screen()
        print("=" * 60)
        print(title)
        print("=" * 60)

    def print_status(self):
        connected_server = self.chat_client.server_name or "not connected"
        username = self.chat_client.username or "anonymous"
        pending = sum(len(messages) for messages in self.chat_client.pending_list.values())
        print(f"Server: {connected_server}")
        print(f"User: {username}")
        print(f"Pending messages: {pending}")
        print("-" * 60)

    def print_help(self):
        print("Available commands:")
        print("  @username   Open a private chat")
        print("  /help       Show this help")
        print("  /refresh    Reload the current screen")
        print("  /chats      Show chat previews")
        print("  /servers    Search and reconnect to a server")
        print("  /pending    Show queued pending messages")
        print("  /quit       Exit the console")

    def print_message(self, message):
        """Print a single message tuple in a human readable form."""
        if message[1] == self.chat_client.username:
            print(f"you: {message[3]} [ID: {message[0]}, Date: {message[4]}]")
        else:
            print(f"{message[1]}: {message[3]} [ID: {message[0]}, Date: {message[4]}]")

    def print_chat(self, interlocutor):
        """Print the full chat history with `interlocutor`."""
        chat = self.chat_client.load_chat(interlocutor)
        if not chat:
            print("No messages yet.")
            return
        for message in chat:
            self.print_message(message)

    def print_chat_previews(self):
        previews = self.chat_client.db.get_chat_previews(self.chat_client.username)
        if not previews:
            print("No chats stored locally yet.")
            return

        print("Recent chats:")
        for partner, last_message in previews:
            print(f"  @{partner}: {last_message}")

    def print_pending_messages(self):
        if not self.chat_client.pending_list:
            print("No queued pending messages.")
            return

        print("Pending delivery queue:")
        for recipient, messages in self.chat_client.pending_list.items():
            print(f"  @{recipient}: {len(messages)} queued message(s)")

    def print_unseen_resume(self):
        """Print a short summary of unseen messages grouped by author."""
        resume = self.chat_client.db.get_unseen_resume(self.chat_client.username)

        if not resume:
            print("No new messages.")
            return

        print("New messages:")
        for user, count in resume:
            suffix = "message" if count == 1 else "messages"
            print(f"  {user}: {count} unseen {suffix}.")

    def update_chat(self, interlocutor):
        """Background updater that fetches and prints unseen messages for the active chat."""
        while self.update_chat_flag:
            unseen = self.chat_client.db.get_unseen_messages(self.chat_client.username, interlocutor)
            for message in unseen:
                self.print_message(message)

            self.chat_client.db.set_messages_as_seen(self.chat_client.username, interlocutor)
            time.sleep(0.3)

    def prompt_server_selection(self, servers):
        while True:
            selection = input("Select a server by number, 'r' to rescan or 'q' to quit: ").strip().lower()
            if selection == "r":
                return None
            if selection == "q":
                return "QUIT"
            if not selection.isdigit():
                print("Please enter a valid number.")
                continue

            index = int(selection) - 1
            if 0 <= index < len(servers):
                return servers[index]

            print("That server number does not exist.")

    def run_ui(self):
        """Start the interactive console UI loop (blocking)."""
        try:
            status = self.search_servers_ui()
            if status != "OK":
                return
            time.sleep(0.5)

            status = self.register_or_login_ui()
            if status != "OK":
                return
            time.sleep(0.5)

            status = "MAIN"
            while self.running:
                if status == "MAIN":
                    status = self.main_menu_ui()
                elif status == "PV":
                    status = self.private_chat_ui()
                elif status == "QUIT":
                    print("Closing console client.")
                    logger.info("Console UI closed by user")
                    break
                else:
                    print("An unexpected state occurred. Returning to main menu...")
                    time.sleep(1)
                    status = "MAIN"
        except KeyboardInterrupt:
            print("\nConsole interrupted by user.")
            logger.info("Console UI interrupted by user")

    def search_servers_ui(self):
        try:
            while True:
                self.print_header("Flock Console | Server Discovery")
                print("Searching for available servers...")
                servers = self.chat_client.discover_servers_multicast()
                if not servers:
                    print("No servers were found.")
                    choice = input("Search again? (y/n): ").strip().lower()
                    if choice == "n":
                        return "NOT OK"
                    continue

                print("Servers found:")
                for i, (name, ip) in enumerate(servers, start=1):
                    print(f"  {i}. {name} ({ip})")

                selection = self.prompt_server_selection(servers)
                if selection == "QUIT":
                    return "NOT OK"
                if selection is None:
                    continue

                self.chat_client.connect_to_server(selection)
                print(f"Connected to {selection[0]} ({selection[1]})")
                logger.info("Console selected server %s", selection)
                return "OK"
        except Exception as e:
            logger.error("Error while searching servers: %s", e)
            print(f"An error occurred while searching servers: {e}")
            return "NOT OK"

    def register_or_login_ui(self):
        try:
            while True:
                self.print_header("Flock Console | Authentication")
                self.print_status()
                username = input("Username: ").strip()
                if " " in username or not username or "-" in username:
                    print("Username cannot contain spaces, hyphens or be empty.")
                    time.sleep(1)
                    continue

                password = getpass.getpass("Password: ").strip()
                if len(password) < 8:
                    print("Password must contain at least 8 characters.")
                    time.sleep(1)
                    continue

                success, error = self.chat_client.authenticate_user(username, password)
                if success:
                    print(f"Authenticated as {username}.")
                    logger.info("Console authenticated user '%s'", username)
                    return "OK"

                print(error or "Authentication failed.")
                logger.warning("Console authentication failed for '%s': %s", username, error)
                retry = input("Try again? (y/n): ").strip().lower()
                if retry == "n":
                    return "NOT OK"
        except Exception as e:
            logger.error("Error while authenticating user: %s", e)
            print(f"An error occurred while registering or logging in: {e}")
            return "NOT OK"

    def main_menu_ui(self):
        self.print_header("Flock Console | Main Menu")
        self.print_status()
        self.print_unseen_resume()
        self.print_help()

        while True:
            command = input("> ").strip()
            if not command:
                continue

            if command.startswith("@") and " " not in command and len(command) > 1:
                self.interlocutor = command[1:]
                logger.info("Opening private chat with '%s'", self.interlocutor)
                return "PV"

            if command == "/help":
                self.print_help()
            elif command == "/refresh":
                return "MAIN"
            elif command == "/chats":
                self.print_chat_previews()
            elif command == "/servers":
                if self.search_servers_ui() == "OK":
                    return "MAIN"
                return "QUIT"
            elif command == "/pending":
                self.print_pending_messages()
            elif command == "/quit":
                return "QUIT"
            else:
                print("Invalid command. Use '@recipient' or '/help'.")

    def private_chat_ui(self):
        try:
            self.print_header(f"Private chat with {self.interlocutor}")
            self.print_status()
            print("Type '/back' to return, '/refresh' to reload, '/pending' to inspect the queue.")
            print("-" * 60)
            self.print_chat(self.interlocutor)

            self.update_chat_flag = True
            threading.Thread(target=self.update_chat, args=(self.interlocutor,), daemon=True).start()

            while True:
                command = input("> ").strip()

                if not command:
                    continue

                if command.lower() == "/back":
                    self.interlocutor = None
                    self.update_chat_flag = False
                    logger.info("Leaving private chat")
                    return "MAIN"

                if command.lower() == "/refresh":
                    self.update_chat_flag = False
                    return "PV"

                if command.lower() == "/pending":
                    self.print_pending_messages()
                    continue

                message = f"MESSAGE {self.chat_client.username} {command}"
                if self.chat_client.send_message(self.interlocutor, message):
                    print("Message sent.")
                else:
                    self.chat_client.add_to_pending_list(self.interlocutor, message)
                    print("User offline or unreachable. Message queued for retry.")
        except Exception as e:
            logger.error("Error in private chat with '%s': %s", self.interlocutor, e)
            print(f"Error in chat: {e}")
            self.interlocutor = None
            self.update_chat_flag = False
            time.sleep(1)
            return "MAIN"


if __name__ == "__main__":
    app = console_app()
    app.run_ui()
