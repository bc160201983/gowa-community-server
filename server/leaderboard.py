# GoW: Ascension community server: leaderboards from the players' own profile stats (2026-11-14). Stdlib only.
#
# Why this shape: the game has no leaderboard screen and never calls sceNpScore (host\mpnet\NOTES.md, 2026-11-14).
# Its multiplayer stats live in the player's TUS profile (data slot 1, JSON "profile.scores": [{name, score}]). The
# runtime (kit sceNpTus.c -> mpnet.c) sends a few of those counters here after the game saves that profile, signed in
# by the same challenge as /hello. The numbers are REPORTED BY THE PLAYERS' GAMES and NOT VERIFIED by this server: it
# only caps them to plausible maxima, rate-limits reports, and lets the admin reset or hide a player.
#
# Storage: <data>/leaderboard.db (kept apart from accounts.db, which the account sync job copies between servers).
#   players(acct PK, name, last JSON, best JSON, first INTEGER, updated INTEGER, reports INTEGER, hidden INTEGER)
#   last = the newest report; best = the highest value each counter ever reported (a reset profile keeps its best).
import json, os, re, sqlite3, time

NOTE = "Reported by players' games (not verified)."

# Counters the runtime may send (names from the game's own score table, R_MULTIPLAYER.WAD) and the largest value we
# accept for each. ESTIMATE: generous caps for a few thousand hours of play; a bigger value is refused, not clamped.
CAPS = {
    "XP": 50_000_000, "Favor": 5_000_000, "PlayerKill": 2_000_000, "Death": 2_000_000, "DeathByPlayer": 2_000_000,
    "Assist": 2_000_000, "GameOver": 200_000, "TeamVictory": 200_000, "FFA1stPlace": 200_000, "WinsTFR": 200_000,
    "WinsCTF": 200_000, "WinsHun": 200_000, "AIKill": 5_000_000, "Waves": 1_000_000, "GoldWave": 1_000_000,
    "FlagCapture": 500_000, "KillStreak10": 200_000,
}
# What one report may add to a counter over the previous one, per hour since that report (+ the first report is
# free: it brings the whole history). ESTIMATE from a match of ~10 min: XP <= ~3,000 per match, kills <= ~60.
GROWTH_PER_HOUR = {"XP": 60_000, "Favor": 20_000, "GameOver": 12, "TeamVictory": 12, "FFA1stPlace": 12}
GROWTH_DEFAULT = 1_000
MIN_INTERVAL = 30          # seconds between two accepted reports of one account

# Boards: (key, title, counters added up). INFERRED: "wins" = team wins + free-for-all first places.
BOARDS = [
    ("xp", "Experience", ("XP",)),
    ("favor", "Favor", ("Favor",)),
    ("kills", "Player kills", ("PlayerKill",)),
    ("wins", "Wins", ("TeamVictory", "FFA1stPlace")),
    ("matches", "Matches played", ("GameOver",)),
    ("waves", "Trial waves", ("Waves",)),
]
BOARD_KEYS = {b[0]: b for b in BOARDS}
STAT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{1,23}$")


class ReportError(Exception):
    pass


def week_of(ts):
    """ISO week of a unix time, e.g. '2026-W46' (weekly boards, 2026-11-15; weeks start Monday 00:00 UTC)."""
    import datetime
    y, w, _ = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isocalendar()
    return "%d-W%02d" % (y, w)


def parse_stats(s):
    """'XP:800,Favor:590' -> {'XP': 800, 'Favor': 590}. Unknown names are skipped; a bad or too big value refuses all."""
    out = {}
    if not s or len(s) > 600:
        raise ReportError("no stats" if not s else "too long")
    for part in s.split(","):
        name, _, val = part.partition(":")
        if not STAT_RE.match(name) or not val.isdigit() or len(val) > 12:
            raise ReportError("bad stat %r" % part[:40])
        if name not in CAPS:
            continue
        v = int(val)
        if v > CAPS[name]:
            raise ReportError("%s %d is above the cap %d" % (name, v, CAPS[name]))
        out[name] = v
    if not out:
        raise ReportError("no known stats")
    return out


class Leaderboard:
    def __init__(self, path):
        """path: a data directory (leaderboard.db inside), or ':memory:' for tests."""
        if path == ":memory:":
            self.db = sqlite3.connect(":memory:")
        else:
            os.makedirs(path, exist_ok=True)
            self.db = sqlite3.connect(os.path.join(path, "leaderboard.db"))
        self.db.execute("CREATE TABLE IF NOT EXISTS players (acct INTEGER PRIMARY KEY, name TEXT NOT NULL, "
                        "last TEXT NOT NULL, best TEXT NOT NULL, first INTEGER NOT NULL, updated INTEGER NOT NULL, "
                        "reports INTEGER NOT NULL, hidden INTEGER NOT NULL DEFAULT 0)")
        # weekly boards (2026-11-15): each account's counters at its first report of an ISO week; the week's value
        # is the newest report minus that start
        self.db.execute("CREATE TABLE IF NOT EXISTS weeks (acct INTEGER NOT NULL, week TEXT NOT NULL, "
                        "start TEXT NOT NULL, PRIMARY KEY (acct, week))")
        self.db.commit()
        self.cache = {}   # board -> (time, rows)

    def report(self, acct, name, stats, now=None):
        """Store one report. Returns the stored best values. Raises ReportError (too soon, implausible growth)."""
        now = int(now if now is not None else time.time())
        row = self.db.execute("SELECT last, best, updated, reports FROM players WHERE acct = ?", (acct,)).fetchone()
        if row is None:
            self.db.execute("INSERT INTO players (acct, name, last, best, first, updated, reports, hidden) "
                            "VALUES (?, ?, ?, ?, ?, ?, 1, 0)", (acct, name, json.dumps(stats), json.dumps(stats), now, now))
            self.db.execute("INSERT OR IGNORE INTO weeks (acct, week, start) VALUES (?, ?, ?)",
                            (acct, week_of(now), json.dumps(stats)))
            self.db.commit()
            self.cache.clear()
            return dict(stats)
        last, best, updated, reports = json.loads(row[0]), json.loads(row[1]), row[2], row[3]
        if now - updated < MIN_INTERVAL:
            raise ReportError("too soon")
        hours = max(now - updated, 0) / 3600.0
        for k, v in stats.items():
            grow = v - max(last.get(k, 0), best.get(k, 0))
            allowed = max(GROWTH_PER_HOUR.get(k, GROWTH_DEFAULT) * max(hours, 0.25), 0)
            if grow > allowed:
                raise ReportError("%s grew by %d in %.1f h (allowed %d)" % (k, grow, hours, allowed))
        for k, v in stats.items():
            best[k] = max(best.get(k, 0), v)
        self.db.execute("UPDATE players SET name = ?, last = ?, best = ?, updated = ?, reports = ? WHERE acct = ?",
                        (name, json.dumps(stats), json.dumps(best), now, reports + 1, acct))
        # a new week starts from the previous report's values, so the first match of the week counts
        self.db.execute("INSERT OR IGNORE INTO weeks (acct, week, start) VALUES (?, ?, ?)",
                        (acct, week_of(now), json.dumps(last)))
        self.db.commit()
        self.cache.clear()
        return best

    def top(self, board, n=50, excluded=(), period="all", now=None):
        """[(rank, name, value, updated)] for one board, best first; hidden players and `excluded` names left out.
        period "week": the gain since the account's first report of this ISO week."""
        key, _, fields = BOARD_KEYS[board]
        rows = []
        ex = {x.lower() for x in excluded}
        if period == "week":
            q = self.db.execute("SELECT p.name, p.last, w.start, p.updated FROM players p JOIN weeks w "
                                "ON w.acct = p.acct AND w.week = ? WHERE p.hidden = 0", (week_of(now or time.time()),))
        else:
            q = self.db.execute("SELECT name, best, NULL, updated FROM players WHERE hidden = 0")
        for name, cur, start, updated in q:
            if name.lower() in ex:
                continue
            c, s = json.loads(cur), (json.loads(start) if start else {})
            v = sum(max(int(c.get(f, 0)) - int(s.get(f, 0)), 0) for f in fields)
            if v > 0:
                rows.append((name, v, updated))
        rows.sort(key=lambda r: (-r[1], r[0].lower()))
        out, rank, prev = [], 0, None
        for i, (name, v, upd) in enumerate(rows[:max(1, min(n, 200))]):
            if v != prev:
                rank, prev = i + 1, v
            out.append({"rank": rank, "name": name, "value": v, "updated": upd})
        return out

    def player(self, name):
        row = self.db.execute("SELECT name, last, best, first, updated, reports, hidden FROM players "
                              "WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
        if not row:
            return None
        return {"name": row[0], "last": json.loads(row[1]), "best": json.loads(row[2]), "first": row[3],
                "updated": row[4], "reports": row[5], "hidden": bool(row[6])}

    def reset(self, name):
        self.db.execute("DELETE FROM weeks WHERE acct IN (SELECT acct FROM players WHERE name = ? COLLATE NOCASE)", (name,))
        n = self.db.execute("DELETE FROM players WHERE name = ? COLLATE NOCASE", (name,)).rowcount
        self.db.commit()
        self.cache.clear()
        return n

    def hide(self, name, hidden=True):
        n = self.db.execute("UPDATE players SET hidden = ? WHERE name = ? COLLATE NOCASE",
                            (1 if hidden else 0, name)).rowcount
        self.db.commit()
        self.cache.clear()
        return n

    def is_hidden(self, acct):
        row = self.db.execute("SELECT hidden FROM players WHERE acct = ?", (acct,)).fetchone()
        return bool(row and row[0])

    def count(self):
        return self.db.execute("SELECT COUNT(*), COALESCE(SUM(hidden), 0) FROM players").fetchone()

    def public(self, board=None, n=10, excluded=(), period="all"):
        """The JSON for GET /api/leaderboard: one board (n rows) or every board (top n each); period all | week."""
        boards = [board] if board else [b[0] for b in BOARDS]
        period = "week" if period == "week" else "all"
        return {"note": NOTE, "period": period, "week": week_of(time.time()),
                "boards": [{"key": k, "title": BOARD_KEYS[k][1], "rows": self.top(k, n, excluded, period)}
                           for k in boards]}


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Leaderboards</title>
<style>
:root{--bg:#14110f;--card:#1f1a17;--text:#efe6dc;--muted:#a89a8c;--accent:#d4a24c;--line:#3a312b}
@media (prefers-color-scheme: light){:root{--bg:#f6f1ea;--card:#fff;--text:#231c16;--muted:#6d6156;--accent:#9a6a12;--line:#e2d8cc}}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,Segoe UI,sans-serif}
main{max-width:1000px;margin:0 auto;padding:20px 16px}
h1{margin:0 0 4px;font-size:24px} .note{color:var(--muted);margin:0 0 18px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:14px}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
h2{font-size:16px;margin:0 0 8px;color:var(--accent)}
table{width:100%;border-collapse:collapse} td{padding:3px 2px;border-top:1px solid var(--line)}
td.r{width:2.5em;color:var(--muted)} td.v{text-align:right;font-variant-numeric:tabular-nums}
.empty{color:var(--muted)}
.tabs button{background:var(--card);color:var(--text);border:1px solid var(--line);border-radius:8px;padding:5px 12px;cursor:pointer}
.tabs button.on{border-color:var(--accent);color:var(--accent)}
</style></head><body><main>
<h1>Community leaderboards</h1>
<p class="note">__NOTE__ This server only. Updated after each saved match.</p>
<p class="tabs"><button id="ta">All time</button> <button id="tw">This week</button> <span id="wk" class="note"></span></p>
<div class="grid" id="g"></div>
</main><script>
function load(p){
document.getElementById('ta').className=p=='all'?'on':''; document.getElementById('tw').className=p=='week'?'on':'';
fetch('/api/leaderboard?n=25&period='+p).then(r=>r.json()).then(d=>{
  const g=document.getElementById('g'); g.textContent='';
  document.getElementById('wk').textContent=p=='week'?('Week '+d.week+' (Monday to Sunday, UTC): gains this week'):'';
  for(const b of d.boards){
    const s=document.createElement('section'); const h=document.createElement('h2'); h.textContent=b.title; s.appendChild(h);
    if(!b.rows.length){const p=document.createElement('p');p.className='empty';p.textContent='No reports yet.';s.appendChild(p);}
    else{const t=document.createElement('table');
      for(const r of b.rows){const tr=t.insertRow();
        const a=tr.insertCell();a.className='r';a.textContent=r.rank;
        tr.insertCell().textContent=r.name;
        const v=tr.insertCell();v.className='v';v.textContent=r.value.toLocaleString();}
      s.appendChild(t);}
    g.appendChild(s);}
}).catch(()=>{document.getElementById('g').textContent='Could not load the leaderboards.';});}
document.getElementById('ta').onclick=()=>load('all'); document.getElementById('tw').onclick=()=>load('week');
load(location.hash=='#week'?'week':'all');
</script></body></html>
""".replace("__NOTE__", NOTE)
