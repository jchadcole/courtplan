"""HTTP API + static web UI for the tournament scheduler."""
import asyncio
import csv
import io
import json
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

import segno
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import __version__, ai, auth, brackets, db, engine
from .events import broker

STATIC = os.path.join(os.path.dirname(__file__), "..", "static")
SECURE_COOKIES = os.environ.get("TOURNEY_SECURE_COOKIES", "0") == "1"

@asynccontextmanager
async def lifespan(_app):
    db.init()
    broker.loop = asyncio.get_running_loop()
    yield


app = FastAPI(title="AI Tournament Scheduler", version=__version__, docs_url="/api/docs", redoc_url=None, lifespan=lifespan)


# ---------------------------------------------------------------------------
# Middleware: security headers + CSRF
# ---------------------------------------------------------------------------

CSRF_EXEMPT = {"/api/auth/login", "/api/auth/register"}


@app.middleware("http")
async def security(request: Request, call_next):
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.url.path.startswith("/api/"):
        origin = request.headers.get("origin")
        if origin and origin.split("://", 1)[-1] != request.headers.get("host"):
            return JSONResponse({"detail": "Cross-origin request blocked."}, status_code=403)
        if request.url.path not in CSRF_EXEMPT:
            conn = db.connect()
            try:
                user, csrf = auth.session_user(conn, request)
            finally:
                conn.close()
            if not user:
                return JSONResponse({"detail": "Sign in required."}, status_code=401)
            if not secrets.compare_digest(request.headers.get("x-csrf-token", ""), csrf):
                return JSONResponse({"detail": "Invalid CSRF token."}, status_code=403)
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
    if SECURE_COOKIES:
        resp.headers["Strict-Transport-Security"] = "max-age=31536000"
    return resp


class Ctx:
    def __init__(self, conn, user):
        self.conn = conn
        self.user = user


def get_ctx(request: Request):
    conn = db.connect()
    try:
        user, _ = auth.session_user(conn, request)
        yield Ctx(conn, user)
    finally:
        conn.close()


def need_user(c: Ctx):
    if not c.user:
        raise HTTPException(401, "Sign in required.")
    return c.user


def audit(c: Ctx, tid, action, detail=""):
    c.conn.execute("INSERT INTO audit_log(tournament_id,user_id,action,detail) VALUES (?,?,?,?)",
                   (tid, c.user["id"] if c.user else None, action, detail))


def one(c: Ctx, sql, *args):
    r = c.conn.execute(sql, args).fetchone()
    if not r:
        raise HTTPException(404, "Not found.")
    return dict(r)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class RegisterIn(BaseModel):
    email: str = Field(max_length=200)
    name: str = Field(min_length=1, max_length=100)
    password: str = Field(max_length=200)
    invite_code: Optional[str] = None

    @field_validator("email")
    @classmethod
    def _email(cls, v):
        v = v.strip().lower()
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError("Enter a valid email address.")
        return v


class LoginIn(BaseModel):
    email: str
    password: str


def _set_cookie(resp: Response, tok: str):
    resp.set_cookie(auth.SESSION_COOKIE, tok, httponly=True, samesite="lax", secure=SECURE_COOKIES,
                    max_age=auth.SESSION_DAYS * 86400, path="/")


@app.post("/api/auth/register")
def register(body: RegisterIn, response: Response, c: Ctx = Depends(get_ctx)):
    auth.check_password_strength(body.password)
    if c.conn.execute("SELECT 1 FROM users WHERE email=?", (body.email,)).fetchone():
        raise HTTPException(409, "An account with that email already exists.")
    first = c.conn.execute("SELECT COUNT(*) n FROM users").fetchone()["n"] == 0
    cur = c.conn.execute("INSERT INTO users(email,name,password_hash,is_superadmin) VALUES (?,?,?,?)",
                         (body.email, body.name.strip(), auth.hash_password(body.password), int(first)))
    uid = cur.lastrowid
    c.conn.commit()
    if body.invite_code:
        _join_team(c, uid, body.invite_code)
    tok, csrf = auth.create_session(c.conn, uid)
    _set_cookie(response, tok)
    return {"ok": True, "csrf": csrf, "superadmin": first}


@app.post("/api/auth/login")
def login(body: LoginIn, request: Request, response: Response, c: Ctx = Depends(get_ctx)):
    key = f"{body.email.lower()}|{request.client.host if request.client else ''}"
    auth.throttle_check(key)
    u = c.conn.execute("SELECT id,password_hash FROM users WHERE email=?", (body.email.strip(),)).fetchone()
    if not u or not auth.verify_password(body.password, u["password_hash"]):
        auth.throttle_fail(key)
        raise HTTPException(401, "Email or password is incorrect.")
    auth.throttle_reset(key)
    tok, csrf = auth.create_session(c.conn, u["id"])
    _set_cookie(response, tok)
    return {"ok": True, "csrf": csrf}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response, c: Ctx = Depends(get_ctx)):
    tok = request.cookies.get(auth.SESSION_COOKIE)
    if tok:
        auth.destroy_session(c.conn, tok)
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return {"ok": True}


@app.get("/api/version")
def version():
    return {"name": "CourtPlan", "version": __version__}


@app.get("/api/me")
def me(request: Request, c: Ctx = Depends(get_ctx)):
    user, csrf = auth.session_user(c.conn, request)
    if not user:
        return {"user": None}
    if user["is_superadmin"]:
        ts = db.rows(c.conn.execute("SELECT id,name,start_date,end_date,'superadmin' role FROM tournaments "
                                    "ORDER BY start_date DESC"))
    else:
        ts = db.rows(c.conn.execute(
            "SELECT t.id,t.name,t.start_date,t.end_date,m.role FROM tournaments t JOIN tournament_members m "
            "ON m.tournament_id=t.id WHERE m.user_id=? ORDER BY t.start_date DESC", (user["id"],)))
    teams = db.rows(c.conn.execute(
        "SELECT tm.team_id id, t.name, t.tournament_id, tr.name tournament, d.name division FROM team_managers tm "
        "JOIN teams t ON t.id=tm.team_id JOIN tournaments tr ON tr.id=t.tournament_id "
        "JOIN divisions d ON d.id=t.division_id WHERE tm.user_id=?", (user["id"],)))
    can_create = bool(user["is_superadmin"]) or any(t["role"] == "admin" for t in ts)
    return {"user": user, "csrf": csrf, "tournaments": ts, "teams": teams, "can_create": can_create,
            "ai_enabled": ai.ai_enabled()}


class JoinIn(BaseModel):
    code: str


def _join_team(c: Ctx, uid: int, code: str):
    t = c.conn.execute("SELECT id,name FROM teams WHERE invite_code=?", (code.strip().upper(),)).fetchone()
    if not t:
        raise HTTPException(404, "That team code wasn't found.")
    c.conn.execute("INSERT OR IGNORE INTO team_managers(team_id,user_id) VALUES (?,?)", (t["id"], uid))
    c.conn.commit()
    return dict(t)


@app.post("/api/teams/join")
def join_team(body: JoinIn, c: Ctx = Depends(get_ctx)):
    u = need_user(c)
    return _join_team(c, u["id"], body.code)


# ---------------------------------------------------------------------------
# Tournaments
# ---------------------------------------------------------------------------

class TournamentIn(BaseModel):
    name: Optional[str] = Field(None, max_length=120)
    location: Optional[str] = Field(None, max_length=200)
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    game_minutes: Optional[int] = Field(None, ge=20, le=180)
    buffer_minutes: Optional[int] = Field(None, ge=0, le=60)
    min_rest_minutes: Optional[int] = Field(None, ge=0, le=600)
    travel_buffer_minutes: Optional[int] = Field(None, ge=0, le=300)
    target_utilization: Optional[float] = Field(None, ge=0.5, le=1.0)
    max_games_per_day: Optional[int] = Field(None, ge=1, le=6)
    day_start: Optional[str] = None
    day_end: Optional[str] = None
    prefs_locked: Optional[bool] = None


T_FIELDS = ["name", "location", "start_date", "end_date", "game_minutes", "buffer_minutes", "min_rest_minutes",
            "travel_buffer_minutes", "target_utilization", "max_games_per_day", "day_start", "day_end",
            "prefs_locked"]


def _check_t(d):
    try:
        if d.get("start_date"):
            datetime.fromisoformat(d["start_date"])
        if d.get("end_date"):
            datetime.fromisoformat(d["end_date"])
        for k in ("day_start", "day_end"):
            if d.get(k):
                engine.hm_to_min(d[k])
    except Exception:
        raise HTTPException(400, "Dates must be YYYY-MM-DD and times HH:MM.")


@app.post("/api/tournaments")
def create_tournament(body: TournamentIn, c: Ctx = Depends(get_ctx)):
    u = need_user(c)
    if not u["is_superadmin"] and not c.conn.execute(
            "SELECT 1 FROM tournament_members WHERE user_id=? AND role='admin'", (u["id"],)).fetchone():
        raise HTTPException(403, "Only tournament organizers can create tournaments.")
    d = body.model_dump(exclude_none=True)
    if not d.get("name") or not d.get("start_date") or not d.get("end_date"):
        raise HTTPException(400, "Name, start date and end date are required.")
    _check_t(d)
    if d["end_date"] < d["start_date"]:
        raise HTTPException(400, "End date is before start date.")
    d["public_slug"] = secrets.token_urlsafe(8)
    d["created_by"] = u["id"]
    cols = ",".join(d)
    cur = c.conn.execute(f"INSERT INTO tournaments({cols}) VALUES ({','.join('?' * len(d))})", list(d.values()))
    tid = cur.lastrowid
    c.conn.execute("INSERT INTO tournament_members(tournament_id,user_id,role) VALUES (?,?, 'admin')", (tid, u["id"]))
    audit(c, tid, "tournament.create", d["name"])
    c.conn.commit()
    return {"id": tid}


@app.get("/api/t/{tid}")
def get_tournament(tid: int, c: Ctx = Depends(get_ctx)):
    role = auth.require_role(c.conn, c.user, tid, auth.STAFF_ROLES | {"team"})
    t = one(c, "SELECT * FROM tournaments WHERE id=?", tid)
    out = {"tournament": t, "role": role}
    out["venues"] = db.rows(c.conn.execute("SELECT * FROM venues WHERE tournament_id=? ORDER BY id", (tid,)))
    out["courts"] = db.rows(c.conn.execute(
        "SELECT c.* FROM courts c JOIN venues v ON v.id=c.venue_id WHERE v.tournament_id=? ORDER BY c.venue_id,c.id",
        (tid,)))
    out["divisions"] = db.rows(c.conn.execute("SELECT * FROM divisions WHERE tournament_id=? ORDER BY name", (tid,)))
    team_cols = "t.id,t.name,t.club,t.division_id,t.coach_id,t.rating"
    if role in auth.STAFF_ROLES:
        team_cols += ",t.invite_code,t.pref_notes,co.name coach_name,co.email coach_email," \
                     "(SELECT COUNT(*) FROM preferences p WHERE p.team_id=t.id) pref_count," \
                     "(SELECT COUNT(*) FROM team_managers m WHERE m.team_id=t.id) manager_count"
    else:
        team_cols += ",co.name coach_name"
    out["teams"] = db.rows(c.conn.execute(
        f"SELECT {team_cols} FROM teams t LEFT JOIN coaches co ON co.id=t.coach_id "
        "WHERE t.tournament_id=? ORDER BY t.division_id,t.name", (tid,)))
    if role in auth.STAFF_ROLES:
        out["coaches"] = db.rows(c.conn.execute(
            "SELECT co.*, (SELECT COUNT(*) FROM teams t WHERE t.coach_id=co.id) team_count FROM coaches co "
            "WHERE tournament_id=? ORDER BY name", (tid,)))
        out["crews"] = db.rows(c.conn.execute("SELECT * FROM ref_crews WHERE tournament_id=? ORDER BY name", (tid,)))
        out["members"] = db.rows(c.conn.execute(
            "SELECT u.id user_id,u.name,u.email,m.role,m.venue_id FROM tournament_members m JOIN users u "
            "ON u.id=m.user_id WHERE m.tournament_id=?", (tid,)))
        out["last_run"] = c.conn.execute(
            "SELECT id,kind,created_at,stats FROM schedule_runs WHERE tournament_id=? ORDER BY id DESC LIMIT 1",
            (tid,)).fetchone()
        if out["last_run"]:
            out["last_run"] = dict(out["last_run"])
            out["last_run"]["stats"] = json.loads(out["last_run"]["stats"] or "{}")
    else:
        out["my_teams"] = [r["team_id"] for r in c.conn.execute(
            "SELECT tm.team_id FROM team_managers tm JOIN teams t ON t.id=tm.team_id WHERE tm.user_id=? AND "
            "t.tournament_id=?", (c.user["id"], tid))]
    return out


@app.patch("/api/t/{tid}")
def update_tournament(tid: int, body: TournamentIn, c: Ctx = Depends(get_ctx)):
    auth.require_role(c.conn, c.user, tid, auth.ADMIN_ROLES)
    d = body.model_dump(exclude_none=True)
    _check_t(d)
    if d:
        c.conn.execute(f"UPDATE tournaments SET {','.join(k + '=?' for k in d)} WHERE id=?", [*d.values(), tid])
        audit(c, tid, "tournament.update", json.dumps(d))
        c.conn.commit()
    return {"ok": True}


@app.delete("/api/t/{tid}")
def delete_tournament(tid: int, c: Ctx = Depends(get_ctx)):
    auth.require_role(c.conn, c.user, tid, auth.ADMIN_ROLES)
    c.conn.execute("DELETE FROM tournaments WHERE id=?", (tid,))
    c.conn.commit()
    return {"ok": True}


# ---- staff ----

class MemberIn(BaseModel):
    email: str
    role: str
    venue_id: Optional[int] = None


@app.post("/api/t/{tid}/members")
def add_member(tid: int, body: MemberIn, c: Ctx = Depends(get_ctx)):
    auth.require_role(c.conn, c.user, tid, auth.ADMIN_ROLES)
    if body.role not in ("admin", "director"):
        raise HTTPException(400, "Role must be admin or director.")
    u = c.conn.execute("SELECT id FROM users WHERE email=?", (body.email.strip(),)).fetchone()
    if not u:
        raise HTTPException(404, "No account with that email yet. Ask them to sign up first.")
    if body.venue_id:
        one(c, "SELECT id FROM venues WHERE id=? AND tournament_id=?", body.venue_id, tid)
    c.conn.execute("INSERT OR REPLACE INTO tournament_members(tournament_id,user_id,role,venue_id) VALUES (?,?,?,?)",
                   (tid, u["id"], body.role, body.venue_id))
    audit(c, tid, "member.add", f"{body.email} {body.role}")
    c.conn.commit()
    return {"ok": True}


@app.delete("/api/t/{tid}/members/{uid}")
def remove_member(tid: int, uid: int, c: Ctx = Depends(get_ctx)):
    auth.require_role(c.conn, c.user, tid, auth.ADMIN_ROLES)
    if uid == c.user["id"]:
        raise HTTPException(400, "You can't remove yourself.")
    c.conn.execute("DELETE FROM tournament_members WHERE tournament_id=? AND user_id=?", (tid, uid))
    c.conn.commit()
    return {"ok": True}


# ---- venues & courts ----

class VenueIn(BaseModel):
    name: Optional[str] = Field(None, max_length=120)
    address: Optional[str] = Field(None, max_length=200)
    open_time: Optional[str] = None
    close_time: Optional[str] = None
    director_contact: Optional[str] = Field(None, max_length=120)
    courts: Optional[int] = Field(None, ge=0, le=40)


def _tid_of_venue(c, vid):
    return one(c, "SELECT tournament_id FROM venues WHERE id=?", vid)["tournament_id"]


@app.post("/api/t/{tid}/venues")
def add_venue(tid: int, body: VenueIn, c: Ctx = Depends(get_ctx)):
    auth.require_role(c.conn, c.user, tid, auth.ADMIN_ROLES)
    if not body.name:
        raise HTTPException(400, "Venue name is required.")
    cur = c.conn.execute("INSERT INTO venues(tournament_id,name,address,open_time,close_time,director_contact) "
                         "VALUES (?,?,?,?,?,?)", (tid, body.name, body.address or "", body.open_time or None,
                                                  body.close_time or None, body.director_contact or ""))
    for i in range(body.courts or 0):
        c.conn.execute("INSERT INTO courts(venue_id,name) VALUES (?,?)", (cur.lastrowid, f"Court {i + 1}"))
    c.conn.commit()
    return {"id": cur.lastrowid}


@app.patch("/api/venues/{vid}")
def update_venue(vid: int, body: VenueIn, c: Ctx = Depends(get_ctx)):
    auth.require_role(c.conn, c.user, _tid_of_venue(c, vid), auth.ADMIN_ROLES)
    d = body.model_dump(exclude_none=True)
    d.pop("courts", None)
    for k in ("open_time", "close_time"):
        if k in d and d[k] == "":
            d[k] = None
    if d:
        c.conn.execute(f"UPDATE venues SET {','.join(k + '=?' for k in d)} WHERE id=?", [*d.values(), vid])
        c.conn.commit()
    return {"ok": True}


@app.delete("/api/venues/{vid}")
def delete_venue(vid: int, c: Ctx = Depends(get_ctx)):
    auth.require_role(c.conn, c.user, _tid_of_venue(c, vid), auth.ADMIN_ROLES)
    c.conn.execute("DELETE FROM venues WHERE id=?", (vid,))
    c.conn.commit()
    return {"ok": True}


class CourtIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)


@app.post("/api/venues/{vid}/courts")
def add_court(vid: int, body: CourtIn, c: Ctx = Depends(get_ctx)):
    auth.require_role(c.conn, c.user, _tid_of_venue(c, vid), auth.ADMIN_ROLES)
    cur = c.conn.execute("INSERT INTO courts(venue_id,name) VALUES (?,?)", (vid, body.name))
    c.conn.commit()
    return {"id": cur.lastrowid}


@app.delete("/api/courts/{cid}")
def delete_court(cid: int, c: Ctx = Depends(get_ctx)):
    vid = one(c, "SELECT venue_id FROM courts WHERE id=?", cid)["venue_id"]
    auth.require_role(c.conn, c.user, _tid_of_venue(c, vid), auth.ADMIN_ROLES)
    c.conn.execute("DELETE FROM courts WHERE id=?", (cid,))
    c.conn.commit()
    return {"ok": True}


# ---- divisions ----

class DivisionIn(BaseModel):
    name: Optional[str] = Field(None, max_length=60)
    pool_size: Optional[int] = Field(None, ge=3, le=8)
    home_venue_id: Optional[int] = None


@app.post("/api/t/{tid}/divisions")
def add_division(tid: int, body: DivisionIn, c: Ctx = Depends(get_ctx)):
    auth.require_role(c.conn, c.user, tid, auth.ADMIN_ROLES)
    if not body.name:
        raise HTTPException(400, "Division name is required.")
    try:
        cur = c.conn.execute("INSERT INTO divisions(tournament_id,name,pool_size,home_venue_id) VALUES (?,?,?,?)",
                             (tid, body.name, body.pool_size or 4, body.home_venue_id or None))
    except Exception:
        raise HTTPException(409, "A division with that name already exists.")
    c.conn.commit()
    return {"id": cur.lastrowid}


@app.patch("/api/divisions/{did}")
def update_division(did: int, body: DivisionIn, c: Ctx = Depends(get_ctx)):
    tid = one(c, "SELECT tournament_id FROM divisions WHERE id=?", did)["tournament_id"]
    auth.require_role(c.conn, c.user, tid, auth.ADMIN_ROLES)
    d = body.model_dump(exclude_unset=True)
    if "home_venue_id" in d and not d["home_venue_id"]:
        d["home_venue_id"] = None
    if d:
        c.conn.execute(f"UPDATE divisions SET {','.join(k + '=?' for k in d)} WHERE id=?", [*d.values(), did])
        c.conn.commit()
    return {"ok": True}


@app.delete("/api/divisions/{did}")
def delete_division(did: int, c: Ctx = Depends(get_ctx)):
    tid = one(c, "SELECT tournament_id FROM divisions WHERE id=?", did)["tournament_id"]
    auth.require_role(c.conn, c.user, tid, auth.ADMIN_ROLES)
    c.conn.execute("DELETE FROM divisions WHERE id=?", (did,))
    c.conn.commit()
    return {"ok": True}


# ---- teams & coaches ----

class TeamIn(BaseModel):
    division_id: Optional[int] = None
    name: Optional[str] = Field(None, max_length=100)
    club: Optional[str] = Field(None, max_length=100)
    coach_name: Optional[str] = Field(None, max_length=100)
    coach_email: Optional[str] = Field(None, max_length=200)
    rating: Optional[float] = Field(None, ge=-1000, le=10000)


def _coach_id(c, tid, name, email):
    if not name and not email:
        return None
    if email:
        r = c.conn.execute("SELECT id FROM coaches WHERE tournament_id=? AND email=?", (tid, email.strip())).fetchone()
        if r:
            return r["id"]
    elif name:
        r = c.conn.execute("SELECT id FROM coaches WHERE tournament_id=? AND name=? AND (email IS NULL OR email='')",
                           (tid, name.strip())).fetchone()
        if r:
            return r["id"]
    return c.conn.execute("INSERT INTO coaches(tournament_id,name,email) VALUES (?,?,?)",
                          (tid, (name or email).strip(), (email or "").strip() or None)).lastrowid


def _new_team(c, tid, division_id, name, club, coach_name, coach_email, rating=None):
    one(c, "SELECT id FROM divisions WHERE id=? AND tournament_id=?", division_id, tid)
    coach = _coach_id(c, tid, coach_name, coach_email)
    return c.conn.execute("INSERT INTO teams(tournament_id,division_id,name,club,coach_id,invite_code,rating) "
                          "VALUES (?,?,?,?,?,?,?)", (tid, division_id, name, club or "", coach,
                                                     auth.new_invite_code(), rating)).lastrowid


@app.post("/api/t/{tid}/teams")
def add_team(tid: int, body: TeamIn, c: Ctx = Depends(get_ctx)):
    auth.require_role(c.conn, c.user, tid, auth.ADMIN_ROLES)
    if not body.name or not body.division_id:
        raise HTTPException(400, "Team name and division are required.")
    team_id = _new_team(c, tid, body.division_id, body.name, body.club, body.coach_name, body.coach_email,
                        body.rating)
    c.conn.commit()
    return {"id": team_id}


@app.patch("/api/teams/{team_id}")
def update_team(team_id: int, body: TeamIn, c: Ctx = Depends(get_ctx)):
    tid = one(c, "SELECT tournament_id FROM teams WHERE id=?", team_id)["tournament_id"]
    auth.require_role(c.conn, c.user, tid, auth.ADMIN_ROLES)
    d = body.model_dump(exclude_none=True)
    upd = {k: d[k] for k in ("name", "club", "division_id", "rating") if k in d}
    if "division_id" in upd:
        one(c, "SELECT id FROM divisions WHERE id=? AND tournament_id=?", upd["division_id"], tid)
    if "coach_name" in d or "coach_email" in d:
        upd["coach_id"] = _coach_id(c, tid, d.get("coach_name"), d.get("coach_email"))
    if upd:
        c.conn.execute(f"UPDATE teams SET {','.join(k + '=?' for k in upd)} WHERE id=?", [*upd.values(), team_id])
        c.conn.commit()
    return {"ok": True}


@app.delete("/api/teams/{team_id}")
def delete_team(team_id: int, c: Ctx = Depends(get_ctx)):
    tid = one(c, "SELECT tournament_id FROM teams WHERE id=?", team_id)["tournament_id"]
    auth.require_role(c.conn, c.user, tid, auth.ADMIN_ROLES)
    c.conn.execute("DELETE FROM teams WHERE id=?", (team_id,))
    c.conn.commit()
    return {"ok": True}


class ImportIn(BaseModel):
    csv: str = Field(max_length=2_000_000)


@app.post("/api/t/{tid}/import")
def import_teams(tid: int, body: ImportIn, c: Ctx = Depends(get_ctx)):
    """CSV columns: division, team, club, coach_name, coach_email, rating (header row required)."""
    auth.require_role(c.conn, c.user, tid, auth.ADMIN_ROLES)
    reader = csv.DictReader(io.StringIO(body.csv.strip()))
    norm = lambda r: {(k or "").strip().lower().replace(" ", "_"): (v or "").strip() for k, v in r.items()}
    added, errors = 0, []
    for i, raw in enumerate(reader, start=2):
        r = norm(raw)
        div, name = r.get("division"), r.get("team") or r.get("team_name")
        if not div or not name:
            errors.append(f"Row {i}: division and team are required")
            continue
        d = c.conn.execute("SELECT id FROM divisions WHERE tournament_id=? AND name=?", (tid, div)).fetchone()
        did = d["id"] if d else c.conn.execute("INSERT INTO divisions(tournament_id,name) VALUES (?,?)",
                                               (tid, div)).lastrowid
        try:
            rating = float(r["rating"]) if r.get("rating") else None
        except ValueError:
            errors.append(f"Row {i}: rating '{r['rating']}' is not a number; imported without it")
            rating = None
        _new_team(c, tid, did, name, r.get("club"), r.get("coach_name") or r.get("coach"), r.get("coach_email"),
                  rating)
        added += 1
    audit(c, tid, "teams.import", f"{added} teams")
    c.conn.commit()
    return {"added": added, "errors": errors}


class CoachIn(BaseModel):
    name: Optional[str] = Field(None, max_length=100)
    email: Optional[str] = Field(None, max_length=200)
    phone: Optional[str] = Field(None, max_length=40)
    min_gap_minutes: Optional[int] = Field(None, ge=0, le=600)


@app.patch("/api/coaches/{coach_id}")
def update_coach(coach_id: int, body: CoachIn, c: Ctx = Depends(get_ctx)):
    """Per-coach rule profile, e.g. a mandatory 75-minute gap between this coach's games anywhere."""
    tid = one(c, "SELECT tournament_id FROM coaches WHERE id=?", coach_id)["tournament_id"]
    auth.require_role(c.conn, c.user, tid, auth.ADMIN_ROLES)
    d = body.model_dump(exclude_unset=True)
    if "min_gap_minutes" in d and not d["min_gap_minutes"]:
        d["min_gap_minutes"] = None
    if d:
        c.conn.execute(f"UPDATE coaches SET {','.join(k + '=?' for k in d)} WHERE id=?", [*d.values(), coach_id])
        audit(c, tid, "coach.update", json.dumps(d))
        c.conn.commit()
    return {"ok": True}


# ---- referee crews ----

class CrewIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    venue_id: Optional[int] = None


@app.post("/api/t/{tid}/crews")
def add_crew(tid: int, body: CrewIn, c: Ctx = Depends(get_ctx)):
    auth.require_role(c.conn, c.user, tid, auth.ADMIN_ROLES)
    cur = c.conn.execute("INSERT INTO ref_crews(tournament_id,name,venue_id) VALUES (?,?,?)",
                         (tid, body.name, body.venue_id or None))
    c.conn.commit()
    return {"id": cur.lastrowid}


@app.delete("/api/crews/{cid}")
def delete_crew(cid: int, c: Ctx = Depends(get_ctx)):
    tid = one(c, "SELECT tournament_id FROM ref_crews WHERE id=?", cid)["tournament_id"]
    auth.require_role(c.conn, c.user, tid, auth.ADMIN_ROLES)
    c.conn.execute("DELETE FROM ref_crews WHERE id=?", (cid,))
    c.conn.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Team preferences
# ---------------------------------------------------------------------------

class PrefIn(BaseModel):
    kind: str
    day: Optional[str] = None
    start: Optional[str] = None
    end: Optional[str] = None
    venue_id: Optional[int] = None
    hard: bool = False
    note: str = Field("", max_length=300)
    source: str = "manual"


def _team_access(c, team_id):
    if not auth.can_manage_team(c.conn, c.user, team_id):
        raise HTTPException(404, "Not found.")
    return one(c, "SELECT t.*, tr.prefs_locked FROM teams t JOIN tournaments tr ON tr.id=t.tournament_id "
                  "WHERE t.id=?", team_id)


@app.get("/api/teams/{team_id}/prefs")
def list_prefs(team_id: int, c: Ctx = Depends(get_ctx)):
    team = _team_access(c, team_id)
    prefs = db.rows(c.conn.execute("SELECT * FROM preferences WHERE team_id=? ORDER BY id", (team_id,)))
    return {"team": {k: team[k] for k in ("id", "name", "pref_notes", "prefs_locked", "tournament_id")},
            "preferences": prefs}


def _validate_pref(c, team, p: PrefIn):
    if p.kind not in ("unavailable", "not_before", "not_after", "preferred_venue"):
        raise HTTPException(400, "Unknown preference type.")
    t = one(c, "SELECT start_date,end_date FROM tournaments WHERE id=?", team["tournament_id"])
    if p.day and not (t["start_date"] <= p.day <= t["end_date"]):
        raise HTTPException(400, "That day is outside the tournament dates.")
    for v in (p.start, p.end):
        if v:
            try:
                engine.hm_to_min(v)
            except Exception:
                raise HTTPException(400, "Times must be HH:MM.")
    if p.kind == "not_before" and not p.start:
        raise HTTPException(400, "Pick the earliest start time.")
    if p.kind == "not_after" and not p.end:
        raise HTTPException(400, "Pick the latest finish time.")
    if p.kind == "preferred_venue":
        if not p.venue_id:
            raise HTTPException(400, "Pick a venue.")
        one(c, "SELECT id FROM venues WHERE id=? AND tournament_id=?", p.venue_id, team["tournament_id"])


@app.post("/api/teams/{team_id}/prefs")
def add_pref(team_id: int, body: PrefIn, c: Ctx = Depends(get_ctx)):
    team = _team_access(c, team_id)
    role = auth.tournament_role(c.conn, c.user, team["tournament_id"])
    if team["prefs_locked"] and role not in auth.ADMIN_ROLES:
        raise HTTPException(423, "Preferences are locked for this tournament.")
    _validate_pref(c, team, body)
    count = c.conn.execute("SELECT COUNT(*) n FROM preferences WHERE team_id=?", (team_id,)).fetchone()["n"]
    if count >= 20:
        raise HTTPException(400, "A team can have at most 20 preferences.")
    cur = c.conn.execute(
        'INSERT INTO preferences(team_id,kind,day,start,"end",venue_id,hard,note,source) VALUES (?,?,?,?,?,?,?,?,?)',
        (team_id, body.kind, body.day or None, body.start or None, body.end or None, body.venue_id,
         int(body.hard), body.note, "ai" if body.source == "ai" else "manual"))
    c.conn.commit()
    return {"id": cur.lastrowid}


@app.delete("/api/prefs/{pid}")
def delete_pref(pid: int, c: Ctx = Depends(get_ctx)):
    p = one(c, "SELECT team_id FROM preferences WHERE id=?", pid)
    team = _team_access(c, p["team_id"])
    role = auth.tournament_role(c.conn, c.user, team["tournament_id"])
    if team["prefs_locked"] and role not in auth.ADMIN_ROLES:
        raise HTTPException(423, "Preferences are locked for this tournament.")
    c.conn.execute("DELETE FROM preferences WHERE id=?", (pid,))
    c.conn.commit()
    return {"ok": True}


class ParseIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


@app.post("/api/teams/{team_id}/prefs/parse")
def parse_prefs(team_id: int, body: ParseIn, c: Ctx = Depends(get_ctx)):
    team = _team_access(c, team_id)
    t = one(c, "SELECT * FROM tournaments WHERE id=?", team["tournament_id"])
    venues = db.rows(c.conn.execute("SELECT id,name FROM venues WHERE tournament_id=?", (t["id"],)))
    c.conn.execute("UPDATE teams SET pref_notes=? WHERE id=?", (body.text, team_id))
    c.conn.commit()
    return ai.parse_preferences(body.text, t, venues)


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------

def load_ctx(c: Ctx, tid: int):
    t = one(c, "SELECT * FROM tournaments WHERE id=?", tid)
    s = engine.Settings(**{k: t[k] for k in (
        "start_date", "end_date", "game_minutes", "buffer_minutes", "min_rest_minutes", "travel_buffer_minutes",
        "target_utilization", "max_games_per_day", "day_start", "day_end")})
    venues = [engine.Venue(r["id"], r["name"], r["open_time"], r["close_time"])
              for r in c.conn.execute("SELECT * FROM venues WHERE tournament_id=?", (tid,))]
    courts = [engine.Court(r["id"], r["venue_id"], r["name"]) for r in c.conn.execute(
        "SELECT c.* FROM courts c JOIN venues v ON v.id=c.venue_id WHERE v.tournament_id=?", (tid,))]
    divs = [engine.Division(r["id"], r["name"], r["pool_size"], r["home_venue_id"])
            for r in c.conn.execute("SELECT * FROM divisions WHERE tournament_id=?", (tid,))]
    teams = [engine.Team(r["id"], r["name"], r["division_id"], r["coach_id"], r["rating"], r["club"] or "")
             for r in c.conn.execute("SELECT * FROM teams WHERE tournament_id=?", (tid,))]
    s.coach_gaps = {r["id"]: r["min_gap_minutes"] for r in c.conn.execute(
        "SELECT id,min_gap_minutes FROM coaches WHERE tournament_id=? AND min_gap_minutes IS NOT NULL", (tid,))}
    prefs = [engine.Pref(r["team_id"], r["kind"], r["day"], r["start"], r["end"], r["venue_id"], bool(r["hard"]))
             for r in c.conn.execute("SELECT p.* FROM preferences p JOIN teams t ON t.id=p.team_id "
                                     "WHERE t.tournament_id=?", (tid,))]
    crews = [engine.Crew(r["id"], r["name"], r["venue_id"])
             for r in c.conn.execute("SELECT * FROM ref_crews WHERE tournament_id=?", (tid,))]
    return t, s, venues, courts, divs, teams, prefs, crews


def _game_dicts(s, rows, courts):
    out = []
    for g in rows:
        if not g["start_at"] or g["court_id"] not in courts:
            continue
        out.append(dict(key=g["key"], id=g["id"], team_a=g["team_a"], team_b=g["team_b"],
                        start=s.iso_to_abs(g["start_at"]), end=s.iso_to_abs(g["end_at"]),
                        court_id=g["court_id"], venue_id=courts[g["court_id"]].venue_id,
                        division_id=g["division_id"], ref_crew_id=g["ref_crew_id"],
                        allow=[a for a in (g.get("allow") or "").split(",") if a]))
    return out


def current_issues(c: Ctx, tid: int):
    t, s, venues, courts, divs, teams, prefs, crews = load_ctx(c, tid)
    sch = engine.Scheduler(s, venues, courts, divs, teams, prefs, crews, games=[], coach_gaps=s.coach_gaps)
    rows = db.rows(c.conn.execute("SELECT * FROM games WHERE tournament_id=?", (tid,)))
    gd = _game_dicts(s, rows, sch.courts)
    issues = engine.validate(s, sch.venues, sch.courts, sch.teams, sch.divisions, gd, sch.hard_windows,
                             sch.soft_prefs, {d.id: d.home_venue_id for d in divs if d.home_venue_id} or sch.home,
                             sch.coach_gaps, deps=brackets.deps_from_rows(rows))
    return issues, rows, s


def _store_flags(c, tid, issues):
    flags = {}
    for i in issues:
        for k in i["games"]:
            flags.setdefault(k, []).append(i["severity"] + ":" + i["type"])
    c.conn.execute("UPDATE games SET flags='' WHERE tournament_id=?", (tid,))
    for k, f in flags.items():
        c.conn.execute("UPDATE games SET flags=? WHERE tournament_id=? AND key=?", (",".join(sorted(set(f))), tid, k))


class GenerateIn(BaseModel):
    time_limit: float = Field(6.0, ge=1, le=60)
    seed: int = 7


@app.post("/api/t/{tid}/schedule/generate")
def generate(tid: int, body: GenerateIn, c: Ctx = Depends(get_ctx)):
    auth.require_role(c.conn, c.user, tid, auth.ADMIN_ROLES)
    t, s, venues, courts, divs, teams, prefs, crews = load_ctx(c, tid)
    if not courts:
        raise HTTPException(400, "Add at least one venue with courts first.")
    if len(teams) < 2:
        raise HTTPException(400, "Add teams first.")
    if c.conn.execute("SELECT 1 FROM games WHERE tournament_id=? AND status IN ('final','in_progress')",
                      (tid,)).fetchone():
        raise HTTPException(409, "Games are already underway. Use 'Repair conflicts' instead of regenerating.")
    games = engine.generate_games(divs, teams)
    src = {}
    for b in db.rows(c.conn.execute("SELECT * FROM brackets WHERE tournament_id=?", (tid,))):
        pool_keys = [g.key for g in games if g.division_id == b["division_id"]]
        games += brackets.game_specs_for_engine(b, pool_keys)
        for sp in brackets.structure(b["id"], b["size"], bool(b["third_place"])):
            src[sp["key"]] = (b["id"], sp["src_a"], sp["src_b"])
    sch = engine.Scheduler(s, venues, courts, divs, teams, prefs, crews, games=games, seed=body.seed,
                           coach_gaps=s.coach_gaps)
    res = sch.run(body.time_limit)
    c.conn.execute("DELETE FROM games WHERE tournament_id=?", (tid,))
    for g in sch.games:
        p = res.placements.get(g.key)
        bid, sa, sb = src.get(g.key, (None, None, None))
        if p:
            st, en = s.abs_to_iso(p.start), s.abs_to_iso(p.end)
            c.conn.execute("INSERT INTO games(tournament_id,key,division_id,pool,round,team_a,team_b,court_id,start_at,"
                           "end_at,original_start_at,ref_crew_id,status,bracket_id,src_a,src_b) "
                           "VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'scheduled',?,?,?)",
                           (tid, g.key, g.division_id, g.pool, g.round, g.team_a, g.team_b, p.court_id, st, en, st,
                            p.ref_crew_id, bid, sa, sb))
        else:
            c.conn.execute("INSERT INTO games(tournament_id,key,division_id,pool,round,team_a,team_b,status,"
                           "bracket_id,src_a,src_b) VALUES (?,?,?,?,?,?,?, 'unscheduled',?,?,?)",
                           (tid, g.key, g.division_id, g.pool, g.round, g.team_a, g.team_b, bid, sa, sb))
    brackets.resolve(c.conn, tid)
    _store_flags(c, tid, res.issues)
    vnames = {v.id: v.name for v in venues}
    stats = dict(res.stats)
    stats["venue_utilization"] = {vnames.get(k, k): v for k, v in stats["venue_utilization"].items()}
    stats["home_venues"] = {d.name: vnames.get(res.home_venues.get(d.id)) for d in divs}
    briefing = ai.schedule_briefing(t, res.stats, res.issues, vnames)
    stats["briefing"] = briefing
    c.conn.execute("INSERT INTO schedule_runs(tournament_id,kind,created_by,stats,issues) VALUES (?,?,?,?,?)",
                   (tid, "generate", c.user["id"], json.dumps(stats), json.dumps(res.issues)))
    audit(c, tid, "schedule.generate", json.dumps(res.stats))
    c.conn.commit()
    broker.publish(tid, {"type": "games_changed"})
    return {"stats": stats, "issues": res.issues, "unscheduled": res.unscheduled}


@app.get("/api/t/{tid}/report")
def report(tid: int, c: Ctx = Depends(get_ctx)):
    auth.require_role(c.conn, c.user, tid, auth.STAFF_ROLES)
    issues, rows, s = current_issues(c, tid)
    last = c.conn.execute("SELECT stats FROM schedule_runs WHERE tournament_id=? ORDER BY id DESC LIMIT 1",
                          (tid,)).fetchone()
    return {"issues": issues, "unscheduled": sum(1 for r in rows if r["status"] == "unscheduled"),
            "last_run": json.loads(last["stats"]) if last else None}


def _games_payload(c, tid, public=False):
    sql = ("SELECT g.id,g.key,g.division_id,g.pool,g.round,g.team_a,g.team_b,g.court_id,g.start_at,g.end_at,"
           "g.original_start_at,g.status,g.score_a,g.score_b,g.ref_crew_id,g.flags,g.bracket_id,g.src_a,g.src_b,"
           "g.locked,g.allow,ta.name team_a_name, tb.name team_b_name, d.name division, c.name court, v.name venue, "
           "v.id venue_id, r.name ref_crew FROM games g LEFT JOIN teams ta ON ta.id=g.team_a "
           "LEFT JOIN teams tb ON tb.id=g.team_b "
           "JOIN divisions d ON d.id=g.division_id LEFT JOIN courts c ON c.id=g.court_id "
           "LEFT JOIN venues v ON v.id=c.venue_id LEFT JOIN ref_crews r ON r.id=g.ref_crew_id "
           "WHERE g.tournament_id=? ORDER BY g.start_at IS NULL, g.start_at, v.id, c.id")
    games = db.rows(c.conn.execute(sql, (tid,)))
    lab = brackets.labels(c.conn, tid) if any(g["bracket_id"] for g in games) else {}
    for g in games:
        L = lab.get(g["key"])
        if L:
            g["bracket"], g["round_name"], g["short"] = L["bracket"], L["round_name"], L["short"]
            g["team_a_name"] = g["team_a_name"] or L["label_a"]
            g["team_b_name"] = g["team_b_name"] or L["label_b"]
        if public:
            for k in ("flags", "ref_crew", "ref_crew_id", "locked", "allow"):
                g.pop(k, None)
    return games


@app.get("/api/t/{tid}/games")
def list_games(tid: int, c: Ctx = Depends(get_ctx)):
    role = auth.require_role(c.conn, c.user, tid, auth.STAFF_ROLES | {"team"})
    return {"games": _games_payload(c, tid, public=role == "team")}


@app.get("/api/t/{tid}/standings")
def get_standings(tid: int, c: Ctx = Depends(get_ctx)):
    auth.require_role(c.conn, c.user, tid, auth.STAFF_ROLES | {"team"})
    return {"standings": engine.standings(_games_payload(c, tid, public=True))}


@app.get("/api/t/{tid}/export.csv")
def export_csv(tid: int, c: Ctx = Depends(get_ctx)):
    auth.require_role(c.conn, c.user, tid, auth.STAFF_ROLES)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["start", "end", "venue", "court", "division", "pool", "team_a", "team_b", "ref_crew", "status",
                "score_a", "score_b", "flags"])
    for g in _games_payload(c, tid):
        w.writerow([g["start_at"], g["end_at"], g["venue"], g["court"], g["division"], g["pool"], g["team_a_name"],
                    g["team_b_name"], g["ref_crew"], g["status"], g["score_a"], g["score_b"], g["flags"]])
    return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="schedule-{tid}.csv"'})


def _director_check(c, tid, venue_id):
    role = auth.require_role(c.conn, c.user, tid, auth.STAFF_ROLES)
    if role == "director":
        m = c.conn.execute("SELECT venue_id FROM tournament_members WHERE tournament_id=? AND user_id=?",
                           (tid, c.user["id"])).fetchone()
        if m["venue_id"] and m["venue_id"] != venue_id:
            raise HTTPException(403, "Site directors can only manage their own venue.")
    return role


def _notify_changes(c, tid, before: dict, s):
    """before: game id -> (start_at, court_id). Creates team notifications for changed games."""
    msgs = []
    for g in _games_payload(c, tid):
        old = before.get(g["id"])
        if not old or (old[0], old[1]) == (g["start_at"], g["court_id"]):
            continue
        when = fmt_when(g["start_at"])
        m = f"{g['team_a_name']} vs {g['team_b_name']} now starts {when} at {g['venue']} {g['court']}"
        ids = [x for x in (g["team_a"], g["team_b"]) if x is not None]
        for team in ids:
            c.conn.execute("INSERT INTO notifications(tournament_id,team_id,message) VALUES (?,?,?)", (tid, team, m))
        if ids:
            msgs.append({"team_ids": ids, "message": m})
    return msgs


def _advance(c, tid):
    """Fill bracket slots after results change; tell teams who they play next. Returns broker events."""
    changed, alerts = brackets.resolve(c.conn, tid)
    events = [{"type": "staff_alert", "message": f"Bracket tie: {a}"} for a in alerts]
    for a in alerts:
        audit(c, tid, "bracket.tie", a)
    if not changed:
        return events
    for g in _games_payload(c, tid):
        if g["id"] not in changed:
            continue
        when = f"{fmt_when(g['start_at'])} at {g['venue']} {g['court']}" if g["start_at"] else "time TBD"
        for me, other, opp in ((g["team_a"], g["team_b"], g["team_b_name"]), (g["team_b"], g["team_a"], g["team_a_name"])):
            if me is None:
                continue
            rnd = g["round_name"].lower()
            m = (f"{g['bracket']}: you play {opp} in the {rnd}, {when}" if other is not None else
                 f"{g['bracket']}: you're in the {rnd}, {when}. Opponent: {opp}")
            c.conn.execute("INSERT INTO notifications(tournament_id,team_id,message) VALUES (?,?,?)", (tid, me, m))
            events.append({"type": "notification", "team_ids": [me], "message": m})
    return events


def fmt_when(iso):
    if not iso:
        return "TBD"
    d = datetime.fromisoformat(iso)
    return f"{d.strftime('%a')} {d.month}/{d.day} {d.hour % 12 or 12}:{d.minute:02d} {'PM' if d.hour >= 12 else 'AM'}"


class DelayIn(BaseModel):
    minutes: int = Field(ge=-120, le=240)
    from_at: Optional[str] = None  # ISO local time; default = first unfinished game on that court


@app.post("/api/courts/{cid}/delay")
def court_delay(cid: int, body: DelayIn, c: Ctx = Depends(get_ctx)):
    court = one(c, "SELECT c.*, v.tournament_id FROM courts c JOIN venues v ON v.id=c.venue_id WHERE c.id=?", cid)
    tid = court["tournament_id"]
    _director_check(c, tid, court["venue_id"])
    t = one(c, "SELECT * FROM tournaments WHERE id=?", tid)
    rows = db.rows(c.conn.execute(
        "SELECT * FROM games WHERE court_id=? AND status IN ('scheduled','in_progress') AND start_at IS NOT NULL "
        "ORDER BY start_at", (cid,)))
    if body.from_at:
        rows = [r for r in rows if r["start_at"] >= body.from_at]
    if not rows:
        raise HTTPException(400, "No upcoming games on that court.")
    s = engine.Settings(start_date=t["start_date"], end_date=t["end_date"], game_minutes=t["game_minutes"])
    day = rows[0]["start_at"][:10]
    rows = [r for r in rows if r["start_at"][:10] == day]
    before = {r["id"]: (r["start_at"], r["court_id"]) for r in rows}
    turnover = 5
    prev_end = None
    for i, r in enumerate(rows):
        st, en = s.iso_to_abs(r["start_at"]), s.iso_to_abs(r["end_at"])
        dur = en - st
        orig = s.iso_to_abs(r["original_start_at"] or r["start_at"])
        if body.minutes >= 0:
            # running late: push the first game, then cascade only as far as buffers/flex gaps can't absorb
            new_st = st + body.minutes if i == 0 else max(st, prev_end + turnover)
        else:
            # catching up: pull games back toward (never before) their published time
            new_st = max(orig, st + body.minutes) if i == 0 else max(orig, min(st, prev_end + turnover))
        prev_end = new_st + dur
        c.conn.execute("UPDATE games SET start_at=?, end_at=? WHERE id=?",
                       (s.abs_to_iso(new_st), s.abs_to_iso(new_st + dur), r["id"]))
    msgs = _notify_changes(c, tid, before, s)
    issues, _, _ = current_issues(c, tid)
    _store_flags(c, tid, issues)
    audit(c, tid, "court.delay", f"court {cid} {body.minutes} min")
    c.conn.commit()
    hard_new = [i for i in issues if i["severity"] == "hard"]
    broker.publish(tid, {"type": "games_changed"})
    for m in msgs:
        broker.publish(tid, {"type": "notification", **m})
    return {"moved": len(msgs), "hard_issues": hard_new}


RULE_TYPES = {"rest", "team_travel", "coach_conflict", "too_many_games", "unavailable", "bracket_order"}


class GameIn(BaseModel):
    status: Optional[str] = None
    score_a: Optional[int] = Field(None, ge=0, le=300)
    score_b: Optional[int] = Field(None, ge=0, le=300)
    court_id: Optional[int] = None
    start_at: Optional[str] = None
    locked: Optional[bool] = None
    allow: Optional[list[str]] = None  # rule types waived for this game ("back-to-back on purpose")


def _settings(t):
    return engine.Settings(start_date=t["start_date"], end_date=t["end_date"], game_minutes=t["game_minutes"],
                           buffer_minutes=t["buffer_minutes"], min_rest_minutes=t["min_rest_minutes"],
                           travel_buffer_minutes=t["travel_buffer_minutes"], max_games_per_day=t["max_games_per_day"],
                           day_start=t["day_start"], day_end=t["day_end"])


@app.patch("/api/games/{gid}")
def update_game(gid: int, body: GameIn, c: Ctx = Depends(get_ctx)):
    g = one(c, "SELECT g.*, c.venue_id FROM games g LEFT JOIN courts c ON c.id=g.court_id WHERE g.id=?", gid)
    tid = g["tournament_id"]
    role = _director_check(c, tid, g["venue_id"])
    d = body.model_dump(exclude_none=True)
    if "status" in d and d["status"] not in ("scheduled", "in_progress", "final", "unscheduled"):
        raise HTTPException(400, "Bad status.")
    if role == "director" and ({"locked", "allow"} & set(d) or d.get("status") == "unscheduled"):
        raise HTTPException(403, "Only admins can change the schedule.")
    if g["bracket_id"] and ("score_a" in d or "score_b" in d or d.get("status") in ("in_progress", "final")):
        if g["team_a"] is None or g["team_b"] is None:
            raise HTTPException(400, "Both teams in this bracket game aren't known yet.")
        if d.get("status", g["status"]) == "final" and d.get("score_a", g["score_a"]) == d.get("score_b", g["score_b"]):
            raise HTTPException(400, "Bracket games need a winner. Enter the final score after overtime.")
    if "allow" in d:
        bad = set(d["allow"]) - RULE_TYPES
        if bad:
            raise HTTPException(400, f"Unknown rule: {', '.join(sorted(bad))}")
        d["allow"] = ",".join(sorted(set(d["allow"])))
    if "locked" in d:
        d["locked"] = int(d["locked"])
    moved = "court_id" in d or "start_at" in d
    if d.get("status") == "unscheduled":
        if g["status"] in ("final", "in_progress"):
            raise HTTPException(400, "That game has started.")
        d.update(court_id=None, start_at=None, end_at=None, locked=0)
        moved = False
    elif moved:
        if role == "director":
            raise HTTPException(403, "Only admins can move games between slots.")
        t = one(c, "SELECT * FROM tournaments WHERE id=?", tid)
        s = _settings(t)
        if "court_id" in d:
            one(c, "SELECT c.id FROM courts c JOIN venues v ON v.id=c.venue_id WHERE c.id=? AND v.tournament_id=?",
                d["court_id"], tid)
        elif not g["court_id"]:
            raise HTTPException(400, "Pick a court.")
        start = d.get("start_at") or g["start_at"]
        try:
            st = s.iso_to_abs(start)
        except Exception:
            raise HTTPException(400, "start_at must be YYYY-MM-DDTHH:MM")
        if not (0 <= st < s.num_days * engine.DAY):
            raise HTTPException(400, "That time is outside the tournament dates.")
        d["start_at"] = s.abs_to_iso(st)
        d["end_at"] = s.abs_to_iso(st + t["game_minutes"])
        court = d.get("court_id", g["court_id"])
        clash = c.conn.execute(
            "SELECT id FROM games WHERE court_id=? AND id<>? AND start_at IS NOT NULL AND start_at<? AND end_at>?",
            (court, gid, d["end_at"], d["start_at"])).fetchone()
        if clash:
            raise HTTPException(409, "Another game is already on that court at that time.")
        d.setdefault("locked", 1)  # a game the director placed by hand stays put when re-optimizing
        if g["status"] == "unscheduled":
            d.setdefault("status", "scheduled")
            d["original_start_at"] = d["start_at"]
    before = {gid: (g["start_at"], g["court_id"])}
    if d:
        c.conn.execute(f"UPDATE games SET {','.join(k + '=?' for k in d)} WHERE id=?", [*d.values(), gid])
    msgs = _notify_changes(c, tid, before, None) if moved else []
    events = _advance(c, tid) if {"score_a", "score_b", "status"} & set(d) else []
    issues, _, _ = current_issues(c, tid)
    _store_flags(c, tid, issues)
    c.conn.commit()
    broker.publish(tid, {"type": "games_changed"})
    for m in msgs:
        broker.publish(tid, {"type": "notification", **m})
    for e in events:
        broker.publish(tid, e)
    mine = [i for i in issues if g["key"] in i["games"]]
    return {"ok": True, "issues": mine}


@app.get("/api/games/{gid}/check")
def check_slots(gid: int, day: str, c: Ctx = Depends(get_ctx)):
    """For drag and drop: which times on `day` (at each venue) break a rule for this game, and why.

    Court occupancy is left to the client, which already knows which cells hold a game."""
    g = one(c, "SELECT * FROM games WHERE id=?", gid)
    tid = g["tournament_id"]
    auth.require_role(c.conn, c.user, tid, auth.ADMIN_ROLES)
    t, s, venues, courts, divs, teams, prefs, crews = load_ctx(c, tid)
    try:
        d0 = s.day_index(day)
    except Exception:
        raise HTTPException(400, "day must be YYYY-MM-DD")
    sch = engine.Scheduler(s, venues, courts, divs, teams, prefs, crews, games=[], coach_gaps=s.coach_gaps)
    rows = db.rows(c.conn.execute("SELECT * FROM games WHERE tournament_id=?", (tid,)))
    deps = brackets.deps_from_rows(rows)
    others = [x for x in _game_dicts(s, rows, sch.courts) if x["key"] != g["key"]]
    # Only games that share a team, a coach or a bracket link with this one can conflict with it.
    mine = {g["team_a"], g["team_b"]} - {None}
    coaches = {sch.teams[x].coach_id for x in mine if x in sch.teams} - {None}
    mates = mine | {x for x in sch.teams if sch.teams[x].coach_id in coaches}
    linked = set(deps.get(g["key"], ())) | {k for k, v in deps.items() if g["key"] in v}
    near = [x for x in others if {x["team_a"], x["team_b"]} & mates or x["key"] in linked]
    dep_sub = {k: v for k, v in deps.items() if k == g["key"] or g["key"] in v}
    home = {dv.id: dv.home_venue_id for dv in divs if dv.home_venue_id} or sch.home
    allow = [a for a in (g["allow"] or "").split(",") if a]
    cells = {}
    for v in venues:
        open_m = engine.hm_to_min(v.open_time or s.day_start)
        close_m = engine.hm_to_min(v.close_time or s.day_end)
        m = open_m
        while m + s.game_minutes <= close_m:
            st = d0 * engine.DAY + m
            cand = dict(key=g["key"], id=g["id"], team_a=g["team_a"], team_b=g["team_b"], start=st,
                        end=st + s.game_minutes, court_id=None, venue_id=v.id, division_id=g["division_id"],
                        ref_crew_id=None, allow=allow)
            iss = [i for i in engine.validate(s, sch.venues, sch.courts, sch.teams, sch.divisions, near + [cand],
                                              sch.hard_windows, sch.soft_prefs, home, sch.coach_gaps, deps=dep_sub)
                   if g["key"] in i["games"] and i["type"] != "court_overlap"]
            hard = [i["message"] for i in iss if i["severity"] == "hard"]
            soft = [i["message"] for i in iss if i["severity"] == "soft"]
            cells[f"{v.id}|{engine.min_to_hm(m)}"] = dict(level="hard" if hard else "soft" if soft else "ok",
                                                          reasons=hard + soft)
            m += s.slot_minutes
    return {"day": day, "cells": cells}


def _replan(c, tid, move_keys, from_abs, time_limit, kind):
    """Re-place `move_keys` with every other game held where it is."""
    issues, rows, s = current_issues(c, tid)
    t, s, venues, courts, divs, teams, prefs, crews = load_ctx(c, tid)
    cmap = {x.id for x in courts}
    deps = brackets.deps_from_rows(rows)
    movable, fixed = [], []
    for r in rows:
        after = tuple(deps.get(r["key"], ()))
        if r["key"] in move_keys:
            movable.append(engine.Game(r["key"], r["division_id"], r["pool"] or "", r["round"] or 0,
                                       r["team_a"], r["team_b"], after=after, phase=1 if r["bracket_id"] else 0))
        elif r["start_at"] and r["court_id"] in cmap:
            fixed.append(engine.Fixed(r["key"], r["team_a"], r["team_b"], r["court_id"],
                                      s.iso_to_abs(r["start_at"]), s.iso_to_abs(r["end_at"]), after=after))
    if not movable:
        return None
    sch = engine.Scheduler(s, venues, courts, divs, teams, prefs, crews, fixed=fixed, games=movable,
                           not_before=from_abs, coach_gaps=s.coach_gaps)
    res = sch.run(time_limit)
    before = {r["id"]: (r["start_at"], r["court_id"]) for r in rows}
    for g in movable:
        p = res.placements.get(g.key)
        if p:
            c.conn.execute("UPDATE games SET court_id=?, start_at=?, end_at=?, status='scheduled', ref_crew_id=?, "
                           "original_start_at=COALESCE(original_start_at, ?) WHERE tournament_id=? AND key=?",
                           (p.court_id, s.abs_to_iso(p.start), s.abs_to_iso(p.end), p.ref_crew_id,
                            s.abs_to_iso(p.start), tid, g.key))
        else:
            c.conn.execute("UPDATE games SET court_id=NULL, start_at=NULL, end_at=NULL, status='unscheduled' "
                           "WHERE tournament_id=? AND key=?", (tid, g.key))
    msgs = _notify_changes(c, tid, before, s)
    issues, _, _ = current_issues(c, tid)
    _store_flags(c, tid, issues)
    remaining = [i for i in issues if i["severity"] == "hard"]
    c.conn.execute("INSERT INTO schedule_runs(tournament_id,kind,created_by,stats,issues) VALUES (?,?,?,?,?)",
                   (tid, kind, c.user["id"], json.dumps(res.stats), json.dumps(issues)))
    audit(c, tid, f"schedule.{kind}", f"moved {len(msgs)}")
    c.conn.commit()
    broker.publish(tid, {"type": "games_changed"})
    for m in msgs:
        broker.publish(tid, {"type": "notification", **m})
    return {"moved": len(msgs), "unplaced": res.unscheduled, "remaining_hard": len(remaining)}


class RepairIn(BaseModel):
    from_at: Optional[str] = None
    time_limit: float = Field(4.0, ge=1, le=30)


@app.post("/api/t/{tid}/schedule/repair")
def repair(tid: int, body: RepairIn, c: Ctx = Depends(get_ctx)):
    """Re-place only conflicted/unscheduled games after `from_at`, keeping everything else locked."""
    auth.require_role(c.conn, c.user, tid, auth.ADMIN_ROLES)
    issues, rows, s = current_issues(c, tid)
    from_abs = s.iso_to_abs(body.from_at) if body.from_at else 0
    # For each hard issue only move the later game(s) so earlier games stay put. Locked games never move.
    move_keys = {r["key"] for r in rows if r["status"] == "unscheduled"}
    for i in issues:
        if i["severity"] != "hard":
            continue
        gs = sorted((r for r in rows if r["key"] in i["games"] and r["start_at"]), key=lambda r: r["start_at"])
        cands = [r for r in gs if r["status"] == "scheduled" and not r["locked"]
                 and s.iso_to_abs(r["start_at"]) >= from_abs]
        if cands:
            move_keys.add(cands[-1]["key"])
    out = _replan(c, tid, move_keys, from_abs, body.time_limit, "repair")
    if out is None:
        return {"moved": 0, "remaining_hard": len([i for i in issues if i["severity"] == "hard"]),
                "message": "Nothing to repair."}
    return out


class OptimizeIn(BaseModel):
    time_limit: float = Field(6.0, ge=1, le=60)


@app.post("/api/t/{tid}/schedule/optimize")
def optimize(tid: int, body: OptimizeIn, c: Ctx = Depends(get_ctx)):
    """Re-plan every game that isn't locked, started or finished. Matchups stay the same."""
    auth.require_role(c.conn, c.user, tid, auth.ADMIN_ROLES)
    rows = db.rows(c.conn.execute("SELECT key,status,locked FROM games WHERE tournament_id=?", (tid,)))
    keys = {r["key"] for r in rows if not r["locked"] and r["status"] in ("scheduled", "unscheduled")}
    out = _replan(c, tid, keys, None, body.time_limit, "optimize")
    if out is None:
        return {"moved": 0, "remaining_hard": 0, "message": "Every game is locked or underway."}
    out["locked"] = sum(1 for r in rows if r["locked"])
    return out


# ---------------------------------------------------------------------------
# Brackets
# ---------------------------------------------------------------------------

class BracketIn(BaseModel):
    division_id: int
    name: str = Field("Championship", min_length=1, max_length=60)
    size: int = Field(ge=2, le=brackets.MAX_SIZE)
    seeding: str = "pool"
    third_place: bool = False


def _bracket_view(c, b, games_by_key):
    specs = brackets.structure(b["id"], b["size"], bool(b["third_place"]))
    by_key = {s["key"]: s for s in specs}
    seeds = json.loads(b["seeds"]) if b["seeds"] else None
    names = {r["id"]: r["name"] for r in c.conn.execute("SELECT id,name FROM teams WHERE division_id=?",
                                                        (b["division_id"],))}
    out = dict(b)
    out.pop("seeds")
    out["manual_seeds"] = seeds
    resolved = {}
    games = []
    for sp in specs:
        g = games_by_key.get(sp["key"], {})
        games.append(dict(key=sp["key"], id=g.get("id"), round=sp["round"], slot=sp["slot"], name=sp["name"],
                          short=sp["short"], src_a=sp["src_a"], src_b=sp["src_b"],
                          label_a=brackets.source_label(sp["src_a"], by_key),
                          label_b=brackets.source_label(sp["src_b"], by_key),
                          team_a=g.get("team_a"), team_b=g.get("team_b"),
                          team_a_name=names.get(g.get("team_a")), team_b_name=names.get(g.get("team_b")),
                          start_at=g.get("start_at"), venue=g.get("venue"), court=g.get("court"),
                          status=g.get("status"), score_a=g.get("score_a"), score_b=g.get("score_b")))
        for src, t in ((sp["src_a"], g.get("team_a")), (sp["src_b"], g.get("team_b"))):
            if src.startswith("S") and t:
                resolved[int(src[1:])] = t
    out["games"] = games
    out["rounds"] = max((sp["round"] for sp in specs), default=0)
    out["seed_list"] = [dict(seed=n, team_id=(seeds[n - 1] if seeds else resolved.get(n)),
                             team=names.get(seeds[n - 1] if seeds else resolved.get(n)))
                        for n in range(1, b["size"] + 1)]
    return out


def _brackets_payload(c, tid, public=False):
    games = {g["key"]: g for g in _games_payload(c, tid, public=public)}
    return [_bracket_view(c, dict(b), games) for b in c.conn.execute(
        "SELECT b.*, d.name division FROM brackets b JOIN divisions d ON d.id=b.division_id "
        "WHERE b.tournament_id=? ORDER BY d.name, b.id", (tid,))]


@app.get("/api/t/{tid}/brackets")
def list_brackets(tid: int, c: Ctx = Depends(get_ctx)):
    role = auth.require_role(c.conn, c.user, tid, auth.STAFF_ROLES | {"team"})
    return {"brackets": _brackets_payload(c, tid, public=role == "team")}


@app.post("/api/t/{tid}/brackets")
def create_bracket(tid: int, body: BracketIn, c: Ctx = Depends(get_ctx)):
    auth.require_role(c.conn, c.user, tid, auth.ADMIN_ROLES)
    div = one(c, "SELECT * FROM divisions WHERE id=? AND tournament_id=?", body.division_id, tid)
    if body.seeding not in ("pool", "division"):
        raise HTTPException(400, "Seeding must be 'pool' or 'division'.")
    n = c.conn.execute("SELECT COUNT(*) n FROM teams WHERE division_id=?", (div["id"],)).fetchone()["n"]
    if body.size > n:
        raise HTTPException(400, f"{div['name']} only has {n} teams.")
    if c.conn.execute("SELECT 1 FROM brackets WHERE division_id=? AND name=?", (div["id"], body.name)).fetchone():
        raise HTTPException(400, f"{div['name']} already has a bracket called {body.name}.")
    bid = c.conn.execute("INSERT INTO brackets(tournament_id,division_id,name,size,seeding,third_place) "
                         "VALUES (?,?,?,?,?,?)", (tid, div["id"], body.name, body.size, body.seeding,
                                                  int(body.third_place))).lastrowid
    for sp in brackets.structure(bid, body.size, body.third_place):
        c.conn.execute("INSERT INTO games(tournament_id,key,division_id,round,status,bracket_id,src_a,src_b) "
                       "VALUES (?,?,?,?, 'unscheduled',?,?,?)",
                       (tid, sp["key"], div["id"], sp["round"], bid, sp["src_a"], sp["src_b"]))
    audit(c, tid, "bracket.create", f"{div['name']} {body.name} ({body.size})")
    c.conn.commit()
    placed = None
    has_pool = c.conn.execute("SELECT 1 FROM games WHERE division_id=? AND bracket_id IS NULL AND start_at IS NOT NULL",
                              (div["id"],)).fetchone()
    if has_pool:
        keys = {r["key"] for r in c.conn.execute("SELECT key FROM games WHERE bracket_id=?", (bid,))}
        placed = _replan(c, tid, keys, None, 4.0, "bracket")
    events = _advance(c, tid)
    c.conn.commit()
    for e in events:
        broker.publish(tid, e)
    broker.publish(tid, {"type": "games_changed"})
    return {"id": bid, "placed": placed}


class SeedsIn(BaseModel):
    team_ids: Optional[list[int]] = None  # None = back to automatic seeding


@app.put("/api/brackets/{bid}/seeds")
def set_seeds(bid: int, body: SeedsIn, c: Ctx = Depends(get_ctx)):
    b = one(c, "SELECT * FROM brackets WHERE id=?", bid)
    tid = b["tournament_id"]
    auth.require_role(c.conn, c.user, tid, auth.ADMIN_ROLES)
    if c.conn.execute("SELECT 1 FROM games WHERE bracket_id=? AND status IN ('final','in_progress')",
                      (bid,)).fetchone():
        raise HTTPException(409, "Bracket play has started, so the seeds can't change.")
    if body.team_ids is not None:
        ids = body.team_ids
        valid = {r["id"] for r in c.conn.execute("SELECT id FROM teams WHERE division_id=?", (b["division_id"],))}
        if len(ids) != b["size"] or len(set(ids)) != len(ids) or not set(ids) <= valid:
            raise HTTPException(400, f"Pick {b['size']} different teams from this division.")
    c.conn.execute("UPDATE brackets SET seeds=? WHERE id=?", (json.dumps(body.team_ids) if body.team_ids else None, bid))
    audit(c, tid, "bracket.seeds", "manual" if body.team_ids else "auto")
    events = _advance(c, tid)
    issues, _, _ = current_issues(c, tid)
    _store_flags(c, tid, issues)
    c.conn.commit()
    for e in events:
        broker.publish(tid, e)
    broker.publish(tid, {"type": "games_changed"})
    return {"ok": True}


@app.delete("/api/brackets/{bid}")
def delete_bracket(bid: int, c: Ctx = Depends(get_ctx)):
    b = one(c, "SELECT * FROM brackets WHERE id=?", bid)
    tid = b["tournament_id"]
    auth.require_role(c.conn, c.user, tid, auth.ADMIN_ROLES)
    c.conn.execute("DELETE FROM brackets WHERE id=?", (bid,))
    audit(c, tid, "bracket.delete", b["name"])
    issues, _, _ = current_issues(c, tid)
    _store_flags(c, tid, issues)
    c.conn.commit()
    broker.publish(tid, {"type": "games_changed"})
    return {"ok": True}


class PublishIn(BaseModel):
    published: bool


@app.post("/api/t/{tid}/publish")
def publish(tid: int, body: PublishIn, c: Ctx = Depends(get_ctx)):
    auth.require_role(c.conn, c.user, tid, auth.ADMIN_ROLES)
    c.conn.execute("UPDATE tournaments SET published=? WHERE id=?", (int(body.published), tid))
    audit(c, tid, "schedule.publish" if body.published else "schedule.unpublish")
    c.conn.commit()
    broker.publish(tid, {"type": "games_changed"})
    return {"ok": True}


class NotifyIn(BaseModel):
    message: str = Field(min_length=1, max_length=500)
    team_id: Optional[int] = None


@app.post("/api/t/{tid}/notify")
def notify(tid: int, body: NotifyIn, c: Ctx = Depends(get_ctx)):
    auth.require_role(c.conn, c.user, tid, auth.STAFF_ROLES)
    if body.team_id:
        one(c, "SELECT id FROM teams WHERE id=? AND tournament_id=?", body.team_id, tid)
    c.conn.execute("INSERT INTO notifications(tournament_id,team_id,message) VALUES (?,?,?)",
                   (tid, body.team_id, body.message))
    c.conn.commit()
    broker.publish(tid, {"type": "notification", "team_ids": [body.team_id] if body.team_id else None,
                         "message": body.message})
    return {"ok": True}


@app.get("/api/t/{tid}/notifications")
def notifications(tid: int, c: Ctx = Depends(get_ctx)):
    role = auth.require_role(c.conn, c.user, tid, auth.STAFF_ROLES | {"team"})
    if role == "team":
        q = ("SELECT n.* FROM notifications n WHERE n.tournament_id=? AND (n.team_id IS NULL OR n.team_id IN "
             "(SELECT team_id FROM team_managers WHERE user_id=?)) ORDER BY n.id DESC LIMIT 100")
        return {"notifications": db.rows(c.conn.execute(q, (tid, c.user["id"])))}
    return {"notifications": db.rows(c.conn.execute(
        "SELECT * FROM notifications WHERE tournament_id=? ORDER BY id DESC LIMIT 100", (tid,)))}


async def _sse(tid: int, request: Request, team_filter=None):
    q = broker.subscribe(tid)
    try:
        yield "retry: 3000\n\n"
        while True:
            if await request.is_disconnected():
                break
            try:
                data = await asyncio.wait_for(q.get(), timeout=20)
            except asyncio.TimeoutError:
                yield ": ping\n\n"
                continue
            ev = json.loads(data)
            if team_filter is not None and ev.get("type") == "staff_alert":
                continue
            if team_filter is not None and ev.get("type") == "notification":
                ids = ev.get("team_ids")
                if ids and not set(ids) & team_filter:
                    continue
            yield f"data: {data}\n\n"
    finally:
        broker.unsubscribe(tid, q)


@app.get("/api/t/{tid}/stream")
async def stream(tid: int, request: Request):
    conn = db.connect()
    try:
        user, _ = auth.session_user(conn, request)
        role = auth.require_role(conn, user, tid, auth.STAFF_ROLES | {"team"})
        teams = None
        if role == "team":
            teams = {r["team_id"] for r in conn.execute("SELECT team_id FROM team_managers WHERE user_id=?",
                                                        (user["id"],))}
    finally:
        conn.close()
    return StreamingResponse(_sse(tid, request, teams), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------
# Public (published) schedule
# ---------------------------------------------------------------------------

def _public_t(c, slug):
    t = c.conn.execute("SELECT id,name,location,start_date,end_date,published FROM tournaments WHERE public_slug=?",
                       (slug,)).fetchone()
    if not t or not t["published"]:
        raise HTTPException(404, "Schedule not published.")
    return dict(t)


@app.get("/api/public/{slug}")
def public_schedule(slug: str, c: Ctx = Depends(get_ctx)):
    t = _public_t(c, slug)
    games = [g for g in _games_payload(c, t["id"], public=True) if g["status"] != "unscheduled"]
    notes = db.rows(c.conn.execute("SELECT message, created_at FROM notifications WHERE tournament_id=? AND "
                                   "team_id IS NULL ORDER BY id DESC LIMIT 10", (t["id"],)))
    return {"tournament": t, "games": games, "announcements": notes, "standings": engine.standings(games),
            "brackets": _brackets_payload(c, t["id"], public=True)}


@app.get("/api/public/{slug}/stream")
async def public_stream(slug: str, request: Request):
    conn = db.connect()
    try:
        t = conn.execute("SELECT id,published FROM tournaments WHERE public_slug=?", (slug,)).fetchone()
    finally:
        conn.close()
    if not t or not t["published"]:
        raise HTTPException(404, "Schedule not published.")
    return StreamingResponse(_sse(t["id"], request, team_filter=set()), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/t/{tid}/qr.svg")
def qr(tid: int, request: Request, c: Ctx = Depends(get_ctx)):
    auth.require_role(c.conn, c.user, tid, auth.STAFF_ROLES)
    t = one(c, "SELECT public_slug FROM tournaments WHERE id=?", tid)
    url = f"{request.base_url}#/p/{t['public_slug']}"
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="svg", scale=6, border=2)
    return Response(buf.getvalue(), media_type="image/svg+xml")


# ---------------------------------------------------------------------------
# Static UI
# ---------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))
