# Flock

A decentralized peer-to-peer messaging system built on a Chord distributed hash table (DHT). Flock enables secure, real-time communication between users across a self-organizing network of servers with no single point of failure.

## Architecture

```
                        Chord Ring (Servers)
                    ┌──────────────────────────┐
                    │                          │
               ┌────┴────┐              ┌──────┴───┐
               │ Server A │◄────────────► Server B  │
               │ [0, 499] │              │[500, 999]│
               └────┬────┘              └──────┬───┘
                    │                          │
                    └──────────┬───────────────┘
                               │
                         ┌─────┴─────┐
                         │ Server C  │
                         │[1000,10¹⁸]│
                         └───────────┘

        Client A ◄──── UDP P2P (encrypted) ────► Client B
           │                                        │
           └──── REGISTER/RESOLVE (via server) ─────┘
```

**Servers** form a Chord ring where each node owns a hash range and stores user registrations for usernames that hash into its range. Servers discover each other via UDP broadcast, join the ring by splitting the longest range, and maintain successor/predecessor lists for fault tolerance.

**Clients** register with any server (the request is routed to the correct node via the hash ring) and then communicate directly peer-to-peer over UDP. The server is only used for user discovery (`RESOLVE`), never for relaying messages.

**Security** is handled primarily at the client level, with the distributed identity layer binding usernames to long-lived public keys. Clients sign presence updates, resolve peer identity keys through the server ring, and encrypt all chat payloads using hybrid RSA-2048 + AES-256-GCM encryption.

## Features

- **Decentralized topology** -- Chord DHT with consistent hashing (`mod 10¹⁸+3`)
- **Fault tolerance** -- Configurable replication factor (default: 3+1 replicas), automatic ring repair on node failure
- **Identity-bound registration** -- Usernames are tied to a public key and presence updates are signed
- **End-to-end encryption** -- Hybrid RSA-2048-OAEP + AES-256-GCM, private keys never touch the server
- **Offline message queue** -- Messages to offline users are persisted locally and retried automatically in the background
- **Real-time web UI** -- Flask + WebSocket (Socket.IO) with push notifications
- **Console UI** -- Lightweight terminal interface for headless environments
- **Auto-discovery** -- Servers and clients find each other via UDP broadcast or multicast
- **Auto-reconnection** -- Clients automatically find a new server if the current one goes down

## Project Structure

```
Flock/
├── server/
│   ├── server.py            # Chord DHT server (ring management, replication)
│   └── db_manager.py        # Server SQLite (users, replicas)
├── client/
│   ├── client.py            # Core client (P2P messaging, encryption, discovery)
│   ├── crypto_manager.py    # RSA key generation + hybrid encryption
│   ├── db_manager.py        # Client SQLite (messages, chat history)
│   ├── ui_flask.py          # Web UI (Flask + Socket.IO)
│   ├── ui_console.py        # Terminal UI
│   ├── templates/           # HTML templates for web UI
│   │   ├── base.html
│   │   ├── servers.html
│   │   ├── register.html
│   │   ├── chats.html
│   │   └── chat.html
│   └── static/              # Static assets
├── router/
│   └── multicast_proxy.py   # UDP multicast proxy for cross-subnet discovery
├── requirements.txt
└── README.md
```

## Quick Start

### Prerequisites

- Python 3.10+
- All machines on the same local network (or connected via the multicast proxy)

### Installation

```bash
git clone <repository-url>
cd Flock
pip install -r requirements.txt
```

### 1. Start server(s)

```bash
cd server
python server.py node1
```

For a multi-machine demo, make the server advertise the LAN IP that the other PC can reach:

```bash
cd server
FLOCK_NODE_IP=<SERVER_LAN_IP> python server.py node1
```

Additional servers on the same network join the ring automatically:

```bash
python server.py node2    # discovers node1 via broadcast, joins ring
python server.py node3    # discovers the ring, joins
```

### 2. Start client (Web UI)

```bash
cd client
python ui_flask.py
```

For a multi-machine demo, make each client advertise its reachable LAN IP for P2P messages:

```bash
cd client
FLOCK_PUBLIC_IP=<CLIENT_LAN_IP> python ui_flask.py
```

Open `http://localhost:5000` in a browser. The flow is:

1. **Server discovery** -- click "Search for servers"
2. **Registration** -- choose a username
3. **Chat list** -- see existing conversations and unread counts
4. **Chat** -- send and receive messages in real time

The Flask UI supports multiple browser sessions in the same process. Each session owns its own client socket, login state and diagnostics, so two users can be demonstrated from different browsers without overwriting each other.

For a second client on the same machine, use a different port:

```bash
cd client
python -c "
from ui_flask import app, socketio
socketio.run(app, host='0.0.0.0', port=5001, allow_unsafe_werkzeug=True)
"
```

### 2b. Start client (Console UI)

```bash
cd client
python ui_console.py
```

Commands: `@username` (open chat), `/back` (return to menu), `/quit` (exit).

### 3. Watch structured logs

Flock writes server, client and console logs as JSON Lines under `logs/` by default. For a presentation-friendly view:

```bash
./tail_logs.sh
./tail_logs.sh server
./tail_logs.sh client
```

Use `./tail_logs.sh --raw` to show the original JSONL records. The formatted view uses `jq` when available and falls back to `tail -F` otherwise.

The web UI also exposes `/diagnostics`, which shows the active node, advertised P2P IP, local UDP socket, last `RESOLVE`, last P2P ping, last delivery result, pending queue, and `STATUS`/`SNAPSHOT`/`CHECKSUM` admin commands.

## Protocol Reference

### Server-to-Server (UDP, port 12345)

| Command | Direction | Format | Description |
|---------|-----------|--------|-------------|
| `DISCOVER` | Broadcast | `DISCOVER` | Find active servers |
| `RANGE` | Request/Response | `RANGE` / `OK <lo> <hi>` | Query hash range |
| `JOIN` | Request/Response | `JOIN` / `OK <lo> <hi> <pred> <succ>` | Join the ring |
| `PRED_CHANGE` | Notification | `PRED_CHANGE <ip>` | Update predecessor |
| `SUCC` | Push | `SUCC <ip> [<ip>...]` | Propagate successor list |
| `FIX` | Broadcast | `FIX` | Trigger ring repair |
| `REPLIC` | Push | `REPLIC <user> <ip> <port> <version> <pubkey_b64>` | Replicate user data |
| `TAKEOVER` | Push | `TAKEOVER <user> <ip> <port> <version> <pubkey_b64>` | Move an owned record to the correct node |
| `DROP_REPLICS` | Push | `DROP_REPLICS <owner_ip>` | Drop replica data |
| `STATUS` | Request/Response | `STATUS` / `OK <json>` | Inspect local topology and replication state |
| `SNAPSHOT` | Request/Response | `SNAPSHOT` / `OK <json>` | Inspect deterministic owned and replica record hashes |
| `CHECKSUM` | Request/Response | `CHECKSUM` / `OK <json>` | Return a stable checksum for local state comparison |
| `SYNC_FROM` | Request/Response | `SYNC_FROM <owner_ip>` / `OK <json>` | Reconcile local replicas for a specific owner |

### Client-to-Server (UDP, port 12345)

| Command | Format | Description |
|---------|--------|-------------|
| `REGISTER` | `REGISTER <username> <ip> <port> <version> <pubkey_b64> <signature_b64>` | Register or refresh a user presence |
| `RESOLVE` | `RESOLVE <username>` / `OK <ip> <port> <pubkey_b64> <version>` | Lookup user address and identity key |

### Client-to-Client (UDP, dynamic port)

| Command | Format | Description |
|---------|--------|-------------|
| `MESSAGE` | `MESSAGE <sender> <encrypted_payload>` | Send a chat message |
| `PUBKEY_REQ` | `PUBKEY_REQ <username>` | Legacy compatibility request for peer's public key |
| `PUBKEY_RES` | `PUBKEY_RES <username> <base64_pubkey>` | Legacy compatibility response validated against the identity manager |
| `PING`/`PONG` | `PING` / `PONG` | Online check |

## Security Model

### Encryption Pipeline

```
Plaintext message
        │
        ▼
┌───────────────────┐
│ AES-256-GCM       │◄── Random 256-bit key + 96-bit nonce
│ Encrypt message   │
└───────┬───────────┘
        │ ciphertext
        ▼
┌───────────────────┐
│ RSA-2048-OAEP     │◄── Recipient's public key
│ Encrypt AES key   │
└───────┬───────────┘
        │
        ▼
[key_len (2B)][encrypted_aes_key (256B)][nonce (12B)][ciphertext]
        │
        ▼
    Base64 encode ──► Wire format
```

### Identity-Bound Registration

```
Alice                                        Identity ring
  │                                                │
  ├── REGISTER alice ip port version pubkey sig ─►│
  │◄─────────────────────────────── OK / ERROR ────┤
  │                                                │
  ├── RESOLVE bob ────────────────────────────────►│
  │◄──────────────── OK ip port bob_pubkey ver ───┤
  │                                                │
  ├── MESSAGE alice <encrypted> ─────────────────► Bob
```

### Key Storage

```
client/keys/
└── <username>/
    ├── private.pem              # RSA-2048 private key (PKCS8, unencrypted)
    ├── public.pem               # RSA-2048 public key
    └── contacts/
        ├── alice.pem            # Alice's public key
        └── bob.pem              # Bob's public key
```

Keys are generated once on first registration and persisted across sessions. The server never sees, stores, or relays any cryptographic material.

## Database Schema

### Server (`server/db/<name>.db`)

```sql
-- Registered users in this node's hash range
CREATE TABLE users (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    ip       TEXT NOT NULL,
    port     INTEGER NOT NULL
);

-- Replicated data from other nodes (fault tolerance)
CREATE TABLE replic_users (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    ip       TEXT NOT NULL,
    port     INTEGER NOT NULL,
    owner    TEXT NOT NULL          -- IP of the owning server
);
```

### Client (`client/chats/<username>.db`)

```sql
CREATE TABLE messages (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    author    TEXT NOT NULL,
    receiver  TEXT NOT NULL,
    text      TEXT NOT NULL,
    date_time DATETIME DEFAULT CURRENT_TIMESTAMP,
    seen      BOOLEAN DEFAULT 0
);
```

## Configuration

| Constant | Location | Default | Description |
|----------|----------|---------|-------------|
| `HASH_MOD` | `server/server.py` | `10¹⁸ + 3` | Hash space size |
| `FAIL_TOLERANCE` | `server/server.py` | `3` | Number of backup replicas |
| Server command port | `server/server.py` | `12345` | UDP port for commands |
| Server ping port | `server/server.py` | `12346` | UDP port for health checks |
| Flask port | `client/ui_flask.py` | `5000` | Web UI HTTP port |
| RSA key size | `client/crypto_manager.py` | `2048` bits | Key strength |
| AES key size | `client/crypto_manager.py` | `256` bits | Symmetric key strength |
| `FLOCK_LOG_LEVEL` | `shared_logging_utils.py` | `INFO` | Minimum log level for console and file output |
| `FLOCK_LOG_DIR` | `shared_logging_utils.py` | `<repo>/logs` | Directory for JSON Lines log files |
| `FLOCK_LOG_MAX_BYTES` | `shared_logging_utils.py` | `1048576` | Rotation size for each log file |
| `FLOCK_LOG_BACKUP_COUNT` | `shared_logging_utils.py` | `1` | Number of rotated backups to keep |
| `FLOCK_PUBLIC_IP` | `client/client.py` | auto-detected | Explicit IP announced by a client for P2P delivery |
| `FLOCK_NODE_IP` | `server/server.py` | auto-detected | Explicit server IP announced to other server nodes |
| `FLOCK_SESSION_TTL_HOURS` | `client/ui_flask.py` | `12` | Flask web-session lifetime in hours |
| `FLOCK_SECRET_KEY` | `client/ui_flask.py` | persisted in `client/auth/flask_session.key` | Flask cookie signing secret |
| `FLOCK_REPLICA_FULL_SYNC_INTERVAL` | `server/server.py` | `30` seconds | Periodic full replica sync interval |
| `FLOCK_STATUS_LOG_INTERVAL` | `server/server.py` | `30` seconds | Periodic status log interval; set `0` to disable |

## Dependencies

```
flask>=3.0
flask-socketio>=5.3
cryptography>=42.0
```

## License

This project was developed as an academic exercise in distributed systems and applied cryptography.
