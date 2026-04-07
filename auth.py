import re
import secrets
from datetime import datetime, timedelta
import hashlib

try:
    import bcrypt  # type: ignore[reportMissingImports]
except Exception:  # pragma: no cover
    bcrypt = None

from db import execute, fetch_one


EMAIL_PATTERN = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
PHONE_PATTERN = re.compile(r"^\+?[1-9]\d{7,14}$")


def validate_signup(username, email, phone, password):
    """Validate signup form fields and return (ok, message)."""
    if not username.strip() or len(username.strip()) < 3:
        return False, "Username must be at least 3 characters long."
    if not EMAIL_PATTERN.match(email.strip()):
        return False, "Enter a valid email address."
    if not PHONE_PATTERN.match(phone.strip()):
        return False, "Enter a valid phone number with country code."
    if len(password) < 8:
        return False, "Password must be at least 8 characters long."
    return True, ""


def hash_password(password):
    """Hash a password with bcrypt."""
    if bcrypt is None:
        raise RuntimeError("bcrypt dependency is missing")
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password, password_hash):
    """Verify plaintext password against bcrypt hash."""
    if bcrypt is None:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except Exception:
        return False


def signup_user(username, email, phone, password):
    """Create a new user if username/email are unique."""
    ok, message = validate_signup(username, email, phone, password)
    if not ok:
        return False, message

    existing = fetch_one(
        "SELECT id FROM users WHERE lower(username)=lower(?) OR lower(email)=lower(?)",
        (username.strip(), email.strip()),
    )
    if existing:
        return False, "Username or email already exists."

    execute(
        """
        INSERT INTO users (username, email, phone, password_hash, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            username.strip(),
            email.strip().lower(),
            phone.strip(),
            hash_password(password),
            datetime.utcnow().isoformat(timespec="seconds"),
        ),
    )
    return True, "Account created successfully."


def login_user(identity, password):
    """Authenticate user by username or email and return public profile."""
    row = fetch_one(
        """
        SELECT id, username, email, phone, password_hash
        FROM users
        WHERE lower(username)=lower(?) OR lower(email)=lower(?)
        """,
        (identity.strip(), identity.strip()),
    )
    if not row:
        return None

    if not verify_password(password, row["password_hash"]):
        return None

    return {
        "id": row["id"],
        "username": row["username"],
        "email": row["email"],
        "phone": row["phone"],
    }


def _session_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_persistent_session(user_id, ttl_days=30):
    """Create and store a remember-me session token for a user."""
    token = secrets.token_urlsafe(32)
    expires_at = datetime.utcnow() + timedelta(days=ttl_days)
    execute(
        "UPDATE users SET session_token_hash=?, session_expires_at=? WHERE id=?",
        (_session_hash(token), expires_at.isoformat(timespec="seconds"), int(user_id)),
    )
    return token


def clear_persistent_session(user_id=None, token=None):
    """Invalidate stored remember-me session by user id or token."""
    if user_id is not None:
        execute(
            "UPDATE users SET session_token_hash=NULL, session_expires_at=NULL WHERE id=?",
            (int(user_id),),
        )
        return

    if token:
        execute(
            "UPDATE users SET session_token_hash=NULL, session_expires_at=NULL WHERE session_token_hash=?",
            (_session_hash(token),),
        )


def get_user_by_session_token(token):
    """Resolve and validate remember-me token, returning public user profile."""
    if not token:
        return None

    row = fetch_one(
        """
        SELECT id, username, email, phone, session_expires_at
        FROM users
        WHERE session_token_hash=?
        """,
        (_session_hash(token),),
    )
    if not row:
        return None

    expires_at = str(row["session_expires_at"] or "").strip()
    if expires_at:
        try:
            expiry = datetime.fromisoformat(expires_at)
            if expiry < datetime.utcnow():
                clear_persistent_session(user_id=row["id"])
                return None
        except Exception:
            clear_persistent_session(user_id=row["id"])
            return None

    return {
        "id": row["id"],
        "username": row["username"],
        "email": row["email"],
        "phone": row["phone"],
    }


def ensure_startup_user(email, password, username=None, phone=None):
    """Create a startup user if missing so login works on first run."""
    email = str(email or "").strip().lower()
    password = str(password or "").strip()
    if not email or not password:
        return False, "Missing email or password"

    existing = fetch_one("SELECT id FROM users WHERE lower(email)=lower(?)", (email,))
    if existing:
        return True, "User already exists"

    local_name = email.split("@", 1)[0] if "@" in email else "user"
    final_username = (username or local_name or "user").strip()
    final_phone = (phone or "+911234567890").strip()
    ok, msg = signup_user(final_username, email, final_phone, password)
    return ok, msg
