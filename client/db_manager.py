import sqlite3
import os

class user_db:
    """Simple SQLite-backed user chat database for a client.

    Manages a per-user database file containing chats and messages.
    """
    def __init__(self):
        self.db_directory = "client/chats"
        self.db_route = ""

    def set_db(self, username):
        """Create or open the database for `username` and ensure tables exist."""

        os.makedirs(self.db_directory, exist_ok=True)
        self.db_route = os.path.join(self.db_directory, f"{username}.db")

        conn = sqlite3.connect(self.db_route, check_same_thread=False)
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

        conn.commit()
        conn.close()

        # print(f"Base de datos creada para el usuario: {username}")

    
    def insert_new_message(self, author, receiver, text, seen):
        """Insert a message record into the database."""
        conn = sqlite3.connect(self.db_route, check_same_thread=False)
        cursor = conn.cursor()

        cursor.execute("INSERT INTO messages (author, receiver, text, seen) VALUES (?, ?, ?, ?)", (author, receiver, text, seen))
        conn.commit()
        conn.close()

    def get_previous_chat(self, user1, user2):
        """Return all messages exchanged between `user1` and `user2`, ordered by date."""
        conn = sqlite3.connect(self.db_route, check_same_thread=False)
        cursor = conn.cursor()

        cursor.execute('''
            SELECT *
            FROM messages
            WHERE (author = ? AND receiver = ?)
            OR (author = ? AND receiver = ? AND seen = 1)
            ORDER BY date_time ASC
        ''', (user1, user2, user2, user1))

        chat = cursor.fetchall()
        conn.commit()
        conn.close()
        return chat
    
    def get_unseen_messages(self, user1, user2):
        """Return messages from `user2` to `user1` that are not yet seen."""
        conn = sqlite3.connect(self.db_route, check_same_thread=False)
        cursor = conn.cursor()

        cursor.execute('''
            SELECT *
            FROM messages
            WHERE author = ? AND receiver = ? AND seen = 0
        ''', (user2, user1))

        unseen_messages = cursor.fetchall()
        conn.commit()
        conn.close()
        return unseen_messages
    
    def set_messages_as_seen(self, user1, user2):
        """Mark messages from `user2` to `user1` as seen."""
        conn = sqlite3.connect(self.db_route, check_same_thread=False)
        cursor = conn.cursor()

        cursor.execute('''
            UPDATE messages
            SET seen = 1
            WHERE author = ? AND receiver = ?
        ''', (user2, user1))

        conn.commit()
        conn.close()
        
    def get_unseen_resume(self, user):
        """Return a list of (author, count) for unseen messages received by `user`."""
        conn = sqlite3.connect(self.db_route, check_same_thread=False)
        cursor = conn.cursor()

        cursor.execute('''
            SELECT author, COUNT(*) 
            FROM messages 
            WHERE receiver = ? AND seen = 0
            GROUP BY author
        ''', (user,))
        unseen_resume = cursor.fetchall()

        conn.close()
        return unseen_resume
    
    def get_chat_previews(self, user):
        """Return a list of (chat_partner, last_message) for each chat, ordered by date."""
        conn = sqlite3.connect(self.db_route, check_same_thread=False)
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
                    ROW_NUMBER() OVER (
                        PARTITION BY 
                            CASE 
                                WHEN author = ? THEN receiver 
                                ELSE author 
                            END 
                        ORDER BY date_time DESC
                    ) AS rn
                FROM messages
                WHERE ? IN (author, receiver)
            )
            SELECT chat_partner, text
            FROM ranked_messages
            WHERE rn = 1
            ORDER BY date_time DESC;
        '''
        cursor.execute(query, (user, user, user))
        chat_previews = cursor.fetchall()
        conn.close()
        return chat_previews