# GoW: Ascension community server: session + matchmaking logic (2026-11-13). Python 3 standard library only.
# Pure logic, no sockets: a Player object has a send(bytes, text) callback, so the unit tests drive it directly.
# The message layouts are VERIFIED static (sam_codec.py); the ORDER of the exchange below is INFERRED from the
# client's handlers and has not been checked against a running game:
#   ConnectRequest            -> ConnectResponse(service 0, session 0)
#   SessionBegin(service s)   -> ConnectResponse(service s, new session id)        (s = 1 Match, 2 Profile, 4)
#   SessionEnd(session, s)    -> the session is dropped, nothing is sent back
#   MatchRequest              -> the player joins the pool of its key; everyone in that pool gets a SearchUpdate
#                                listing the pool; once min_players are waiting they all get GameWorldReserved
#                                (same world id, same roster, peer index = position) and leave the pool
#   MatchPlayerReport         -> logged; players named with a non-zero flag are kept, the others dropped from the
#                                world, and the rest of the world gets a GameWorldUpdate   (meaning INFERRED)
#   disconnect                -> left pool / world; the others get a SearchUpdate / GameWorldUpdate
import itertools
import os
import time
import sam_codec as C


class Player:
    def __init__(self, send, log=None):
        self.send, self.log = send, (log or (lambda *a: None))
        self.acct = None
        self.online_id = None
        self.sessions = {}        # service -> session id
        self.request = None       # the parsed MatchRequest while searching
        self.world = None
        self.playing = None       # world id of the round being played after the game started (admin page only)
        self.playing_mode, self.playing_count = None, 0   # that round's mode and player count (presence)
        # 2026-11-17: this search began as Quick Match (flag6). The game then repeats a FIXED mode with flags 0 and
        # accepts another mode (MEASURED 2026-11-13); a player who PICKED a mode leaves a match in another mode
        # (MEASURED 2026-11-17, USA world 8215: SaitoRaijin, mode 4/4, put into a 2-player world by the fallback,
        # closed its connection 80 ms after GameWorldReserved). Only Quick Match players may be moved by the fallback.
        self.quick = False


class World:
    def __init__(self, wid, key16, players):
        self.id, self.key16, self.players = wid, key16, list(players)
        self.closed = False      # the game started: clients ended their Match session; no more roster messages
        self.mode = None         # the mode reserved for it (slot)

    def roster(self):
        # the first player of the world is its host (+0x61 = 01 00 00 00; MEASURED need, run 121141)
        out = [C.roster_record(p.request["players"][0]["raw"], i, host=(i == 0)) for i, p in enumerate(self.players)]
        # test only (--test-fake-players N, loopback servers): N made-up players after the real ones, copies of the
        # host's record with their own account id (+0..+8) and name (+8..+0x18), to reproduce 6/8-player matches
        # with one game window (2026-11-18: every 6-player world on the live servers failed).
        if TEST_FAKE_PLAYERS and self.players:
            raw = bytearray(self.players[0].request["players"][0]["raw"])
            for k in range(TEST_FAKE_PLAYERS):
                r = bytearray(raw)
                r[0:8] = (0x7E57000000000001 + k).to_bytes(8, "big")
                r[8:0x18] = (b"FakeP%d" % (k + 1)).ljust(16, b"\0")
                out.append(C.roster_record(bytes(r), len(out)))
        return out[:C.MAX_PLAYERS]


TEST_FAKE_PLAYERS = 0   # --test-fake-players N (gowa_server.py, loopback only)


# Quick Play modes, biggest first: (mode slot, min players to start, max) - host\mp\NOTES.md mode table (STATIC)
QUICK_MODES = ((5, 6, 8), (4, 4, 4), (8, 2, 2))


class Matchmaker:
    def __init__(self, min_players=2, max_players=C.MAX_PLAYERS, log=None, later=None,
                 update_repeats=(2, 5, 10, 20), world_mode=None, party_of=None, fallback_s=20, clock=None):
        self.min_players = max(1, min(int(min_players), C.MAX_PLAYERS))
        self.max_players = max(self.min_players, min(int(max_players), C.MAX_PLAYERS))
        self.log = log or (lambda *a: None)
        self.later = later                    # later(seconds, fn): the server's event loop; None in unit tests
        self.update_repeats = update_repeats
        self.world_mode = world_mode          # None = the host's requested mode; else this mode for every world
        self.party_of = party_of              # party_of(online_id) -> private party code or None
        self.fallback_s = fallback_s          # after this long, everyone waiting gets the biggest mode they can fill
        self.clock = clock or time.monotonic
        self.pools = {}           # key -> [Player]
        self.worlds = {}          # id -> World
        self._sid = itertools.count(0x1001)
        self._wid = itertools.count(0x2001)
        self.stats = {"logins": 0, "sessions": 0, "searches": 0, "worlds": 0}
        self.on_round_start = None   # fn(world_id, mode slot, [online ids]) when a world's round starts (2026-11-15)

    # ---- session service
    def on_connect(self, pl, acct, online_id):
        pl.acct, pl.online_id = acct, online_id
        self.stats["logins"] += 1
        pl.send(C.connect_response(acct, 0, 0), "ConnectResponse acct %d service 0 session 0" % acct)

    def on_session_begin(self, pl, service):
        if pl.acct is None:
            return False
        if service not in (C.SVC_MATCH, C.SVC_PROFILE, C.SVC_GAMETRACKER):
            # the client never asks for other services; a reply with session 0 would be its error 0x6A
            self.log("SessionBegin for service %d: not answered" % service)
            return False
        sid = pl.sessions.get(service) or next(self._sid)
        pl.sessions[service] = sid
        self.stats["sessions"] += 1
        # MEASURED 2026-11-13 (run 114215): a ConnectResponse here is "Junk msg from server serviceId=0 messageId=1".
        # VERIFIED: the running-state dispatcher 0x3CFA20 sends (0,2) to 0x3CF228, which reads u32 sessionId (0 =
        # error 0x6A) + u8 service and opens Match (1, 0x3D14A0), Profile (2, 0x3D387C) or service 4.
        pl.send(C.session_begin(sid, service), "SessionBegin reply service %d session %d" % (service, sid))
        return True

    def on_session_end(self, pl, session_id, service):
        if pl.sessions.get(service) == session_id:
            del pl.sessions[service]
            if service == C.SVC_MATCH:
                if pl.world is not None:
                    # MEASURED: clients end their Match session at the countdown ("Session 4097 down") and play on in
                    # their own mesh. The world is theirs now: no updates, no shrinking (research-102.md; a 1-player
                    # GameWorldUpdate here may be what sent the guest back to the lobby in run 124003).
                    w = pl.world
                    if not w.closed and self.on_round_start is not None:
                        try:   # 2026-11-15: Discord "match started" post etc.; never breaks matchmaking
                            self.on_round_start(w.id, w.mode, [q.online_id for q in w.players])
                        except Exception as e:
                            self.log("on_round_start failed: %s" % e)
                    w.closed = True
                    pl.quick = False    # the next search starts again from the player's own choice
                    pl.playing = w.id   # still playing that round in its own mesh (admin page: "in match")
                    pl.playing_mode, pl.playing_count = w.mode, getattr(w, "size", len(w.players))
                    w.players = [q for q in w.players if q is not pl]
                    if not w.players:
                        self.worlds.pop(w.id, None)
                    pl.world = None
                self.leave(pl)          # MEASURED 2026-11-13: clearing the request first left a ghost in the pool
            return True
        return False

    # ---- match service
    def on_match_request(self, pl, req):
        pl.playing = None                 # back to searching: the last round is over
        if C.SVC_MATCH not in pl.sessions:
            self.log("match request without a Match session: ignored")
            return False
        if len(req["players"]) != 1:
            # a party search (several records in one request) is not handled yet: answer "Unmatchable"
            pl.send(C.game_world_reserved(0, 2), "GameWorldReserved result 2 (party search not supported yet)")
            return False
        rec = req["players"][0]
        if rec["acct"] != pl.acct or (pl.online_id and rec["name"].lower() != pl.online_id.lower()):
            # Fair play (2026-11-13): the record a player sends about itself must be its signed-in account; a
            # different account id or name would let it pose as someone else in the roster. Result 10 =
            # MatchResultCode InvalidRequest (the client's own name for it).
            self.log("match request with a record for %r/%d from account %d (%r): refused"
                     % (rec["name"], rec["acct"], pl.acct, pl.online_id))
            pl.send(C.game_world_reserved(0, 10), "GameWorldReserved result 10 (InvalidRequest: not your record)")
            return False
        if req.get("flag6"):
            pl.quick = True
        self.leave(pl)
        # Private matches (2026-11-13): a player with a party code (runtime PS3_MPNET_PARTY -> /hello?party=, later a
        # Discord invite) searches only among players with the same code; everyone else uses the public queue.
        party = self.party_of(pl.online_id) if (self.party_of and pl.online_id) else None
        # Same game data only (2026-11-15, MEASURED on the Europe server): the shard key carries the game's data hash
        # (mods change it). Two players with different data were matched, and the second one's game left the match
        # within a second, every time. So the shard is part of the pool, and a party mate on other data is logged.
        shard = req.get("shard", 0)
        if party:
            others = [q for k, ps in self.pools.items() if isinstance(k, tuple) and k[:2] == ("party", party)
                      for q in ps if q.request.get("shard", 0) != shard]
            for q in others:
                self.log("%s and %s share party %s but have different game data (%08X vs %08X): mods on online or "
                         "another game version; they cannot play together" % (pl.online_id, q.online_id, party,
                         shard >> 32, q.request.get("shard", 0) >> 32))
            req = dict(req, pool=("party", party, shard, req["pool"]))
            self.log("%s searches in private party %s" % (pl.online_id, party))
        else:
            req = dict(req, pool=(shard, req["pool"]))
        pl.request = req
        pl.search_t = self.clock()
        self.stats["searches"] += 1
        pool = self.pools.setdefault(req["pool"], [])
        pool.append(pl)
        self._search_update(pool)
        self._try_reserve(req["pool"])
        if self.later and self.fallback_s:   # look again when this search has waited long enough
            self.later(self.fallback_s + 0.2, lambda k=req["pool"]: self._try_reserve(k))
        return True

    def _fit(self, pool):
        """(mode, members) to reserve now, or None.
        Each search request carries its mode (slot), that mode's min/max players (envelope a/b; MEASURED: THUN2 2/2,
        mode 6 4/4, mode 5 6/8) and flag6 = "any mode" (VERIFIED 0x3D1714: set when the player's mode ctx+0x31 is -1,
        i.e. Quick Match; MEASURED flags 0x40). An any-mode player accepts the mode the server reserves ("requested
        game mode:5, using game mode:6"); a player who picked a mode must get exactly that one (else error 0x71).
        Candidates are tried oldest request first; a mode starts when enough compatible players wait (at least its
        min and --min-players), with up to its max. --mode forces one mode for everyone (tests)."""
        if self.world_mode is not None:
            return (self.world_mode, pool[:self.max_players]) if len(pool) >= self.min_players else None
        for p in pool:
            r = p.request
            mode = r["slot"]
            lo, hi = max(1, r["a"] or 1), max(1, r["b"] or C.MAX_PLAYERS)
            ok = [q for q in pool if q.request["flag6"] or q.request["slot"] == mode]
            if p in ok and len(ok) >= max(lo, self.min_players):
                return mode, ok[:min(hi, self.max_players)]
        # Quick Match players ask for a RANDOM mode each, so two of them could wait forever for a 4- or 8-player mode
        # (found 2026-11-13 before the first public tests). When enough any-mode players wait, the server picks the
        # biggest Quick Play mode they can fill: 2 -> Trial of the Gods (2), 4-5 -> Favour of the Gods (4), 6+ ->
        # Favour of the Gods (8). They accept it ("requested game mode:5, using game mode:6", MEASURED).
        anym = [q for q in pool if q.request["flag6"]]
        n = len(anym)
        if n >= self.min_players:
            for mode, lo, hi in QUICK_MODES:
                if n >= lo:
                    return mode, anym[:min(hi, self.max_players)]
        # Nobody can be matched as asked, and someone has waited fallback_s: after its first search the game repeats a
        # FIXED mode (MEASURED 2026-11-13: the laptop asked for mode 4/3 again and again and accepted mode 8 when matched),
        # so two players can wait forever for 4- and 8-player modes. Everyone waiting who started as Quick Match gets
        # the biggest mode they fill; a player who picked a mode is never moved (2026-11-17, see Player.quick).
        flex = [q for q in pool if q.request["flag6"] or getattr(q, "quick", False)]
        if self.fallback_s and len(flex) >= self.min_players and \
                any(self.clock() - getattr(q, "search_t", 0) >= self.fallback_s for q in flex):
            for mode, lo, hi in QUICK_MODES:
                if len(flex) >= lo:
                    return mode, flex[:min(hi, self.max_players)]
        return None

    def _try_reserve(self, key):
        pool = self.pools.get(key, [])
        fit = self._fit(pool)
        if fit:
            self._reserve(key, fit[0], fit[1])
            self._try_reserve(key)            # the rest may form another game

    def _search_update(self, pool):
        if not pool:
            return
        names = [p.request["players"][0]["name16"] for p in pool[:C.MAX_PLAYERS]]
        for p in pool:
            p.send(C.search_update(0, names), "SearchUpdate playerCount %d" % len(names))

    def _reserve(self, key, mode, members):
        pool = self.pools.get(key, [])
        rest = [q for q in pool if q not in members]
        self.pools[key] = rest
        if not rest:
            del self.pools[key]
        w = World(next(self._wid), os.urandom(16), members)
        w.mode = mode
        w.size = len(members)   # players at match start (presence: the count while the round is played)
        self.worlds[w.id] = w
        self.stats["worlds"] += 1
        roster = w.roster()
        for p in members:
            p.world = w
            # Quick Match: each client asks with its own random mode (MEASURED: 6 and 5 in run 122952, so pools per
            # mode never matched) but accepts the one the server reserves ("requested game mode:N, using game mode:N"):
            # pool per key only, and the whole world plays the host's mode.
            p.send(C.game_world_reserved(w.id, 0, w.key16, mode, roster),
                   "GameWorldReserved world %d result 0, %d players" % (w.id, len(roster)))
        # MEASURED 2026-11-13 (snapshot of run 120419): after GameWorldReserved alone the mesh sits in state 2 with
        # its host state machine at 1. VERIFIED: GameWorldUpdate (0x3D33CC -> 0x1A981C) appends the roster to the
        # mesh's pending-peer list, which the state-2 loop 0x1A3850 hands to ProcessNewPeer (0x1A33C0), ourselves
        # included. So the roster follows at once as a GameWorldUpdate.
        self._world_update(w)
        # MEASURED 2026-11-13 (run 120745): the first GameWorldUpdate is handled before the client has built its
        # transport/mesh, and the mesh still never leaves state 2. Repeat it while the world lives (experiment).
        if self.later:
            for t in self.update_repeats:
                self.later(t, lambda w=w: self._world_update(w) if self.worlds.get(w.id) is w else None)
        self._search_update(rest)

    def _world_update(self, w):
        if w.closed:
            return
        roster = w.roster()
        for p in w.players:
            p.send(C.game_world_update(w.id, roster), "GameWorldUpdate world %d, %d players" % (w.id, len(roster)))

    def on_player_report(self, pl, rep):
        w = pl.world
        if not w or w.id != rep["world_id"]:
            self.log("player report for world %d from a player not in it: ignored" % rep["world_id"])
            return False
        gone = {a for a, f in zip(rep["accts"], rep["flags"]) if f == 0}
        self._drop(w, [p for p in w.players if p.acct in gone and p is not pl])
        return True

    def _drop(self, w, leaving):
        if not leaving:
            return
        for p in leaving:
            p.world = None
        w.players = [p for p in w.players if p not in leaving]
        if not w.players:
            self.worlds.pop(w.id, None)
            return
        if w.closed:
            return
        roster = w.roster()
        for p in w.players:
            p.send(C.game_world_update(w.id, roster), "GameWorldUpdate world %d, %d players" % (w.id, len(roster)))

    def leave(self, pl):
        """The player stops searching and leaves its world (disconnect, session end, a new search)."""
        if pl.request is not None:
            pool = self.pools.get(pl.request["pool"])
            if pool and pl in pool:
                pool.remove(pl)
                if pool:
                    self._search_update(pool)
                else:
                    del self.pools[pl.request["pool"]]
        for key in [k for k, pool in self.pools.items() if pl in pool]:   # any leftover entry, whatever its request
            self.pools[key] = [q for q in self.pools[key] if q is not pl]
            if not self.pools[key]:
                del self.pools[key]
        if pl.world is not None:
            self._drop(pl.world, [pl])
        pl.request = None
