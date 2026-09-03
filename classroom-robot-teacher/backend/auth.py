"""Teacher authentication: salted PBKDF2 password hashing + in-memory sessions.

Local-prototype scope: a single teacher account is seeded on first run
(username: teacher / password: teach123 - change it via change_password()).
Session tokens live in memory and expire after 12 h; they are delivered as
an HttpOnly cookie so the browser scripts never see them.
"""
import hashlib
import secrets
import time

from . import database as db

COOKIE_NAME = "cat_auth"
SESSION_TTL = 12 * 3600
DEFAULT_USERNAME = "teacher"
DEFAULT_PASSWORD = "teach123"

_sessions = {}   # token -> {"username": str, "expires": float}


def _hash_pw(password: str, salt_hex: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(),
                               bytes.fromhex(salt_hex), 100_000).hex()


def ensure_default_teacher() -> None:
    if not db.list_teachers():
        salt = secrets.token_hex(16)
        db.create_teacher(DEFAULT_USERNAME, salt, _hash_pw(DEFAULT_PASSWORD, salt))
        print(f"[auth] Seeded default teacher account: "
              f"{DEFAULT_USERNAME} / {DEFAULT_PASSWORD} (change it after first login)")


def login(username: str, password: str):
    """Return a session token on success, else None."""
    teacher = db.get_teacher((username or "").strip().lower())
    if not teacher or not secrets.compare_digest(
            _hash_pw(password or "", teacher["pw_salt"]), teacher["pw_hash"]):
        return None
    token = secrets.token_hex(32)
    _sessions[token] = {"username": teacher["username"],
                        "expires": time.time() + SESSION_TTL}
    return token


def check(token) -> str:
    """Return the logged-in username for a valid token, else None."""
    s = _sessions.get(token or "")
    if not s:
        return None
    if s["expires"] < time.time():
        _sessions.pop(token, None)
        return None
    return s["username"]


def logout(token) -> None:
    _sessions.pop(token or "", None)


def change_password(username: str, old_password: str, new_password: str) -> bool:
    teacher = db.get_teacher(username)
    if not teacher or not secrets.compare_digest(
            _hash_pw(old_password or "", teacher["pw_salt"]), teacher["pw_hash"]):
        return False
    if len(new_password or "") < 6:
        return False
    salt = secrets.token_hex(16)
    db.update_teacher_password(username, salt, _hash_pw(new_password, salt))
    return True
