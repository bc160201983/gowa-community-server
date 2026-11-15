# GoW: Ascension community server: admin web page + JSON API (2026-11-13). Python 3 standard library only.
#
#   http://127.0.0.1:<http port>/admin        the page (it asks once for the admin key)
#
# Who may use it: requests from the server PC itself (loopback) that carry the admin key (header X-Admin-Key). The key
# is made on first start in <data>\admin.key (32 hex chars). Even with --lan the admin pages refuse other PCs.
#
# API (JSON):
#   GET  /admin/api/overview                     players online / searching / in a match, matches, accounts, banned
#   GET  /admin/api/players                      every account + live state (offline / lobby / searching / in match)
#   POST /admin/api/ban    {"name": N}           ban (and kick if online)       /unban {"name": N}
#   POST /admin/api/kick   {"name": N}           close the player's connection (they can sign in again)
#   GET  /admin/api/servers                      the community server list      (public copy: GET /api/servers)
#   POST /admin/api/servers/add   {"name","address","region","note"}      /remove {"id": I}
#   POST /admin/api/servers/check {"id": I}      fetch that server's /api/status (an outgoing request, on click only)
#   POST /admin/api/lb/hide {"name": N}  /lb/show  leave a player off the public leaderboards (or list them again)
#   POST /admin/api/lb/reset {"name": N}          delete a player's reported stats (the next report starts over)
#   GET  /admin/api/lb?name=N                     a player's reported stats (last, best, reports)
import hmac, ipaddress, json, os, re, secrets, socket, time, urllib.request

ADDRESS_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}(:[0-9]{1,5})?$")


class Admin:
    def __init__(self, data_dir, accounts, online, matchmaker, stats, version):
        self.accounts, self.online, self.mm, self.stats, self.version = accounts, online, matchmaker, stats, version
        self.t0 = time.time()
        if data_dir:
            os.makedirs(data_dir, exist_ok=True)
            kf = os.path.join(data_dir, "admin.key")
            if not os.path.exists(kf):
                with open(kf, "w") as f:
                    f.write(secrets.token_hex(16))
            with open(kf) as f:
                self.key = f.read().strip()
        else:
            self.key = secrets.token_hex(16)   # accounts off (tests): a key nobody knows
        db = accounts.db if accounts is not None else None
        if db is not None:
            db.execute("CREATE TABLE IF NOT EXISTS servers (id INTEGER PRIMARY KEY, name TEXT NOT NULL, "
                       "address TEXT NOT NULL, region TEXT NOT NULL, note TEXT NOT NULL, added INTEGER NOT NULL)")
            db.commit()
        self.db = db

    # ---- helpers
    local_ip = ""   # the server's own LAN address (--lan): a request from it comes from the server PC itself

    def local(self, peer_ip):
        try:
            return ipaddress.ip_address(peer_ip).is_loopback or bool(self.local_ip and peer_ip == self.local_ip)
        except ValueError:
            return False

    # Wrong-key lockout (2026-11-13, admin page reachable over HTTPS through a local reverse proxy): per real client
    # address (X-Forwarded-For, trusted only from the proxy on this machine), 8 wrong keys -> 15 minutes locked.
    _fails = {}

    def _client(self, peer_ip, hdrs):
        fwd = hdrs.get("x-forwarded-for", "")
        if fwd and self.local(peer_ip):
            return fwd.split(",")[0].strip()[:64]
        return peer_ip

    def allowed(self, peer_ip, hdrs):
        if not self.local(peer_ip):
            return False
        who, now = self._client(peer_ip, hdrs), time.time()
        n, until = self._fails.get(who, (0, 0))
        if until > now:
            return False
        if hmac.compare_digest(hdrs.get("x-admin-key", ""), self.key):
            self._fails.pop(who, None)
            return True
        n += 1
        self._fails[who] = (0, now + 900) if n >= 8 else (n, 0)
        if len(self._fails) > 5000:
            self._fails.clear()
        return False

    def _state(self, conn):
        pl = conn.player
        if pl.world is not None or getattr(pl, "playing", None) is not None:
            return "in match"
        if pl.request is not None:
            return "searching"
        return "lobby"

    def _matches(self):
        """Worlds still being formed plus rounds being played (the server closes a world when its round starts)."""
        ids = set(self.mm.worlds) if self.mm else set()
        ids |= {c.player.playing for c in list(self.online.values()) if getattr(c.player, "playing", None) is not None}
        return len(ids)

    leaderboard = None   # leaderboard.Leaderboard, set by gowa_server (2026-11-14)

    def _lb_state(self, name):
        p = self.leaderboard.player(name) if self.leaderboard is not None and name else None
        return "" if p is None else ("hidden" if p["hidden"] else "listed")

    # set by gowa_server after start (no import cycle): party_of(online_id) -> code, mode_names {slot: (name, max)}
    party_of = staticmethod(lambda oid: None)
    mode_names = {}

    def _mode(self, slot):
        return self.mode_names.get(slot, ("", 0))[0] if slot is not None else ""

    def live(self):
        """Matches being formed or played, and who is searching (2026-11-13 dashboard)."""
        matches = {}
        if self.mm:
            for w in list(self.mm.worlds.values()):
                matches[w.id] = {"id": w.id, "mode": self._mode(w.mode), "state": "playing" if w.closed else "starting",
                                 "players": [p.online_id for p in w.players]}
        for c in list(self.online.values()):
            pl = c.player
            wid = getattr(pl, "playing", None)
            if wid is not None and pl.world is None:
                m = matches.setdefault(wid, {"id": wid, "mode": self._mode(pl.playing_mode), "state": "playing",
                                             "players": []})
                if c.online_id not in m["players"]:
                    m["players"].append(c.online_id)
        for m in matches.values():
            m["party"] = next((self.party_of(n) for n in m["players"] if self.party_of(n)), "") or ""
        searching = []
        if self.mm:
            for pool in list(self.mm.pools.values()):
                for pl in pool:
                    r = pl.request or {}
                    searching.append({"name": pl.online_id, "mode": "any" if r.get("flag6") else self._mode(r.get("slot")),
                                      "party": self.party_of(pl.online_id) or "",
                                      "waiting_s": int(self.mm.clock() - getattr(pl, "search_t", self.mm.clock()))})
        return {"matches": sorted(matches.values(), key=lambda m: m["id"]), "searching": searching}

    def overview(self):
        states = [self._state(c) for c in list(self.online.values())]
        accts = banned = 0
        if self.accounts is not None:
            accts, banned = self.accounts.db.execute("SELECT COUNT(*), COALESCE(SUM(banned), 0) FROM accounts").fetchone()
        return {"version": self.version, "uptime_s": int(time.time() - self.t0),
                "online": len(states), "lobby": states.count("lobby"), "searching": states.count("searching"),
                "in_match": states.count("in match"), "matches": self._matches(),
                "accounts": accts, "banned": banned, "sign_ins": self.stats.get("sam_logins", 0),
                "relayed": self.stats.get("relay_packets", 0)}

    def players(self):
        out, seen = [], set()
        for acct, c in list(self.online.items()):
            seen.add(acct)
            pl = c.player
            slot = (pl.world.mode if pl.world is not None else getattr(pl, "playing_mode", None)
                    if getattr(pl, "playing", None) is not None else
                    (None if (pl.request or {}).get("flag6") else (pl.request or {}).get("slot")))
            out.append({"name": c.online_id, "acct": str(acct), "state": self._state(c), "address": c.peer[0],
                        "since": int(c.t_login), "banned": False, "created": None, "last_seen": int(time.time()),
                        "mode": self._mode(slot), "party": self.party_of(c.online_id) or "",
                        "lb": self._lb_state(c.online_id)})
        if self.accounts is not None:
            for name, acct, created, last, banned in self.accounts.db.execute(
                    "SELECT name, acct, created, last_seen, banned FROM accounts ORDER BY last_seen DESC LIMIT 2000"):
                if acct in seen:
                    for p in out:
                        if p["acct"] == str(acct):
                            p.update(banned=bool(banned), created=created, name=name)
                    continue
                out.append({"name": name, "acct": str(acct), "state": "offline", "address": "", "since": None,
                            "banned": bool(banned), "created": created, "last_seen": last,
                            "lb": self._lb_state(name)})
        return out

    def kick(self, name):
        n = 0
        for acct, c in list(self.online.items()):
            if (c.online_id or "").lower() == name.lower():
                c.kick()
                n += 1
        return n

    def servers(self):
        if self.db is None:
            return []
        return [{"id": i, "name": n, "address": a, "region": r, "note": t, "added": d}
                for i, n, a, r, t, d in self.db.execute("SELECT id, name, address, region, note, added FROM servers "
                                                        "ORDER BY region, name")]

    @staticmethod
    def check_server(address):
        host, _, port = address.partition(":")
        url = "http://%s:%s/api/status" % (host, port or "36101")
        try:
            with urllib.request.urlopen(url, timeout=4) as r:
                st = json.loads(r.read(4096).decode("utf-8", "replace"))
            return {"ok": True, "status": {k: st.get(k) for k in ("name", "version", "players_online", "searching",
                                                                   "matches", "accounts")}}
        except (OSError, ValueError, socket.timeout) as e:
            return {"ok": False, "error": str(e)[:200]}

    # ---- entry point: returns (code, content type, body bytes)
    def handle(self, method, path, hdrs, body, peer_ip):
        p = path.split("?")[0]
        if p in ("/admin", "/admin/"):
            if not self.local(peer_ip):
                return 403, "text/plain", b"admin pages are only available on the server PC\n"
            return 200, "text/html; charset=utf-8", PAGE.encode("utf-8")
        if not self.allowed(peer_ip, hdrs):
            return 403, "application/json", b'{"error": "admin key required (server PC only)"}'
        try:
            req = json.loads(body.decode("utf-8")) if body else {}
            if not isinstance(req, dict):
                raise ValueError
        except ValueError:
            return 400, "application/json", b'{"error": "bad JSON"}'
        name = str(req.get("name", ""))[:16]
        ok = lambda d: (200, "application/json", json.dumps(d).encode())
        if method == "GET" and p == "/admin/api/overview":
            return ok(self.overview())
        if method == "GET" and p == "/admin/api/live":
            return ok(self.live())
        if method == "GET" and p == "/admin/api/players":
            return ok({"players": self.players()})
        if method == "POST" and p in ("/admin/api/ban", "/admin/api/unban"):
            if self.accounts is None or not self.accounts.db.execute(
                    "SELECT 1 FROM accounts WHERE name = ?", (name,)).fetchone():
                return 404, "application/json", b'{"error": "no such account"}'
            self.accounts.ban(name, p.endswith("/ban"))
            kicked = self.kick(name) if p.endswith("/ban") else 0
            return ok({"done": True, "kicked": kicked})
        if method == "POST" and p == "/admin/api/kick":
            return ok({"kicked": self.kick(name)})
        if p.startswith("/admin/api/lb") and self.leaderboard is None:
            return 404, "application/json", b'{"error": "leaderboards off"}'
        if method == "GET" and p == "/admin/api/lb":
            q = dict(kv.split("=", 1) for kv in (path.split("?", 1) + [""])[1].split("&") if "=" in kv)
            st = self.leaderboard.player(q.get("name", "")[:16])
            return ok(st) if st else (404, "application/json", b'{"error": "no reports from this player"}')
        if method == "POST" and p in ("/admin/api/lb/hide", "/admin/api/lb/show"):
            return ok({"changed": self.leaderboard.hide(name, p.endswith("/hide"))})
        if method == "POST" and p == "/admin/api/lb/reset":
            return ok({"deleted": self.leaderboard.reset(name)})
        if method == "GET" and p == "/admin/api/servers":
            return ok({"servers": self.servers()})
        if method == "POST" and p == "/admin/api/servers/add" and self.db is not None:
            sname = str(req.get("name", "")).strip()[:40]
            addr = str(req.get("address", "")).strip()[:260]
            region = str(req.get("region", "")).strip()[:24] or "Other"
            note = str(req.get("note", "")).strip()[:120]
            if not sname or not ADDRESS_RE.match(addr):
                return 400, "application/json", b'{"error": "name and a host or host:port address are needed"}'
            self.db.execute("INSERT INTO servers (name, address, region, note, added) VALUES (?, ?, ?, ?, ?)",
                            (sname, addr, region, note, int(time.time())))
            self.db.commit()
            return ok({"servers": self.servers()})
        if method == "POST" and p == "/admin/api/servers/remove" and self.db is not None:
            self.db.execute("DELETE FROM servers WHERE id = ?", (int(req.get("id", 0) or 0),))
            self.db.commit()
            return ok({"servers": self.servers()})
        if method == "POST" and p == "/admin/api/servers/check" and self.db is not None:
            row = self.db.execute("SELECT address FROM servers WHERE id = ?", (int(req.get("id", 0) or 0),)).fetchone()
            if not row:
                return 404, "application/json", b'{"error": "no such server"}'
            return "CHECK", row[0], None     # the caller runs check_server() off the event loop
        return 404, "application/json", b'{"error": "unknown admin call"}'


# The page itself (2026-11-13 redesign: dashboard with live matches and the search queue, players, server list):
# admin_page.html next to this file, read once at start.
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "admin_page.html"), encoding="utf-8") as _f:
    PAGE = _f.read()

