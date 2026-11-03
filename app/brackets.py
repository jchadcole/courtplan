"""Single-elimination brackets that fill themselves from pool play.

A bracket game's two sides are *sources*:
  ``S3``      the 3rd seed
  ``W:<key>`` the winner of another bracket game
  ``L:<key>`` the loser of another bracket game (3rd-place game)

Seeds come from pool standings once every pool game in the division is final. ``pool`` seeding ranks
pool winners first, then runners-up, and so on (the usual "1st in A, 1st in B, ..." order), comparing
teams on the same finishing line by win rate, then point differential, then points allowed.
``division`` seeding ranks every team in the division by record regardless of pool. When a tie can't
be broken the bracket waits and the director picks the seeds by hand.
"""
from __future__ import annotations

import json

from . import engine

MAX_SIZE = 32
ROUND_NAMES = {1: ("Final", "F"), 2: ("Semifinal", "SF"), 4: ("Quarterfinal", "QF"), 8: ("Round of 16", "R16-"),
               16: ("Round of 32", "R32-")}


def seed_positions(p: int) -> list:
    """Bracket line order for a power-of-two field: 1 v p, then the halves mirror each other."""
    order = [1]
    while len(order) < p:
        n = len(order) * 2
        order = [x for s in order for x in (s, n + 1 - s)]
    return order


def round_name(games_in_round: int, r: int):
    full, short = ROUND_NAMES.get(games_in_round, (f"Round {r}", f"R{r}"))
    return full, short


def structure(bid: int, size: int, third_place: bool = False) -> list:
    """Game specs for a single-elimination bracket of `size` seeds (byes go to the top seeds).

    Each spec: key, round (1 = first round), slot (0-based within round), name, short, src_a, src_b.
    """
    if not 2 <= size <= MAX_SIZE:
        raise ValueError(f"A bracket needs 2 to {MAX_SIZE} teams.")
    p = 1
    while p < size:
        p *= 2
    rounds = p.bit_length() - 1
    line = [f"S{s}" if s <= size else None for s in seed_positions(p)]  # None = bye
    specs = []
    for r in range(1, rounds + 1):
        nxt = []
        pairs = [(line[2 * i], line[2 * i + 1]) for i in range(len(line) // 2)]
        real = sum(1 for a, b in pairs if a and b)
        full, short = round_name(p >> r, r)
        n = 0
        for i, (a, b) in enumerate(pairs):
            if a is None or b is None:
                nxt.append(a or b)  # bye: the seed walks into the next round
                continue
            n += 1
            key = f"b{bid}-r{r}-g{i + 1}"
            specs.append(dict(key=key, round=r, slot=i, name=(f"{full}, game {n}" if full.startswith("Round") else f"{full} {n}") if real > 1 else full,
                              short=f"{short}{n}" if real > 1 else short.rstrip("-"), src_a=a, src_b=b))
            nxt.append(f"W:{key}")
        line = nxt
    if third_place and rounds >= 2:
        semis = [x for x in specs if x["round"] == rounds - 1]
        if len(semis) == 2:
            specs.append(dict(key=f"b{bid}-r{rounds}-3rd", round=rounds, slot=1, name="3rd place", short="3rd",
                              src_a=f"L:{semis[0]['key']}", src_b=f"L:{semis[1]['key']}"))
    return specs


def source_label(src: str, specs_by_key: dict) -> str:
    if not src:
        return "TBD"
    if src.startswith("S"):
        return f"Seed {src[1:]}"
    kind, key = src.split(":", 1)
    sp = specs_by_key.get(key)
    name = sp["short"] if sp else "game"
    return f"{'Winner' if kind == 'W' else 'Loser'} {name}"


def feeders(src_a, src_b) -> list:
    return [s.split(":", 1)[1] for s in (src_a, src_b) if s and ":" in s]


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------

def _rank_key(r):
    played = r["w"] + r["l"]
    return (-(r["w"] / played if played else 0), -r["diff"], r["pa"])


def compute_seeds(bracket: dict, pool_games: list, division_name: str):
    """Returns (team_ids, status, note). status: waiting | ready | tie."""
    if not pool_games:
        return None, "waiting", "Waiting for pool games to be scheduled."
    left = sum(1 for g in pool_games if g["status"] != "final")
    if left:
        return None, "waiting", f"Seeds fill in when pool play ends ({left} pool games left)."
    table = engine.standings(pool_games).get(division_name, {})
    if bracket["seeding"] == "division":
        rows = sorted((r for pool in table.values() for r in pool), key=lambda r: (_rank_key(r), r["team_id"]))
        tiers = [rows]
    else:
        depth = max((len(v) for v in table.values()), default=0)
        tiers = []
        for k in range(depth):
            tier = [rows[k] for _, rows in sorted(table.items()) if k < len(rows)]
            tiers.append(sorted(tier, key=lambda r: (_rank_key(r), r["team_id"])))
    order = [r for tier in tiers for r in tier]
    size = bracket["size"]
    if len(order) < size:
        return None, "tie", f"Only {len(order)} teams finished pool play for a {size}-team bracket."
    # A tie the tiebreakers can't split matters if it changes who's in, or who plays whom.
    for tier in tiers:
        for a, b in zip(tier, tier[1:]):
            ia, ib = order.index(a), order.index(b)
            if min(ia, ib) < size and _rank_key(a) == _rank_key(b):
                return None, "tie", (f"{a['team']} and {b['team']} are tied on record, point differential and "
                                     f"points allowed. Choose the seeds by hand.")
    # Pool standings themselves can hold an unbreakable tie (same wins, diff and points allowed).
    if bracket["seeding"] != "division":
        for pool, rows in table.items():
            for a, b in zip(rows, rows[1:]):
                same_w = [r for r in rows if r["w"] == a["w"]]
                if len(same_w) == 2 and _head_to_head(pool_games, a["team_id"], b["team_id"]):
                    continue
                if (a["w"], a["diff"], a["pa"]) == (b["w"], b["diff"], b["pa"]) and order.index(a) < size:
                    return None, "tie", (f"Pool {pool}: {a['team']} and {b['team']} can't be separated by the "
                                         f"tiebreakers. Choose the seeds by hand.")
    return [r["team_id"] for r in order[:size]], "ready", ""


def _head_to_head(games, x, y):
    return any({g["team_a"], g["team_b"]} == {x, y} and g["status"] == "final" and g["score_a"] != g["score_b"]
               for g in games)


def _winner_loser(g):
    if not g or g["status"] != "final" or g["score_a"] is None or g["score_b"] is None or g["score_a"] == g["score_b"]:
        return None, None
    if g["team_a"] is None or g["team_b"] is None:
        return None, None
    return (g["team_a"], g["team_b"]) if g["score_a"] > g["score_b"] else (g["team_b"], g["team_a"])


def resolve(conn, tid: int):
    """Fill bracket slots from standings and results. Returns (changed game ids, alerts)."""
    changed, alerts = [], []
    brackets = [dict(r) for r in conn.execute("SELECT * FROM brackets WHERE tournament_id=?", (tid,))]
    if not brackets:
        return changed, alerts
    games = [dict(r) for r in conn.execute(
        "SELECT g.*, d.name division, ta.name team_a_name, tb.name team_b_name FROM games g "
        "JOIN divisions d ON d.id=g.division_id LEFT JOIN teams ta ON ta.id=g.team_a "
        "LEFT JOIN teams tb ON tb.id=g.team_b WHERE g.tournament_id=?", (tid,))]
    by_key = {g["key"]: g for g in games}
    for b in brackets:
        pool_games = [g for g in games if g["division_id"] == b["division_id"] and not g["bracket_id"]]
        div = conn.execute("SELECT name FROM divisions WHERE id=?", (b["division_id"],)).fetchone()
        if b["seeds"]:
            seeds, status, note = json.loads(b["seeds"]), "manual", "Seeds set by hand."
        else:
            seeds, status, note = compute_seeds(b, pool_games, div["name"] if div else "")
        if status != b["status"] or note != (b["note"] or ""):
            conn.execute("UPDATE brackets SET status=?, note=? WHERE id=?", (status, note, b["id"]))
            if status == "tie":
                alerts.append(f"{b['name']} ({div['name'] if div else ''}): {note}")
        bgames = sorted((g for g in games if g["bracket_id"] == b["id"]), key=lambda g: (g["round"], g["key"]))
        for g in bgames:
            if g["status"] in ("final", "in_progress"):
                continue
            new = []
            for src in (g["src_a"], g["src_b"]):
                t = None
                if src and src.startswith("S"):
                    n = int(src[1:])
                    t = seeds[n - 1] if seeds and n <= len(seeds) else None
                elif src and ":" in src:
                    kind, key = src.split(":", 1)
                    w, l = _winner_loser(by_key.get(key))
                    t = w if kind == "W" else l
                new.append(t)
            if (g["team_a"], g["team_b"]) != tuple(new):
                conn.execute("UPDATE games SET team_a=?, team_b=? WHERE id=?", (new[0], new[1], g["id"]))
                g["team_a"], g["team_b"] = new
                changed.append(g["id"])
    return changed, alerts


def game_specs_for_engine(bracket: dict, pool_keys: list) -> list:
    """engine.Game objects for a bracket: first-round games wait for all of the division's pool games."""
    out = []
    for sp in structure(bracket["id"], bracket["size"], bool(bracket["third_place"])):
        after = tuple(feeders(sp["src_a"], sp["src_b"]))
        if any(src.startswith("S") for src in (sp["src_a"], sp["src_b"])):
            after = after + tuple(pool_keys)
        out.append(engine.Game(sp["key"], bracket["division_id"], None, sp["round"], None, None, after=after,
                               phase=1))
    return out


def deps_from_rows(rows: list) -> dict:
    """key -> predecessor keys, for stored games (used by validation and re-planning)."""
    pool_by_div = {}
    for r in rows:
        if not r["bracket_id"]:
            pool_by_div.setdefault(r["division_id"], []).append(r["key"])
    deps = {}
    for r in rows:
        if not r["bracket_id"]:
            continue
        d = feeders(r["src_a"], r["src_b"])
        if any(s and s.startswith("S") for s in (r["src_a"], r["src_b"])):
            d += pool_by_div.get(r["division_id"], [])
        deps[r["key"]] = d
    return deps


def labels(conn, tid: int) -> dict:
    """bracket game key -> dict(name, label_a, label_b, bracket)"""
    out = {}
    for b in conn.execute("SELECT * FROM brackets WHERE tournament_id=?", (tid,)):
        specs = structure(b["id"], b["size"], bool(b["third_place"]))
        by_key = {s["key"]: s for s in specs}
        for s in specs:
            out[s["key"]] = dict(bracket=b["name"], round_name=s["name"], short=s["short"],
                                 label_a=source_label(s["src_a"], by_key), label_b=source_label(s["src_b"], by_key))
    return out
