"""Accounts, passwords and cookie sessions.

Temporary users are the point of this module: the admin mints a username and a
password that stop working on a date, and nothing else about them is
recoverable. Passwords are PBKDF2-HMAC-SHA256 with a per-user salt -- stdlib
only, so there is no native wheel to build and nothing to keep patched.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass

from fastapi import Request

from app import db
from app.config import cfg

_ITERATIONS = 240_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, want_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                 bytes.fromhex(salt_hex), int(iters))
    except (ValueError, TypeError):
        return False
    # Constant time: a timing oracle on a login form is cheap to avoid.
    return hmac.compare_digest(dk.hex(), want_hex)


@dataclass
class User:
    id: str
    username: str
    role: str
    expires_at: float | None
    theme: str = "dark"

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def ensure_admin() -> None:
    """Create or re-sync the admin from the environment on every boot.

    Re-syncing matters: the admin password lives in `.env`, so changing it there
    and restarting must actually change the login. An admin row that drifted
    from the file would be a credential nobody can find and nobody can rotate.
    """
    row = db.one("SELECT * FROM users WHERE username=? COLLATE NOCASE",
                 (cfg.ADMIN_USERNAME,))
    if row is None:
        db.run(
            "INSERT INTO users (id, username, password_hash, role, created_at, note) "
            "VALUES (?,?,?,?,?,?)",
            (db.new_id("usr"), cfg.ADMIN_USERNAME, hash_password(cfg.ADMIN_PASSWORD),
             "admin", db.now(), "from STUDIO_ADMIN_PASSWORD"))
        return
    if not verify_password(cfg.ADMIN_PASSWORD, row["password_hash"]):
        db.run("UPDATE users SET password_hash=?, role='admin', disabled=0, expires_at=NULL "
               "WHERE id=?", (hash_password(cfg.ADMIN_PASSWORD), row["id"]))


def is_unlimited(user_id: str) -> bool:
    """Exempt from the per-user hourly limits. Every quota check asks this.

    The admin always is, by role rather than by the flag: the admin row is not
    listed on /admin, so a flag on it could be neither seen nor changed there,
    and a renamed admin is a fresh row that would lose it.
    """
    row = db.one("SELECT unlimited, role FROM users WHERE id=?", (user_id,))
    return bool(row and (row["unlimited"] or row["role"] == "admin"))


def create_user(username: str, password: str, *, expires_at: float | None,
                note: str = "") -> str:
    user_id = db.new_id("usr")
    db.run(
        "INSERT INTO users (id, username, password_hash, role, created_at, expires_at, note) "
        "VALUES (?,?,?,?,?,?,?)",
        (user_id, username, hash_password(password), "user", db.now(), expires_at, note))
    return user_id


def authenticate(username: str, password: str) -> tuple[User | None, str]:
    """(user, reason). `reason` is for the operator's log, not for the form.

    The form says one thing for every failure. Telling a stranger that the
    username exists but the password is wrong, or that the account expired
    yesterday, is telling them which usernames are real.
    """
    row = db.one("SELECT * FROM users WHERE username=? COLLATE NOCASE", (username,))
    if row is None:
        # Spend the same work anyway, so a missing user is not measurably
        # faster to reject than a wrong password.
        verify_password(password, hash_password("decoy"))
        return None, "no such user"
    if not verify_password(password, row["password_hash"]):
        return None, "bad password"
    if row["disabled"]:
        return None, "disabled"
    if row["expires_at"] is not None and row["expires_at"] < db.now():
        return None, "expired"
    return User(row["id"], row["username"], row["role"], row["expires_at"]), "ok"


def start_session(user_id: str) -> str:
    token = os.urandom(32).hex()
    expires = db.now() + cfg.SESSION_TTL_HOURS * 3600
    db.run("INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?,?,?,?)",
           (token, user_id, db.now(), expires))
    return token


def end_session(token: str) -> None:
    db.run("DELETE FROM sessions WHERE token=?", (token,))


def revoke_sessions(user_id: str) -> None:
    """Drop every way this user is currently signed in -- browser AND assistants.

    The MCP tokens go here too, on purpose: disabling an account or resetting
    its password has to cut off a connected ChatGPT/Claude as well, or the
    "temporary" account lives on inside somebody's connector for a month.
    """
    db.run("DELETE FROM sessions WHERE user_id=?", (user_id,))
    db.run("DELETE FROM oauth_tokens WHERE user_id=?", (user_id,))
    db.run("DELETE FROM oauth_codes WHERE user_id=?", (user_id,))


def current_user(request: Request) -> User | None:
    """The signed-in user, or None. Re-checks the ACCOUNT, not just the cookie.

    A session that outlives its user's expiry date is the obvious way a
    "temporary" account stops being temporary, so expiry, disabling and deletion
    are all enforced here on every single request rather than only at login.
    """
    token = request.cookies.get("studio_session")
    if not token:
        return None
    row = db.one(
        "SELECT s.token, s.expires_at AS s_exp, u.id, u.username, u.role, "
        "       u.expires_at AS u_exp, u.disabled, u.theme "
        "FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token=?", (token,))
    if row is None:
        return None
    if row["s_exp"] < db.now():
        db.run("DELETE FROM sessions WHERE token=?", (token,))
        return None
    if row["disabled"] or (row["u_exp"] is not None and row["u_exp"] < db.now()):
        db.run("DELETE FROM sessions WHERE user_id=?", (row["id"],))
        return None
    return User(row["id"], row["username"], row["role"], row["u_exp"], row["theme"] or "dark")


THEMES = ("dark", "light", "system")
PLANNERS = ("27b", "flash")


def planner_of(user_id: str | None) -> str:
    """The thinking model this person chose in Settings: "27b" (default, faster) or "flash"
    (Qwen3.8-Flash-Next: stronger answers, slower)."""
    if not user_id:
        return "27b"
    row = db.one("SELECT planner FROM users WHERE id=?", (user_id,))
    return (row["planner"] if row and row["planner"] in PLANNERS else "27b")


def set_planner(user_id: str, planner: str) -> None:
    if planner not in PLANNERS:
        raise ValueError(planner)
    db.run("UPDATE users SET planner=? WHERE id=?", (planner, user_id))


def set_theme(user_id: str, theme: str) -> None:
    if theme not in THEMES:
        raise ValueError(theme)
    db.run("UPDATE users SET theme=? WHERE id=?", (theme, user_id))


def purge_expired() -> int:
    """Drop dead sessions. Expired USERS are kept so the admin can see them."""
    with db.connect() as conn:
        cur = conn.execute("DELETE FROM sessions WHERE expires_at < ?", (db.now(),))
        conn.execute("DELETE FROM oauth_tokens WHERE expires_at < ?", (db.now(),))
        conn.execute("DELETE FROM oauth_codes WHERE expires_at < ?", (db.now(),))
        return cur.rowcount
