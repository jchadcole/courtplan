import os, tempfile, importlib
import pytest

@pytest.fixture()
def client(monkeypatch):
    d = tempfile.mkdtemp()
    path = os.path.join(d, "t.db")
    monkeypatch.setenv("TOURNEY_DB", path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from app import db
    monkeypatch.setattr(db, "DB_PATH", path)
    from app import auth
    auth._attempts.clear()
    import seed
    seed.main(path)
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c

def login(c, email):
    r = c.post("/api/auth/login", json={"email": email, "password": "demo-password-1"})
    assert r.status_code == 200, r.text
    return {"X-CSRF-Token": r.json()["csrf"]}

def test_csrf_and_auth_required(client):
    assert client.post("/api/tournaments", json={}).status_code == 401
    h = login(client, "admin@demo.test")
    assert client.post("/api/tournaments", json={"name": "x"}).status_code == 403  # missing CSRF
    r = client.post("/api/tournaments", headers=h, json={"name": "Spring", "start_date": "2027-04-01", "end_date": "2027-04-02"})
    assert r.status_code == 200

def test_login_throttle(client):
    for _ in range(8):
        assert client.post("/api/auth/login", json={"email": "admin@demo.test", "password": "nope"}).status_code == 401
    assert client.post("/api/auth/login", json={"email": "admin@demo.test", "password": "nope"}).status_code == 429

def test_full_flow(client):
    h = login(client, "admin@demo.test")
    tid = client.get("/api/me").json()["tournaments"][0]["id"]
    r = client.post(f"/api/t/{tid}/schedule/generate", headers=h, json={"time_limit": 3})
    assert r.status_code == 200, r.text
    st = r.json()["stats"]
    assert st["games_unscheduled"] == 0
    assert not [i for i in r.json()["issues"] if i["severity"] == "hard"]
    games = client.get(f"/api/t/{tid}/games").json()["games"]
    assert len(games) == st["games_total"]
    # delay a court and confirm cascade + notifications
    g0 = games[0]
    r = client.post(f"/api/courts/{g0['court_id']}/delay", headers=h, json={"minutes": 25})
    assert r.status_code == 200, r.text
    assert r.json()["moved"] >= 1
    after = {g["id"]: g for g in client.get(f"/api/t/{tid}/games").json()["games"]}
    assert after[g0["id"]]["start_at"] != g0["start_at"]
    # repair resolves any new hard conflicts
    r = client.post(f"/api/t/{tid}/schedule/repair", headers=h, json={})
    assert r.status_code == 200 and r.json()["remaining_hard"] == 0, r.text
    # public schedule
    slug = client.get(f"/api/t/{tid}").json()["tournament"]["public_slug"]
    assert client.get(f"/api/public/{slug}").status_code == 200
    assert "ref_crew" not in client.get(f"/api/public/{slug}").json()["games"][0]

def test_team_isolation(client):
    h = login(client, "coach@demo.test")
    me = client.get("/api/me").json()
    assert len(me["teams"]) == 2
    tid = me["teams"][0]["tournament_id"]
    data = client.get(f"/api/t/{tid}").json()
    assert data["role"] == "team" and "invite_code" not in data["teams"][0]
    mine = me["teams"][0]["id"]
    other = next(t["id"] for t in data["teams"] if t["id"] not in data["my_teams"])
    assert client.get(f"/api/teams/{mine}/prefs").status_code == 200
    assert client.get(f"/api/teams/{other}/prefs").status_code == 404
    assert client.post(f"/api/t/{tid}/schedule/generate", headers=h, json={}).status_code == 403
    r = client.post(f"/api/teams/{mine}/prefs/parse", headers=h, json={"text": "We can't play before 10am Saturday. We would prefer to finish by 5pm on Sunday"})
    prefs = r.json()["preferences"]
    kinds = {p["kind"] for p in prefs}
    assert {"not_before", "not_after"} <= kinds, prefs
    for p in prefs:
        p["source"] = "ai"
        assert client.post(f"/api/teams/{mine}/prefs", headers=h, json=p).status_code == 200

def test_director_scoped_to_venue(client):
    h = login(client, "admin@demo.test")
    tid = client.get("/api/me").json()["tournaments"][0]["id"]
    client.post(f"/api/t/{tid}/schedule/generate", headers=h, json={"time_limit": 2})
    data = client.get(f"/api/t/{tid}").json()
    north = next(v["id"] for v in data["venues"] if v["name"].startswith("North"))
    south_court = next(c["id"] for c in data["courts"] if c["venue_id"] != north)
    client.post("/api/auth/logout", headers=h)
    h = login(client, "director@demo.test")
    assert client.post(f"/api/courts/{south_court}/delay", headers=h, json={"minutes": 10}).status_code == 403
    north_court = next(c["id"] for c in data["courts"] if c["venue_id"] == north)
    assert client.post(f"/api/courts/{north_court}/delay", headers=h, json={"minutes": 10}).status_code == 200

def test_csv_import(client):
    h = login(client, "admin@demo.test")
    r = client.post("/api/tournaments", headers=h, json={"name": "Imp", "start_date": "2027-01-01", "end_date": "2027-01-01"})
    tid = r.json()["id"]
    csv = "division,team,club,coach_name,coach_email\n10U,A,,Pat,pat@x.test\n10U,B,,Pat,pat@x.test\n12U,C,,Lee,\n"
    r = client.post(f"/api/t/{tid}/import", headers=h, json={"csv": csv})
    assert r.json()["added"] == 3
    data = client.get(f"/api/t/{tid}").json()
    assert len(data["divisions"]) == 2
    assert max(c["team_count"] for c in data["coaches"]) == 2
