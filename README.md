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

**Security** is handled entirely at the client level. Before the first message, clients perform a P2P RSA public key handshake. All subsequent messages are encrypted using hybrid RSA-2048 + AES-256-GCM encryption.

## Features

- **Decentralized topology** -- Chord DHT with consistent hashing (`mod 10¹⁸+3`)
- **Fault tolerance** -- Configurable replication factor (default: 3+1 replicas), automatic ring repair on node failure
- **End-to-end encryption** -- Hybrid RSA-2048-OAEP + AES-256-GCM, keys never touch the server
- **P2P key exchange** -- `PUBKEY_REQ`/`PUBKEY_RES` handshake directly between clients
- **Offline message queue** -- Messages to offline users are retried automatically in the background
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

Open `http://localhost:5000` in a browser. The flow is:

1. **Server discovery** -- click "Search for servers"
2. **Registration** -- choose a username
3. **Chat list** -- see existing conversations and unread counts
4. **Chat** -- send and receive messages in real time

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
| `REPLIC` | Push | `REPLIC <user> <ip> <port>` | Replicate user data |
| `DROP_REPLICS` | Push | `DROP_REPLICS <owner_ip>` | Drop replica data |

### Client-to-Server (UDP, port 12345)

| Command | Format | Description |
|---------|--------|-------------|
| `REGISTER` | `REGISTER <username> <ip> <port>` | Register a user |
| `RESOLVE` | `RESOLVE <username>` / `OK <ip> <port>` | Lookup user address |

### Client-to-Client (UDP, dynamic port)

| Command | Format | Description |
|---------|--------|-------------|
| `MESSAGE` | `MESSAGE <sender> <encrypted_payload>` | Send a chat message |
| `PUBKEY_REQ` | `PUBKEY_REQ <username>` | Request peer's public key |
| `PUBKEY_RES` | `PUBKEY_RES <username> <base64_pubkey>` | Respond with public key |
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

### Key Exchange (P2P Handshake)

```
Alice                                          Bob
  │                                              │
  ├── PUBKEY_REQ alice ─────────────────────────►│
  │                                              ├── stores Alice's address
  │◄─────────────────── PUBKEY_RES bob <key> ────┤
  │                                              │
  │  (if Bob doesn't have Alice's key yet)       │
  │◄─────────────────── PUBKEY_REQ bob ──────────┤
  ├── PUBKEY_RES alice <key> ───────────────────►│
  │                                              │
  ├── MESSAGE alice <encrypted> ────────────────►│
  │◄──────────────── MESSAGE bob <encrypted> ────┤
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

## Dependencies

```
flask>=3.0
flask-socketio>=5.3
cryptography>=42.0
```

## License

This project was developed as an academic exercise in distributed systems and applied cryptography.
