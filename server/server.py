import socket
import threading
import sys
import json
import db_manager
import time
import random
# from termcolor import colored as col
import struct
import logging

# Configure basic logging to console
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s:%(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


HASH_MOD = 10**18+3
FAIL_TOLERANCE = 3


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
        self.db_lock = threading.Lock()

        self.lower_bound = 0
        self.upper_bound = HASH_MOD - 1

        self.predecessor = None
        self.successor = None
        self.successors = []

        self.replics = []
        self.replicants = []

        self.running = True
        self.crisis = False


    
    #region Start Sequence 
    
    def start(self):
        """Initialize DB, discover/join other servers and start background services."""
        with self.db_lock:
            self.db_manager.set_db(self.name)

        servers = self.discover_servers()

        if not servers:
            logger.info("No other servers running")
        else:
            for server in servers:
                logger.info(f"Server found: {server[0]} at {server[1]}")
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

        self.listen_for_messages()



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

            return servers

    def join_to_servers(self, servers):
        """Join the cluster by requesting to join the server that manages the largest range."""
        longest_range_server = self.get_longest_range_server(servers)
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
                data, address = self.command_socket.recvfrom(1024)
                message = data.decode()

                if message != "PING":
                    logger.info(f"Message from {address}: {message}")

                if message.startswith("DISCOVER"):
                    self.command_socket.sendto(f"{self.name}".encode(), address)

                elif message.startswith("PING"):
                    self.command_socket.sendto("PONG".encode(), address)

                elif message.startswith("RANGE"):
                    self.command_socket.sendto(f"OK {self.lower_bound} {self.upper_bound}".encode(), address)

                elif message.startswith("JOIN"):
                    logger.info(f"Processing JOIN from {address}")
                    self.process_join_request(address)
                    self.print_info()

                elif message.startswith("PRED_CHANGE"):
                    _, predecessor = message.split(" ")
                    self.change_predecessor(predecessor)
                    self.print_info()

                elif message.startswith("REGISTER"):
                    try:
                        _, answer_to_ip, answer_to_port, username, ip, port = message.split(" ")  
                    except Exception:
                        _, username, ip, port = message.split(" ")
                        answer_to_ip = address[0]
                        answer_to_port = address[1]
                    self.register_user(answer_to_ip, int(answer_to_port), username, ip, int(port))
                    logger.info(f"REGISTER request for user '{username}' handled/forwarded")

                elif message.startswith("RESOLVE"):
                    try:
                        _, answer_to_ip, answer_to_port, username = message.split(" ")  
                    except Exception:
                        _, username = message.split(" ")
                        answer_to_ip = address[0]
                        answer_to_port = address[1]
                    self.resolve_user(answer_to_ip, int(answer_to_port), username)
                    logger.info(f"RESOLVE request for user '{username}' processed")

                elif message.startswith("SUCC"):
                    _, successors = message.split(" ", 1)
                    successors_list = successors.split(" ")
                    self.successors = successors_list[: FAIL_TOLERANCE + 1]
                    if self.predecessor:
                        self.command_socket.sendto(f"SUCC {self.get_ip()} {successors}".encode(), (self.predecessor, 12345))

                elif message.startswith("FIX"):
                    self.crisis = True
                    logger.warning("Entering FIX/crisis mode")
                    self.fix_tape()
                    self.replicants_manager()
                    self.correct_bd()
                    self.crisis = False

                elif message.startswith("REPLIC"):
                    _, username, ip, port = message.split(" ")
                    if address[0] not in self.replicants:
                        self.replicants.append(address[0])
                    with self.db_lock:
                        self.db_manager.register_replic_user(username, ip, port, address[0])
                    logger.info(f"Registered replic user '{username}' from {address[0]}")

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


    def register_user(self, answer_to_ip, answer_to_port, username, ip, port):
        """Register a user in the ring or forward the registration to the appropriate neighbor.

        If the user's hash belongs to this node's range, persist it locally and notify replicas.
        Otherwise forward the REGISTER command to predecessor or successor.
        """
        username_hash = self.rolling_hash(username)

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            if username_hash < self.lower_bound:
                sock.sendto(
                    f"REGISTER {answer_to_ip} {answer_to_port} {username} {ip} {port}".encode(),
                    (self.predecessor, 12345),
                )
                logger.info(f"Forwarded REGISTER for '{username}' to predecessor {self.predecessor}")

            elif username_hash > self.upper_bound:
                sock.sendto(
                    f"REGISTER {answer_to_ip} {answer_to_port} {username} {ip} {port}".encode(),
                    (self.successor, 12345),
                )
                logger.info(f"Forwarded REGISTER for '{username}' to successor {self.successor}")
            else:
                with self.db_lock:
                    self.db_manager.register_user(username, ip, port)
                response = f"OK User '{username}' in ({ip}:{port}) successfully registered"
                if answer_to_ip != '.':
                    sock.sendto(response.encode(), (answer_to_ip, answer_to_port))
                for replic in self.replics:
                    sock.sendto(f"REPLIC {username} {ip} {port}".encode(), (replic, 12345))
                logger.info(response)


    def resolve_user(self, answer_to_ip, answer_to_port, username):
        """Resolve `username` to an (ip,port) tuple, forwarding the request if needed."""
        username_hash = self.rolling_hash(username)

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            if username_hash < self.lower_bound:
                sock.sendto(
                    f"RESOLVE {answer_to_ip} {answer_to_port} {username}".encode(),
                    (self.predecessor, 12345),
                )
                logger.info(f"Forwarded RESOLVE for '{username}' to predecessor {self.predecessor}")

            elif username_hash > self.upper_bound:
                sock.sendto(
                    f"RESOLVE {answer_to_ip} {answer_to_port} {username}".encode(),
                    (self.successor, 12345),
                )
                logger.info(f"Forwarded RESOLVE for '{username}' to successor {self.successor}")

            else:
                with self.db_lock:
                    address = self.db_manager.resolve_user(username)
                if address:
                    ip, port = address
                    response = f"OK {ip} {port}"
                    sock.sendto(response.encode(), (answer_to_ip, answer_to_port))
                    logger.info(f"Resolved address of user '{username}', ({ip}:{port})")
                else:
                    response = f"ERROR 404 User not found"
                    sock.sendto(response.encode(), (answer_to_ip, answer_to_port))
                    logger.warning(f"User not found during RESOLVE: {username}")


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

    def request_predecessor_change(self, target, new_predecessor):
        """Notify `target` server that its predecessor should be changed to `new_predecessor`."""
        if target is not None:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(1)
                sock.sendto(f"PRED_CHANGE {new_predecessor}".encode(), (target, 12345))




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
                    logger.warning("Tape integrity compromised")
                    sock.sendto("FIX".encode(), broadcast_address)

            time.sleep(1)

        logger.info("Shutting tape integrity check off")

    def fix_tape(self):
        """Attempt to repair the ring by checking neighbors and promoting backups when needed."""
        self.crisis = True
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.1)
            if self.successor:
                try:
                    sock.sendto(f"PING", (self.successor, 12346))
                    _, _ = sock.recvfrom(1024)
                except Exception:
                    self.fix_tape_forward()
            time.sleep(0.1 * 3 * FAIL_TOLERANCE)
            if self.predecessor:
                try:
                    sock.sendto(f"PING", (self.predecessor, 12346))
                    _, _ = sock.recvfrom(1024)
                except Exception:
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
                    return
                except Exception as e:
                    logger.warning(f"Server {successor} unavailable: {e}")
            self.upper_bound = HASH_MOD - 1
            self.successor = None
            self.successors = []

    def fix_tape_backward(self):
        """Handle predecessor failure by instructing it to terminate and clearing predecessor state."""
        if self.predecessor:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(0.5)
                try:
                    sock.sendto(f"PING".encode(), (self.predecessor, 12346))
                    _, _ = sock.recvfrom(1024)
                except Exception:
                    sock.sendto(f"KILL".encode(), (self.predecessor, 12345))
                    self.lower_bound = 0
                    self.predecessor = None


    def correct_bd(self):
        """Move users that do not belong to this node's range to the correct nodes."""
        with self.db_lock:
            alien_users = self.db_manager.get_alien_users(self.lower_bound, self.upper_bound, self.rolling_hash)
        for user in alien_users:
            with self.db_lock:
                self.register_user('.', '.', user[0], user[1], user[2])
                self.db_manager.delete_user(user[0])


    def replics_manager(self):
        """Maintain a set of replicator servers that hold copies of this node's user data."""
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
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
                        sock.sendto(b"DROP_REPLIC", (replic, 12345))
                if new_replics_needed > 0:            
                    new_replics = self.find_new_replics(new_replics_needed, replics)
                    if new_replics:
                        logger.info(f"New replics: {new_replics}")
                    replics.extend(new_replics)
                    self.replics = replics

                    with self.db_lock:
                        user_info = self.db_manager.get_bd_copy()
                    for user in user_info:
                        for replic in new_replics:
                            sock.sendto(f"REPLIC {user[0]} {user[1]} {user[2]}".encode(), (replic, 12345))
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
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            for replicant in self.replicants:
                if not self.ping(replicant):
                    with self.db_lock:
                        user_info = self.db_manager.get_replics(replicant)
                    for user in user_info:
                        self.register_user('.', '.', user[0], user[1], user[2])
                    self.replicants.remove(replicant)
                    with self.db_lock:
                        self.db_manager.drop_replics(replicant)
                    logger.info(f"Asimilated data from {replicant}")


    def info_updater(self):
        """Periodically print server info for monitoring."""
        while self.running:
            self.print_info()
            time.sleep(10)


    #region Utils

    def get_ip(self):
        """Return the IP address of the current host."""
        return socket.gethostbyname(socket.gethostname())
    

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
        """Print a short status line describing the server state."""
        logger.info(f"Server '{self.name}' on ({self.get_ip()}:12345). Storing in range ({self.lower_bound}, {self.upper_bound}). Predecessor is {self.predecessor}, successor is {self.successor}")
        logger.info(f"Successors: {self.successors}")
        logger.info(f"Replics: {self.replics}")
        logger.info(f"Replicants: {self.replicants}")

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

        
        logger.info(f"[Muticast] Escuchando mensajes en {MCAST_GRP}:{MCAST_PORT}")
        

        while True:
            try:
                data, addr = sock.recvfrom(BUFFER_SIZE)
                message = data.decode().strip()
                logger.info(f"Recibido mensaje: '{message}' desde {addr}")
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