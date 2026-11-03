import os
import sqlite3
import threading

DB_PATH = os.environ.get("TOURNEY_DB", os.path.join(os.path.dirname(__file__), "..", "data", "tourney.db"))

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL COLLATE NOCASE, name TEXT NOT NULL,
  password_hash TEXT NOT NULL, is_superadmin INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  csrf TEXT NOT NULL, expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tournaments (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, location TEXT DEFAULT '',
  start_date TEXT NOT NULL, end_date TEXT NOT NULL,
  game_minutes INTEGER NOT NULL DEFAULT 50, buffer_minutes INTEGER NOT NULL DEFAULT 10,
  min_rest_minutes INTEGER NOT NULL DEFAULT 60, travel_buffer_minutes INTEGER NOT NULL DEFAULT 75,
  target_utilization REAL NOT NULL DEFAULT 0.85, max_games_per_day INTEGER NOT NULL DEFAULT 3,
  day_start TEXT NOT NULL DEFAULT '08:00', day_end TEXT NOT NULL DEFAULT '21:00',
  public_slug TEXT UNIQUE NOT NULL, published INTEGER NOT NULL DEFAULT 0,
  prefs_locked INTEGER NOT NULL DEFAULT 0,
  created_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS tournament_members (
  tournament_id INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role TEXT NOT NULL CHECK (role IN ('admin','director')),
  venue_id INTEGER REFERENCES venues(id) ON DELETE SET NULL,
  PRIMARY KEY (tournament_id, user_id)
);
CREATE TABLE IF NOT EXISTS venues (
  id INTEGER PRIMARY KEY, tournament_id INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
  name TEXT NOT NULL, address TEXT DEFAULT '', open_time TEXT, close_time TEXT,
  director_contact TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS courts (
  id INTEGER PRIMARY KEY, venue_id INTEGER NOT NULL REFERENCES venues(id) ON DELETE CASCADE,
  name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS divisions (
  id INTEGER PRIMARY KEY, tournament_id INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
  name TEXT NOT NULL, pool_size INTEGER NOT NULL DEFAULT 4,
  home_venue_id INTEGER REFERENCES venues(id) ON DELETE SET NULL,
  UNIQUE (tournament_id, name)
);
CREATE TABLE IF NOT EXISTS coaches (
  id INTEGER PRIMARY KEY, tournament_id INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
  name TEXT NOT NULL, email TEXT COLLATE NOCASE, phone TEXT DEFAULT '',
  min_gap_minutes INTEGER
);
CREATE TABLE IF NOT EXISTS teams (
  id INTEGER PRIMARY KEY, tournament_id INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
  division_id INTEGER NOT NULL REFERENCES divisions(id) ON DELETE CASCADE,
  name TEXT NOT NULL, club TEXT DEFAULT '',
  coach_id INTEGER REFERENCES coaches(id) ON DELETE SET NULL,
  invite_code TEXT UNIQUE NOT NULL, pref_notes TEXT DEFAULT '', rating REAL
);
CREATE TABLE IF NOT EXISTS team_managers (
  team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  PRIMARY KEY (team_id, user_id)
);
CREATE TABLE IF NOT EXISTS preferences (
  id INTEGER PRIMARY KEY, team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('unavailable','not_before','not_after','preferred_venue')),
  day TEXT, start TEXT, "end" TEXT, venue_id INTEGER REFERENCES venues(id) ON DELETE CASCADE,
  hard INTEGER NOT NULL DEFAULT 0, note TEXT DEFAULT '', source TEXT NOT NULL DEFAULT 'manual'
);
CREATE TABLE IF NOT EXISTS ref_crews (
  id INTEGER PRIMARY KEY, tournament_id INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
  name TEXT NOT NULL, venue_id INTEGER REFERENCES venues(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS games (
  id INTEGER PRIMARY KEY, tournament_id INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
  key TEXT NOT NULL, division_id INTEGER REFERENCES divisions(id) ON DELETE CASCADE,
  pool TEXT, round INTEGER, team_a INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
  team_b INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
  court_id INTEGER REFERENCES courts(id) ON DELETE SET NULL,
  start_at TEXT, end_at TEXT, original_start_at TEXT,
  ref_crew_id INTEGER REFERENCES ref_crews(id) ON DELETE SET NULL,
  status TEXT NOT NULL DEFAULT 'scheduled'
    CHECK (status IN ('scheduled','in_progress','final','unscheduled')),
  score_a INTEGER, score_b INTEGER, flags TEXT DEFAULT '',
  UNIQUE (tournament_id, key)
);
CREATE INDEX IF NOT EXISTS games_t ON games(tournament_id, start_at);
CREATE TABLE IF NOT EXISTS schedule_runs (
  id INTEGER PRIMARY KEY, tournament_id INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
  kind TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (datetime('now')), created_by INTEGER,
  stats TEXT, issues TEXT
);
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY, tournament_id INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
  team_id INTEGER REFERENCES teams(id) ON DELETE CASCADE, message TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY, tournament_id INTEGER, user_id INTEGER, action TEXT NOT NULL,
  detail TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_local = threading.local()


def connect(path: str | None = None) -> sqlite3.Connection:
    path = path or DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init(path: str | None = None):
    conn = connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def rows(cur) -> list:
    return [dict(r) for r in cur.fetchall()]
