import json

from app import brackets
from app.engine import DAY, Division, Game, Scheduler, Team, Venue, Court, Settings
from tests.test_api import client, login  # noqa: F401  (fixture)


def test_structure_byes_go_to_top_seeds():
    specs = brackets.structure(1, 6, third_place=True)
    first = [s for s in specs if s["round"] == 1]
    assert [(s["src_a"], s["src_b"]) for s in first] == [("S4", "S5"), ("S3", "S6")]
    semis = [s for s in specs if s["name"].startswith("Semifinal")]
    assert semis[0]["src_a"] == "S1" and semis[1]["src_a"] == "S2"
    assert specs[-1]["name"] == "3rd place" and specs[-1]["src_a"].startswith("L:")
    assert len([s for s in specs if s["name"] != "3rd place"]) == 5  # n - 1 games


def test_engine_puts_bracket_after_pool_play():
    s = Settings(start_date="2026-11-14", end_date="2026-11-15")
    v = [Venue(1, "V1")]
    c = [Court(i, 1, f"C{i}") for i in range(1, 5)]
    d = [Division(1, "D1")]
    t = [Team(i, f"T{i}", 1, coach_id=i) for i in range(1, 9)]
    from app.engine import generate_games
    pool = generate_games(d, t)
    b = dict(id=9, division_id=1, size=4, third_place=0)
    games = pool + brackets.game_specs_for_engine(b, [g.key for g in pool])
    r = Scheduler(s, v, c, d, t, games=games).run(2)
    assert not r.unscheduled
    last_pool_end = max(r.placements[g.key].end for g in pool)
    semis = [r.placements[k] for k in ("b9-r1-g1", "b9-r1-g2")]
    final = r.placements["b9-r2-g1"]
    assert min(p.start for p in semis) >= last_pool_end + s.min_rest_minutes
    assert final.start >= max(p.end for p in semis) + s.min_rest_minutes
    assert not [i for i in r.issues if i["severity"] == "hard"]


def _finish(client, h, games):
    for i, g in enumerate(games):
        r = client.patch(f"/api/games/{g['id']}", headers=h,
                         json={"status": "final", "score_a": 50 + (g["id"] % 7) + i % 3, "score_b": 40 + (g["id"] % 5)})
        assert r.status_code == 200, r.text


def test_bracket_fills_from_standings_and_advances(client):
    h = login(client, "admin@demo.test")
    tid = client.get("/api/me").json()["tournaments"][0]["id"]
    data = client.get(f"/api/t/{tid}").json()
    div = data["divisions"][0]
    r = client.post(f"/api/t/{tid}/brackets", headers=h,
                    json={"division_id": div["id"], "name": "Gold", "size": 4, "third_place": True})
    assert r.status_code == 200, r.text
    r = client.post(f"/api/t/{tid}/schedule/generate", headers=h, json={"time_limit": 3})
    assert r.status_code == 200, r.text
    assert r.json()["stats"]["games_unscheduled"] == 0
    assert not [i for i in r.json()["issues"] if i["severity"] == "hard"]
    games = client.get(f"/api/t/{tid}/games").json()["games"]
    bgames = [g for g in games if g["bracket_id"]]
    assert len(bgames) == 4 and all(g["team_a"] is None for g in bgames)
    assert {g["team_a_name"] for g in bgames if g["round"] == 1} == {"Seed 1", "Seed 2"}
    pool = [g for g in games if g["division_id"] == div["id"] and not g["bracket_id"]]
    assert max(g["end_at"] for g in pool) < min(g["start_at"] for g in bgames)
    # bracket scores are refused until teams are known
    assert client.patch(f"/api/games/{bgames[0]['id']}", headers=h, json={"status": "final", "score_a": 1,
                                                                            "score_b": 0}).status_code == 400
    _finish(client, h, pool)
    b = client.get(f"/api/t/{tid}/brackets").json()["brackets"][0]
    assert b["status"] in ("ready", "tie"), b
    if b["status"] == "tie":  # unbreakable tie: director picks seeds
        ids = [t["id"] for t in data["teams"] if t["division_id"] == div["id"]][:4]
        assert client.put(f"/api/brackets/{b['id']}/seeds", headers=h, json={"team_ids": ids}).status_code == 200
        b = client.get(f"/api/t/{tid}/brackets").json()["brackets"][0]
    semis = [g for g in b["games"] if g["round"] == 1]
    assert all(g["team_a"] and g["team_b"] for g in semis)
    seeds = [s["team_id"] for s in b["seed_list"]]
    assert {semis[0]["team_a"], semis[0]["team_b"]} == {seeds[0], seeds[3]}
    # a tie score in a bracket game is refused
    assert client.patch(f"/api/games/{semis[0]['id']}", headers=h,
                        json={"status": "final", "score_a": 50, "score_b": 50}).status_code == 400
    for g in semis:
        assert client.patch(f"/api/games/{g['id']}", headers=h,
                            json={"status": "final", "score_a": 60, "score_b": 55}).status_code == 200
    b = client.get(f"/api/t/{tid}/brackets").json()["brackets"][0]
    final = next(g for g in b["games"] if g["name"] == "Final")
    third = next(g for g in b["games"] if g["name"] == "3rd place")
    assert (final["team_a"], final["team_b"]) == (semis[0]["team_a"], semis[1]["team_a"])
    assert (third["team_a"], third["team_b"]) == (semis[0]["team_b"], semis[1]["team_b"])
    # the winners were told who they play next
    notes = client.get(f"/api/t/{tid}/notifications").json()["notifications"]
    assert any("final" in n["message"] and n["team_id"] == final["team_a"] for n in notes)


def test_manual_moves_lock_and_waive(client):
    h = login(client, "admin@demo.test")
    tid = client.get("/api/me").json()["tournaments"][0]["id"]
    client.post(f"/api/t/{tid}/schedule/generate", headers=h, json={"time_limit": 3})
    games = client.get(f"/api/t/{tid}/games").json()["games"]
    a = games[0]
    team = a["team_a"]
    nxt = next(g for g in games if g["id"] != a["id"] and team in (g["team_a"], g["team_b"])
               and g["start_at"][:10] == a["start_at"][:10])
    # the slot check flags the time right after `a` for the team's other game
    cells = client.get(f"/api/games/{nxt['id']}/check", params={"day": a["start_at"][:10]}).json()["cells"]
    t_after = f"{int(a['start_at'][11:13]) + 1:02d}:{a['start_at'][14:16]}"
    key = f"{a['venue_id']}|{t_after}"
    assert cells[key]["level"] == "hard" and cells[key]["reasons"]
    # find a free court at that time and drop the game there: it saves, locks, and is flagged
    occupied = {g["court_id"] for g in games if g["start_at"] and g["start_at"][11:16] == t_after
                and g["start_at"][:10] == a["start_at"][:10]}
    court = next(c["id"] for c in client.get(f"/api/t/{tid}").json()["courts"]
                 if c["venue_id"] == a["venue_id"] and c["id"] not in occupied)
    r = client.patch(f"/api/games/{nxt['id']}", headers=h,
                     json={"court_id": court, "start_at": f"{a['start_at'][:10]}T{t_after}"})
    assert r.status_code == 200, r.text
    assert [i for i in r.json()["issues"] if i["severity"] == "hard"]
    g = next(x for x in client.get(f"/api/t/{tid}/games").json()["games"] if x["id"] == nxt["id"])
    assert g["locked"] == 1 and "hard:rest" in g["flags"]
    # waive the rest rule on purpose: the conflict becomes a note, not a hard issue
    r = client.patch(f"/api/games/{nxt['id']}", headers=h, json={"allow": ["rest"]})
    assert not [i for i in r.json()["issues"] if i["severity"] == "hard" and i["type"] == "rest"]
    # occupied cells are refused
    r = client.patch(f"/api/games/{nxt['id']}", headers=h,
                     json={"court_id": a["court_id"], "start_at": a["start_at"]})
    assert r.status_code == 409
    # re-optimize leaves the locked game where the director put it
    r = client.post(f"/api/t/{tid}/schedule/optimize", headers=h, json={"time_limit": 2})
    assert r.status_code == 200, r.text
    g2 = next(x for x in client.get(f"/api/t/{tid}/games").json()["games"] if x["id"] == nxt["id"])
    assert (g2["court_id"], g2["start_at"]) == (court, f"{a['start_at'][:10]}T{t_after}")
    # unschedule sends it back to the tray
    r = client.patch(f"/api/games/{nxt['id']}", headers=h, json={"status": "unscheduled"})
    g3 = next(x for x in client.get(f"/api/t/{tid}/games").json()["games"] if x["id"] == nxt["id"])
    assert g3["status"] == "unscheduled" and g3["start_at"] is None and not g3["locked"]
