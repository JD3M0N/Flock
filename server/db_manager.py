import sqlite3
import os

APPLIED = "applied"
STALE = "stale"
IDENTITY_CONFLICT = "identity_conflict"
IDEMPOTENT = "idempotent"


class server_db:
    """Server-side simple SQLite storage for user registration and replication info."""
    def __init__(self):
        self.db_directory = os.path.join(os.path.dirname(__file__), "db")
        self.db_route = ""

    def _connect(self):
        if not self.db_route:
            raise RuntimeError("Server database is not initialized")
        conn = sqlite3.connect(self.db_route, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn


    def set_db(self, username):     
        os.makedirs(self.db_directory, exist_ok=True)
        self.db_route = os.path.join(self.db_directory, f"{username}.db")

        with self._connect() as conn:
            cursor = conn.cursor()

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    ip TEXT NOT NULL,
                    port INTEGER NOT NULL,
                    public_key TEXT NOT NULL DEFAULT '',
                    version INTEGER NOT NULL DEFAULT 0
                )
            ''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS replic_users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    ip TEXT NOT NULL,
                    port INTEGER NOT NULL,
                    public_key TEXT NOT NULL DEFAULT '',
                    version INTEGER NOT NULL DEFAULT 0,
                    owner TEXT NOT NULL
                )
            ''')

            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_users_username
                ON users(username)
            ''')

            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_replic_users_owner
                ON replic_users(owner)
            ''')

            self._ensure_column(cursor, "users", "public_key", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(cursor, "users", "version", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(cursor, "replic_users", "public_key", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(cursor, "replic_users", "version", "INTEGER NOT NULL DEFAULT 0")

    def _ensure_column(self, cursor, table_name, column_name, column_definition):
        cursor.execute(f"PRAGMA table_info({table_name})")
        columns = {row[1] for row in cursor.fetchall()}
        if column_name not in columns:
            cursor.execute(
                f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}"
            )

    def get_user_record(self, username):
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT username, ip, port, public_key, version
                FROM users
                WHERE username = ?
            ''', (username,))
            return cursor.fetchone()

    def _resolve_version_conflict(self, existing_public_key, existing_version, public_key, version):
        if version < existing_version:
            return STALE
        if version == existing_version:
            if existing_public_key != public_key:
                return IDENTITY_CONFLICT
            return IDEMPOTENT
        return APPLIED

    def register_user(self, username, ip, port, public_key="", version=0):
        return self.upsert_user(username, ip, port, public_key=public_key, version=version)[0] in (APPLIED, IDEMPOTENT)

    def upsert_user(self, username, ip, port, public_key="", version=0):
        with self._connect() as conn:
            cursor = conn.cursor()

            cursor.execute('''
                SELECT ip, port, public_key, version
                FROM users
                WHERE username = ?
            ''', (username,))

            existing_user = cursor.fetchone()

            if existing_user:
                existing_ip, existing_port, existing_public_key, existing_version = existing_user
                resolution = self._resolve_version_conflict(
                    existing_public_key,
                    existing_version,
                    public_key,
                    version,
                )
                if resolution in (STALE, IDENTITY_CONFLICT):
                    return resolution, False
                if resolution == IDEMPOTENT:
                    return resolution, True
                cursor.execute('''
                    UPDATE users
                    SET ip = ?, port = ?, public_key = ?, version = ?
                    WHERE username = ?
                ''', (
                    ip,
                    port,
                    public_key or existing_public_key,
                    version,
                    username,
                ))
            else:
                cursor.execute('''
                    INSERT INTO users (username, ip, port, public_key, version)
                    VALUES (?, ?, ?, ?, ?)
                ''', (username, ip, port, public_key, version))

            conn.commit()
            return APPLIED, True

    def register_replic_user(self, username, ip, port, public_key="", version=0, owner=""):
        return self.upsert_replic_user(
            username,
            ip,
            port,
            public_key=public_key,
            version=version,
            owner=owner,
        )[0] in (APPLIED, IDEMPOTENT)

    def upsert_replic_user(self, username, ip, port, public_key="", version=0, owner=""):
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT ip, port, public_key, version, owner
                FROM replic_users
                WHERE username = ?
            ''', (username,))

            existing_user = cursor.fetchone()

            if existing_user:
                _, _, existing_public_key, existing_version, existing_owner = existing_user
                resolution = self._resolve_version_conflict(
                    existing_public_key,
                    existing_version,
                    public_key,
                    version,
                )
                if resolution in (STALE, IDENTITY_CONFLICT):
                    return resolution, False
                if resolution == IDEMPOTENT:
                    return resolution, True
                cursor.execute('''
                    UPDATE replic_users
                    SET ip = ?, port = ?, public_key = ?, version = ?, owner = ?
                    WHERE username = ?
                ''', (
                    ip,
                    port,
                    public_key or existing_public_key,
                    version,
                    owner or existing_owner,
                    username,
                ))
            else:
                cursor.execute('''
                    INSERT INTO replic_users (username, ip, port, public_key, version, owner)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (username, ip, port, public_key, version, owner))

            conn.commit()
            return APPLIED, True

    def resolve_user(self, username):
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT ip, port, public_key, version
                FROM users
                WHERE username = ?
            ''', (username,))
            address = cursor.fetchone()
            return address
        

    def get_bd_copy(self):
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT username, ip, port, public_key, version FROM users ORDER BY username
            ''')

            data = cursor.fetchall()
            return data

    def list_owned_records(self):
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT username, ip, port, public_key, version
                FROM users
                ORDER BY username
            ''')
            return cursor.fetchall()

    def list_replica_records(self):
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT username, ip, port, public_key, version, owner
                FROM replic_users
                ORDER BY owner, username
            ''')
            return cursor.fetchall()


    def get_alien_users(self, lower_bound, upper_bound, hash_function):
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT username, ip, port, public_key, version FROM users
            ''')

            data = cursor.fetchall()
            alien_users = []
            for user in data:
                user_hash = hash_function(user[0])
                if user_hash < lower_bound or user_hash > upper_bound:
                    alien_users.append(user)

            return alien_users
        
    def delete_user(self, username):
        with self._connect() as conn:
            cursor = conn.cursor()
            
            cursor.execute('''
                DELETE FROM users WHERE username = ?
            ''', (username,))
            
            conn.commit()

    def drop_replics(self, owner):
        with self._connect() as conn:
            cursor = conn.cursor()
            
            cursor.execute('''
                DELETE FROM replic_users WHERE owner = ?
            ''', (owner,))

            conn.commit()

    def get_replics(self, owner):
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT username, ip, port, public_key, version FROM replic_users WHERE owner = ? ORDER BY username
            ''', (owner,))

            data = cursor.fetchall()

            return data
