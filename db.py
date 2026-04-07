import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except Exception:  # pragma: no cover
    psycopg2 = None
    RealDictCursor = None

DB_PATH = Path(__file__).resolve().parent / "sos.db"


def _database_url():
    return (os.getenv("DATABASE_URL") or "").strip()


def _use_postgres():
    url = _database_url().lower()
    return url.startswith("postgres://") or url.startswith("postgresql://")


def _normalize_query(query):
    if _use_postgres():
        return query.replace("?", "%s")
    return query


@contextmanager
def get_connection():
    """Yield a DB connection with dict-like row access for SQLite/PostgreSQL."""
    if _use_postgres():
        if psycopg2 is None:
            raise RuntimeError("DATABASE_URL is set for PostgreSQL, but psycopg2 is not installed.")
        conn = psycopg2.connect(_database_url(), cursor_factory=RealDictCursor)
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")

    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Create core tables if they do not already exist."""
    with get_connection() as conn:
        cur = conn.cursor()
        if _use_postgres():
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id BIGSERIAL PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    email TEXT NOT NULL UNIQUE,
                    phone TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )

            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS session_token_hash TEXT")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS session_expires_at TEXT")

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS contacts (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    email TEXT NOT NULL,
                    phone TEXT,
                    relationship TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_contacts_user_email_unique
                ON contacts (user_id, lower(email))
                """
            )
            cur.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_contacts_user_phone_unique
                ON contacts (user_id, phone)
                WHERE phone IS NOT NULL AND phone <> ''
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS alerts (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    latitude DOUBLE PRECISION NOT NULL,
                    longitude DOUBLE PRECISION NOT NULL,
                    map_link TEXT NOT NULL,
                    custom_message TEXT,
                    email_status TEXT NOT NULL,
                    recipients_count INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
        else:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    email TEXT NOT NULL UNIQUE,
                    phone TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )

            existing_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(users)").fetchall()
            }
            if "session_token_hash" not in existing_columns:
                cur.execute("ALTER TABLE users ADD COLUMN session_token_hash TEXT")
            if "session_expires_at" not in existing_columns:
                cur.execute("ALTER TABLE users ADD COLUMN session_expires_at TEXT")

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS contacts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    email TEXT NOT NULL,
                    phone TEXT,
                    relationship TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
                )
                """
            )
            cur.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_contacts_user_email_unique
                ON contacts (user_id, email)
                """
            )
            cur.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_contacts_user_phone_unique
                ON contacts (user_id, phone)
                WHERE phone IS NOT NULL AND phone <> ''
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    latitude REAL NOT NULL,
                    longitude REAL NOT NULL,
                    map_link TEXT NOT NULL,
                    custom_message TEXT,
                    email_status TEXT NOT NULL,
                    recipients_count INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
                )
                """
            )


def execute(query, params=()):
    """Execute a write query and return last row id."""
    with get_connection() as conn:
        normalized = _normalize_query(query)
        if _use_postgres():
            cur = conn.cursor()
            lower_q = normalized.strip().lower()
            if lower_q.startswith("insert") and "returning" not in lower_q:
                normalized = normalized.rstrip().rstrip(";") + " RETURNING id"
            cur.execute(normalized, params)
            if lower_q.startswith("insert"):
                row = cur.fetchone()
                if row:
                    return row.get("id")
            return None

        cur = conn.execute(normalized, params)
        return cur.lastrowid


def fetch_one(query, params=()):
    """Fetch a single row from SQLite."""
    with get_connection() as conn:
        normalized = _normalize_query(query)
        if _use_postgres():
            cur = conn.cursor()
            cur.execute(normalized, params)
            return cur.fetchone()
        cur = conn.execute(normalized, params)
        return cur.fetchone()


def fetch_all(query, params=()):
    """Fetch all rows from SQLite."""
    with get_connection() as conn:
        normalized = _normalize_query(query)
        if _use_postgres():
            cur = conn.cursor()
            cur.execute(normalized, params)
            return cur.fetchall()
        cur = conn.execute(normalized, params)
        return cur.fetchall()
