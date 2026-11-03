from app.engine import *

def build(n_div=4, per=8, courts_per=(3,3), days=("2026-11-14","2026-11-15")):
    s = Settings(start_date=days[0], end_date=days[1])
    venues = [Venue(i+1, f"V{i+1}") for i in range(len(courts_per))]
    courts, cid = [], 1
    for v, n in zip(venues, courts_per):
        for _ in range(n):
            courts.append(Court(cid, v.id, f"C{cid}")); cid += 1
    divs = [Division(i+1, f"D{i+1}") for i in range(n_div)]
    teams, tid = [], 1
    for d in divs:
        for _ in range(per):
            teams.append(Team(tid, f"T{tid}", d.id, coach_id=tid)); tid += 1
    return s, venues, courts, divs, teams

def test_round_robin_pairs_everyone_once():
    rr = round_robin([1,2,3,4,5])
    pairs = [frozenset(p) for r in rr for p in r]
    assert len(pairs) == 10 and len(set(pairs)) == 10

def test_no_hard_violations_and_multi_coach():
    s, v, c, d, t = build()
    # same coach for a team in D1 and D2 -> must never overlap and must respect travel
    for i in range(4):
        t[8+i].coach_id = t[i].coach_id
    r = Scheduler(s, v, c, d, t).run(2)
    assert r.stats["games_unscheduled"] == 0
    assert not [i for i in r.issues if i["severity"] == "hard"]

def test_hard_unavailability_respected():
    s, v, c, d, t = build()
    prefs = [Pref(t[0].id, "unavailable", "2026-11-14", None, None, hard=True)]
    sch = Scheduler(s, v, c, d, t, prefs)
    r = sch.run(2)
    for k, p in r.placements.items():
        g = sch.gmap[k]
        if t[0].id in (g.team_a, g.team_b):
            assert p.start >= DAY

def test_flex_slots_reserved():
    s, v, c, d, t = build()
    sch = Scheduler(s, v, c, d, t)
    flex = sum(sl.flex for sl in sch.slots) / len(sch.slots)
    assert 0.1 < flex < 0.2

def test_validate_detects_coach_conflict():
    s, v, c, d, t = build()
    t[1].coach_id = t[0].coach_id
    teams = {x.id: x for x in t}
    courts = {x.id: x for x in c}
    venues = {x.id: x for x in v}
    games = [dict(key="a", team_a=t[0].id, team_b=t[2].id, start=600, end=650, venue_id=1, court_id=1),
             dict(key="b", team_a=t[1].id, team_b=t[3].id, start=660, end=710, venue_id=2, court_id=4)]
    issues = validate(s, venues, courts, teams, {}, games)
    assert any(i["type"] == "coach_conflict" for i in issues)

def test_rule_parser():
    from app.ai import rule_parse
    t = {"start_date": "2026-11-14", "end_date": "2026-11-15", "day_start": "08:00", "day_end": "21:00"}
    out = rule_parse("No games before 9am on Saturday; we can't play Sunday 1pm-3pm", t, [])
    kinds = sorted(p["kind"] for p in out["preferences"])
    assert kinds == ["not_before", "unavailable"]

def test_pools_balanced_by_rating_and_clubs_split():
    teams = [Team(i, f"T{i}", 1, rating=r, club=("Storm" if i in (1, 2) else f"C{i}")) for i, r in enumerate([.9, .85, .8, .7, .6, .5, .4, .3], 1)]
    pools = make_pools(teams, 4)
    assert len(pools) == 2
    avg = [sum(t.rating for t in p) / len(p) for p in pools]
    assert abs(avg[0] - avg[1]) < 0.06
    for p in pools:
        assert sum(t.club == "Storm" for t in p) <= 1

def test_coach_rule_profile_gap():
    s, v, c, d, t = build()
    t[8].coach_id = t[0].coach_id
    sch = Scheduler(s, v, c, d, t, coach_gaps={t[0].coach_id: 120})
    r = sch.run(2)
    assert not [i for i in r.issues if i["severity"] == "hard"]
    def games_of(team):
        return [(p.start, p.end) for k, p in r.placements.items() if team in (sch.gmap[k].team_a, sch.gmap[k].team_b)]
    for a in games_of(t[0].id):
        for b in games_of(t[8].id):
            assert max(a[0], b[0]) - min(a[1], b[1]) >= 120

def test_standings_order():
    g = lambda a, b, sa, sb: dict(division="D", pool="A", team_a=a, team_b=b, team_a_name=str(a), team_b_name=str(b), score_a=sa, score_b=sb, status="final")
    st = standings([g(1, 2, 50, 40), g(3, 1, 45, 44), g(2, 3, 60, 30)])
    order = [r["team_id"] for r in st["D"]["A"]]
    assert order[0] == 2  # all 1-1; 2 has best point differential (+20)
