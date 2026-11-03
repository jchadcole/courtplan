"""Create a demo tournament: 3 venues, 13 courts, 8 divisions, 96 teams, multi-team coaches.

    python seed.py            # creates data/tourney.db with demo accounts
Accounts (password for all: demo-password-1):
    admin@demo.test     superadmin / tournament admin
    director@demo.test  site director for North Fieldhouse
    coach@demo.test     manages two teams (multi-team coach)
"""
import random
import secrets
import sys

from app import auth, db

PW = "demo-password-1"


def main(path=None):
    db.init(path)
    conn = db.connect(path)
    if conn.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]:
        print("Database already has users; skipping seed.")
        return
    rnd = random.Random(3)
    uid = {}
    for email, name, sa in [("admin@demo.test", "Avery Admin", 1), ("director@demo.test", "Dana Director", 0),
                            ("coach@demo.test", "Casey Coach", 0)]:
        uid[email] = conn.execute("INSERT INTO users(email,name,password_hash,is_superadmin) VALUES (?,?,?,?)",
                                  (email, name, auth.hash_password(PW), sa)).lastrowid
    tid = conn.execute(
        "INSERT INTO tournaments(name,location,start_date,end_date,public_slug,created_by,published) "
        "VALUES (?,?,?,?,?,?,1)", ("Fall Hoops Showcase", "Metro Area", "2026-11-14", "2026-11-15",
                                   secrets.token_urlsafe(8), uid["admin@demo.test"])).lastrowid
    venues = {}
    for name, n, addr in [("North Fieldhouse", 5, "100 North Rd"), ("South Sports Complex", 5, "22 South Ave"),
                          ("East High School", 3, "9 East St")]:
        vid = conn.execute("INSERT INTO venues(tournament_id,name,address) VALUES (?,?,?)", (tid, name, addr)).lastrowid
        venues[name] = vid
        for i in range(n):
            conn.execute("INSERT INTO courts(venue_id,name) VALUES (?,?)", (vid, f"Court {i + 1}"))
        for i in range(n - (1 if n > 3 else 0)):
            conn.execute("INSERT INTO ref_crews(tournament_id,name,venue_id) VALUES (?,?,?)",
                         (tid, f"{name.split()[0]} Crew {i + 1}", vid))
    conn.execute("INSERT INTO tournament_members(tournament_id,user_id,role) VALUES (?,?, 'admin')",
                 (tid, uid["admin@demo.test"]))
    conn.execute("INSERT INTO tournament_members(tournament_id,user_id,role,venue_id) VALUES (?,?, 'director', ?)",
                 (tid, uid["director@demo.test"], venues["North Fieldhouse"]))
    clubs = ["Lightning", "Rise", "Elite", "Hoop Dreams", "Ballers", "Storm", "Express", "Select", "Phoenix",
             "Panthers", "Thunder", "Wolves", "Titans", "Heat", "Swish", "Legends"]
    teams_by_div = {}
    coach_ids = []
    for age in [11, 12, 13, 14, 15, 16, 17, 18]:
        div = f"{age}U Boys" if age % 2 else f"{age}U Girls"
        did = conn.execute("INSERT INTO divisions(tournament_id,name,pool_size) VALUES (?,?,4)", (tid, div)).lastrowid
        teams_by_div[did] = []
        for club in rnd.sample(clubs, 12):
            cid = conn.execute("INSERT INTO coaches(tournament_id,name,email) VALUES (?,?,?)",
                               (tid, f"Coach {club} {age}", f"{club.lower().replace(' ', '')}{age}@clubs.test")
                               ).lastrowid
            coach_ids.append(cid)
            team = conn.execute("INSERT INTO teams(tournament_id,division_id,name,club,coach_id,invite_code,rating) "
                                "VALUES (?,?,?,?,?,?,?)", (tid, did, f"{club} {age}U", club, cid,
                                                           auth.new_invite_code(), round(rnd.uniform(0.35, 0.75), 3))
                                ).lastrowid
            teams_by_div[did].append(team)
    # Multi-team coaches: the same coach runs a team in two adjacent age groups.
    divs = list(teams_by_div)
    casey = conn.execute("INSERT INTO coaches(tournament_id,name,email,min_gap_minutes) VALUES (?,?,?,75)",
                         (tid, "Casey Coach", "coach@demo.test")).lastrowid
    for i in range(0, len(divs) - 1, 2):
        for j in range(3):
            a, b = teams_by_div[divs[i]][j], teams_by_div[divs[i + 1]][j]
            shared = casey if (i, j) == (0, 0) else conn.execute("SELECT coach_id FROM teams WHERE id=?",
                                                                 (a,)).fetchone()[0]
            conn.execute("UPDATE teams SET coach_id=? WHERE id IN (?,?)", (shared, a, b))
            if (i, j) == (0, 0):
                for t in (a, b):
                    conn.execute("INSERT INTO team_managers(team_id,user_id) VALUES (?,?)", (t, uid["coach@demo.test"]))
    # A few preferences
    sample = [t for ts in teams_by_div.values() for t in ts]
    for t in rnd.sample(sample, 10):
        conn.execute('INSERT INTO preferences(team_id,kind,day,start,"end",hard,note) VALUES (?,?,?,?,?,?,?)',
                     (t, "unavailable", "2026-11-14", "08:00", "11:00", 1, "Travelling in Saturday morning"))
    for t in rnd.sample(sample, 10):
        conn.execute('INSERT INTO preferences(team_id,kind,day,start,"end",hard,note) VALUES (?,?,?,?,?,?,?)',
                     (t, "not_after", "2026-11-15", None, "16:00", 0, "Long drive home Sunday"))
    conn.commit()
    print(f"Seeded tournament {tid} with {len(sample)} teams. Sign in as admin@demo.test / {PW}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
