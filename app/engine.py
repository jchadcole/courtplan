"""Constraint-based scheduling engine.

Implements the AAU best-practice rules as hard constraints and soft costs:

Hard (never violated by the engine):
  * a court hosts one game at a time
  * a team never plays two games inside its minimum rest window
  * a team that changes venues gets at least the travel buffer between games
  * a coach with several teams is never in two places at once, and gets the
    travel buffer if those teams play at different venues ("multi-team coach trap")
  * hard team unavailability windows
  * max games per team per day

Soft (minimised by search):
  * keep each division anchored to its home venue (venue zoning)
  * don't bounce a team between venues within a day
  * leave ~15% of slots empty as flex/catch-up slots (85/15 rule)
  * team soft time/venue preferences
  * avoid very long idle gaps between a team's games
  * keep the tournament compact (earlier is better)

Search: randomized greedy construction with restarts, a one-step ejection repair
for games that don't fit, then simulated annealing over move/swap neighbourhoods.
"""
from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

DAY = 1440


def hm_to_min(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def min_to_hm(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


@dataclass
class Settings:
    start_date: str
    end_date: str
    game_minutes: int = 50
    buffer_minutes: int = 10
    min_rest_minutes: int = 60
    travel_buffer_minutes: int = 75
    target_utilization: float = 0.85
    max_games_per_day: int = 3
    day_start: str = "08:00"
    day_end: str = "21:00"

    @property
    def slot_minutes(self) -> int:
        return self.game_minutes + self.buffer_minutes

    @property
    def num_days(self) -> int:
        return (date.fromisoformat(self.end_date) - date.fromisoformat(self.start_date)).days + 1

    def abs_to_iso(self, m: int) -> str:
        d = date.fromisoformat(self.start_date) + timedelta(days=m // DAY)
        return f"{d.isoformat()}T{min_to_hm(m % DAY)}"

    def iso_to_abs(self, s: str) -> int:
        d, t = s.split("T")
        return (date.fromisoformat(d) - date.fromisoformat(self.start_date)).days * DAY + hm_to_min(t[:5])

    def day_index(self, iso_date: str) -> int:
        return (date.fromisoformat(iso_date) - date.fromisoformat(self.start_date)).days


@dataclass
class Venue:
    id: int
    name: str
    open_time: Optional[str] = None
    close_time: Optional[str] = None


@dataclass
class Court:
    id: int
    venue_id: int
    name: str


@dataclass
class Division:
    id: int
    name: str
    pool_size: int = 4
    home_venue_id: Optional[int] = None


@dataclass
class Team:
    id: int
    name: str
    division_id: int
    coach_id: Optional[int] = None
    rating: Optional[float] = None
    club: str = ""


@dataclass
class Pref:
    team_id: int
    kind: str  # unavailable | not_before | not_after | preferred_venue
    day: Optional[str] = None  # ISO date, None = every day
    start: Optional[str] = None  # HH:MM
    end: Optional[str] = None
    venue_id: Optional[int] = None
    hard: bool = False


@dataclass
class Crew:
    id: int
    name: str
    venue_id: Optional[int]


@dataclass
class Game:
    key: str
    division_id: int
    pool: str
    round: int
    team_a: int
    team_b: int


@dataclass
class Fixed:
    """A game whose time/court is locked (already played, in progress, or kept during repair)."""
    key: str
    team_a: int
    team_b: int
    court_id: int
    start: int
    end: int


@dataclass
class Slot:
    idx: int
    court_id: int
    venue_id: int
    start: int
    end: int
    flex: bool


@dataclass
class Placement:
    key: str
    court_id: int
    venue_id: int
    start: int
    end: int
    ref_crew_id: Optional[int] = None


@dataclass
class Result:
    placements: dict
    unscheduled: list
    issues: list
    stats: dict
    home_venues: dict


# --------------------------------------------------------------------------
# Matchup generation
# --------------------------------------------------------------------------

def make_pools(teams: list, pool_size: int) -> list:
    """Split teams into competitively balanced pools.

    Teams are seeded by rating (e.g. RPI or prior results) and snake-distributed so each pool gets a
    similar spread of strength, then same-club teams are swapped apart where possible.
    """
    teams = sorted(teams, key=lambda t: (getattr(t, "rating", None) is None, -(getattr(t, "rating", None) or 0), t.id))
    n = len(teams)
    if n == 0:
        return []
    pool_size = max(3, pool_size)
    k = max(1, round(n / pool_size))
    while k > 1 and n / k < 3:
        k -= 1
    pools = [[] for _ in range(k)]
    for i, t in enumerate(teams):
        r, c = divmod(i, k)
        pools[c if r % 2 == 0 else k - 1 - c].append(t)
    _separate_clubs(pools)
    return pools


def _separate_clubs(pools):
    """Swap same-club teams into different pools, trading with a team at the same seed line."""
    if len(pools) < 2:
        return
    for _ in range(3):
        moved = False
        for pi, pool in enumerate(pools):
            seen = {}
            for i, t in enumerate(pool):
                club = (getattr(t, "club", "") or "").strip().lower()
                if not club:
                    continue
                if club not in seen:
                    seen[club] = i
                    continue
                for pj, other in enumerate(pools):
                    if pj == pi or i >= len(other):
                        continue
                    cand = other[i]
                    cc = (getattr(cand, "club", "") or "").strip().lower()
                    other_clubs = {(getattr(x, "club", "") or "").strip().lower() for x in other if x is not cand}
                    mine_clubs = {(getattr(x, "club", "") or "").strip().lower() for x in pool if x is not t}
                    if club not in other_clubs and cc not in mine_clubs:
                        pool[i], other[i] = cand, t
                        moved = True
                        break
        if not moved:
            return


def round_robin(ids: list) -> list:
    """Circle method. Returns list of rounds, each a list of (a, b)."""
    ids = list(ids)
    if len(ids) % 2:
        ids.append(None)
    n = len(ids)
    rounds = []
    for r in range(n - 1):
        pairs = []
        for i in range(n // 2):
            a, b = ids[i], ids[n - 1 - i]
            if a is not None and b is not None:
                pairs.append((a, b) if r % 2 == 0 else (b, a))
        rounds.append(pairs)
        ids = [ids[0]] + [ids[-1]] + ids[1:-1]
    return rounds


def generate_games(divisions: list, teams: list) -> list:
    games = []
    by_div = {}
    for t in teams:
        by_div.setdefault(t.division_id, []).append(t)
    for d in divisions:
        dteams = by_div.get(d.id, [])
        for pi, pool in enumerate(make_pools(dteams, d.pool_size)):
            pname = chr(ord("A") + pi)
            for ri, pairs in enumerate(round_robin([t.id for t in pool])):
                for gi, (a, b) in enumerate(pairs):
                    games.append(Game(f"d{d.id}-{pname}-r{ri + 1}-g{gi + 1}", d.id, pname, ri + 1, a, b))
    return games


# --------------------------------------------------------------------------
# Scheduler
# --------------------------------------------------------------------------

W_FLEX = 40
W_AWAY_FROM_HOME = 50
W_SOFT_PREF = 20
W_VENUE_SWITCH = 30
W_IDLE_PER_HOUR = 6
IDLE_FREE = 240
W_EARLY = 1.0 / 600  # ~1 point per 10 hours into the event


class Scheduler:
    def __init__(self, settings: Settings, venues, courts, divisions, teams, prefs=(), crews=(),
                 fixed=(), games=None, not_before: Optional[int] = None, seed: int = 7,
                 coach_gaps: Optional[dict] = None):
        self.s = settings
        self.venues = {v.id: v for v in venues}
        self.courts = {c.id: c for c in courts}
        self.divisions = {d.id: d for d in divisions}
        self.teams = {t.id: t for t in teams}
        self.crews = list(crews)
        self.rng = random.Random(seed)
        self.fixed = list(fixed)
        self.games = list(games) if games is not None else generate_games(divisions, teams)
        self.gmap = {g.key: g for g in self.games}
        self.not_before = not_before
        self.coach_gaps = {k: v for k, v in (coach_gaps or {}).items() if v}

        self.coach_teams = {}
        for t in teams:
            if t.coach_id is not None:
                self.coach_teams.setdefault(t.coach_id, set()).add(t.id)

        self.hard_windows = {}  # team -> list[(start,end)] unavailable
        self.soft_prefs = {}  # team -> list[Pref]
        for p in prefs:
            if p.team_id not in self.teams:
                continue
            if p.kind == "unavailable" or (p.hard and p.kind in ("not_before", "not_after")):
                for w in self._pref_windows(p):
                    self.hard_windows.setdefault(p.team_id, []).append(w)
            else:
                self.soft_prefs.setdefault(p.team_id, []).append(p)

        self.slots = self._build_slots()
        self.home = self._assign_home_venues()

        # dynamic state
        self.iv = {}  # key -> (start, end, venue_id, court_id)
        self.team_keys = {t: set() for t in self.teams}
        for f in self.fixed:
            c = self.courts.get(f.court_id)
            self.iv[f.key] = (f.start, f.end, c.venue_id if c else None, f.court_id)
            for t in (f.team_a, f.team_b):
                self.team_keys.setdefault(t, set()).add(f.key)
        self.fixed_teams = {f.key: (f.team_a, f.team_b) for f in self.fixed}
        self.slot_of = {}  # key -> slot idx
        self.owner = {}  # slot idx -> key

    # ---- setup helpers ----
    def _pref_windows(self, p: Pref):
        days = [self.s.day_index(p.day)] if p.day else range(self.s.num_days)
        for d in days:
            base = d * DAY
            if p.kind == "unavailable":
                st = hm_to_min(p.start) if p.start else 0
                en = hm_to_min(p.end) if p.end else DAY
            elif p.kind == "not_before":
                st, en = 0, hm_to_min(p.start or "00:00")
            elif p.kind == "not_after":
                st, en = hm_to_min(p.end or p.start or "23:59"), DAY
            else:
                continue
            yield (base + st, base + en)

    def _build_slots(self):
        s = self.s
        slots = []
        fixed_by_court = {}
        for f in self.fixed:
            fixed_by_court.setdefault(f.court_id, []).append((f.start, f.end))
        flex_every = 0
        if s.target_utilization < 0.999:
            flex_every = max(2, round(1 / (1 - s.target_utilization)))
        for c in sorted(self.courts.values(), key=lambda c: (c.venue_id, c.id)):
            v = self.venues.get(c.venue_id)
            open_m = hm_to_min(v.open_time) if v and v.open_time else hm_to_min(s.day_start)
            close_m = hm_to_min(v.close_time) if v and v.close_time else hm_to_min(s.day_end)
            for d in range(s.num_days):
                m = open_m
                n = 0
                while m + s.game_minutes <= close_m:
                    st, en = d * DAY + m, d * DAY + m + s.game_minutes
                    m += s.slot_minutes
                    n += 1
                    if self.not_before is not None and st < self.not_before:
                        continue
                    if any(st < fe + s.buffer_minutes and fs < en + s.buffer_minutes
                           for fs, fe in fixed_by_court.get(c.id, [])):
                        continue
                    # stagger flex slots across courts so a catch-up gap exists every wave
                    flex = bool(flex_every) and (n + c.id) % flex_every == 0
                    slots.append(Slot(len(slots), c.id, c.venue_id, st, en, flex))
        return slots

    def _assign_home_venues(self):
        """Venue zoning: each division anchored to one venue, balanced by capacity."""
        cap = {}
        for sl in self.slots:
            if not sl.flex:
                cap[sl.venue_id] = cap.get(sl.venue_id, 0) + 1
        home = {}
        load = {v: 0 for v in cap}
        need = {}
        for g in self.games:
            need[g.division_id] = need.get(g.division_id, 0) + 1
        for d in self.divisions.values():
            if d.home_venue_id in cap:
                home[d.id] = d.home_venue_id
                load[d.home_venue_id] += need.get(d.id, 0)
        # Largest divisions first, to the venue with most remaining capacity.
        for did in sorted(need, key=lambda x: -need[x]):
            if did in home or not cap:
                continue
            v = max(cap, key=lambda v: (cap[v] - load[v]) / max(cap[v], 1))
            home[did] = v
            load[v] += need[did]
        return home

    # ---- constraint checks ----
    def _gap(self, a, b):
        """Minutes between two intervals (negative = overlap)."""
        return max(a[0], b[0]) - min(a[1], b[1])

    def feasible(self, g: Game, sl: Slot) -> bool:
        s = self.s
        if self.owner.get(sl.idx) not in (None, g.key):
            return False
        me = (sl.start, sl.end)
        day = sl.start // DAY
        for t in (g.team_a, g.team_b):
            per_day = 0
            for k in self.team_keys.get(t, ()):
                if k == g.key:
                    continue
                o = self.iv[k]
                gap = self._gap(me, o)
                need = s.min_rest_minutes
                if o[2] is not None and o[2] != sl.venue_id:
                    need = max(need, s.travel_buffer_minutes)
                if gap < need:
                    return False
                if o[0] // DAY == day:
                    per_day += 1
            if per_day >= s.max_games_per_day:
                return False
            for ws, we in self.hard_windows.get(t, ()):
                if sl.start < we and ws < sl.end:
                    return False
        # multi-team coach trap
        playing = {g.team_a, g.team_b}
        for t in (g.team_a, g.team_b):
            coach = self.teams[t].coach_id
            if coach is None:
                continue
            for u in self.coach_teams.get(coach, ()):
                if u in playing:
                    continue
                for k in self.team_keys.get(u, ()):
                    if k == g.key:
                        continue
                    o = self.iv[k]
                    gap = self._gap(me, o)
                    need = 0 if o[2] == sl.venue_id else s.travel_buffer_minutes
                    need = max(need, self.coach_gaps.get(coach, 0))
                    if gap < need:
                        return False
        return True

    # ---- costs ----
    def game_cost(self, g: Game, sl: Slot) -> float:
        c = 0.0
        if sl.flex:
            c += W_FLEX
        hv = self.home.get(g.division_id)
        if hv is not None and sl.venue_id != hv:
            c += W_AWAY_FROM_HOME
        for t in (g.team_a, g.team_b):
            for p in self.soft_prefs.get(t, ()):
                if self._violates_soft(p, sl):
                    c += W_SOFT_PREF
        c += sl.start * W_EARLY
        return c

    def _violates_soft(self, p: Pref, sl: Slot) -> bool:
        day = sl.start // DAY
        if p.day and self.s.day_index(p.day) != day:
            return False
        tod = sl.start % DAY
        if p.kind == "not_before":
            return tod < hm_to_min(p.start or "00:00")
        if p.kind == "not_after":
            return (sl.end % DAY) > hm_to_min(p.end or p.start or "23:59")
        if p.kind == "preferred_venue":
            return p.venue_id is not None and sl.venue_id != p.venue_id
        if p.kind == "unavailable":
            st = day * DAY + (hm_to_min(p.start) if p.start else 0)
            en = day * DAY + (hm_to_min(p.end) if p.end else DAY)
            return sl.start < en and st < sl.end
        return False

    def team_cost(self, t: int) -> float:
        ivs = sorted(self.iv[k] for k in self.team_keys.get(t, ()))
        c = 0.0
        for a, b in zip(ivs, ivs[1:]):
            if a[0] // DAY != b[0] // DAY:
                continue
            if a[2] != b[2]:
                c += W_VENUE_SWITCH
            idle = b[0] - a[1]
            if idle > IDLE_FREE:
                c += W_IDLE_PER_HOUR * (idle - IDLE_FREE) / 60
        return c

    def total_cost(self) -> float:
        c = sum(self.game_cost(self.gmap[k], self.slots[i]) for k, i in self.slot_of.items())
        c += sum(self.team_cost(t) for t in self.teams)
        return c

    # ---- state mutation ----
    def assign(self, g: Game, sl: Slot):
        self.slot_of[g.key] = sl.idx
        self.owner[sl.idx] = g.key
        self.iv[g.key] = (sl.start, sl.end, sl.venue_id, sl.court_id)
        self.team_keys[g.team_a].add(g.key)
        self.team_keys[g.team_b].add(g.key)

    def unassign(self, g: Game):
        i = self.slot_of.pop(g.key, None)
        if i is None:
            return None
        del self.owner[i]
        del self.iv[g.key]
        self.team_keys[g.team_a].discard(g.key)
        self.team_keys[g.team_b].discard(g.key)
        return self.slots[i]

    def reset(self):
        for k in list(self.slot_of):
            self.unassign(self.gmap[k])

    # ---- search ----
    def _tightness(self, g: Game) -> float:
        x = 0.0
        for t in (g.team_a, g.team_b):
            x += len(self.hard_windows.get(t, ())) * 2
            coach = self.teams[t].coach_id
            if coach is not None:
                x += (len(self.coach_teams.get(coach, ())) - 1) * 3
        return x

    def construct(self, noise: float):
        order = sorted(self.games, key=lambda g: (g.round, -self._tightness(g) + self.rng.random() * noise))
        unplaced = []
        for g in order:
            best, best_c = None, math.inf
            ta, tb = g.team_a, g.team_b
            base_t = self.team_cost(ta) + self.team_cost(tb)
            for sl in self.slots:
                if sl.idx in self.owner or not self.feasible(g, sl):
                    continue
                c = self.game_cost(g, sl)
                if c >= best_c:
                    continue
                self.assign(g, sl)
                c += self.team_cost(ta) + self.team_cost(tb) - base_t
                self.unassign(g)
                c += self.rng.random() * noise
                if c < best_c:
                    best, best_c = sl, c
            if best is None:
                unplaced.append(g)
            else:
                self.assign(g, best)
        return unplaced

    def repair(self, unplaced):
        """Ejection chain of depth 1: move a blocking game to free a slot."""
        still = []
        for g in unplaced:
            done = False
            for sl in self.slots:
                if sl.idx in self.owner:
                    hk = self.owner[sl.idx]
                    h = self.gmap[hk]
                    hs = self.unassign(h)
                    if self.feasible(g, sl):
                        self.assign(g, sl)
                        alt = next((x for x in self.slots if x.idx not in self.owner and self.feasible(h, x)), None)
                        if alt:
                            self.assign(h, alt)
                            done = True
                            break
                        self.unassign(g)
                    self.assign(h, hs)
                elif self.feasible(g, sl):
                    self.assign(g, sl)
                    done = True
                    break
            if not done:
                still.append(g)
        return still

    def anneal(self, deadline: float, unplaced: list):
        keys = [k for k in self.slot_of]
        if not keys:
            return unplaced
        cur = self.total_cost()
        t0, t1 = 30.0, 0.2
        start = time.time()
        span = max(deadline - start, 1e-3)
        it = 0
        n = len(self.slots)
        while True:
            it += 1
            if it % 200 == 0:
                now = time.time()
                if now >= deadline:
                    break
                if unplaced:
                    unplaced = self.repair(unplaced)
                    keys = list(self.slot_of)
                    cur = self.total_cost()
            frac = min(1.0, (time.time() - start) / span) if it % 50 == 0 or it == 1 else frac
            temp = t0 * (t1 / t0) ** frac
            g = self.gmap[keys[self.rng.randrange(len(keys))]]
            sl = self.slots[self.rng.randrange(n)]
            if self.slot_of.get(g.key) == sl.idx:
                continue
            other_key = self.owner.get(sl.idx)
            h = self.gmap[other_key] if other_key else None
            affected = {g.team_a, g.team_b} | ({h.team_a, h.team_b} if h else set())
            before = sum(self.team_cost(t) for t in affected)
            gs = self.slots[self.slot_of[g.key]]
            before += self.game_cost(g, gs) + (self.game_cost(h, sl) if h else 0)
            self.unassign(g)
            if h:
                self.unassign(h)
            ok = self.feasible(g, sl)
            if ok:
                self.assign(g, sl)
                if h:
                    ok = self.feasible(h, gs)
                    if ok:
                        self.assign(h, gs)
                    else:
                        self.unassign(g)
            if not ok:
                self.assign(g, gs)
                if h:
                    self.assign(h, sl)
                continue
            after = sum(self.team_cost(t) for t in affected)
            after += self.game_cost(g, sl) + (self.game_cost(h, gs) if h else 0)
            delta = after - before
            if delta <= 0 or self.rng.random() < math.exp(-delta / temp):
                cur += delta
            else:
                self.unassign(g)
                if h:
                    self.unassign(h)
                self.assign(g, gs)
                if h:
                    self.assign(h, sl)
        return unplaced

    def snapshot(self):
        return dict(self.slot_of)

    def restore(self, snap):
        self.reset()
        for k, i in snap.items():
            self.assign(self.gmap[k], self.slots[i])

    def run(self, time_limit: float = 4.0) -> Result:
        t_start = time.time()
        deadline = t_start + time_limit
        best = None
        construct_budget = t_start + time_limit * 0.35
        attempt = 0
        while attempt == 0 or (time.time() < construct_budget and attempt < 25):
            self.reset()
            unplaced = self.construct(noise=0 if attempt == 0 else 4.0)
            if unplaced:
                unplaced = self.repair(unplaced)
            score = (len(unplaced), self.total_cost())
            if best is None or score < best[0]:
                best = (score, self.snapshot(), [g.key for g in unplaced])
            attempt += 1
            if score[0] == 0 and attempt >= 3:
                break
        self.restore(best[1])
        unplaced = [self.gmap[k] for k in best[2]]
        unplaced = self.anneal(deadline, unplaced)
        placements = {}
        for k, i in self.slot_of.items():
            sl = self.slots[i]
            placements[k] = Placement(k, sl.court_id, sl.venue_id, sl.start, sl.end)
        self.assign_crews(placements)
        issues = validate(self.s, self.venues, self.courts, self.teams, self.divisions,
                          self._all_intervals(placements), self.hard_windows, self.soft_prefs, self.home,
                          self.coach_gaps)
        stats = self._stats(placements, unplaced, attempt, time.time() - t_start)
        return Result(placements, [g.key for g in unplaced], issues, stats, dict(self.home))

    def _all_intervals(self, placements):
        out = []
        for p in placements.values():
            g = self.gmap[p.key]
            out.append(dict(key=p.key, team_a=g.team_a, team_b=g.team_b, start=p.start, end=p.end,
                            venue_id=p.venue_id, court_id=p.court_id, division_id=g.division_id,
                            ref_crew_id=p.ref_crew_id))
        for f in self.fixed:
            c = self.courts.get(f.court_id)
            out.append(dict(key=f.key, team_a=f.team_a, team_b=f.team_b, start=f.start, end=f.end,
                            venue_id=c.venue_id if c else None, court_id=f.court_id, division_id=None,
                            ref_crew_id=None))
        return out

    def assign_crews(self, placements, block_minutes: int = 240):
        """Ref zoning: a crew stays at one venue, on one court, for a fixed block of games."""
        crews_by_venue = {}
        floating = []
        for c in self.crews:
            (crews_by_venue.setdefault(c.venue_id, []) if c.venue_id in self.venues else floating).append(c)
        # Floating crews get attached to the venue with the most courts lacking crews.
        need = {}
        for v in self.venues:
            nc = sum(1 for c in self.courts.values() if c.venue_id == v)
            need[v] = nc - len(crews_by_venue.get(v, []))
        for c in floating:
            v = max(need, key=need.get) if need else None
            if v is None:
                break
            crews_by_venue.setdefault(v, []).append(c)
            need[v] -= 1
        by_vdb = {}
        for p in placements.values():
            day = p.start // DAY
            block = (p.start % DAY) // block_minutes
            by_vdb.setdefault((p.venue_id, day, block), []).append(p)
        for (v, day, block), ps in by_vdb.items():
            crews = crews_by_venue.get(v, [])
            if not crews:
                continue
            courts_here = sorted({p.court_id for p in ps})
            for i, cid in enumerate(courts_here):
                crew = crews[(i + block) % len(crews)] if len(crews) >= len(courts_here) else (
                    crews[i] if i < len(crews) else None)
                for p in ps:
                    if p.court_id == cid and crew:
                        p.ref_crew_id = crew.id

    def _stats(self, placements, unplaced, attempts, elapsed):
        total_slots = len(self.slots)
        used = len(placements)
        venue_slots, venue_used = {}, {}
        for sl in self.slots:
            venue_slots[sl.venue_id] = venue_slots.get(sl.venue_id, 0) + 1
        for p in placements.values():
            venue_used[p.venue_id] = venue_used.get(p.venue_id, 0) + 1
        sw = 0
        for t in self.teams:
            ivs = sorted(self.iv[k] for k in self.team_keys.get(t, ()))
            sw += sum(1 for a, b in zip(ivs, ivs[1:]) if a[0] // DAY == b[0] // DAY and a[2] != b[2])
        away = sum(1 for p in placements.values()
                   if self.home.get(self.gmap[p.key].division_id) not in (None, p.venue_id))
        flex_used = sum(1 for k, i in self.slot_of.items() if self.slots[i].flex)
        return dict(
            games_total=len(self.games), games_scheduled=used, games_unscheduled=len(unplaced),
            slots_available=total_slots, utilization=round(used / total_slots, 3) if total_slots else 0,
            target_utilization=self.s.target_utilization,
            venue_utilization={v: round(venue_used.get(v, 0) / n, 3) for v, n in venue_slots.items()},
            team_venue_switches=sw, games_away_from_division_venue=away, flex_slots_used=flex_used,
            cost=round(self.total_cost(), 1), restarts=attempts, seconds=round(elapsed, 2),
        )


# --------------------------------------------------------------------------
# Validation (works on real times, so it also checks schedules after delays)
# --------------------------------------------------------------------------

def validate(s: Settings, venues, courts, teams, divisions, games, hard_windows=None,
             soft_prefs=None, home=None, coach_gaps=None) -> list:
    """games: list of dicts with key, team_a, team_b, start, end, venue_id, court_id."""
    hard_windows = hard_windows or {}
    soft_prefs = soft_prefs or {}
    home = home or {}
    coach_gaps = coach_gaps or {}
    issues = []
    tname = lambda t: teams[t].name if t in teams else str(t)

    def add(sev, kind, keys, msg):
        issues.append(dict(severity=sev, type=kind, games=list(keys), message=msg))

    by_court = {}
    by_team = {}
    for g in games:
        by_court.setdefault(g["court_id"], []).append(g)
        for t in (g["team_a"], g["team_b"]):
            by_team.setdefault(t, []).append(g)

    for cid, gs in by_court.items():
        gs.sort(key=lambda g: g["start"])
        for a, b in zip(gs, gs[1:]):
            if b["start"] < a["end"]:
                cname = courts[cid].name if cid in courts else cid
                add("hard", "court_overlap", [a["key"], b["key"]],
                    f"{cname}: games overlap at {s.abs_to_iso(b['start'])}")

    for t, gs in by_team.items():
        gs.sort(key=lambda g: g["start"])
        per_day = {}
        for g in gs:
            per_day[g["start"] // DAY] = per_day.get(g["start"] // DAY, 0) + 1
            for ws, we in hard_windows.get(t, ()):
                if g["start"] < we and ws < g["end"]:
                    add("hard", "unavailable", [g["key"]],
                        f"{tname(t)} plays at {s.abs_to_iso(g['start'])} during a blocked window")
        for d, n in per_day.items():
            if n > s.max_games_per_day:
                add("hard", "too_many_games", [g["key"] for g in gs if g["start"] // DAY == d],
                    f"{tname(t)} has {n} games on day {d + 1} (max {s.max_games_per_day})")
        for a, b in zip(gs, gs[1:]):
            gap = b["start"] - a["end"]
            if a["venue_id"] != b["venue_id"] and gap < max(s.travel_buffer_minutes, s.min_rest_minutes):
                add("hard", "team_travel", [a["key"], b["key"]],
                    f"{tname(t)} only has {gap} min to change venues (needs {s.travel_buffer_minutes})")
            elif gap < s.min_rest_minutes:
                add("hard", "rest", [a["key"], b["key"]],
                    f"{tname(t)} only gets {gap} min rest before {s.abs_to_iso(b['start'])} "
                    f"(needs {s.min_rest_minutes})")
            elif a["start"] // DAY == b["start"] // DAY and a["venue_id"] != b["venue_id"]:
                add("soft", "venue_switch", [a["key"], b["key"]], f"{tname(t)} changes venues mid-day")

    coach_teams = {}
    for t in teams.values():
        if t.coach_id is not None:
            coach_teams.setdefault(t.coach_id, []).append(t.id)
    for coach, ts in coach_teams.items():
        if len(ts) < 2:
            continue
        gs = sorted({g["key"]: g for t in ts for g in by_team.get(t, [])}.values(), key=lambda g: g["start"])
        cg = coach_gaps.get(coach, 0)
        for i, a in enumerate(gs):
            for b in gs[i + 1:]:
                if b["start"] - a["end"] > max(s.travel_buffer_minutes, cg):
                    break
                if {a["team_a"], a["team_b"]} & {b["team_a"], b["team_b"]}:
                    continue
                gap = b["start"] - a["end"]
                need = 0 if a["venue_id"] == b["venue_id"] else s.travel_buffer_minutes
                need = max(need, cg)
                if gap < need:
                    add("hard", "coach_conflict", [a["key"], b["key"]],
                        f"A coach of {', '.join(tname(t) for t in ts)} is double-booked around "
                        f"{s.abs_to_iso(b['start'])} ({gap} min apart"
                        + (f", needs {need})" if need else ")"))

    for g in games:
        for t in (g["team_a"], g["team_b"]):
            for p in soft_prefs.get(t, ()):
                fake = Slot(0, g["court_id"], g["venue_id"], g["start"], g["end"], False)
                if Scheduler._violates_soft(_SoftCtx(s), p, fake):
                    add("soft", "preference", [g["key"]], f"{tname(t)}: soft preference '{p.kind}' not met")
        dv = g.get("division_id")
        if dv is not None and home.get(dv) not in (None, g["venue_id"]):
            vn = venues[g["venue_id"]].name if g["venue_id"] in venues else g["venue_id"]
            add("soft", "zoning", [g["key"]], f"Game played at {vn}, away from its division's home venue")
    return issues


class _SoftCtx:
    def __init__(self, s):
        self.s = s


def standings(games: list) -> dict:
    """Pool standings from final games. games: dicts with division, pool, team names/ids, scores, status.

    Order: wins, then head-to-head (two-way ties), then point differential, then points allowed.
    """
    table = {}
    h2h = {}
    for g in games:
        key = (g["division"], g["pool"])
        pool = table.setdefault(key, {})
        for tid, name in ((g["team_a"], g["team_a_name"]), (g["team_b"], g["team_b_name"])):
            pool.setdefault(tid, dict(team_id=tid, team=name, w=0, l=0, pf=0, pa=0))
        if g["status"] != "final" or g["score_a"] is None or g["score_b"] is None:
            continue
        a, b = pool[g["team_a"]], pool[g["team_b"]]
        a["pf"] += g["score_a"]; a["pa"] += g["score_b"]
        b["pf"] += g["score_b"]; b["pa"] += g["score_a"]
        if g["score_a"] > g["score_b"]:
            a["w"] += 1; b["l"] += 1; h2h[(g["team_a"], g["team_b"])] = 1; h2h[(g["team_b"], g["team_a"])] = -1
        elif g["score_b"] > g["score_a"]:
            b["w"] += 1; a["l"] += 1; h2h[(g["team_b"], g["team_a"])] = 1; h2h[(g["team_a"], g["team_b"])] = -1
    out = {}
    import functools

    for (div, pool), teams in table.items():
        rows = list(teams.values())
        for r in rows:
            r["diff"] = r["pf"] - r["pa"]

        def cmp(x, y):
            if x["w"] != y["w"]:
                return y["w"] - x["w"]
            tied = [r for r in rows if r["w"] == x["w"]]
            if len(tied) == 2 and (x["team_id"], y["team_id"]) in h2h:
                return -h2h[(x["team_id"], y["team_id"])]
            if x["diff"] != y["diff"]:
                return y["diff"] - x["diff"]
            return x["pa"] - y["pa"]
        rows.sort(key=functools.cmp_to_key(cmp))
        out.setdefault(div, {})[pool] = rows
    return out
