import socket
import threading
import os
import json
import time
import db_manager
import struct


class chat_client:
    def __init__(self):
        self.client_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.client_socket.settimeout(3)
        self.message_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.message_socket.settimeout(3)
        self.message_socket.bind(("", 0))

        self.server_address = None
        self.server_name = None
        self.username = None
        self.running = True
        self.file_lock = threading.Lock()
        self.pending_list = {}
        self.pending_lock = threading.Lock()

        self.server_down = False

        self.contact_list = {}
        self.on_message_received = None

        self.crypto = None
        self.pending_key_exchanges = {}

        self.db = db_manager.user_db()

    def server_auto_reconnect(self):
        while self.running:
            # print("Checking server connection")
            if self.server_down:
                # print("Server down")
                if self.auto_connect():
                    self.server_down = False
                    # print("Server reconnected")
            time.sleep(3)


    def set_user(self, username):
        self.username = username
        self.db.set_db(username)
        try:
            import crypto_manager
            self.crypto = crypto_manager.CryptoManager(username)
        except Exception as e:
            print(f"Crypto not available: {e}")
            self.crypto = None
        self.run_background()


    def read_response(self, socket):
        response = ''
        address = None

        while True:
            part, address = socket.recvfrom(8192)
            response += part.decode()
            if response.endswith('\r\n') or len(part) < 8192:
                break
        return response, address

    def send_command(self, command) -> str:
        try:
            self.client_socket.sendto(
                f"{command}".encode(), self.server_address)
            response, _ = self.read_response(self.client_socket)
            return response
        except Exception as e:
            self.server_down = True
            # print(f"ERROR in communication with server: {e}")
            return f"ERROR in communication with server: {e}"

    def send_message(self, recipient, message):

        _, sender, text = message.split(" ", 2)

        if recipient == self.username:
            self.db.insert_new_message(self.username, recipient, text, True)
            return True

        try:
            address = self.contact_list.get(recipient)
            if not address:
                address = self.resolve_user(recipient)

            if not address:
                return False

            if self.crypto:
                self.ensure_peer_key(recipient)

            if self.crypto and self.crypto.has_peer_key(recipient):
                encrypted_text = self.crypto.encrypt_message(recipient, text)
                wire_message = f"MESSAGE {sender} {encrypted_text}"
            else:
                wire_message = message

            if self.is_user_online(address):
                self.db.insert_new_message(self.username, recipient, text, True)
                self.message_socket.sendto(wire_message.encode(), address)
                return True
            else:
                address = self.resolve_user(recipient)
                if address and self.is_user_online(address):
                    self.db.insert_new_message(self.username, recipient, text, True)
                    self.message_socket.sendto(wire_message.encode(), address)
                    return True
                return False
        except Exception as e:
            print(f"ERROR sending message: {e}")
            return False

    def add_to_pending_list(self, recipient, message):
        with self.pending_lock:
            if recipient in self.pending_list.keys():
                self.pending_list[recipient].append(message)
            else:
                self.pending_list[recipient] = [message]

    def resolve_user(self, username):
        response = self.send_command(f"RESOLVE {username}")
        if response.startswith("OK"):
            _, ip, port = response.split()
            self.contact_list[username] = (ip, int(port))
            return (ip, int(port))
        else:
            return None

    def ensure_peer_key(self, recipient, timeout=5):
        if not self.crypto or self.crypto.has_peer_key(recipient):
            return True

        address = self.contact_list.get(recipient)
        if not address:
            address = self.resolve_user(recipient)
        if not address:
            return False

        event = threading.Event()
        self.pending_key_exchanges[recipient] = event
        request = f"PUBKEY_REQ {self.username}"
        self.message_socket.sendto(request.encode(), address)

        success = event.wait(timeout=timeout)
        self.pending_key_exchanges.pop(recipient, None)
        return success
        
    def register_user(self, username):
        try:
            message_ip = self.get_ip()
            _, message_port = self.message_socket.getsockname()
            response = self.send_command(f"REGISTER {username} {message_ip} {message_port}")
            print(response)
            if response.startswith("OK"):
                self.set_user(username)
                return True
            return False
        except Exception as e:
            print(f"Registration error: {e}")
            return False

    def discover_servers(self):
        self.client_socket.settimeout(3)
        servers = []
        broadcast_address = ("<broadcast>", 12345)
        self.client_socket.setsockopt(
            socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            self.client_socket.sendto("DISCOVER".encode(), broadcast_address)
            while True:
                data, address = self.client_socket.recvfrom(1024)
                server_name = data.decode()
                servers.append((server_name, address[0]))
        except socket.timeout:
            pass

        return servers

    def connect_to_server(self, server):
        try:
            self.server_address = (server[1], 12345)
            self.server_name = server[0]
        except Exception as e:
            return f"ERROR connecting with server: {e}"
        
    def auto_connect(self):
        servers = self.discover_servers()
        # servers = self.discover_servers_multicast()
        if len(servers) == 0:
            return False
        
        self.connect_to_server(servers[0])
        return True


    def load_chat(self, interlocutor):

        chat = self.db.get_previous_chat(self.username, interlocutor)

        return chat


    def listen_for_messages(self):
        while self.running:
            try:
                message, address = self.read_response(self.message_socket)
                if message.startswith("MESSAGE"):
                    _, sender, encrypted_text = message.split(" ", 2)

                    if self.crypto and self.crypto.has_peer_key(sender):
                        try:
                            text = self.crypto.decrypt_message(encrypted_text)
                        except Exception:
                            text = encrypted_text
                    else:
                        text = encrypted_text

                    self.db.insert_new_message(sender, self.username, text, False)
                    self.contact_list[sender] = address

                    if self.on_message_received:
                        try:
                            self.on_message_received(sender, text)
                        except Exception:
                            pass

                elif message.startswith("PUBKEY_REQ"):
                    _, requester = message.split(" ", 1)
                    if self.crypto:
                        response = f"PUBKEY_RES {self.username} {self.crypto.get_public_key_b64()}"
                        self.message_socket.sendto(response.encode(), address)
                        self.contact_list[requester] = address
                        if not self.crypto.has_peer_key(requester):
                            req = f"PUBKEY_REQ {self.username}"
                            self.message_socket.sendto(req.encode(), address)

                elif message.startswith("PUBKEY_RES"):
                    _, peer_username, b64_key = message.split(" ", 2)
                    if self.crypto:
                        self.crypto.store_peer_key(peer_username, b64_key)
                        self.contact_list[peer_username] = address
                        event = self.pending_key_exchanges.get(peer_username)
                        if event:
                            event.set()

                elif message.startswith("PING"):
                    self.message_socket.sendto("PONG".encode(), address)
            except Exception as e:
                pass

    def is_user_online(self, address):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(0.5)
                sock.bind(('', 0))
                sock_port = sock.getsockname()[1]
                sock.sendto(f"PING".encode(), address)
                response, _ = sock.recvfrom(1024)
                return response.decode() == "PONG"
        except socket.timeout:
            # print(f"Timeout while checking if user is online at {address}")
            pass
        except Exception as e:
            # print(f"Error checking if user is online at {address}: {e}")
            pass
        return False

    def send_pending_messages(self):
        while self.running:
            # print(f"Pending messages: {self.pending_list}")
            try:
                with self.pending_lock:
                    pending_users = list(self.pending_list.keys())
                for username in pending_users:
                    with self.pending_lock:
                        while self.send_message(username, self.pending_list[username][0]):
                            self.pending_list[username].pop(0)
                            if len(self.pending_list[username]) == 0:
                                del self.pending_list[username]
                                break
                        else:
                            pass
                time.sleep(1)
            except Exception as e:
                print(f"Error sending pending messages: {e}")
                pass

    def get_ip(self):
        return socket.gethostbyname(socket.gethostname())

    def run_background(self):
        # print("Starting background threads")
        threading.Thread(target=self.listen_for_messages, daemon=True).start()
        threading.Thread(target=self.send_pending_messages, daemon=True).start()
        threading.Thread(target=self.server_auto_reconnect, daemon=True).start()
        # print("Background threads started")
        time.sleep(1)


    def discover_servers_multicast(self, timeout: int = 3) -> list:
        """
        Sends a multicast request to discover servers and waits for responses.

        :param timeout: Maximum time (in seconds) to wait for responses.
        :return: List of discovered server IPs.
        """
        MCAST_GRP = "224.0.0.1"
        MCAST_PORT = 10003
        MESSAGE = "DISCOVER_SERVER"
        BUFFER_SIZE = 1024

        # Crear socket UDP para enviar y recibir
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.settimeout(timeout)

        # Configurar TTL del paquete multicast
        ttl = struct.pack("b", 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)

        # Enviar la petición multicast
        try:
            sock.sendto(MESSAGE.encode(), (MCAST_GRP, MCAST_PORT))
        except Exception as e:
            print(f"Error enviando el mensaje multicast: {e}")
            return []

        print('a')

        servers = []
        start_time = time.time()
        while True:
            try:
                data, addr = sock.recvfrom(BUFFER_SIZE)
                server_ip = data.decode().strip()
                servers.append(server_ip)
                print(f"Servidor descubierto: {server_ip} (respuesta desde {addr})")
            except socket.timeout:
                break
            except Exception as e:
                print(f"Error recibiendo datos: {e}")
                break
            if time.time() - start_time > timeout:
                break

        sock.close()

        servers = [("main", server) for server in servers]

        return servers


if __name__ == "__main__":
    # client = chat_client()
    # client.run_ui()
    pass


"""
TODO:
- [x] Change to TCP at least for sending messages (one socket UDP for pinging and one thread with TCP for every conversation)
- [x] Implement what happens if the server is down (auto search of new server)
"""
