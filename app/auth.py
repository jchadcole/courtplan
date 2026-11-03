"""Password hashing, sessions, CSRF and role checks."""
import hashlib
import hmac
import os
import secrets
import time
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request

SESSION_COOKIE = "ts_session"
SESSION_DAYS = 14
_SCRYPT = dict(n=2 ** 14, r=8, p=1, dklen=32)


def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, salt, dk = stored.split("$")
        got = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), **_SCRYPT)
        return hmac.compare_digest(got.hex(), dk)
    except Exception:
        return False


def check_password_strength(pw: str):
    if len(pw) < 10:
        raise HTTPException(400, "Password must be at least 10 characters.")


def _hash_token(tok: str) -> str:
    return hashlib.sha256(tok.encode()).hexdigest()


def create_session(conn, user_id: int) -> tuple[str, str]:
    tok = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    exp = (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).isoformat()
    conn.execute("INSERT INTO sessions(token_hash,user_id,csrf,expires_at) VALUES (?,?,?,?)",
                 (_hash_token(tok), user_id, csrf, exp))
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (datetime.now(timezone.utc).isoformat(),))
    conn.commit()
    return tok, csrf


def destroy_session(conn, tok: str):
    conn.execute("DELETE FROM sessions WHERE token_hash=?", (_hash_token(tok),))
    conn.commit()


def session_user(conn, request: Request):
    tok = request.cookies.get(SESSION_COOKIE)
    if not tok:
        return None, None
    r = conn.execute(
        "SELECT u.id,u.email,u.name,u.is_superadmin,s.csrf,s.expires_at FROM sessions s "
        "JOIN users u ON u.id=s.user_id WHERE s.token_hash=?", (_hash_token(tok),)).fetchone()
    if not r or r["expires_at"] < datetime.now(timezone.utc).isoformat():
        return None, None
    u = dict(r)
    return u, u.pop("csrf")


# ---- simple in-memory login throttle (per email+ip) ----
_attempts: dict = {}
MAX_ATTEMPTS = 8
WINDOW = 15 * 60


def throttle_check(key: str):
    now = time.time()
    hits = [t for t in _attempts.get(key, []) if now - t < WINDOW]
    _attempts[key] = hits
    if len(hits) >= MAX_ATTEMPTS:
        raise HTTPException(429, "Too many login attempts. Try again in 15 minutes.")


def throttle_fail(key: str):
    _attempts.setdefault(key, []).append(time.time())


def throttle_reset(key: str):
    _attempts.pop(key, None)


# ---- authorization ----

def tournament_role(conn, user, tid: int):
    """Returns 'superadmin' | 'admin' | 'director' | 'team' | None."""
    if not user:
        return None
    if user["is_superadmin"]:
        return "superadmin"
    r = conn.execute("SELECT role FROM tournament_members WHERE tournament_id=? AND user_id=?",
                     (tid, user["id"])).fetchone()
    if r:
        return r["role"]
    r = conn.execute("SELECT 1 FROM team_managers tm JOIN teams t ON t.id=tm.team_id "
                     "WHERE t.tournament_id=? AND tm.user_id=?", (tid, user["id"])).fetchone()
    return "team" if r else None


ADMIN_ROLES = {"superadmin", "admin"}
STAFF_ROLES = {"superadmin", "admin", "director"}


def require_role(conn, user, tid: int, allowed: set) -> str:
    if not user:
        raise HTTPException(401, "Sign in required.")
    role = tournament_role(conn, user, tid)
    if role not in allowed:
        # 404 rather than 403 so tournament existence isn't leaked to outsiders
        raise HTTPException(404 if role is None else 403, "Not found." if role is None else "Not allowed.")
    return role


def can_manage_team(conn, user, team_id: int) -> bool:
    if not user:
        return False
    t = conn.execute("SELECT tournament_id FROM teams WHERE id=?", (team_id,)).fetchone()
    if not t:
        return False
    if tournament_role(conn, user, t["tournament_id"]) in ADMIN_ROLES:
        return True
    return conn.execute("SELECT 1 FROM team_managers WHERE team_id=? AND user_id=?",
                        (team_id, user["id"])).fetchone() is not None


def new_invite_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(10))
