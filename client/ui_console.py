import getpass
import os
import threading
import time

import client
from logging_utils import configure_logger


logger = configure_logger("flock.console", "console.log")


class console_app:
    """Console-based UI for interacting with a `chat_client` instance."""

    WIDTH = 72

    def __init__(self):
        self.running = True
        self.interlocutor = None
        self.update_chat_flag = False
        self.chat_client = client.chat_client()

    def clear_screen(self):
        os.system("cls" if os.name == "nt" else "clear")

    def line(self, char="-"):
        print(char * self.WIDTH)

    def print_ok(self, message):
        print(f"[OK] {message}")

    def print_warn(self, message):
        print(f"[!] {message}")

    def print_error(self, message):
        print(f"[ERROR] {message}")

    def print_section(self, title):
        print()
        print(title)
        self.line()

    def print_header(self, title):
        self.clear_screen()
        self.line("=")
        print(f" {title}")
        self.line("=")

    def print_status(self):
        connected_server = self.chat_client.server_name or "sin conexion"
        username = self.chat_client.username or "sin autenticar"
        pending = sum(len(messages) for messages in self.chat_client.pending_list.values())
        server_ip = self.chat_client.server_address[0] if self.chat_client.server_address else "-"
        print(f"Nodo: {connected_server} ({server_ip})")
        print(f"Usuario: {username} | Pendientes: {pending}")
        self.line()

    def print_help(self):
        self.print_section("Comandos")
        print("  @usuario   Abrir chat privado")
        print("  /chats     Ver conversaciones locales")
        print("  /pending   Ver cola de mensajes pendientes")
        print("  /servers   Buscar y cambiar nodo")
        print("  /refresh   Recargar pantalla")
        print("  /help      Mostrar ayuda")
        print("  /quit      Salir")

    def print_message(self, message):
        """Print a single message tuple in a human readable form."""
        author = "tu" if message[1] == self.chat_client.username else f"@{message[1]}"
        when = message[4] or "sin fecha"
        prefix = ">>" if message[1] == self.chat_client.username else "<<"
        print(f"{prefix} {author}  [{when}]")
        for line in str(message[3]).splitlines() or [""]:
            print(f"   {line}")

    def print_empty(self, message):
        print(f"(sin datos) {message}")

    def print_list_item(self, label, detail):
        print(f"  - {label:<18} {detail}")

    def wait_briefly(self):
        time.sleep(1)

    def ask_yes_no(self, prompt):
        return input(f"{prompt} [s/N]: ").strip().lower() == "s"

    def prompt(self, label):
        return input(f"{label}> ").strip()

    def print_server_list(self, servers):
        self.print_section("Nodos encontrados")
        for i, (name, ip) in enumerate(servers, start=1):
            print(f"  {i:>2}. {name:<20} {ip}")

    def print_auth_mode(self, username):
        if self.chat_client.has_local_profile(username):
            print("Modo: entrar con perfil local existente")
        else:
            print("Modo: crear perfil local y publicar presencia")

    def print_chat(self, interlocutor):
        """Print the full chat history with `interlocutor`."""
        chat = self.chat_client.load_chat(interlocutor)
        if not chat:
            self.print_empty("no hay mensajes con este contacto todavia.")
            return
        for message in chat:
            self.print_message(message)

    def print_chat_previews(self):
        previews = self.chat_client.db.get_chat_previews(self.chat_client.username)
        if not previews:
            self.print_empty("no hay conversaciones guardadas localmente.")
            return

        self.print_section("Conversaciones recientes")
        for partner, last_message in previews:
            preview = last_message if len(last_message) <= 70 else f"{last_message[:67]}..."
            self.print_list_item(f"@{partner}", preview)

    def print_pending_messages(self):
        if not self.chat_client.pending_list:
            self.print_empty("la cola de entrega esta limpia.")
            return

        self.print_section("Cola de entrega local")
        for recipient, messages in self.chat_client.pending_list.items():
            suffix = "mensaje" if len(messages) == 1 else "mensajes"
            self.print_list_item(f"@{recipient}", f"{len(messages)} {suffix} pendiente(s)")

    def print_unseen_resume(self):
        """Print a short summary of unseen messages grouped by author."""
        resume = self.chat_client.db.get_unseen_resume(self.chat_client.username)

        if not resume:
            self.print_empty("no hay mensajes nuevos.")
            return

        self.print_section("Mensajes no leidos")
        for user, count in resume:
            suffix = "mensaje" if count == 1 else "mensajes"
            self.print_list_item(f"@{user}", f"{count} {suffix} sin leer")

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
            selection = input("Selecciona numero, 'r' para reintentar o 'q' para salir: ").strip().lower()
            if selection == "r":
                return None
            if selection == "q":
                return "QUIT"
            if not selection.isdigit():
                self.print_warn("Introduce un numero valido.")
                continue

            index = int(selection) - 1
            if 0 <= index < len(servers):
                return servers[index]

            self.print_warn("Ese numero no corresponde a ningun nodo listado.")

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
                    self.print_ok("Cerrando cliente de consola.")
                    logger.info("Console UI closed by user")
                    break
                else:
                    self.print_warn("Estado inesperado. Volviendo al menu principal.")
                    time.sleep(1)
                    status = "MAIN"
        except KeyboardInterrupt:
            print()
            self.print_ok("Consola interrumpida por el usuario.")
            logger.info("Console UI interrupted by user")

    def search_servers_ui(self):
        try:
            while True:
                self.print_header("Flock Consola | Descubrimiento de nodos")
                print("Buscando nodos disponibles...")
                servers = self.chat_client.discover_servers_multicast()
                if not servers:
                    self.print_warn("No se encontraron nodos activos.")
                    if not self.ask_yes_no("Buscar otra vez?"):
                        return "NOT OK"
                    continue

                self.print_server_list(servers)

                selection = self.prompt_server_selection(servers)
                if selection == "QUIT":
                    return "NOT OK"
                if selection is None:
                    continue

                self.chat_client.connect_to_server(selection)
                self.print_ok(f"Conectado a {selection[0]} ({selection[1]}).")
                logger.info("Console selected server %s", selection)
                return "OK"
        except Exception as e:
            logger.error("Error while searching servers: %s", e)
            self.print_error(f"No se pudo completar la busqueda de nodos: {e}")
            return "NOT OK"

    def register_or_login_ui(self):
        try:
            while True:
                self.print_header("Flock Consola | Autenticacion")
                self.print_status()
                username = self.prompt("Usuario")
                if " " in username or not username or "-" in username:
                    self.print_warn("El usuario no puede estar vacio ni contener espacios o guiones.")
                    self.wait_briefly()
                    continue

                self.print_auth_mode(username)
                password = getpass.getpass("Contrasena: ").strip()
                if len(password) < 8:
                    self.print_warn("La contrasena debe tener al menos 8 caracteres.")
                    self.wait_briefly()
                    continue

                success, error = self.chat_client.authenticate_user(username, password)
                if success:
                    self.print_ok(f"Sesion iniciada como @{username}.")
                    logger.info("Console authenticated user '%s'", username)
                    return "OK"

                self.print_error(error or "No se pudo autenticar el usuario.")
                logger.warning("Console authentication failed for '%s': %s", username, error)
                if not self.ask_yes_no("Intentar de nuevo?"):
                    return "NOT OK"
        except Exception as e:
            logger.error("Error while authenticating user: %s", e)
            self.print_error(f"No se pudo registrar o iniciar sesion: {e}")
            return "NOT OK"

    def main_menu_ui(self):
        self.print_header("Flock Consola | Menu principal")
        self.print_status()
        self.print_unseen_resume()
        self.print_help()

        while True:
            command = self.prompt("flock")
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
                self.print_warn("Comando no reconocido. Usa '@usuario' o '/help'.")

    def private_chat_ui(self):
        try:
            self.print_header(f"Flock Consola | Chat con @{self.interlocutor}")
            self.print_status()
            print("Escribe un mensaje o usa /back, /refresh, /pending.")
            self.line()
            self.print_chat(self.interlocutor)

            self.update_chat_flag = True
            threading.Thread(target=self.update_chat, args=(self.interlocutor,), daemon=True).start()

            while True:
                command = self.prompt("mensaje")

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
                    self.print_ok("Mensaje enviado.")
                else:
                    self.chat_client.add_to_pending_list(self.interlocutor, message)
                    self.print_warn("Contacto offline o inalcanzable. Mensaje guardado en cola local.")
        except Exception as e:
            logger.error("Error in private chat with '%s': %s", self.interlocutor, e)
            self.print_error(f"Error en el chat: {e}")
            self.interlocutor = None
            self.update_chat_flag = False
            self.wait_briefly()
            return "MAIN"


if __name__ == "__main__":
    app = console_app()
    app.run_ui()
