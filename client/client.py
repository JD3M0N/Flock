import socket
import threading
import os
import json
import time
import shutil
import db_manager
import struct
import hashlib
import hmac
import secrets
import uuid
import ipaddress

from logging_utils import configure_logger, log_event, summarize_command


logger = configure_logger("flock.client", "client.log")


class chat_client:
    """Client-side chat manager handling server discovery, messaging and local storage."""
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
        self.background_started = False
        self.auth_directory = os.path.join(os.path.dirname(__file__), "auth")
        self.session_id = None
        self.last_advertised_ip = None
        self.last_server_command = None
        self.last_resolve = None
        self.last_peer_ping = None
        self.last_delivery = None
        self.delivery_events = []

        self.db = db_manager.user_db()

    def set_session_id(self, session_id):
        """Attach a web-session identifier to future logs."""
        self.session_id = session_id

    def _operation_id(self, prefix):
        return f"{prefix}-{uuid.uuid4().hex[:10]}"

    def _event_time(self):
        return time.strftime("%H:%M:%S")

    def _remember_delivery_event(self, kind, **details):
        event = {
            "kind": kind,
            "time": self._event_time(),
            **details,
        }
        self.delivery_events.insert(0, event)
        del self.delivery_events[30:]
        return event

    def delivery_diagnostics(self):
        """Return recent network diagnostics safe to expose in the demo UI."""
        return {
            "advertised_ip": self.last_advertised_ip,
            "last_server_command": self.last_server_command,
            "last_resolve": self.last_resolve,
            "last_peer_ping": self.last_peer_ping,
            "last_delivery": self.last_delivery,
            "events": self.delivery_events[:10],
        }

    def server_auto_reconnect(self):
        """Background loop that attempts reconnect when server is marked down."""
        while self.running:
            if self.server_down:
                logger.warning("Active server marked as down; attempting auto-reconnect")
                if self.auto_connect():
                    self.server_down = False
                    logger.info(
                        "Client reconnected to server '%s' at %s",
                        self.server_name,
                        self.server_address,
                    )
            time.sleep(3)


    def _credentials_path(self, username):
        os.makedirs(self.auth_directory, exist_ok=True)
        return os.path.join(self.auth_directory, f"{username}.json")

    def has_local_profile(self, username):
        return os.path.exists(self._credentials_path(username))

    def list_local_profiles(self):
        if not os.path.isdir(self.auth_directory):
            return []
        profiles = []
        for entry in os.listdir(self.auth_directory):
            if entry.endswith(".json"):
                profiles.append(entry[:-5])
        return sorted(profiles)

    def _hash_password(self, password, salt):
        return hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            200_000,
        )

    def create_local_profile(self, username, password):
        if self.has_local_profile(username):
            raise ValueError("Local profile already exists")

        salt = secrets.token_bytes(16)
        password_hash = self._hash_password(password, salt)
        profile = {
            "username": username,
            "salt": salt.hex(),
            "password_hash": password_hash.hex(),
        }
        with open(self._credentials_path(username), "w", encoding="utf-8") as handle:
            json.dump(profile, handle)
        logger.info("Created local profile for user '%s'", username)

    def delete_local_profile(self, username):
        """Remove a partially created local profile and key material."""
        credentials_path = self._credentials_path(username)
        if os.path.exists(credentials_path):
            os.remove(credentials_path)

        keys_path = os.path.join(os.path.dirname(__file__), "keys", username)
        if os.path.isdir(keys_path):
            shutil.rmtree(keys_path)
        logger.info("Deleted local profile for user '%s'", username)

    def authenticate_local_profile(self, username, password):
        if not self.has_local_profile(username):
            return False

        with open(self._credentials_path(username), "r", encoding="utf-8") as handle:
            profile = json.load(handle)

        salt = bytes.fromhex(profile["salt"])
        expected = bytes.fromhex(profile["password_hash"])
        current = self._hash_password(password, salt)
        return hmac.compare_digest(current, expected)

    def set_user(self, username, password=None):
        """Configure client for `username`, initialize DB and crypto, start background threads."""
        self.username = username
        self.db.set_db(username)
        import crypto_manager
        self.crypto = crypto_manager.CryptoManager(username, password=password)
        self._load_pending_messages()
        self.run_background()
        logger.info("Client session initialized for user '%s'", username)

    def _load_pending_messages(self):
        """Hydrate the in-memory pending queue from persistent storage."""
        pending = {}
        for _, recipient, payload in self.db.get_pending_messages():
            pending.setdefault(recipient, []).append(payload)
        with self.pending_lock:
            self.pending_list = pending

    def _remove_pending_cache_item(self, recipient, payload):
        with self.pending_lock:
            messages = self.pending_list.get(recipient, [])
            for index, queued_payload in enumerate(messages):
                if queued_payload == payload:
                    messages.pop(index)
                    break
            if not messages and recipient in self.pending_list:
                del self.pending_list[recipient]


    def read_response(self, socket):
        """Read a possibly segmented response from a socket until end marker or short chunk."""
        response = ''
        address = None

        while True:
            part, address = socket.recvfrom(8192)
            response += part.decode()
            if response.endswith('\r\n') or len(part) < 8192:
                break
        return response, address

    def send_command(self, command, operation_id=None) -> str:
        """Send a command to the configured server and return its response string.

        Marks the server as down on any communication error.
        """
        operation_id = operation_id or self._operation_id("server")
        command_summary = summarize_command(command)
        peer_ip = self.server_address[0] if self.server_address else None
        peer_port = self.server_address[1] if self.server_address else None
        start = time.monotonic()
        try:
            self.client_socket.sendto(f"{command}".encode(), self.server_address)
            response, _ = self.read_response(self.client_socket)
            duration_ms = int((time.monotonic() - start) * 1000)
            response_status = response.split(" ", 1)[0] if response else "EMPTY"
            self.last_server_command = {
                "time": self._event_time(),
                "command": command_summary.get("command"),
                "peer_ip": peer_ip,
                "peer_port": peer_port,
                "status": response_status,
                "duration_ms": duration_ms,
            }
            log_event(
                logger,
                "INFO",
                "server_command_completed",
                node=self.username,
                session_id=self.session_id,
                operation_id=operation_id,
                phase="server_command",
                peer=str(self.server_address),
                peer_ip=peer_ip,
                peer_port=peer_port,
                username=command_summary.get("username"),
                version=command_summary.get("version"),
                duration_ms=duration_ms,
                result={"request": command_summary, "response_status": response_status},
            )
            return response
        except Exception as e:
            duration_ms = int((time.monotonic() - start) * 1000)
            self.server_down = True
            self.last_server_command = {
                "time": self._event_time(),
                "command": command_summary.get("command"),
                "peer_ip": peer_ip,
                "peer_port": peer_port,
                "status": "ERROR",
                "duration_ms": duration_ms,
                "reason": str(e),
            }
            log_event(
                logger,
                "ERROR",
                "server_command_failed",
                node=self.username,
                session_id=self.session_id,
                operation_id=operation_id,
                phase="server_command",
                peer=str(self.server_address),
                peer_ip=peer_ip,
                peer_port=peer_port,
                username=command_summary.get("username"),
                version=command_summary.get("version"),
                duration_ms=duration_ms,
                reason=str(e),
                result=command_summary,
            )
            return f"ERROR in communication with server: {e}"

    def send_message(self, recipient, message):
        """Send `message` to `recipient`. Handles encryption and pending delivery.

        `message` is expected in the form 'MESSAGE <sender> <text>'. Returns True on success.
        """
        operation_id = self._operation_id("msg")
        start = time.monotonic()
        _, sender, text = message.split(" ", 2)
        self.last_delivery = {
            "time": self._event_time(),
            "operation_id": operation_id,
            "recipient": recipient,
            "status": "started",
        }
        log_event(
            logger,
            "INFO",
            "message_delivery_started",
            node=self.username,
            session_id=self.session_id,
            operation_id=operation_id,
            phase="start",
            username=sender,
            peer=recipient,
            result={"text_length": len(text)},
        )

        if recipient == self.username:
            self.db.insert_new_message(self.username, recipient, text, True)
            duration_ms = int((time.monotonic() - start) * 1000)
            self.last_delivery = {
                "time": self._event_time(),
                "operation_id": operation_id,
                "recipient": recipient,
                "status": "delivered",
                "reason": "loopback",
                "duration_ms": duration_ms,
            }
            self._remember_delivery_event("message_delivered", recipient=recipient, reason="loopback")
            log_event(
                logger,
                "INFO",
                "message_delivery_completed",
                node=self.username,
                session_id=self.session_id,
                operation_id=operation_id,
                phase="loopback",
                username=sender,
                peer=recipient,
                duration_ms=duration_ms,
                reason="loopback",
                result="stored_locally",
            )
            return True

        try:
            address = self.contact_list.get(recipient)
            if not address:
                address = self.resolve_user(recipient, operation_id=operation_id)

            if not address:
                duration_ms = int((time.monotonic() - start) * 1000)
                self.last_delivery = {
                    "time": self._event_time(),
                    "operation_id": operation_id,
                    "recipient": recipient,
                    "status": "queued",
                    "reason": "resolve_failed",
                    "duration_ms": duration_ms,
                }
                self._remember_delivery_event("message_queued", recipient=recipient, reason="resolve_failed")
                log_event(
                    logger,
                    "WARNING",
                    "message_delivery_failed",
                    node=self.username,
                    session_id=self.session_id,
                    operation_id=operation_id,
                    phase="resolve",
                    username=sender,
                    peer=recipient,
                    duration_ms=duration_ms,
                    reason="resolve_failed",
                )
                return False

            if not self.ensure_peer_key(recipient, operation_id=operation_id):
                duration_ms = int((time.monotonic() - start) * 1000)
                self.last_delivery = {
                    "time": self._event_time(),
                    "operation_id": operation_id,
                    "recipient": recipient,
                    "address": f"{address[0]}:{address[1]}",
                    "status": "queued",
                    "reason": "peer_key_missing",
                    "duration_ms": duration_ms,
                }
                self._remember_delivery_event("message_queued", recipient=recipient, reason="peer_key_missing")
                log_event(
                    logger,
                    "WARNING",
                    "message_delivery_failed",
                    node=self.username,
                    session_id=self.session_id,
                    operation_id=operation_id,
                    phase="peer_key",
                    username=sender,
                    peer=recipient,
                    peer_ip=address[0],
                    peer_port=address[1],
                    duration_ms=duration_ms,
                    reason="peer_key_missing",
                )
                return False

            encrypted_text = self.crypto.encrypt_message(recipient, text)
            wire_message = f"MESSAGE {sender} {encrypted_text}"

            if self.is_user_online(address, operation_id=operation_id, recipient=recipient):
                self.db.insert_new_message(self.username, recipient, text, True)
                self.message_socket.sendto(wire_message.encode(), address)
                duration_ms = int((time.monotonic() - start) * 1000)
                self.last_delivery = {
                    "time": self._event_time(),
                    "operation_id": operation_id,
                    "recipient": recipient,
                    "address": f"{address[0]}:{address[1]}",
                    "status": "delivered",
                    "duration_ms": duration_ms,
                }
                self._remember_delivery_event(
                    "message_delivered",
                    recipient=recipient,
                    address=f"{address[0]}:{address[1]}",
                )
                log_event(
                    logger,
                    "INFO",
                    "message_delivery_completed",
                    node=self.username,
                    session_id=self.session_id,
                    operation_id=operation_id,
                    phase="p2p_send",
                    username=sender,
                    peer=recipient,
                    peer_ip=address[0],
                    peer_port=address[1],
                    duration_ms=duration_ms,
                    result="sent",
                )
                return True
            else:
                address = self.resolve_user(recipient, operation_id=operation_id)
                if address and self.is_user_online(address, operation_id=operation_id, recipient=recipient):
                    self.db.insert_new_message(self.username, recipient, text, True)
                    self.message_socket.sendto(wire_message.encode(), address)
                    duration_ms = int((time.monotonic() - start) * 1000)
                    self.last_delivery = {
                        "time": self._event_time(),
                        "operation_id": operation_id,
                        "recipient": recipient,
                        "address": f"{address[0]}:{address[1]}",
                        "status": "delivered",
                        "reason": "after_resolve_refresh",
                        "duration_ms": duration_ms,
                    }
                    self._remember_delivery_event(
                        "message_delivered",
                        recipient=recipient,
                        address=f"{address[0]}:{address[1]}",
                        reason="after_resolve_refresh",
                    )
                    log_event(
                        logger,
                        "INFO",
                        "message_delivery_completed",
                        node=self.username,
                        session_id=self.session_id,
                        operation_id=operation_id,
                        phase="p2p_send_after_refresh",
                        username=sender,
                        peer=recipient,
                        peer_ip=address[0],
                        peer_port=address[1],
                        duration_ms=duration_ms,
                        reason="after_resolve_refresh",
                        result="sent",
                    )
                    return True
                duration_ms = int((time.monotonic() - start) * 1000)
                self.last_delivery = {
                    "time": self._event_time(),
                    "operation_id": operation_id,
                    "recipient": recipient,
                    "address": f"{address[0]}:{address[1]}" if address else None,
                    "status": "queued",
                    "reason": "peer_offline",
                    "duration_ms": duration_ms,
                }
                self._remember_delivery_event("message_queued", recipient=recipient, reason="peer_offline")
                log_event(
                    logger,
                    "WARNING",
                    "message_delivery_failed",
                    node=self.username,
                    session_id=self.session_id,
                    operation_id=operation_id,
                    phase="p2p_ping",
                    username=sender,
                    peer=recipient,
                    peer_ip=address[0] if address else None,
                    peer_port=address[1] if address else None,
                    duration_ms=duration_ms,
                    reason="peer_offline",
                )
                return False
        except Exception as e:
            duration_ms = int((time.monotonic() - start) * 1000)
            self.last_delivery = {
                "time": self._event_time(),
                "operation_id": operation_id,
                "recipient": recipient,
                "status": "queued",
                "reason": str(e),
                "duration_ms": duration_ms,
            }
            self._remember_delivery_event("message_queued", recipient=recipient, reason=str(e))
            log_event(
                logger,
                "ERROR",
                "message_delivery_failed",
                node=self.username,
                session_id=self.session_id,
                operation_id=operation_id,
                phase="exception",
                username=sender,
                peer=recipient,
                duration_ms=duration_ms,
                reason=str(e),
            )
            return False

    def add_to_pending_list(self, recipient, message):
        """Add a message to the pending queue for `recipient` (thread-safe)."""
        self.db.add_pending_message(recipient, message)
        with self.pending_lock:
            if recipient in self.pending_list.keys():
                self.pending_list[recipient].append(message)
            else:
                self.pending_list[recipient] = [message]
            queue_count = len(self.pending_list[recipient])
            self._remember_delivery_event("message_queued_persisted", recipient=recipient, queue_count=queue_count)
            log_event(
                logger,
                "INFO",
                "message_queued",
                node=self.username,
                session_id=self.session_id,
                phase="queue",
                peer=recipient,
                username=self.username,
                queue_count=queue_count,
                result="persisted",
            )

    def resolve_user(self, username, operation_id=None):
        """Ask server to resolve `username` and cache the result locally."""
        operation_id = operation_id or self._operation_id("resolve")
        response = self.send_command(f"RESOLVE {username}", operation_id=operation_id)
        if response.startswith("OK"):
            _, ip, port, public_key, _version = response.split(" ", 4)
            self.contact_list[username] = (ip, int(port))
            if self.crypto and public_key:
                self.crypto.store_peer_key(username, public_key)
            self.last_resolve = {
                "time": self._event_time(),
                "operation_id": operation_id,
                "username": username,
                "ip": ip,
                "port": int(port),
                "status": "ok",
                "version": _version,
            }
            log_event(
                logger,
                "INFO",
                "resolve_completed",
                node=self.username,
                session_id=self.session_id,
                operation_id=operation_id,
                phase="resolve",
                peer=username,
                peer_ip=ip,
                peer_port=int(port),
                username=username,
                version=_version,
                result="cached",
            )
            return (ip, int(port))
        else:
            self.last_resolve = {
                "time": self._event_time(),
                "operation_id": operation_id,
                "username": username,
                "status": "failed",
                "reason": response,
            }
            log_event(
                logger,
                "WARNING",
                "resolve_failed",
                node=self.username,
                session_id=self.session_id,
                operation_id=operation_id,
                phase="resolve",
                peer=username,
                username=username,
                reason=response,
            )
            return None

    def ensure_peer_key(self, recipient, timeout=5, operation_id=None):
        """Ensure we have a trusted server-backed public key for `recipient`."""
        if self.crypto.has_peer_key(recipient):
            log_event(
                logger,
                "INFO",
                "peer_key_ready",
                node=self.username,
                session_id=self.session_id,
                operation_id=operation_id,
                phase="peer_key",
                peer=recipient,
                result="cached",
            )
            return True

        resolved = self.resolve_user(recipient, operation_id=operation_id) is not None
        log_event(
            logger,
            "INFO" if resolved else "WARNING",
            "peer_key_ready" if resolved else "peer_key_missing",
            node=self.username,
            session_id=self.session_id,
            operation_id=operation_id,
            phase="peer_key",
            peer=recipient,
            result="resolved" if resolved else "missing",
        )
        return resolved
        
    def _register_remote_user(self, username):
        try:
            server_ip = self.server_address[0] if self.server_address else None
            message_ip = self.get_ip(server_ip)
            self.last_advertised_ip = message_ip
            _, message_port = self.message_socket.getsockname()
            version = time.time_ns()
            public_key = self.crypto.get_public_key_b64()
            signed_payload = f"{username}|{message_ip}|{message_port}|{version}|{public_key}"
            signature = self.crypto.sign_text(signed_payload)
            command = (
                f"REGISTER {username} {message_ip} {message_port} "
                f"{version} {public_key} {signature}"
            )

            for attempt in range(2):
                operation_id = self._operation_id("register")
                response = self.send_command(command, operation_id=operation_id)
                log_event(
                    logger,
                    "INFO",
                    "presence_register_attempt",
                    node=username,
                    session_id=self.session_id,
                    operation_id=operation_id,
                    phase="register",
                    username=username,
                    version=version,
                    advertised_ip=message_ip,
                    peer_ip=server_ip,
                    peer_port=self.server_address[1] if self.server_address else None,
                    result={"attempt": attempt + 1, "response_status": response.split(" ", 1)[0]},
                )
                if response.startswith("OK"):
                    return True

                # If the selected server just died, reconnect synchronously and retry once.
                if self.server_down and attempt == 0 and self.auto_connect():
                    self.server_down = False
                    log_event(
                        logger,
                        "WARNING",
                        "presence_register_retry",
                        node=username,
                        session_id=self.session_id,
                        operation_id=operation_id,
                        phase="register",
                        username=username,
                        advertised_ip=message_ip,
                        peer=str(self.server_address),
                        reason="server_reconnected",
                    )
                    continue
                break

            return False
        except Exception as e:
            log_event(
                logger,
                "ERROR",
                "presence_register_failed",
                node=username,
                session_id=self.session_id,
                phase="register",
                username=username,
                reason=str(e),
            )
            return False

    def register_user(self, username, password):
        """Create a protected local profile and register the username on the server."""
        if self.has_local_profile(username):
            return False, "This username already exists on this device. Use login instead."

        try:
            self.create_local_profile(username, password)
            self.set_user(username, password=password)
        except Exception as e:
            return False, f"Unable to initialize secure profile: {e}"

        if self._register_remote_user(username):
            logger.info("User '%s' registered locally and remotely", username)
            return True, None

        self.username = None
        self.crypto = None
        self.delete_local_profile(username)
        logger.error("Remote registration failed for '%s'", username)
        return False, "Unable to register on the selected server."

    def login_user(self, username, password):
        """Unlock an existing local profile and refresh presence on the server."""
        if not self.authenticate_local_profile(username, password):
            return False, "Invalid credentials"

        try:
            self.set_user(username, password=password)
        except Exception:
            return False, "Unable to unlock local encryption keys"

        if self._register_remote_user(username):
            logger.info("User '%s' logged in and presence refreshed", username)
            return True, None

        self.username = None
        self.crypto = None
        logger.error("Unable to refresh remote presence for '%s'", username)
        return False, "Unable to refresh your connection with the server."

    def authenticate_user(self, username, password):
        """Log in with an existing local profile or create one on first access."""
        if self.has_local_profile(username):
            return self.login_user(username, password)
        return self.register_user(username, password)

    def discover_servers(self):
        """Discover servers on the local network using UDP broadcast."""
        self.client_socket.settimeout(3)
        servers = []
        broadcast_address = ("<broadcast>", 12345)
        self.client_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            self.client_socket.sendto("DISCOVER".encode(), broadcast_address)
            while True:
                data, address = self.client_socket.recvfrom(1024)
                server_name = data.decode()
                servers.append((server_name, address[0]))
        except socket.timeout:
            pass

        logger.info("Broadcast discovery found %s server(s)", len(servers))

        return servers

    def connect_to_server(self, server):
        """Set the active server address from a `(name, ip)` tuple."""
        try:
            self.server_address = (server[1], 12345)
            self.server_name = server[0]
            logger.info("Connected to server '%s' at %s", self.server_name, self.server_address)
        except Exception as e:
            return f"ERROR connecting with server: {e}"
        
    def auto_connect(self):
        """Try to auto-connect to the first discovered server. Returns True on success."""
        servers = self.discover_servers()
        if len(servers) == 0:
            logger.warning("Auto-connect could not find any server")
            return False

        self.connect_to_server(servers[0])
        return True


    def load_chat(self, interlocutor):
        """Return the chat history with `interlocutor` from local DB."""
        chat = self.db.get_previous_chat(self.username, interlocutor)
        return chat


    def listen_for_messages(self):
        """Background loop that receives messages on `self.message_socket` and handles them."""
        while self.running:
            try:
                message, address = self.read_response(self.message_socket)
                if message.startswith("MESSAGE"):
                    _, sender, encrypted_text = message.split(" ", 2)

                    if not self.crypto.has_peer_key(sender):
                        self.resolve_user(sender)

                    if not self.crypto.has_peer_key(sender):
                        log_event(
                            logger,
                            "WARNING",
                            "message_dropped",
                            node=self.username,
                            session_id=self.session_id,
                            phase="receive",
                            peer=sender,
                            peer_ip=address[0],
                            peer_port=address[1],
                            reason="peer_key_missing",
                        )
                        continue

                    try:
                        text = self.crypto.decrypt_message(encrypted_text)
                    except Exception as exc:
                        log_event(
                            logger,
                            "WARNING",
                            "message_dropped",
                            node=self.username,
                            session_id=self.session_id,
                            phase="decrypt",
                            peer=sender,
                            peer_ip=address[0],
                            peer_port=address[1],
                            reason=str(exc),
                        )
                        continue

                    self.db.insert_new_message(sender, self.username, text, False)
                    self.contact_list[sender] = address
                    self._remember_delivery_event(
                        "message_received",
                        sender=sender,
                        address=f"{address[0]}:{address[1]}",
                    )
                    log_event(
                        logger,
                        "INFO",
                        "message_received",
                        node=self.username,
                        session_id=self.session_id,
                        phase="receive",
                        peer=sender,
                        peer_ip=address[0],
                        peer_port=address[1],
                        username=sender,
                        result={"text_length": len(text)},
                    )

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
                        log_event(
                            logger,
                            "INFO",
                            "peer_key_shared",
                            node=self.username,
                            session_id=self.session_id,
                            phase="peer_key",
                            peer=requester,
                            peer_ip=address[0],
                            peer_port=address[1],
                            result="PUBKEY_RES",
                        )
                        if not self.crypto.has_peer_key(requester):
                            req = f"PUBKEY_REQ {self.username}"
                            self.message_socket.sendto(req.encode(), address)

                elif message.startswith("PUBKEY_RES"):
                    _, peer_username, b64_key = message.split(" ", 2)
                    if self.crypto:
                        resolved_address = self.resolve_user(peer_username)
                        if resolved_address and self.crypto.has_peer_key(peer_username):
                            self.contact_list[peer_username] = address
                            log_event(
                                logger,
                                "INFO",
                                "peer_key_validated",
                                node=self.username,
                                session_id=self.session_id,
                                phase="peer_key",
                                peer=peer_username,
                                peer_ip=address[0],
                                peer_port=address[1],
                                result="identity_manager_match",
                            )
                            event = self.pending_key_exchanges.get(peer_username)
                            if event:
                                event.set()

                elif message.startswith("PING"):
                    self.message_socket.sendto("PONG".encode(), address)
            except socket.timeout:
                continue
            except Exception as exc:
                log_event(
                    logger,
                    "DEBUG",
                    "client_listener_error",
                    node=self.username,
                    session_id=self.session_id,
                    phase="receive",
                    reason=str(exc),
                )

    def is_user_online(self, address, operation_id=None, recipient=None):
        """Return True if a short UDP ping to `address` receives a PONG reply."""
        start = time.monotonic()
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(0.5)
                sock.bind(('', 0))
                sock.sendto(f"PING".encode(), address)
                response, _ = sock.recvfrom(1024)
                ok = response.decode() == "PONG"
                duration_ms = int((time.monotonic() - start) * 1000)
                self.last_peer_ping = {
                    "time": self._event_time(),
                    "operation_id": operation_id,
                    "recipient": recipient,
                    "ip": address[0],
                    "port": address[1],
                    "ok": ok,
                    "duration_ms": duration_ms,
                }
                log_event(
                    logger,
                    "INFO" if ok else "WARNING",
                    "peer_ping_completed",
                    node=self.username,
                    session_id=self.session_id,
                    operation_id=operation_id,
                    phase="p2p_ping",
                    peer=recipient,
                    peer_ip=address[0],
                    peer_port=address[1],
                    duration_ms=duration_ms,
                    result="PONG" if ok else response.decode(),
                )
                return ok
        except socket.timeout:
            reason = "timeout"
        except Exception as exc:
            reason = str(exc)
        duration_ms = int((time.monotonic() - start) * 1000)
        self.last_peer_ping = {
            "time": self._event_time(),
            "operation_id": operation_id,
            "recipient": recipient,
            "ip": address[0],
            "port": address[1],
            "ok": False,
            "duration_ms": duration_ms,
            "reason": reason,
        }
        log_event(
            logger,
            "WARNING",
            "peer_ping_failed",
            node=self.username,
            session_id=self.session_id,
            operation_id=operation_id,
            phase="p2p_ping",
            peer=recipient,
            peer_ip=address[0],
            peer_port=address[1],
            duration_ms=duration_ms,
            reason=reason,
        )
        return False

    def send_pending_messages(self):
        """Background worker that retries delivery of pending messages."""
        while self.running:
            try:
                for message_id, username, payload in self.db.get_pending_messages():
                    if self.send_message(username, payload):
                        self.db.delete_pending_message(message_id)
                        self._remove_pending_cache_item(username, payload)
                        logger.info("Delivered pending message to '%s'", username)
                time.sleep(1)
            except Exception as e:
                logger.error("Error sending pending messages: %s", e)
                pass

    def _is_loopback_ip(self, ip):
        try:
            return ipaddress.ip_address(ip).is_loopback
        except ValueError:
            return False

    def _valid_ipv4(self, ip):
        try:
            parsed = ipaddress.ip_address(ip)
            return parsed.version == 4 and not parsed.is_unspecified
        except ValueError:
            return False

    def _hostname_candidates(self):
        candidates = []
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ip = info[4][0]
                if ip not in candidates:
                    candidates.append(ip)
        except Exception:
            pass
        try:
            ip = socket.gethostbyname(socket.gethostname())
            if ip not in candidates:
                candidates.append(ip)
        except Exception:
            pass
        return candidates

    def get_ip(self, target_ip=None):
        """Return the best local IP to advertise to peers.

        Priority:
        1. FLOCK_PUBLIC_IP, when explicitly configured.
        2. The source IP selected by the OS route to the active server.
        3. A non-loopback hostname address.
        4. 127.0.0.1 only when the selected server is local or no better address exists.
        """
        explicit_ip = os.environ.get("FLOCK_PUBLIC_IP", "").strip()
        if explicit_ip:
            if self._valid_ipv4(explicit_ip):
                self.last_advertised_ip = explicit_ip
                return explicit_ip
            log_event(
                logger,
                "WARNING",
                "advertised_ip_invalid",
                node=self.username,
                session_id=self.session_id,
                advertised_ip=explicit_ip,
                reason="FLOCK_PUBLIC_IP is not a valid IPv4 address",
            )

        if target_ip:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                    sock.connect((target_ip, 12345))
                    candidate = sock.getsockname()[0]
                    if self._valid_ipv4(candidate) and (
                        not self._is_loopback_ip(candidate) or self._is_loopback_ip(target_ip)
                    ):
                        self.last_advertised_ip = candidate
                        return candidate
            except Exception as exc:
                log_event(
                    logger,
                    "WARNING",
                    "advertised_ip_route_failed",
                    node=self.username,
                    session_id=self.session_id,
                    peer_ip=target_ip,
                    reason=str(exc),
                )

        for candidate in self._hostname_candidates():
            if self._valid_ipv4(candidate) and not self._is_loopback_ip(candidate):
                self.last_advertised_ip = candidate
                return candidate

        fallback = "127.0.0.1"
        self.last_advertised_ip = fallback
        return fallback

    def run_background(self):
        """Start background threads for message receiving, pending delivery and reconnection."""
        if self.background_started:
            return
        threading.Thread(target=self.listen_for_messages, daemon=True).start()
        threading.Thread(target=self.send_pending_messages, daemon=True).start()
        threading.Thread(target=self.server_auto_reconnect, daemon=True).start()
        self.background_started = True
        time.sleep(1)
        logger.info("Background client workers started")


    def discover_servers_multicast(self, timeout: int = 3) -> list:
        """Discover servers using multicast; return list of (name, ip) tuples."""
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
            logger.error("Error sending multicast discovery: %s", e)
            return []

        # helper debug removed

        servers = []
        start_time = time.time()
        while True:
            try:
                data, addr = sock.recvfrom(BUFFER_SIZE)
                server_ip = data.decode().strip()
                servers.append(server_ip)
                logger.info("Multicast discovery received server %s from %s", server_ip, addr)
            except socket.timeout:
                break
            except Exception as e:
                logger.error("Error receiving multicast discovery data: %s", e)
                break
            if time.time() - start_time > timeout:
                break

        sock.close()

        servers = [("main", server) for server in servers]
        logger.info("Multicast discovery found %s server(s)", len(servers))

        return servers


if __name__ == "__main__":
    # client = chat_client()
    # client.run_ui()
    pass
