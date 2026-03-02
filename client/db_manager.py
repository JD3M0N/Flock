import os
import sqlite3
import threading

class user_db:
    """Simple SQLite-backed user chat database for a client.

    Manages a per-user database file containing chats and messages.
    """
    def __init__(self):
        self.db_directory = "client/chats"
        self.db_route = ""
        self._db_lock = threading.RLock()

    def _connect(self):
        if not self.db_route:
            raise RuntimeError("User database is not initialized")
        conn = sqlite3.connect(self.db_route, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def set_db(self, username):
        """Create or open the database for `username` and ensure tables exist."""

        os.makedirs(self.db_directory, exist_ok=True)
        self.db_route = os.path.join(self.db_directory, f"{username}.db")

        with self._db_lock, self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS chats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE
                )
            ''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    author TEXT NOT NULL,
                    receiver TEXT NOT NULL,
                    text TEXT NOT NULL,
                    date_time DATETIME DEFAULT CURRENT_TIMESTAMP,
                    seen BOOLEAN DEFAULT 0
                )
            ''')

            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_messages_conversation
                ON messages(author, receiver, date_time)
            ''')

            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_messages_unseen
                ON messages(receiver, author, seen, date_time)
            ''')

    
    def insert_new_message(self, author, receiver, text, seen):
        """Insert a message record into the database."""
        with self._db_lock, self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO messages (author, receiver, text, seen) VALUES (?, ?, ?, ?)",
                (author, receiver, text, seen),
            )

    def get_previous_chat(self, user1, user2):
        """Return all messages exchanged between `user1` and `user2`, ordered by date."""
        with self._db_lock, self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT *
                FROM messages
                WHERE (author = ? AND receiver = ?)
                OR (author = ? AND receiver = ?)
                ORDER BY date_time ASC, id ASC
            ''', (user1, user2, user2, user1))
            return cursor.fetchall()
    
    def get_unseen_messages(self, user1, user2):
        """Return messages from `user2` to `user1` that are not yet seen."""
        with self._db_lock, self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT *
                FROM messages
                WHERE author = ? AND receiver = ? AND seen = 0
                ORDER BY date_time ASC, id ASC
            ''', (user2, user1))
            return cursor.fetchall()
    
    def set_messages_as_seen(self, user1, user2):
        """Mark messages from `user2` to `user1` as seen."""
        with self._db_lock, self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE messages
                SET seen = 1
                WHERE author = ? AND receiver = ?
            ''', (user2, user1))
        
    def get_unseen_resume(self, user):
        """Return a list of (author, count) for unseen messages received by `user`."""
        with self._db_lock, self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT author, COUNT(*)
                FROM messages
                WHERE receiver = ? AND seen = 0
                GROUP BY author
                ORDER BY MAX(date_time) DESC
            ''', (user,))
            return cursor.fetchall()
    
    def get_chat_previews(self, user):
        """Return a list of (chat_partner, last_message) for each chat, ordered by date."""
        with self._db_lock, self._connect() as conn:
            cursor = conn.cursor()
            query = '''
                WITH ranked_messages AS (
                    SELECT
                        CASE
                            WHEN author = ? THEN receiver
                            ELSE author
                        END AS chat_partner,
                        text,
                        date_time,
                        id,
                        ROW_NUMBER() OVER (
                            PARTITION BY
                                CASE
                                    WHEN author = ? THEN receiver
                                    ELSE author
                                END
                            ORDER BY date_time DESC, id DESC
                        ) AS rn
                    FROM messages
                    WHERE ? IN (author, receiver)
                )
                SELECT chat_partner, text
                FROM ranked_messages
                WHERE rn = 1
                ORDER BY date_time DESC, id DESC
            '''
            cursor.execute(query, (user, user, user))
            return cursor.fetchall()
