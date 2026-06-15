import socket
import threading
import sys
import json
import db_manager
import time
import random
import os
import base64
import hashlib
import ipaddress
# from termcolor import colored as col
import struct
from logging_utils import configure_logger, log_event, summarize_command
try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.exceptions import InvalidSignature
except ModuleNotFoundError:
    hashes = None
    serialization = None
    padding = None

    class InvalidSignature(Exception):
        pass


logger = configure_logger("flock.server", "server.log")


HASH_MOD = 10**18+3
FAIL_TOLERANCE = int(os.environ.get("FLOCK_FAIL_TOLERANCE", "3"))
REPLICA_FULL_SYNC_INTERVAL = float(os.environ.get("FLOCK_REPLICA_FULL_SYNC_INTERVAL", "30"))
STATUS_LOG_INTERVAL = float(os.environ.get("FLOCK_STATUS_LOG_INTERVAL", "30"))


class ChatServer:
    """Distributed chat server node implementing a simple ring and replication logic.

    The server listens for UDP commands, maintains a local SQLite DB via `server.db_manager`,
    and runs background tasks for pinging, replication and multicast discovery.
    """
    def __init__(self, name):
        self.name = name

        self.command_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.command_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        self.ping_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.ping_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        self.db_manager = db_manager.server_db()
        self.db_lock = threading.RLock()

        self.lower_bound = 0
        self.upper_bound = HASH_MOD - 1

        self.predecessor = None
        self.successor = None
        self.successors = []

        self.replics = []
        self.replicants = []

        self.running = True
        self.crisis = False
        log_event(
            logger,
            "INFO",
            "node_initialized",
            node=self.name,
            result={
                "fail_tolerance": FAIL_TOLERANCE,
                "replica_full_sync_interval": REPLICA_FULL_SYNC_INTERVAL,
                "status_log_interval": STATUS_LOG_INTERVAL,
            },
        )

    def is_valid_username(self, username):
        return (
            isinstance(username, str)
            and 3 <= len(username) <= 20
            and "-" not in username
            and not any(char.isspace() for char in username)
            and username.replace("_", "").isalnum()
        )

    def is_valid_client_address(self, ip, port):
        try:
            socket.inet_aton(ip)
            return 0 < int(port) <= 65535
        except Exception:
            return False

    def _valid_ipv4(self, ip):
        try:
            parsed = ipaddress.ip_address(ip)
            return parsed.version == 4 and not parsed.is_unspecified
        except ValueError:
            return False

    def _is_loopback_ip(self, ip):
        try:
            return ipaddress.ip_address(ip).is_loopback
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


    
    #region Start Sequence 
    
    def start(self):
        """Initialize DB, discover/join other servers and start background services."""
        with self.db_lock:
            self.db_manager.set_db(self.name)

        self.print_banner("Arrancando nodo servidor")
        servers = self.discover_servers()

        if not servers:
            logger.info("[OK] No se detectaron otros servidores; este nodo inicia el anillo")
        else:
            for server in servers:
                logger.info("[OK] Nodo descubierto: %s (%s)", server[0], server[1])
            self.join_to_servers(servers)

        self.command_socket.bind(("", 12345))
        self.ping_socket.bind(("", 12346))

        self.print_info()

        threading.Thread(target=self.tape_integrity_check, daemon=True).start()
        threading.Thread(target=self.successors_provider, daemon=True).start()
        threading.Thread(target=self.listen_for_ping, daemon=True).start()
        threading.Thread(target=self.replics_manager, daemon=True).start()
        threading.Thread(target=self.info_updater, daemon=True).start()
        threading.Thread(target=self.multicast_listener, daemon=True).start()

        logger.info("[OK] Servicios de fondo iniciados para '%s'", self.name)
        self.listen_for_messages()

    def print_banner(self, title):
        logger.info("=" * 72)
        logger.info("Flock Server | %s", title)
        logger.info("=" * 72)
        logger.info("Nodo: %s | IP local: %s | Puerto comandos: 12345 | Puerto health: 12346", self.name, self.get_ip())
        logger.info("Replicas configuradas: %s | Sync replicas: cada %.1fs", FAIL_TOLERANCE, REPLICA_FULL_SYNC_INTERVAL)
        logger.info("-" * 72)


    def discover_servers(self):
        """Discover other servers using UDP broadcast and return a list of (name, ip)."""
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(1)
            servers = []
            broadcast_address = ("<broadcast>", 12345)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            try:
                sock.sendto("DISCOVER".encode(), broadcast_address)
                while True:
                    data, address = sock.recvfrom(1024)
                    server_name = data.decode()
                    servers.append((server_name, address[0]))
            except socket.timeout:
                pass

            logger.info("[OK] Descubrimiento broadcast encontro %s nodo(s)", len(servers))
            return servers

    def join_to_servers(self, servers):
        """Join the cluster by requesting to join the server that manages the largest range."""
        longest_range_server = self.get_longest_range_server(servers)
        logger.info("Joining cluster through server %s", longest_range_server)
        self.request_join(longest_range_server)

    def get_longest_range_server(self, servers):
        """Query servers for their range and return the server with largest range."""
        longest_range = -1
        longest_range_server = None
        for server in servers:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                    sock.settimeout(3)
                    sock.sendto(f"RANGE".encode(), (server[1], 12345))
                    data, _ = sock.recvfrom(1024)
                    response = data.decode()
                    if response.startswith("OK"):
                        _, lower_bound, upper_bound = response.split(" ")
                        range = int(upper_bound) - int(lower_bound)
                        if range > longest_range:
                            longest_range_server = server
                            longest_range = range
            except Exception as e:
                logger.warning(f"Error getting range from server '{server[0]}': {e}")

        return longest_range_server
    

    def request_join(self, server):
        """Send a JOIN request to `server` and initialize local bounds & neighbors on success."""
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(1)
            sock.sendto(f"JOIN".encode(), (server[1], 12345))
            data, _ = sock.recvfrom(1024)
            response = data.decode()
            if response.startswith("OK"):
                _, lower_bound, upper_bound, predecessor, successor = response.split()
                self.lower_bound = int(lower_bound)
                self.upper_bound = int(upper_bound)
                self.predecessor = predecessor
                if successor != "_":
                    self.successor = successor
                else:
                    self.successor = None
                log_event(
                    logger,
                    "INFO",
                    "node_joined",
                    node=self.name,
                    peer=server[1],
                    range={"lower": self.lower_bound, "upper": self.upper_bound},
                    result={"predecessor": self.predecessor, "successor": self.successor},
                )
            else:
                logger.error(f"Joining request failed: {response}") 
                raise ValueError

    def get_successors(self):
        """Return a list of successor IPs queried from the current successor node."""
        successors = []
        if self.successor:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                try:
                    sock.settimeout(0.5)
                    sock.sendto(f"SUCC {FAIL_TOLERANCE+1}".encode(), (self.successor, 12345))
                    data, _ = sock.recvfrom(1024)
                    response = data.decode()
                    if response.startswith("OK"):
                        _, successors = response.split(" ", 1)
                        successors = successors.split(" ")
                    else:
                        successors = self.successors
                except Exception as e:
                    logger.warning(f"Getting backup successors failed with exception: {e}")
                    successors = self.successors
            return successors
    


    #region Commands

    def listen_for_messages(self):
        """Main loop handling incoming UDP command messages on `self.command_socket`."""
        while self.running:
            try:
                data, address = self.command_socket.recvfrom(65535)
                message = data.decode()

                if message != "PING":
                    command_summary = summarize_command(message)
                    log_event(
                        logger,
                        "DEBUG",
                        "command_received",
                        node=self.name,
                        peer=address[0],
                        username=command_summary.get("username"),
                        version=command_summary.get("version"),
                        result=command_summary,
                    )

                if message.startswith("DISCOVER"):
                    self.command_socket.sendto(f"{self.name}".encode(), address)

                elif message.startswith("PING"):
                    self.command_socket.sendto("PONG".encode(), address)

                elif message.startswith("RANGE"):
                    self.command_socket.sendto(f"OK {self.lower_bound} {self.upper_bound}".encode(), address)

                elif message.startswith("STATUS"):
                    self.send_json_response(address, self.status_payload())

                elif message.startswith("SNAPSHOT"):
                    self.send_json_response(address, self.snapshot_payload())

                elif message.startswith("CHECKSUM"):
                    self.send_json_response(address, self.checksum_payload())

                elif message.startswith("SYNC_FROM"):
                    try:
                        _, owner = message.split(" ", 1)
                    except ValueError:
                        self.send_json_response(address, {"error": "missing owner"}, ok=False)
                        continue
                    self.send_json_response(address, self.sync_from_owner(owner.strip()))

                elif message.startswith("JOIN"):
                    log_event(logger, "INFO", "node_joined", node=self.name, peer=address[0], result="join_requested")
                    self.process_join_request(address)
                    self.print_info()

                elif message.startswith("PRED_CHANGE"):
                    _, predecessor = message.split(" ")
                    self.change_predecessor(predecessor)
                    self.print_info()

                elif message.startswith("REGISTER"):
                    payload = self.parse_register_message(message, address)
                    if payload is None:
                        log_event(
                            logger,
                            "WARNING",
                            "register_rejected",
                            node=self.name,
                            peer=f"{address[0]}:{address[1]}",
                            peer_ip=address[0],
                            peer_port=address[1],
                            phase="parse",
                            reason="malformed_payload",
                        )
                        continue
                    log_event(
                        logger,
                        "INFO",
                        "register_received",
                        node=self.name,
                        peer=f"{address[0]}:{address[1]}",
                        peer_ip=address[0],
                        peer_port=address[1],
                        phase="receive",
                        username=payload["username"],
                        version=payload["version"],
                        advertised_ip=payload["ip"],
                        result={"advertised_port": payload["port"]},
                    )
                    self.register_user(**payload)

                elif message.startswith("RESOLVE"):
                    try:
                        _, answer_to_ip, answer_to_port, username = message.split(" ")  
                    except Exception:
                        _, username = message.split(" ")
                        answer_to_ip = address[0]
                        answer_to_port = address[1]
                    log_event(
                        logger,
                        "INFO",
                        "resolve_received",
                        node=self.name,
                        peer=f"{address[0]}:{address[1]}",
                        peer_ip=address[0],
                        peer_port=address[1],
                        phase="receive",
                        username=username,
                        result={"answer_to": f"{answer_to_ip}:{answer_to_port}"},
                    )
                    self.resolve_user(answer_to_ip, int(answer_to_port), username)

                elif message.startswith("SUCC"):
                    _, successors = message.split(" ", 1)
                    successors_list = successors.split(" ")
                    self.successors = successors_list[: FAIL_TOLERANCE + 1]
                    if self.predecessor:
                        self.command_socket.sendto(f"SUCC {self.get_ip()} {successors}".encode(), (self.predecessor, 12345))

                elif message.startswith("FIX"):
                    self.crisis = True
                    log_event(logger, "WARNING", "fix_started", node=self.name, peer=address[0], result="broadcast_received")
                    self.fix_tape()
                    self.replicants_manager()
                    self.correct_bd()
                    self.crisis = False

                elif message.startswith("REPLIC"):
                    try:
                        _, username, ip, port, version, public_key = message.split(" ", 5)
                    except ValueError:
                        logger.warning("Rejected malformed REPLIC payload from %s", address)
                        continue
                    if address[0] not in self.replicants:
                        self.replicants.append(address[0])
                    with self.db_lock:
                        resolution, stored = self.db_manager.upsert_replic_user(
                            username,
                            ip,
                            int(port),
                            public_key=public_key,
                            version=int(version),
                            owner=address[0],
                        )
                    if stored:
                        log_event(
                            logger,
                            "INFO",
                            "replica_written",
                            node=self.name,
                            peer=address[0],
                            username=username,
                            version=version,
                            result=resolution,
                        )
                    else:
                        logger.warning("Rejected replica for '%s' from %s (%s)", username, address[0], resolution)

                elif message.startswith("TAKEOVER"):
                    try:
                        _, username, ip, port, version, public_key = message.split(" ", 5)
                    except ValueError:
                        logger.warning("Rejected malformed TAKEOVER payload from %s", address)
                        continue
                    self.place_user_record(
                        username,
                        ip,
                        int(port),
                        public_key,
                        int(version),
                    )
                    logger.info("Accepted TAKEOVER for user '%s'", username)

                elif message.startswith("DROP_REPLICS"):
                    _, owner = message.split(" ")
                    with self.db_lock:
                        self.db_manager.drop_replics(owner)
                    try:
                        self.replicants.remove(owner)
                    except Exception:
                        pass

                elif message.startswith("KILL"):
                    self.running = False
                    time.sleep(3)
                    return

            except Exception as e:
                logger.error(f"Server error: {e}")


    def listen_for_ping(self):
        """Respond to simple PING messages on the ping socket with PONG."""
        while self.running:
            try:
                data, address = self.ping_socket.recvfrom(1024)
                message = data.decode()
                if message == "PING":
                    self.ping_socket.sendto("PONG".encode(), address)
            except:
                pass


    def change_predecessor(self, predecessor):
        """Update the predecessor pointer for this node."""
        self.predecessor = predecessor
        logger.info("Predecessor updated to %s", predecessor)

    def parse_register_message(self, message, address):
        parts = message.split(" ")
        if len(parts) == 7:
            _, username, ip, port, version, public_key, signature = parts
            answer_to_ip, answer_to_port = address[0], address[1]
        elif len(parts) == 9:
            _, answer_to_ip, answer_to_port, username, ip, port, version, public_key, signature = parts
        else:
            return None

        try:
            return {
                "answer_to_ip": answer_to_ip,
                "answer_to_port": int(answer_to_port),
                "username": username,
                "ip": ip,
                "port": int(port),
                "version": int(version),
                "public_key": public_key,
                "signature": signature,
            }
        except (TypeError, ValueError):
            return None

    def registration_payload(self, username, ip, port, version, public_key):
        return f"{username}|{ip}|{port}|{version}|{public_key}"

    def verify_registration_signature(self, public_key, payload, signature):
        if not all((serialization, padding, hashes)):
            logger.error("cryptography dependency is not available; cannot verify registrations")
            return False
        try:
            key = serialization.load_pem_public_key(base64.b64decode(public_key))
            key.verify(
                base64.b64decode(signature),
                payload.encode(),
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.MAX_LENGTH,
                ),
                hashes.SHA256(),
            )
            return True
        except (ValueError, TypeError, InvalidSignature):
            return False

    def place_user_record(self, username, ip, port, public_key, version):
        """Route an already authenticated user record to its owning node."""
        username_hash = self.rolling_hash(username)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            if username_hash < self.lower_bound and self.predecessor:
                sock.sendto(
                    f"TAKEOVER {username} {ip} {port} {version} {public_key}".encode(),
                    (self.predecessor, 12345),
                )
                return "forwarded_predecessor"
            elif username_hash > self.upper_bound and self.successor:
                sock.sendto(
                    f"TAKEOVER {username} {ip} {port} {version} {public_key}".encode(),
                    (self.successor, 12345),
                )
                return "forwarded_successor"
            else:
                with self.db_lock:
                    resolution, stored = self.db_manager.upsert_user(
                        username,
                        ip,
                        port,
                        public_key=public_key,
                        version=version,
                    )
                if stored:
                    logger.info("Placed user record '%s' locally (%s)", username, resolution)
                else:
                    logger.warning("Rejected local user record '%s' during placement (%s)", username, resolution)
                return resolution


    def register_user(self, answer_to_ip, answer_to_port, username, ip, port, version, public_key, signature):
        """Register a user in the ring or forward the registration to the appropriate neighbor.

        If the user's hash belongs to this node's range, persist it locally and notify replicas.
        Otherwise forward the REGISTER command to predecessor or successor.
        """
        if (
            not self.is_valid_username(username)
            or not self.is_valid_client_address(ip, port)
            or not public_key
            or not signature
            or version <= 0
        ):
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                if answer_to_ip != ".":
                    sock.sendto(b"ERROR Invalid registration payload", (answer_to_ip, answer_to_port))
            log_event(
                logger,
                "WARNING",
                "register_rejected",
                node=self.name,
                peer=f"{answer_to_ip}:{answer_to_port}",
                peer_ip=answer_to_ip,
                peer_port=answer_to_port,
                phase="validate",
                username=username,
                version=version,
                advertised_ip=ip,
                reason="invalid_payload",
                result={"advertised_port": port},
            )
            return

        username_hash = self.rolling_hash(username)
        payload = self.registration_payload(username, ip, port, version, public_key)

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            if username_hash < self.lower_bound:
                sock.sendto(
                    f"REGISTER {answer_to_ip} {answer_to_port} {username} {ip} {port} {version} {public_key} {signature}".encode(),
                    (self.predecessor, 12345),
                )
                log_event(
                    logger,
                    "INFO",
                    "register_forwarded",
                    node=self.name,
                    peer=self.predecessor,
                    peer_ip=self.predecessor,
                    peer_port=12345,
                    phase="forward_predecessor",
                    username=username,
                    version=version,
                    advertised_ip=ip,
                    result={"hash": username_hash, "range": {"lower": self.lower_bound, "upper": self.upper_bound}},
                )

            elif username_hash > self.upper_bound:
                sock.sendto(
                    f"REGISTER {answer_to_ip} {answer_to_port} {username} {ip} {port} {version} {public_key} {signature}".encode(),
                    (self.successor, 12345),
                )
                log_event(
                    logger,
                    "INFO",
                    "register_forwarded",
                    node=self.name,
                    peer=self.successor,
                    peer_ip=self.successor,
                    peer_port=12345,
                    phase="forward_successor",
                    username=username,
                    version=version,
                    advertised_ip=ip,
                    result={"hash": username_hash, "range": {"lower": self.lower_bound, "upper": self.upper_bound}},
                )
            else:
                with self.db_lock:
                    if not self.verify_registration_signature(public_key, payload, signature):
                        response = "ERROR Invalid registration signature"
                    else:
                        resolution, stored = self.db_manager.upsert_user(
                            username,
                            ip,
                            port,
                            public_key=public_key,
                            version=version,
                        )
                        if not stored and resolution == db_manager.STALE:
                            response = "ERROR Stale registration version"
                        elif not stored and resolution == db_manager.IDENTITY_CONFLICT:
                            response = "ERROR Username belongs to a different identity key"
                        else:
                            response = f"OK User '{username}' in ({ip}:{port}) successfully registered"

                if answer_to_ip != '.':
                    sock.sendto(response.encode(), (answer_to_ip, answer_to_port))
                if not response.startswith("OK"):
                    log_event(
                        logger,
                        "WARNING",
                        "register_rejected",
                        node=self.name,
                        peer=f"{answer_to_ip}:{answer_to_port}",
                        peer_ip=answer_to_ip,
                        peer_port=answer_to_port,
                        phase="store",
                        username=username,
                        version=version,
                        advertised_ip=ip,
                        reason=response,
                        result={"hash": username_hash, "advertised_port": port},
                    )
                    return
                self.replicate_owned_records([(username, ip, port, public_key, version)])
                log_event(
                    logger,
                    "INFO",
                    "register_accepted",
                    node=self.name,
                    peer=f"{answer_to_ip}:{answer_to_port}",
                    peer_ip=answer_to_ip,
                    peer_port=answer_to_port,
                    phase="store",
                    username=username,
                    version=version,
                    advertised_ip=ip,
                    range={"lower": self.lower_bound, "upper": self.upper_bound},
                    result={"status": "stored_and_replicated", "advertised_port": port, "hash": username_hash},
                )


    def resolve_user(self, answer_to_ip, answer_to_port, username):
        """Resolve `username` to an (ip,port) tuple, forwarding the request if needed."""
        username_hash = self.rolling_hash(username)

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            if username_hash < self.lower_bound:
                sock.sendto(
                    f"RESOLVE {answer_to_ip} {answer_to_port} {username}".encode(),
                    (self.predecessor, 12345),
                )
                log_event(
                    logger,
                    "INFO",
                    "resolve_forwarded",
                    node=self.name,
                    peer=self.predecessor,
                    peer_ip=self.predecessor,
                    peer_port=12345,
                    phase="forward_predecessor",
                    username=username,
                    result={"answer_to": f"{answer_to_ip}:{answer_to_port}", "hash": username_hash},
                )

            elif username_hash > self.upper_bound:
                sock.sendto(
                    f"RESOLVE {answer_to_ip} {answer_to_port} {username}".encode(),
                    (self.successor, 12345),
                )
                log_event(
                    logger,
                    "INFO",
                    "resolve_forwarded",
                    node=self.name,
                    peer=self.successor,
                    peer_ip=self.successor,
                    peer_port=12345,
                    phase="forward_successor",
                    username=username,
                    result={"answer_to": f"{answer_to_ip}:{answer_to_port}", "hash": username_hash},
                )

            else:
                with self.db_lock:
                    address = self.db_manager.resolve_user(username)
                if address:
                    ip, port, public_key, version = address
                    response = f"OK {ip} {port} {public_key} {version}"
                    sock.sendto(response.encode(), (answer_to_ip, answer_to_port))
                    log_event(
                        logger,
                        "INFO",
                        "resolve_completed",
                        node=self.name,
                        peer=f"{answer_to_ip}:{answer_to_port}",
                        peer_ip=answer_to_ip,
                        peer_port=answer_to_port,
                        phase="resolve",
                        username=username,
                        version=version,
                        advertised_ip=ip,
                        result={
                            "status": "OK",
                            "answer_to": f"{answer_to_ip}:{answer_to_port}",
                            "resolved": f"{ip}:{port}",
                            "hash": username_hash,
                        },
                    )
                else:
                    response = f"ERROR 404 User not found"
                    sock.sendto(response.encode(), (answer_to_ip, answer_to_port))
                    log_event(
                        logger,
                        "WARNING",
                        "resolve_failed",
                        node=self.name,
                        peer=f"{answer_to_ip}:{answer_to_port}",
                        peer_ip=answer_to_ip,
                        peer_port=answer_to_port,
                        phase="resolve",
                        username=username,
                        reason="user_not_found",
                        result={"hash": username_hash},
                    )


    def process_join_request(self, joinee):
        """Process an incoming JOIN request from `joinee` and hand over half the range."""
        joinee_lower_bound = int((self.lower_bound + self.upper_bound) / 2)
        joinee_upper_bound = self.upper_bound
        joinee_successor = "_" if self.successor is None else self.successor
        joinee_predecessor = self.get_ip()

        self.request_predecessor_change(self.successor, joinee[0])

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(f"OK {joinee_lower_bound} {joinee_upper_bound} {joinee_predecessor} {joinee_successor}".encode(), joinee)

        self.upper_bound = joinee_lower_bound - 1
        self.successor = joinee[0]
        log_event(
            logger,
            "INFO",
            "range_split",
            node=self.name,
            peer=joinee[0],
            range={"lower": self.lower_bound, "upper": self.upper_bound},
            result={"new_successor": self.successor},
        )

    def request_predecessor_change(self, target, new_predecessor):
        """Notify `target` server that its predecessor should be changed to `new_predecessor`."""
        if target is not None:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(1)
                sock.sendto(f"PRED_CHANGE {new_predecessor}".encode(), (target, 12345))
            logger.info("Requested predecessor change on %s -> %s", target, new_predecessor)




    #region Services

    def successors_provider(self):
        """Periodically advertise successor information to predecessor if needed."""
        while self.running:
            if self.successor is None and self.predecessor:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                    sock.sendto(f"SUCC {self.get_ip()}".encode(), (self.predecessor, 12345))
            time.sleep(5)


    def tape_integrity_check(self):
        """Check successor/predecessor liveness and broadcast FIX if ring integrity is compromised."""
        while self.running:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                broadcast_address = ("<broadcast>", 12345)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sock.settimeout(0.1)
                try:
                    if self.successor:
                        sock.sendto("PING".encode(), (self.successor, 12346))
                        _, _ = sock.recvfrom(1024)

                    if self.predecessor:
                        sock.sendto("PING".encode(), (self.predecessor, 12346))
                        _, _ = sock.recvfrom(1024)

                except Exception as e:
                    log_event(
                        logger,
                        "WARNING",
                        "node_unreachable",
                        node=self.name,
                        result=f"integrity_check_failed:{e}",
                    )
                    sock.sendto("FIX".encode(), broadcast_address)
                    log_event(logger, "WARNING", "fix_started", node=self.name, result="broadcasted")

            time.sleep(1)

        logger.info("[INFO] Verificacion de integridad del anillo detenida")

    def fix_tape(self):
        """Attempt to repair the ring by checking neighbors and promoting backups when needed."""
        self.crisis = True
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.1)
            if self.successor:
                try:
                    sock.sendto(b"PING", (self.successor, 12346))
                    _, _ = sock.recvfrom(1024)
                except Exception:
                    log_event(logger, "WARNING", "node_unreachable", node=self.name, peer=self.successor, result="successor_ping_failed")
                    self.fix_tape_forward()
            time.sleep(0.1 * 3 * FAIL_TOLERANCE)
            if self.predecessor:
                try:
                    sock.sendto(b"PING", (self.predecessor, 12346))
                    _, _ = sock.recvfrom(1024)
                except Exception:
                    log_event(logger, "WARNING", "node_unreachable", node=self.name, peer=self.predecessor, result="predecessor_ping_failed")
                    self.fix_tape_backward()
        self.print_info()
        self.crisis = False

                
    def fix_tape_forward(self):
        """Select a new successor from backup successors when the current successor fails."""
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.1)
            for successor in self.successors:
                try:
                    sock.sendto(f"PING".encode(), (successor, 12346))
                    _, _ = sock.recvfrom(1024)
                    sock.settimeout(5)
                    sock.sendto(f"RANGE".encode(), (successor, 12345))
                    data, _ = sock.recvfrom(1024)
                    data.decode()
                    _, lower_bound, _ = data.split()
                    self.upper_bound = int(lower_bound) - 1
                    self.request_predecessor_change(successor, self.get_ip())
                    self.successor = successor
                    log_event(
                        logger,
                        "WARNING",
                        "successor_promoted",
                        node=self.name,
                        peer=successor,
                        range={"lower": self.lower_bound, "upper": self.upper_bound},
                        result="backup_successor_alive",
                    )
                    return
                except Exception as e:
                    log_event(logger, "WARNING", "node_unreachable", node=self.name, peer=successor, result=str(e))
            self.upper_bound = HASH_MOD - 1
            self.successor = None
            self.successors = []
            logger.warning("No live successor found; node now owns tail of ring")

    def fix_tape_backward(self):
        """Handle predecessor failure by instructing it to terminate and clearing predecessor state."""
        if self.predecessor:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(0.5)
                try:
                    sock.sendto(f"PING".encode(), (self.predecessor, 12346))
                    _, _ = sock.recvfrom(1024)
                except Exception:
                    failed_predecessor = self.predecessor
                    sock.sendto(f"KILL".encode(), (failed_predecessor, 12345))
                    self.lower_bound = 0
                    self.predecessor = None
                    log_event(logger, "WARNING", "node_unreachable", node=self.name, peer=failed_predecessor, result="predecessor_removed")


    def correct_bd(self):
        """Move users that do not belong to this node's range to the correct nodes."""
        with self.db_lock:
            alien_users = self.db_manager.get_alien_users(self.lower_bound, self.upper_bound, self.rolling_hash)
        for user in alien_users:
            self.place_user_record(
                user[0],
                user[1],
                user[2],
                user[3],
                user[4],
            )
            with self.db_lock:
                self.db_manager.delete_user(user[0])
            logger.info("Moved alien user '%s' to the correct server", user[0])


    def replics_manager(self):
        """Maintain a set of replicator servers that hold copies of this node's user data."""
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            last_full_sync = 0.0
            while self.running:
                if self.crisis:
                    time.sleep(1)
                    continue
                new_replics_needed = FAIL_TOLERANCE + 1 - len(self.replics)
                replics = self.replics.copy()
                for replic in replics:
                    if not self.ping(replic): 
                        new_replics_needed += 1
                        replics.remove(replic)
                        sock.sendto(f"DROP_REPLICS {self.get_ip()}".encode(), (replic, 12345))
                        log_event(logger, "WARNING", "node_unreachable", node=self.name, peer=replic, result="replica_target_removed")
                new_replics = []
                if new_replics_needed > 0:
                    new_replics = self.find_new_replics(new_replics_needed, replics)
                    if new_replics:
                        logger.info("[OK] Nuevos nodos de replica seleccionados: %s", new_replics)
                    replics.extend(new_replics)
                self.replics = replics

                full_sync_targets = list(new_replics)
                if time.time() - last_full_sync >= REPLICA_FULL_SYNC_INTERVAL:
                    full_sync_targets = list(replics)
                    last_full_sync = time.time()

                if full_sync_targets:
                    with self.db_lock:
                        user_info = self.db_manager.get_bd_copy()
                    self.replicate_owned_records(user_info, targets=full_sync_targets)
                time.sleep(1)


    def find_new_replics(self, needed, actual_replics):
        """Return a list of `needed` server IPs chosen as new replicators (excluding this node)."""
        servers = self.ping_all_servers()
        if not servers:
            return []

        try:
            servers.remove(self.get_ip())
        except Exception:
            pass

        for replic in actual_replics:
            try:
                servers.remove(replic)
            except Exception:
                pass

        needed = min(needed, len(servers))

        return random.sample(servers, needed)        


    def replicants_manager(self):
        """Assimilate data from replicant nodes that become unavailable (one-shot)."""
        assimilated_records = []
        for replicant in list(self.replicants):
            if not self.ping(replicant):
                log_event(logger, "WARNING", "node_unreachable", node=self.name, peer=replicant, result="replica_owner_unavailable")
                with self.db_lock:
                    user_info = self.db_manager.get_replics(replicant)
                for user in user_info:
                    resolution = self.place_user_record(
                        user[0],
                        user[1],
                        user[2],
                        user[3],
                        user[4],
                    )
                    if resolution not in ("forwarded_predecessor", "forwarded_successor", db_manager.STALE, db_manager.IDENTITY_CONFLICT):
                        assimilated_records.append(user)
                self.replicants.remove(replicant)
                with self.db_lock:
                    self.db_manager.drop_replics(replicant)
                log_event(
                    logger,
                    "INFO",
                    "replica_assimilated",
                    node=self.name,
                    peer=replicant,
                    result={"records": len(user_info)},
                )
        if assimilated_records:
            logger.warning("[!] Re-replicando %s registro(s) asimilado(s)", len(assimilated_records))
            self.replicate_owned_records(assimilated_records, log_level="INFO")


    def info_updater(self):
        """Periodically print server info for monitoring."""
        if STATUS_LOG_INTERVAL <= 0:
            return
        while self.running:
            time.sleep(STATUS_LOG_INTERVAL)
            self.print_info()


    #region Utils

    def get_ip(self, target_ip=None):
        """Return the best IP this server should advertise to other nodes.

        `FLOCK_NODE_IP` is the explicit override for multi-machine demos.
        `FLOCK_PUBLIC_IP` is accepted as a compatibility alias.
        """
        for env_name in ("FLOCK_NODE_IP", "FLOCK_PUBLIC_IP"):
            explicit_ip = os.environ.get(env_name, "").strip()
            if explicit_ip:
                if self._valid_ipv4(explicit_ip):
                    return explicit_ip
                log_event(
                    logger,
                    "WARNING",
                    "node_ip_invalid",
                    node=self.name,
                    advertised_ip=explicit_ip,
                    reason=f"{env_name} is not a valid IPv4 address",
                )

        if target_ip:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                    sock.connect((target_ip, 12345))
                    candidate = sock.getsockname()[0]
                    if self._valid_ipv4(candidate) and (
                        not self._is_loopback_ip(candidate) or self._is_loopback_ip(target_ip)
                    ):
                        return candidate
            except Exception:
                pass

        for candidate in self._hostname_candidates():
            if self._valid_ipv4(candidate) and not self._is_loopback_ip(candidate):
                return candidate

        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect(("10.255.255.255", 1))
                candidate = sock.getsockname()[0]
                if self._valid_ipv4(candidate) and not self._is_loopback_ip(candidate):
                    return candidate
        except Exception:
            pass

        for candidate in self._hostname_candidates():
            if self._valid_ipv4(candidate):
                return candidate

        return "127.0.0.1"
    

    def ping(self, ip, timeout=0.1):
        """Return True if `ip` responds to a short UDP PING within `timeout` seconds."""
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            try:
                sock.sendto(b"PING", (ip, 12346))
                _, _ = sock.recvfrom(1024)
                return True
            except Exception:
                return False
            
    def ping_all_servers(self, timeout=0.1):
        """Broadcast a PING and collect responding server IPs."""
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            servers = []
            broadcast_address = ("<broadcast>", 12346)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            try:
                sock.sendto("PING".encode(), broadcast_address)
                while True:
                    _, address = sock.recvfrom(1024)
                    servers.append(address[0])
            except socket.timeout:
                pass

            return servers


    def print_info(self):
        """Print a short status block describing the server state."""
        logger.info("Estado del nodo '%s'", self.name)
        logger.info("  Direccion: %s:12345", self.get_ip())
        logger.info("  Rango hash: [%s, %s]", self.lower_bound, self.upper_bound)
        logger.info("  Predecesor: %s", self.predecessor or "ninguno")
        logger.info("  Sucesor: %s", self.successor or "ninguno")
        logger.info("  Sucesores respaldo: %s", self.successors or "[]")
        logger.info("  Replicas propias en: %s", self.replics or "[]")
        logger.info("  Replicas recibidas de: %s", self.replicants or "[]")
        logger.info("-" * 72)

    def send_json_response(self, address, payload, ok=True):
        status = "OK" if ok else "ERROR"
        response = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        self.command_socket.sendto(f"{status} {response}".encode(), address)

    def status_payload(self):
        return {
            "name": self.name,
            "ip": self.get_ip(),
            "range": {"lower": self.lower_bound, "upper": self.upper_bound},
            "predecessor": self.predecessor,
            "successor": self.successor,
            "successors": list(self.successors),
            "replicas": list(self.replics),
            "replics": list(self.replics),
            "replicants": list(self.replicants),
        }

    def record_hash(self, record):
        canonical = json.dumps(record, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def snapshot_payload(self):
        with self.db_lock:
            owned_records = self.db_manager.list_owned_records()
            replica_records = self.db_manager.list_replica_records()

        owner_ip = self.get_ip()
        owned = []
        for username, ip, port, public_key, version in owned_records:
            canonical = {
                "type": "owned",
                "username": username,
                "ip": ip,
                "port": port,
                "public_key": public_key,
                "version": version,
                "owner": owner_ip,
            }
            owned.append({
                "username": username,
                "version": version,
                "owner": owner_ip,
                "hash": self.record_hash(canonical),
            })

        replicas = []
        for username, ip, port, public_key, version, owner in replica_records:
            canonical = {
                "type": "replica",
                "username": username,
                "ip": ip,
                "port": port,
                "public_key": public_key,
                "version": version,
                "owner": owner,
            }
            replicas.append({
                "username": username,
                "version": version,
                "owner": owner,
                "hash": self.record_hash(canonical),
            })

        return {"owned": owned, "replicas": replicas}

    def checksum_payload(self):
        snapshot = self.snapshot_payload()
        canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
        payload = {
            "checksum": hashlib.sha256(canonical.encode()).hexdigest(),
            "records": len(snapshot["owned"]) + len(snapshot["replicas"]),
        }
        log_event(logger, "INFO", "checksum_generated", node=self.name, result=payload)
        return payload

    def sync_from_owner(self, owner):
        with self.db_lock:
            user_info = self.db_manager.get_replics(owner)
        applied = 0
        forwarded = 0
        rejected = 0
        applied_records = []
        for user in user_info:
            resolution = self.place_user_record(
                user[0],
                user[1],
                user[2],
                user[3],
                user[4],
            )
            if resolution in ("forwarded_predecessor", "forwarded_successor"):
                forwarded += 1
            elif resolution in (db_manager.STALE, db_manager.IDENTITY_CONFLICT):
                rejected += 1
            else:
                applied += 1
                applied_records.append(user)
        if applied:
            self.replicate_owned_records(applied_records)
        result = {
            "owner": owner,
            "seen": len(user_info),
            "applied": applied,
            "forwarded": forwarded,
            "rejected": rejected,
        }
        log_event(logger, "INFO", "sync_completed", node=self.name, peer=owner, result=result)
        return result

    def replicate_owned_records(self, records, targets=None, log_level="DEBUG"):
        targets = list(self.replics if targets is None else targets)
        if not records or not targets:
            return 0
        sent = 0
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            for user in records:
                username, ip, port, public_key, version = user
                for replic in targets:
                    sock.sendto(
                        f"REPLIC {username} {ip} {port} {version} {public_key}".encode(),
                        (replic, 12345),
                    )
                    sent += 1
                    log_event(
                        logger,
                        log_level,
                        "replica_written",
                        node=self.name,
                        peer=replic,
                        username=username,
                        version=version,
                        result="sent",
                    )
        return sent

    def rolling_hash(self, s: str, base=911382629, mod=HASH_MOD) -> int:   
        """Compute a rolling hash for string `s` used to distribute keys in the ring."""
        hash_value = 0
        for c in s:
            hash_value = (hash_value * base + ord(c)) % mod
        return hash_value
    


    # region Multicast Stuff
    def multicast_listener(self) -> None:
        """Listen for multicast discovery messages and reply with this server's IP."""
        MCAST_GRP = "224.0.0.1"
        MCAST_PORT = 10003
        DISCOVER_MSG = "DISCOVER_SERVER"
        BUFFER_SIZE = 1024
        # Crear socket UDP
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        # Permitir que varias instancias puedan reutilizar el puerto
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        # Vincular el socket a todas las interfaces en el puerto MCAST_PORT
        sock.bind(("", MCAST_PORT))

        # Unirse al grupo multicast
        mreq = struct.pack("=4sl", socket.inet_aton(MCAST_GRP), socket.INADDR_ANY)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

        
        logger.info("[Multicast] Escuchando descubrimiento en %s:%s", MCAST_GRP, MCAST_PORT)
        

        while True:
            try:
                data, addr = sock.recvfrom(BUFFER_SIZE)
                message = data.decode().strip()
                logger.info("[Multicast] Mensaje recibido desde %s: %s", addr, message)
                if message.startswith(DISCOVER_MSG + ":"):
                    local_ip = self.get_ip()
                    _, rec_ip, rec_port = message.split(":")
                    logger.debug(f"Multicast reply target {rec_ip} {rec_port}")
                    sock.sendto(local_ip.encode(), (rec_ip, int(rec_port)))
                else:
                    local_ip = self.get_ip()
                    sock.sendto(local_ip.encode(), addr)
            except Exception as e:
                logger.error(f"Error en el listener: {e}")
                time.sleep(1)



if __name__ == "__main__":
    if len(sys.argv) != 2:
        logger.error("Use: python server.py <name>")
        sys.exit(1)
    name = sys.argv[1]
    server = ChatServer(name)
    server.start() 



'''
 To do:
 -Add locks to database operations
 -Add replication (to predecessor and successor and some random)
    -Add a process that manages replication

 -Update client commands to work with ring
'''
