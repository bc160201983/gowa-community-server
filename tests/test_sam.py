# Unit tests for the community server's SAM codec and matchmaking (2026-11-13). Synthetic frames only: no game.
#   python -I test_sam.py
# The size rules asserted here are the ones the game's own parsers enforce (addresses in sam_codec.py).
import argparse, asyncio, os, struct, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server"))
import sam_codec as C      # noqa: E402
import sam_match           # noqa: E402
import gowa_server         # noqa: E402


def player_record(acct, name):
    r = struct.pack(">Q", acct) + name.encode().ljust(36, b"\0") + bytes(range(0x61 - 44))
    assert len(r) == C.PLAYER_REC
    return r


def ticket(acct, name):
    return b"MPN1" + struct.pack(">IQ", 1, acct) + name.encode().ljust(32, b"\0") + b"\0" * 16


def connect_request(acct, name):
    t = ticket(acct, name)
    return C.frame(0, 0, struct.pack(">QQIHBB", 111, 9, 3, len(t), 0, 1) + t)


class FakeSink:
    def __init__(self):
        self.frames = []

    def __call__(self, data, what):
        fr, rest = C.split_frames(data)
        assert rest == b""
        self.frames += fr

    def take(self):
        f, self.frames = self.frames, []
        return f


class CodecTests(unittest.TestCase):
    def test_frame_split(self):
        buf = b"\0\0" + C.frame(1, 0x17, b"abc") + C.frame(0, 1, b"")[:3]
        fr, rest = C.split_frames(buf)
        self.assertEqual(fr, [None, (1, 0x17, b"abc")])
        self.assertEqual(rest, C.frame(0, 1, b"")[:3])
        with self.assertRaises(C.CodecError):
            C.split_frames(b"\0\3xx")
        with self.assertRaises(C.CodecError):
            C.frame(0, 0, b"x" * C.MAX_FRAME)

    def test_connect_response_layout(self):
        f = C.connect_response(0x0123456789ABCDEF, 0x1001, 1)
        self.assertEqual(f.hex(), "00140001" + "0123456789abcdef" + "00001001" + "01000000")
        r = C.parse_connect_response(f[4:])
        self.assertEqual((r["acct"], r["session_id"], r["service"], r["response"]), (0x0123456789ABCDEF, 0x1001, 1, 0))

    def test_session_messages(self):
        self.assertEqual(C.parse_session_begin(C.session_begin(7, 2)[4:]), {"value": 7, "service": 2})
        self.assertEqual(C.parse_session_end(C.session_end(0x1001, 1, 3)[4:]),
                         {"session_id": 0x1001, "service": 1, "reason": 3})
        self.assertEqual(C.parse_session_data(struct.pack(">HB", 3, 9) + b"xyz"), {"kind": 9, "data": b"xyz"})

    def test_match_request_roundtrip(self):
        rec = player_record(42, "MpTestA")
        f = C.match_request(9, -1, [rec], ids=[5, 6], app_data=b"APP", a=1, b=2, c=3, d=4, flag7=1)
        (svc, msg, p), = C.split_frames(f)[0]
        self.assertEqual((svc, msg), (1, 1))
        # the game's writer: 0x10 header + 8 per id + 0x61 per player + application data
        self.assertEqual(struct.unpack(">H", p[:2])[0], 0x10 + 16 + 0x61 + 3)
        m = C.parse_match_request(p)
        self.assertEqual((m["key"], m["slot"], m["ids"], m["app_data"], m["flag7"], m["flag6"]),
                         (9, -1, [5, 6], b"APP", 1, 0))
        self.assertEqual((m["a"], m["b"], m["c"], m["d"]), (1, 2, 3, 4))
        self.assertEqual((m["players"][0]["acct"], m["players"][0]["name"], m["players"][0]["raw"]), (42, "MpTestA", rec))
        with self.assertRaises(C.CodecError):
            C.parse_match_request(p[:-10] if False else p[:40])

    def test_search_update_size_rule(self):
        f = C.search_update(0, [b"MpTestA", b"MpTestB"])
        p = f[4:]
        n = struct.unpack(">H", p[:2])[0]
        # client: len >= 2, (len - 2) % 16 == 0, count == (len - 2) / 16, 1 <= count <= 8
        self.assertEqual((n, (n - 2) % 16, p[3], (n - 2) // 16), (34, 0, 2, 2))
        self.assertEqual(C.parse_search_update(p)["names16"][1], b"MpTestB".ljust(16, b"\0"))
        with self.assertRaises(C.CodecError):
            C.search_update(0, [])
        with self.assertRaises(C.CodecError):
            C.search_update(0, [b"x"] * 9)

    def test_game_world_reserved_size_rule(self):
        roster = [C.roster_record(player_record(1, "A"), 0), C.roster_record(player_record(2, "B"), 1)]
        f = C.game_world_reserved(0x2001, 0, b"K" * 16, -1, roster)
        p = f[4:]
        wid, n, result = struct.unpack(">IHB", p[:7])
        # client: len == 0x13 + 0x67 * count, count at header +0x11, flags at +0x12
        self.assertEqual((wid, result, n, p[7 + 0x11], p[7 + 0x12]), (0x2001, 0, 0x13 + 2 * 0x67, 2, 0))
        g = C.parse_game_world_reserved(p)
        self.assertEqual((g["world_key16"], g["slot"], g["count"], g["roster"]), (b"K" * 16, -1, 2, roster))
        self.assertEqual(roster[1][0x65], 1)                         # peer index
        self.assertEqual(roster[1][:0x61], player_record(2, "B"))    # the client's own record, unchanged
        err = C.game_world_reserved(0, 1)                            # Timeout: no blob
        self.assertEqual(err[4:], struct.pack(">IHB", 0, 0, 1))
        self.assertEqual(C.RESULT_CODES[1], "Timeout")

    def test_game_world_update_size_rule(self):
        roster = [C.roster_record(player_record(1, "A"), 0)]
        p = C.game_world_update(7, roster)[4:]
        wid, n = struct.unpack(">IH", p[:6])
        self.assertEqual((wid, n, p[6]), (7, 1 + 0x67, 1))           # client: len == 1 + 0x67 * count
        self.assertEqual(C.parse_game_world_update(p)["roster"], roster)

    def test_player_report(self):
        p = struct.pack(">IB", 0x2001, 2) + bytes([1, 0]) + struct.pack(">QQ", 11, 22)
        self.assertEqual(C.parse_player_report(p), {"world_id": 0x2001, "flags": [1, 0], "accts": [11, 22]})

    def test_describe_never_raises(self):
        for key in list(C.MESSAGES) + [(9, 9)]:
            self.assertIsInstance(C.describe(key[0], key[1], b"\x01"), str)


class MatchTests(unittest.TestCase):
    def mk(self, mm, acct, name):
        sink = FakeSink()
        pl = sam_match.Player(sink)
        mm.on_connect(pl, acct, name)
        (s, m, p), = sink.take()
        self.assertEqual((s, m), (0, 1))
        r = C.parse_connect_response(p)
        self.assertEqual((r["acct"], r["service"], r["session_id"], r["response"]), (acct, 0, 0, 0))
        return pl, sink

    def search(self, mm, pl, name, key=9, slot=-1, any_mode=0, shard=0):
        (s, m, p), = C.split_frames(C.match_request(key, slot, [player_record(pl.acct, name)], flag6=any_mode,
                                                     shard=shard))[0]
        return mm.on_match_request(pl, C.parse_match_request(p))

    def test_session_begin_and_end(self):
        mm = sam_match.Matchmaker()
        pl, sink = self.mk(mm, 1, "A")
        self.assertTrue(mm.on_session_begin(pl, C.SVC_MATCH))
        (s, m, p), = sink.take()
        self.assertEqual((s, m), (0, 2))                             # (0,2), not a ConnectResponse (MEASURED: junk)
        r = C.parse_session_begin(p)
        r["session_id"] = r.pop("value")
        self.assertEqual(r["service"], 1)
        self.assertNotEqual(r["session_id"], 0)                      # session 0 is the client's error 0x6A
        self.assertFalse(mm.on_session_begin(pl, 3))                 # a service the client never asks for
        self.assertEqual(sink.take(), [])                            # ... gets no answer
        self.assertFalse(mm.on_session_end(pl, 999, 1))
        self.assertTrue(mm.on_session_end(pl, r["session_id"], 1))
        self.assertEqual(sink.take(), [])                            # nothing is sent back for a session end

    def test_search_needs_match_session(self):
        mm = sam_match.Matchmaker()
        pl, sink = self.mk(mm, 1, "A")
        self.assertFalse(self.search(mm, pl, "A"))
        self.assertEqual(sink.take(), [])

    def test_two_players_one_world(self):
        mm = sam_match.Matchmaker(min_players=2)
        a, sa = self.mk(mm, 1, "MpTestA")
        b, sb = self.mk(mm, 2, "MpTestB")
        for pl, sink in ((a, sa), (b, sb)):
            mm.on_session_begin(pl, C.SVC_MATCH)
            sink.take()
        self.search(mm, a, "MpTestA")
        (s, m, p), = sa.take()
        self.assertEqual((s, m, len(C.parse_search_update(p)["names16"])), (1, 0x17, 1))
        self.search(mm, b, "MpTestB")
        fa, fb = sa.take(), sb.take()
        # SearchUpdate, GameWorldReserved, then the roster as GameWorldUpdate (MEASURED need, run 120419)
        self.assertEqual([(s, m) for s, m, _ in fa], [(1, 0x17), (1, 0x15), (1, 0x16)])
        self.assertEqual([(s, m) for s, m, _ in fb], [(1, 0x17), (1, 0x15), (1, 0x16)])
        ua = C.parse_game_world_update(fa[2][2])
        self.assertEqual((ua["world_id"], len(ua["roster"])), (C.parse_game_world_reserved(fa[1][2])["world_id"], 2))
        self.assertEqual(len(C.parse_search_update(fa[0][2])["names16"]), 2)
        ga, gb = C.parse_game_world_reserved(fa[1][2]), C.parse_game_world_reserved(fb[1][2])
        self.assertEqual((ga["world_id"], ga["world_key16"], ga["roster"]), (gb["world_id"], gb["world_key16"], gb["roster"]))
        self.assertEqual((ga["result"], ga["count"], ga["slot"], gb["slot"]), (0, 2, -1, -1))  # the host's mode
        self.assertEqual([struct.unpack(">Q", r[:8])[0] for r in ga["roster"]], [1, 2])
        self.assertEqual([r[0x65] for r in ga["roster"]], [0, 1])    # distinct peer indexes
        self.assertEqual([r[0x61:0x65].hex() for r in ga["roster"]], ["01000000", "00000000"])  # host mark
        self.assertEqual(mm.pools, {})
        # B disconnects: A gets a GameWorldUpdate with one player
        mm.leave(b)
        (s, m, p), = sa.take()
        u = C.parse_game_world_update(p)
        self.assertEqual((s, m, u["world_id"], len(u["roster"])), (1, 0x16, ga["world_id"], 1))
        mm.leave(a)
        self.assertEqual(mm.worlds, {})

    def started(self, mm, *names):
        out = []
        for i, n in enumerate(names):
            pl, sink = self.mk(mm, i + 1, n)
            mm.on_session_begin(pl, C.SVC_MATCH)
            sink.take()
            out.append((pl, sink))
        return out

    def test_private_party_only_meets_its_own_code(self):
        parties = {"a": "WOLF42", "c": "WOLF42"}
        mm = sam_match.Matchmaker(min_players=2, party_of=lambda oid: parties.get(oid.lower()))
        (a, sa), (b, sb), (c, sc) = self.started(mm, "A", "B", "C")
        self.search(mm, a, "A", slot=8, any_mode=1)
        self.search(mm, b, "B", slot=8, any_mode=1)      # public queue: must not join A's party
        self.assertFalse(any(m == 0x15 for _, m, _p in sa.take() + sb.take()))
        self.search(mm, c, "C", slot=8, any_mode=1)      # same party code as A: matched
        fa, fc = sa.take(), sc.take()
        self.assertIn(0x15, [m for _, m, _p in fa])
        self.assertIn(0x15, [m for _, m, _p in fc])
        self.assertEqual(C.parse_game_world_reserved([p for _, m, p in fa if m == 0x15][0])["count"], 2)
        self.assertEqual(len(mm.pools), 1)                # only B still waits, in the public pool

    def test_picked_mode_waits_for_its_players(self):
        mm = sam_match.Matchmaker(min_players=2)
        (a, sa), (b, sb) = self.started(mm, "A", "B")
        # both picked Favour of the Gods (4) from the Game Mode list: min 4 players -> no world with 2
        for pl, n in ((a, "A"), (b, "B")):
            (s, m, p), = C.split_frames(C.match_request(9, 4, [player_record(pl.acct, n)], a=4, b=4))[0]
            mm.on_match_request(pl, C.parse_match_request(p))
        self.assertFalse(any(m == 0x15 for _, m, _p in sa.take() + sb.take()))

    def test_picked_mode_and_quick_match_meet(self):
        mm = sam_match.Matchmaker(min_players=2)
        (a, sa), (b, sb) = self.started(mm, "A", "B")
        (s, m, p), = C.split_frames(C.match_request(9, 8, [player_record(a.acct, "A")], a=2, b=2))[0]
        mm.on_match_request(a, C.parse_match_request(p))      # A picked Trial of the Gods (2)
        self.search(mm, b, "B", slot=5, any_mode=1)            # B: Quick Match (any mode)
        g = [C.parse_game_world_reserved(p) for _, m, p in sb.take() if m == 0x15]
        self.assertEqual(len(g), 1)
        self.assertEqual((g[0]["result"], g[0]["slot"], g[0]["count"]), (0, 8, 2))   # B is put into A's mode

    def test_presence_follows_the_player(self):
        import gowa_server as G
        mm = sam_match.Matchmaker(min_players=2)
        (a, sa), (b, sb) = self.started(mm, "A", "B")
        class Conn:
            def __init__(self, oid, pl): self.online_id, self.player = oid, pl
        G.ONLINE.clear(); G.ONLINE.update({1: Conn("A", a), 2: Conn("B", b)}); G.PARTIES.clear(); G.PARTIES["a"] = "WOLF42"
        try:
            self.assertEqual(G.presence("nobody")["state"], "offline")
            self.assertEqual((G.presence("A")["state"], G.presence("A")["party"]), ("lobby", "WOLF42"))
            (s, m, p), = C.split_frames(C.match_request(9, 8, [player_record(a.acct, "A")], a=2, b=2))[0]
            mm.on_match_request(a, C.parse_match_request(p))
            self.assertEqual((G.presence("a")["state"], G.presence("a")["mode"]), ("searching", "Trial of the Gods (2)"))
            (s, m, p), = C.split_frames(C.match_request(9, 8, [player_record(b.acct, "B")], a=2, b=2))[0]
            mm.on_match_request(b, C.parse_match_request(p))
            pa = G.presence("A")
            self.assertEqual((pa["state"], pa["mode"], pa["players"], pa["max"]), ("in match", "Trial of the Gods (2)", 2, 2))
            sid = a.sessions[C.SVC_MATCH]
            mm.on_session_end(a, sid, C.SVC_MATCH)            # the round started: the server closes the world
            mm.on_session_end(b, b.sessions[C.SVC_MATCH], C.SVC_MATCH)
            self.assertEqual(G.presence("B")["players"], 2)   # still 2 for the second one (MEASURED n20: was 1/2)
            pa = G.presence("A")
            self.assertEqual((pa["state"], pa["mode"], pa["players"]), ("in match", "Trial of the Gods (2)", 2))
            self.assertNotIn("address", pa)
        finally:
            G.ONLINE.clear(); G.PARTIES.clear()

    def test_quick_match_pairs_fill_a_mode_they_can(self):
        mm = sam_match.Matchmaker(min_players=2)
        (a, sa), (b, sb) = self.started(mm, "A", "B")
        # Quick Match: each asks for a random mode it cannot fill alone (FFR4 4/4, TCTF8 6/8)
        for pl, n, slot, lo, hi in ((a, "A", 4, 4, 4), (b, "B", 3, 6, 8)):
            (s, m, p), = C.split_frames(C.match_request(9, slot, [player_record(pl.acct, n)], a=lo, b=hi, flag6=1))[0]
            mm.on_match_request(pl, C.parse_match_request(p))
        g = [C.parse_game_world_reserved(p) for _, m, p in sb.take() if m == 0x15]
        self.assertEqual(len(g), 1)
        self.assertEqual((g[0]["slot"], g[0]["count"]), (8, 2))        # Trial of the Gods (2) for two players

    def test_waiting_players_get_a_mode_they_fill_after_fallback(self):
        now = [100.0]
        mm = sam_match.Matchmaker(min_players=2, fallback_s=20, clock=lambda: now[0])
        (a, sa), (b, sb) = self.started(mm, "A", "B")
        # both repeat a FIXED 4-player mode (flag6 = 0), as the game does after its first search
        for pl, n in ((a, "A"), (b, "B")):
            (s, m, p), = C.split_frames(C.match_request(9, 6, [player_record(pl.acct, n)], a=4, b=4))[0]
            mm.on_match_request(pl, C.parse_match_request(p))
        self.assertFalse(any(m == 0x15 for _, m, _p in sa.take() + sb.take()))      # not yet
        now[0] += 21
        mm._try_reserve(a.request["pool"])                                            # the timer
        g = [C.parse_game_world_reserved(p) for _, m, p in sb.take() if m == 0x15]
        self.assertEqual((len(g), g[0]["slot"], g[0]["count"]), (1, 8, 2))

    def test_cancelled_search_leaves_no_ghost(self):
        mm = sam_match.Matchmaker(min_players=2)
        (a, sa), (b, sb) = self.started(mm, "A", "B")
        self.search(mm, a, "A", slot=8, any_mode=1)
        mm.on_session_end(a, a.sessions[C.SVC_MATCH], C.SVC_MATCH)   # the player cancels: Match session ends
        self.assertEqual(sum(len(p) for p in mm.pools.values()), 0)
        mm.on_session_begin(a, C.SVC_MATCH); sa.take()
        self.search(mm, a, "A", slot=8, any_mode=1)
        self.assertFalse(any(m == 0x15 for _, m, _p in sa.take()))    # never matched with itself

    def test_keys_do_not_mix_and_leaving_updates_the_pool(self):
        mm = sam_match.Matchmaker(min_players=3)
        ps = [self.mk(mm, i + 1, "P%d" % i) for i in range(3)]
        for pl, sink in ps:
            mm.on_session_begin(pl, C.SVC_MATCH)
            sink.take()
        self.search(mm, ps[0][0], "P0", key=1)
        self.search(mm, ps[1][0], "P1", key=2)
        self.search(mm, ps[2][0], "P2", key=1)
        self.assertEqual(sorted(len(v) for v in mm.pools.values()), [1, 2])
        self.assertTrue(all(m == 0x17 for _, sink in ps for _, m, _p in sink.take()))   # nobody was matched
        mm.leave(ps[2][0])
        (s, m, p), = ps[0][1].take()
        self.assertEqual(len(C.parse_search_update(p)["names16"]), 1)

    def test_player_report_drops_players(self):
        mm = sam_match.Matchmaker(min_players=2)
        a, sa = self.mk(mm, 1, "A")
        b, sb = self.mk(mm, 2, "B")
        for pl in (a, b):
            mm.on_session_begin(pl, C.SVC_MATCH)
        self.search(mm, a, "A")
        self.search(mm, b, "B")
        sa.take(), sb.take()
        self.assertFalse(mm.on_player_report(a, {"world_id": 1, "flags": [0], "accts": [2]}))
        self.assertTrue(mm.on_player_report(a, {"world_id": a.world.id, "flags": [1, 0], "accts": [1, 2]}))
        self.assertEqual(len(C.parse_game_world_update(sa.take()[0][2])["roster"]), 1)
        self.assertIsNone(b.world)

    def test_different_game_data_never_matched(self):
        # 2026-11-15 (Europe server): a player with mods on (data hash B92E156B) was matched with one on the disc's
        # data (F8C0B979) through a shared party code; the second game left at once. Different shards never match.
        logs = []
        mm = sam_match.Matchmaker(min_players=2, party_of=lambda oid: "FRNG4P", log=logs.append)
        a, sa = self.mk(mm, 1, "A")
        b, sb = self.mk(mm, 2, "B")
        c, sc = self.mk(mm, 3, "C")
        for pl in (a, b, c):
            mm.on_session_begin(pl, C.SVC_MATCH)
        self.search(mm, a, "A", shard=0xF8C0B979800038D0)
        self.search(mm, b, "B", shard=0xB92E156B800038D0)
        self.assertIsNone(a.world)
        self.assertIsNone(b.world)
        self.assertTrue(any("different game data" in l and "B92E156B" in l for l in logs))
        self.search(mm, c, "C", shard=0xF8C0B979800038D0)
        self.assertIsNotNone(a.world)
        self.assertIs(a.world, c.world)
        self.assertIsNone(b.world)

    def test_modes_do_not_mix_and_real_request_decodes(self):
        mm = sam_match.Matchmaker(min_players=2)
        a, sa = self.mk(mm, 1, "A")
        b, sb = self.mk(mm, 2, "B")
        for pl in (a, b):
            mm.on_session_begin(pl, C.SVC_MATCH)
        self.search(mm, a, "A", slot=8)
        self.search(mm, b, "B", slot=9)
        self.assertEqual(len(mm.pools[(0, 9)]), 2)                           # two players who PICKED different modes wait
        mm.leave(a), mm.leave(b), sa.take(), sb.take()
        self.search(mm, a, "A", slot=8, any_mode=1)
        self.search(mm, b, "B", slot=9, any_mode=1)
        self.assertEqual(mm.pools, {})                                 # Quick Match ("any mode") players match

        modes = [C.parse_game_world_reserved(f[2])["slot"] for sink in (sa, sb) for f in sink.take() if f[1] == 0x15]
        self.assertEqual(modes, [8, 8])                                 # everyone plays the host's mode
        # header bytes of the real request from run 114525
        hdr = bytes.fromhex("b92e156b800038d0" "00000000" "08" "00" "01" "40")
        blob = hdr + player_record(3755424436348344775, "MpTestA")
        m = C.parse_match_request(struct.pack(">HBBBB", len(blob), 2, 2, 2, 0) + blob)
        self.assertEqual((m["shard"], m["key"], m["slot"], m["flag6"], len(m["players"])),
                         (0xB92E156B800038D0, 0, 8, 1, 1))

    def test_mode_is_chosen_by_player_count(self):
        """Quick Match: each request carries its mode and that mode's min/max (MEASURED 4/4 mode 6, 6/8 mode 5,
        2/2 mode 8). Nothing starts until some waiting request's mode can start; then that mode for everyone."""
        mm = sam_match.Matchmaker(min_players=1)
        ps = [self.mk(mm, i + 1, "P%d" % i) for i in range(3)]
        for pl, sink in ps:
            mm.on_session_begin(pl, C.SVC_MATCH)
            sink.take()
        for (pl, sink), (mode, lo, hi) in zip(ps, ((6, 4, 4), (5, 6, 8), (8, 2, 2))):
            f = C.match_request(0, mode, [player_record(pl.acct, "P%d" % (pl.acct - 1))], a=lo, b=hi, flag6=1)
            mm.on_match_request(pl, C.parse_match_request(C.split_frames(f)[0][0][2]))
        got = {}
        for pl, sink in ps:
            for s_, m, p in sink.take():
                if m == 0x15:
                    got[pl.acct] = C.parse_game_world_reserved(p)
        self.assertEqual(sorted(got), [1, 2])                           # the two oldest searchers ...
        self.assertEqual({g["slot"] for g in got.values()}, {8})         # ... play the 2-player mode
        self.assertEqual({g["count"] for g in got.values()}, {2})
        self.assertEqual([p.acct for p in sum(mm.pools.values(), [])], [3])   # the third keeps waiting

    def test_game_start_closes_the_world(self):
        """MEASURED: at the countdown each client ends its Match session; from then on the server must not send
        roster changes (no 1-player GameWorldUpdate), and the players can search again for the next round."""
        mm = sam_match.Matchmaker(min_players=2)
        a, sa = self.mk(mm, 1, "A")
        b, sb = self.mk(mm, 2, "B")
        for pl in (a, b):
            mm.on_session_begin(pl, C.SVC_MATCH)
        self.search(mm, a, "A")
        self.search(mm, b, "B")
        sa.take(), sb.take()
        wid = a.world.id
        self.assertTrue(mm.on_session_end(a, a.sessions[C.SVC_MATCH], C.SVC_MATCH))
        self.assertEqual(sb.take(), [])                                 # B gets no 1-player update
        mm._world_update(mm.worlds[wid])
        self.assertEqual(sa.take() + sb.take(), [])                     # no more repeats for a started game
        mm.on_session_end(b, b.sessions[C.SVC_MATCH], C.SVC_MATCH)
        self.assertNotIn(wid, mm.worlds)
        for pl in (a, b):                                               # next round: new session, new search
            mm.on_session_begin(pl, C.SVC_MATCH)
        self.search(mm, a, "A")
        self.search(mm, b, "B")
        self.assertEqual(a.world, b.world)
        self.assertNotEqual(a.world.id, wid)

    def test_party_request_is_refused_cleanly(self):
        mm = sam_match.Matchmaker()
        a, sa = self.mk(mm, 1, "A")
        mm.on_session_begin(a, C.SVC_MATCH)
        sa.take()
        f = C.match_request(9, -1, [player_record(1, "A"), player_record(3, "C")])
        self.assertFalse(mm.on_match_request(a, C.parse_match_request(C.split_frames(f)[0][0][2])))
        self.assertEqual(C.parse_game_world_reserved(sa.take()[0][2])["result"], 2)


class ConfigTests(unittest.TestCase):
    def test_targets_cover_the_shard_key(self):
        """SamStateStart 0x3D48F4 picks a target with RangeLow <= key <= RangeHigh; the key from func_00327720
        always has bit 31 set (0x80000000 | ...), so 0..0 can never hold it."""
        import json, tss
        v, entries = tss.parse(tss.build("127.0.0.1", 36102, 36101, "GET /geo HTTP/1.0\r\n\r\n"))
        mc = json.loads(entries[0][5])["MatchConfig"]
        for key in (0x80000000, (0x1234 << 32) | 0x80000000 | 0x3FFF,
                    (0xA5A9B090 << 32) | 0x80000000 | 915664, (0xB92E156B << 32) | 0x80000000 | 915664):
            for i in range(4):
                t = mc["target%d" % i]
                u64 = lambda v: v & 0xFFFFFFFFFFFFFFFF      # the game stores the value and compares unsigned
                self.assertTrue(u64(t["RangeLow"]) <= key <= u64(t["RangeHigh"]), (i, hex(key)))
                for v in (t["RangeLow"], t["RangeHigh"]):    # the game's JSON reader is signed 64-bit
                    self.assertTrue(-2**63 <= v < 2**63, v)
                self.assertEqual((t["SAMServerHostAddr"], t["SAMServerPort"]), ("127.0.0.1", 36102))
                self.assertTrue(1 <= t["Probability"])

    def test_geo_reply_has_the_tokens_the_client_scans(self):
        """SamGeo 0x3D0970: one recv of at most 0x3FF bytes, strstr for each token, then ="%f" or ="text"."""
        import re
        body = b'<location latitude="0.000000" longitude="0.000000" countryCode="US" city="Local"/>'
        resp = (b"HTTP/1.0 200 OK\r\nContent-Type: text/xml\r\nContent-Length: %d\r\nConnection: close\r\n\r\n"
                % len(body)) + body
        self.assertLessEqual(len(resp), 0x3FF)
        for tok in (b'latitude="', b'longitude="', b'countryCode="', b'city="'):
            self.assertIn(tok, resp)
        self.assertLessEqual(len(re.search(rb'countryCode="([^"]*)"', resp).group(1)), 2)   # 3-byte buffer


class PeerTests(unittest.TestCase):
    def test_hello_and_peer_over_loopback(self):
        async def get(port, path):
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.write(("GET %s HTTP/1.0\r\n\r\n" % path).encode())
            data = await r.read()
            w.close()
            head, body = data.split(b"\r\n\r\n", 1)
            return int(head.split()[1]), body

        async def go():
            a = argparse.Namespace(host="127.0.0.1", http=0, sam=0, connect_reply="svc0", echo_keepalive=True,
                                   idle=10, min_players=2, matchmaker=None)
            ready = asyncio.get_running_loop().create_future()
            task = asyncio.ensure_future(gowa_server.serve(a, ready))
            http_port, _ = await ready
            out = [await get(http_port, "/peer?id=MpTestB"),
                   await get(http_port, "/hello?id=MpTestB&port=3659"),
                   await get(http_port, "/peer?id=MpTestB"),
                   await get(http_port, "/hello?id=MpTestC&port=99999")]
            task.cancel()
            return out

        gowa_server.PEERS.clear()
        r = asyncio.run(go())
        self.assertEqual(r[0][0], 404)
        self.assertEqual(r[1][0], 200)
        self.assertEqual(r[2], (200, b"127.0.0.1 3659"))     # the format kit mpnet_peer_lookup() parses
        self.assertEqual(r[3][0], 404)                         # bad port refused


class IdleTests(unittest.TestCase):
    """2026-11-14: rounds longer than the idle time were cut (the game is silent on SAM during a round)."""

    def _run_quiet(self, in_match):
        from unittest import mock

        class W:
            def get_extra_info(self, k):
                return ("127.0.0.1", 1)
            def write(self, d):
                pass
            def close(self):
                pass
            async def wait_closed(self):
                pass

        async def go():
            r = asyncio.StreamReader()          # never gets data: a silent client
            a = argparse.Namespace(idle=0.05, matchmaker=mock.MagicMock(), accounts=None, limits=mock.MagicMock())
            c = gowa_server.SamConn(r, W(), a)
            if in_match:
                c.player.playing = 8193
            t0 = asyncio.get_running_loop().time()
            await asyncio.wait_for(c.run(), 5)
            return asyncio.get_running_loop().time() - t0

        with mock.patch.object(gowa_server, "MATCH_QUIET_MAX", 0.4):
            return asyncio.run(go())

    def test_silent_lobby_connection_is_closed(self):
        self.assertLess(self._run_quiet(False), 0.3)

    def test_silent_connection_in_a_match_stays_open(self):
        self.assertGreaterEqual(self._run_quiet(True), 0.35)


class LanBeaconTests(unittest.TestCase):
    """2026-11-15: LAN discovery answers private askers only, with name + address + http port."""

    def _ask(self, peer, data=b"GOWA?1"):
        sent = []

        class T:
            def sendto(self, d, a):
                sent.append((d, a))

        a = argparse.Namespace(name="Home LAN", host="192.168.1.10", http=36101)
        b = gowa_server.LanBeacon(a)
        b.connection_made(T())
        b.datagram_received(data, (peer, 50000))
        return sent

    def test_private_asker_gets_the_server(self):
        import json
        sent = self._ask("192.168.1.20")
        self.assertEqual(len(sent), 1)
        r = json.loads(sent[0][0])
        self.assertEqual((r["gowa"], r["name"], r["address"], r["http"]), (1, "Home LAN", "192.168.1.10", 36101))

    def test_public_asker_and_wrong_question_get_nothing(self):
        self.assertEqual(self._ask("8.8.8.8"), [])
        self.assertEqual(self._ask("192.168.1.20", b"hello"), [])


class DiscordNotifyTests(unittest.TestCase):
    """2026-11-15: webhook posts: no mentions, names cleaned, rate limit, URL check, round-start hook, weekly top."""

    def _notifier(self):
        import discord_notify, threading
        got, ev = [], threading.Event()

        def send(payload):
            got.append(payload)
            ev.set()
        n = discord_notify.Notifier("https://discord.com/api/webhooks/1/x", "Test Server", send=send)
        return n, got, ev

    def test_match_post_is_plain_and_mentions_off(self):
        n, got, ev = self._notifier()
        self.assertTrue(n.match_started("Trial of the Gods (2)", ["Ann", "@everyone", "<@123>"]))
        ev.wait(2)
        p = got[0]
        self.assertEqual(p["allowed_mentions"], {"parse": []})
        self.assertNotIn("@", p["content"])
        self.assertIn("Trial of the Gods (2)", p["content"])

    def test_rate_limit_and_url_check(self):
        import discord_notify, os, tempfile
        n, got, ev = self._notifier()
        self.assertTrue(n.post("one"))
        self.assertFalse(n.post("two, too soon"))
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("https://evil.example/api/webhooks/1/x")
        try:
            with self.assertRaises(ValueError):
                discord_notify.read_url(f.name)
        finally:
            os.unlink(f.name)

    def test_round_start_hook_fires_once(self):
        mm, (a, b) = self._two_in_world()
        seen = []
        mm.on_round_start = lambda wid, mode, names: seen.append((wid, sorted(names)))
        for pl in (a, b):
            mm.on_session_end(pl, pl.sessions[C.SVC_MATCH], C.SVC_MATCH)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][1], ["A", "B"])

    def _two_in_world(self):
        mm = sam_match.Matchmaker(2)
        ps = []
        for name, acct in (("A", 1), ("B", 2)):
            pl = sam_match.Player(FakeSink())
            mm.on_connect(pl, acct, name)
            mm.on_session_begin(pl, C.SVC_MATCH)
            ps.append(pl)
        for pl in ps:
            (s, m, p), = C.split_frames(C.match_request(9, -1, [player_record(pl.acct, pl.online_id)]))[0]
            mm.on_match_request(pl, C.parse_match_request(p))
        self.assertIsNotNone(ps[0].world)
        return mm, ps

    def test_weekly_top_text(self):
        import leaderboard
        n, got, ev = self._notifier()
        lb = leaderboard.Leaderboard(":memory:")
        lb.report(1, "Ann", {"XP": 100})
        lb.db.execute("UPDATE players SET updated = updated - 100")
        lb.report(1, "Ann", {"XP": 900})
        self.assertTrue(n.weekly_top(lb))
        ev.wait(2)
        self.assertIn("Ann 800", got[0]["content"])


class InternetTests(unittest.TestCase):
    def setUp(self):
        import gowa_server as G
        self.G = G
        for d in (G.PEERS, G.PEER_LAN, G.PEER_SEEN, G.UDP_TOKENS):
            d.clear()

    tearDown = setUp

    def test_peer_address_choice(self):
        G = self.G
        G.PEERS["A"] = ("203.0.113.5", 3658)              # /hello came from A's router
        self.assertEqual(G.peer_address("A", "198.51.100.9"), ("203.0.113.5", 3658))   # no probe yet: /hello address
        G.PEER_LAN["A"] = ("192.168.1.20", 3658)
        G.PEER_SEEN["A"] = ("203.0.113.5", 51234)          # the router's outside port for A's game socket
        self.assertEqual(G.peer_address("A", "198.51.100.9"), ("203.0.113.5", 51234))  # another home: outside address
        self.assertEqual(G.peer_address("A", "203.0.113.5"), ("192.168.1.20", 3658))   # same router: home address

    def test_udp_probe_records_only_known_tokens(self):
        G = self.G
        G.UDP_TOKENS["ab" * 16] = "A"
        pr = G.UdpProbe(None)
        pr.datagram_received(b"MPN1UDP " + b"cd" * 16, ("203.0.113.7", 4000))   # unknown token: ignored
        pr.datagram_received(b"junk", ("203.0.113.7", 4000))
        self.assertEqual(G.PEER_SEEN, {})
        pr.datagram_received(b"MPN1UDP " + b"ab" * 16, ("203.0.113.5", 51234))
        self.assertEqual(G.PEER_SEEN, {"A": ("203.0.113.5", 51234)})


class RelayTests(unittest.TestCase):
    def setUp(self):
        import gowa_server as G
        self.G = G
        for d in (G.PEERS, G.PEER_LAN, G.PEER_SEEN, G.UDP_TOKENS, G.ONLINE):
            d.clear()

    tearDown = setUp

    def world(self, *names):
        G = self.G
        class P: pass
        class W: pass
        w = W(); w.id = 0x2001
        for i, n in enumerate(names):
            pl = P(); pl.world, pl.playing = w, None
            c = P(); c.online_id, c.player = n, pl
            G.ONLINE[i + 1] = c

    def test_relay_inside_one_match_only(self):
        G = self.G
        self.world("A", "B")
        stranger = type("P", (), {})(); stranger.online_id = "C"
        stranger.player = type("P", (), {"world": None, "playing": None})()
        G.ONLINE[9] = stranger
        G.UDP_TOKENS.update({"a" * 32: "A", "c" * 32: "C"})
        G.PEER_SEEN.update({"B": ("198.51.100.2", 40000), "A": ("203.0.113.1", 50000)})
        sent = []
        pr = G.UdpProbe(None)
        pr.connection_made(type("T", (), {"sendto": lambda self, d, a: sent.append((d, a))})())
        pkt = lambda tok, to, p: b"MPR1" + tok + to.ljust(16, b"\0") + p
        pr.datagram_received(pkt(b"a" * 32, b"B", b"hello"), ("203.0.113.1", 50000))
        self.assertEqual(sent, [(b"MPR2" + b"A".ljust(16, b"\0") + b"hello", ("198.51.100.2", 40000))])
        sent.clear()
        pr.datagram_received(pkt(b"c" * 32, b"B", b"x"), ("192.0.2.9", 1))       # not in the match: dropped
        pr.datagram_received(pkt(b"z" * 32, b"B", b"x"), ("192.0.2.9", 1))       # unknown token: dropped
        pr.datagram_received(pkt(b"a" * 32, b"A", b"x"), ("203.0.113.1", 50000)) # to itself: dropped
        pr.datagram_received(pkt(b"a" * 32, b"B", b"x" * 2000), ("203.0.113.1", 50000))   # too big: dropped
        self.assertEqual(sent, [])


class AdminLockoutTests(unittest.TestCase):
    def test_wrong_keys_lock_one_client_out(self):
        import admin as A
        ad = A.Admin(None, None, {}, None, {}, "t")
        ad._fails = {}
        good = {"x-admin-key": ad.key, "x-forwarded-for": "198.51.100.7"}
        bad = {"x-admin-key": "nope", "x-forwarded-for": "198.51.100.7"}
        other = {"x-admin-key": ad.key, "x-forwarded-for": "203.0.113.9"}
        self.assertTrue(ad.allowed("127.0.0.1", good))
        for _ in range(8):
            self.assertFalse(ad.allowed("127.0.0.1", bad))
        self.assertFalse(ad.allowed("127.0.0.1", good))      # locked, even with the right key
        self.assertTrue(ad.allowed("127.0.0.1", other))      # another client is not affected
        self.assertFalse(ad.allowed("192.0.2.1", other))     # never from another machine directly


class AccountTests(unittest.TestCase):
    TOK_A, TOK_B = "a" * 32, "b" * 32

    def test_register_sign_in_and_name_theft(self):
        import accounts
        acc = accounts.Accounts(":memory:")
        a = acc.sign_in("MpTestA", self.TOK_A)
        self.assertEqual(acc.sign_in("MpTestA", self.TOK_A), a)            # same token: same account
        self.assertEqual(acc.sign_in("mptesta", self.TOK_A), a)            # names are case-insensitive
        with self.assertRaises(accounts.AccountError):
            acc.sign_in("MpTestA", self.TOK_B)                              # someone else's name
        for bad in ("ab", "1abc", "a" * 17, "Mp Test", "Mp;Test"):
            with self.assertRaises(accounts.AccountError):
                acc.sign_in(bad, self.TOK_A)
        with self.assertRaises(accounts.AccountError):
            acc.sign_in("MpTestC", "short")

    def test_ticket_is_64_bytes_signed_and_checked(self):
        import accounts
        acc = accounts.Accounts(":memory:")
        a = acc.sign_in("MpTestA", self.TOK_A)
        t = acc.issue_ticket(a, "MpTestA", now=1000)
        self.assertEqual(len(t), 64)                                         # the game's ticket buffer
        self.assertEqual(acc.verify_ticket(t, now=2000), (a, "MpTestA"))
        forged = t[:8] + struct.pack(">Q", a + 1) + t[16:]                   # someone edits the account id
        for bad, when in ((forged, 2000), (t[:63] + bytes([t[63] ^ 1]), 2000), (t, 1000 + 25 * 3600)):
            with self.assertRaises(accounts.AccountError):
                acc.verify_ticket(bad, now=when)
        other = accounts.Accounts(":memory:")                                 # another server's key
        with self.assertRaises(accounts.AccountError):
            other.verify_ticket(t, now=2000)
        acc.ban("MpTestA")
        with self.assertRaises(accounts.AccountError):
            acc.verify_ticket(t, now=2000)

    def test_ticket_hello_and_sign_in_over_loopback(self):
        import accounts
        async def get(port, path):
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.write(("GET %s HTTP/1.0\r\n\r\n" % path).encode())
            data = await r.read()
            w.close()
            head, body = data.split(b"\r\n\r\n", 1)
            return int(head.split()[1]), body

        async def sign_in(port, ticket):
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.write(C.frame(0, 0, struct.pack(">QQIHBB", 1, 9, 0, len(ticket), 0, 0) + ticket))
            buf = b""
            while len(buf) < 20:
                buf += await asyncio.wait_for(r.read(64), 5)
            w.close()
            return C.parse_connect_response(C.split_frames(buf)[0][0][2])

        async def go():
            a = argparse.Namespace(host="127.0.0.1", http=0, sam=0, connect_reply="svc0", echo_keepalive=True,
                                   idle=10, min_players=2, matchmaker=None, accounts=accounts.Accounts(":memory:"),
                                   allow_unsigned=False)
            ready = asyncio.get_running_loop().create_future()
            task = asyncio.ensure_future(gowa_server.serve(a, ready))
            hp, sp = await ready
            r = {"t1": await get(hp, "/ticket?id=MpTestA&token=" + self.TOK_A),
                 "t2": await get(hp, "/ticket?id=MpTestA&token=" + self.TOK_B),
                 "h1": await get(hp, "/hello?id=MpTestA&port=3658&token=" + self.TOK_B),
                 "p1": await get(hp, "/peer?id=MpTestA"),
                 "h2": await get(hp, "/hello?id=MpTestA&port=3658&token=" + self.TOK_A),
                 "p2": await get(hp, "/peer?id=MpTestA")}
            r["ok"] = await sign_in(sp, r["t1"][1])
            r["old"] = await sign_in(sp, ticket(42, "MpTestB"))                 # an unsigned v1 ticket
            task.cancel()
            return r

        gowa_server.PEERS.clear()
        r = asyncio.run(go())
        self.assertEqual((r["t1"][0], len(r["t1"][1])), (200, 64))
        self.assertEqual(r["t2"][0], 403)                                       # wrong token for the name
        self.assertEqual(r["p1"][0], 404)                                       # /hello with the wrong token ignored
        self.assertEqual(r["p2"], (200, b"127.0.0.1 3658"))
        self.assertEqual((r["ok"]["response"], r["ok"]["acct"]), (0, struct.unpack(">Q", r["t1"][1][8:16])[0]))
        self.assertEqual(r["old"]["response"], 1)                               # unsigned refused by default


class LimitTests(unittest.TestCase):
    def test_buckets_and_sign_in_failures(self):
        import limits
        now = [0.0]
        lim = limits.Limits(sam_per_ip=2, clock=lambda: now[0])
        self.assertEqual(sum(lim.http_ok("1.2.3.4") for _ in range(100)), 40)     # burst 40
        now[0] += 1.0
        self.assertEqual(sum(lim.http_ok("1.2.3.4") for _ in range(100)), 20)     # 20 per second
        self.assertTrue(lim.http_ok("5.6.7.8"))                                     # per address
        for _ in range(5):
            self.assertTrue(lim.may_try_sign_in("9.9.9.9"))
            lim.sign_in_failed("9.9.9.9")
        self.assertFalse(lim.may_try_sign_in("9.9.9.9"))                           # 6th try refused
        now[0] += 13.0
        self.assertTrue(lim.may_try_sign_in("9.9.9.9"))                            # one more after 12 s
        self.assertTrue(lim.sam_open_ok("a")), self.assertTrue(lim.sam_open_ok("a"))
        self.assertFalse(lim.sam_open_ok("a"))
        lim.sam_closed("a")
        self.assertTrue(lim.sam_open_ok("a"))

    def test_status_json_and_ticket_lockout_over_loopback(self):
        import accounts, json as _json
        async def get(port, path):
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.write(("GET %s HTTP/1.0" % path).encode() + bytes([13, 10, 13, 10]))
            data = await r.read()
            w.close()
            head, body = data.split(bytes([13, 10, 13, 10]), 1)
            return int(head.split()[1]), body

        async def go():
            a = argparse.Namespace(host="127.0.0.1", http=0, sam=0, connect_reply="svc0", echo_keepalive=True,
                                   idle=10, min_players=2, matchmaker=None, accounts=accounts.Accounts(":memory:"),
                                   allow_unsigned=False)
            ready = asyncio.get_running_loop().create_future()
            task = asyncio.ensure_future(gowa_server.serve(a, ready))
            hp, _ = await ready
            out = [await get(hp, "/ticket?id=MpTestA&token=" + "a" * 32)]
            out += [await get(hp, "/ticket?id=MpTestA&token=" + "b" * 32) for _ in range(6)]
            out.append(await get(hp, "/api/status"))
            task.cancel()
            return out

        r = asyncio.run(go())
        self.assertEqual(r[0][0], 200)
        self.assertEqual([c for c, _ in r[1:6]], [403] * 5)                       # five wrong tokens ...
        self.assertEqual(r[6][0], 429)                                              # ... then locked out
        st = _json.loads(r[7][1])
        self.assertEqual((st["players_online"], st["matches"], st["accounts"]), (0, 0, True))

    def test_admin_ban_list(self):
        import accounts, tempfile, io, contextlib
        d = tempfile.mkdtemp()
        acc = accounts.Accounts(d)
        acc.sign_in("MpTestA", "a" * 32)
        acc.db.close()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(gowa_server.admin(argparse.Namespace(data=d, admin_cmd=["ban", "MpTestA"])), 0)
            self.assertEqual(gowa_server.admin(argparse.Namespace(data=d, admin_cmd=["ban", "Nobody"])), 1)
            gowa_server.admin(argparse.Namespace(data=d, admin_cmd=["list"]))
        self.assertIn("BANNED", out.getvalue())
        with self.assertRaises(accounts.AccountError):
            accounts.Accounts(d).sign_in("MpTestA", "a" * 32)


class FairPlayTests(unittest.TestCase):
    def test_record_must_be_your_own_account(self):
        mm = sam_match.Matchmaker(min_players=2)
        sink = FakeSink()
        pl = sam_match.Player(sink)
        mm.on_connect(pl, 7, "MpTestA")
        mm.on_session_begin(pl, C.SVC_MATCH)
        sink.take()
        for acct, name in ((8, "MpTestA"), (7, "MpTestB")):          # someone else's id / someone else's name
            f = C.match_request(0, 8, [player_record(acct, name)], flag6=1)
            self.assertFalse(mm.on_match_request(pl, C.parse_match_request(C.split_frames(f)[0][0][2])))
            (s_, m, p), = sink.take()
            self.assertEqual((m, C.parse_game_world_reserved(p)["result"]), (0x15, 10))
        self.assertEqual(mm.pools, {})
        f = C.match_request(0, 8, [player_record(7, "mptesta")], flag6=1)   # own record (any case) is fine
        self.assertTrue(mm.on_match_request(pl, C.parse_match_request(C.split_frames(f)[0][0][2])))


class FuzzTests(unittest.TestCase):
    def test_parsers_never_crash(self):
        import random
        rnd = random.Random(1234)
        parsers = [C.parse_connect_request, C.parse_connect_response, C.parse_session_begin, C.parse_session_end,
                   C.parse_session_data, C.parse_match_request, C.parse_player_report, C.parse_search_update,
                   C.parse_game_world_reserved, C.parse_game_world_update]
        good = [C.match_request(0, 8, [player_record(1, "A")], ids=[1, 2], app_data=b"xy")[4:],
                C.game_world_reserved(1, 0, b"k", 8, [C.roster_record(player_record(1, "A"), 0)])[4:]]
        for i in range(20000):
            if i % 3 == 0:
                g = bytearray(rnd.choice(good))                     # mutate a real message
                for _ in range(rnd.randint(1, 6)):
                    g[rnd.randrange(len(g))] = rnd.randrange(256)
                data = bytes(g[:rnd.randint(0, len(g))])
            else:
                data = bytes(rnd.randrange(256) for _ in range(rnd.randint(0, 300)))
            for fn in parsers:
                try:
                    fn(data)
                except C.CodecError:
                    pass
            C.describe(rnd.randrange(6), rnd.randrange(32), data)   # never raises
            try:
                C.split_frames(data)
            except C.CodecError:
                pass

    def test_server_survives_garbage_over_loopback(self):
        import random, accounts
        rnd = random.Random(99)

        async def go():
            a = argparse.Namespace(host="127.0.0.1", http=0, sam=0, connect_reply="svc0", echo_keepalive=True,
                                   idle=10, min_players=2, matchmaker=None, accounts=accounts.Accounts(":memory:"),
                                   allow_unsigned=False)
            ready = asyncio.get_running_loop().create_future()
            task = asyncio.ensure_future(gowa_server.serve(a, ready))
            hp, sp = await ready
            for i in range(60):
                port = sp if i % 2 else hp
                r, w = await asyncio.open_connection("127.0.0.1", port)
                junk = bytes(rnd.randrange(256) for _ in range(rnd.randint(1, 3000)))
                if i % 4 == 1:   # a valid frame header with a hostile body
                    junk = C.frame(rnd.randrange(3), rnd.randrange(32), junk[:3000])
                w.write(junk)
                try:
                    await w.drain()
                    await asyncio.wait_for(r.read(4096), 0.5)
                except Exception:
                    pass
                w.close()
            # still alive: a normal ticket + sign-in works
            r, w = await asyncio.open_connection("127.0.0.1", hp)
            w.write(("GET /ticket?id=MpTestZ&token=" + "c" * 32 + " HTTP/1.0").encode() + bytes([13, 10, 13, 10]))
            body = (await r.read()).split(bytes([13, 10, 13, 10]), 1)[1]
            w.close()
            r, w = await asyncio.open_connection("127.0.0.1", sp)
            w.write(C.frame(0, 0, struct.pack(">QQIHBB", 1, 9, 0, len(body), 0, 0) + body))
            buf = b""
            while len(buf) < 20:
                buf += await asyncio.wait_for(r.read(64), 5)
            w.close()
            task.cancel()
            return len(body), C.parse_connect_response(C.split_frames(buf)[0][0][2])["response"]

        self.assertEqual(asyncio.run(go()), (64, 0))


class AdminTests(unittest.TestCase):
    def test_admin_page_and_api_over_loopback(self):
        import accounts, tempfile, json as _json
        d = tempfile.mkdtemp()

        async def http(port, method, path, key=None, body=None):
            r, w = await asyncio.open_connection("127.0.0.1", port)
            data = _json.dumps(body).encode() if body is not None else b""
            head = "%s %s HTTP/1.0" % (method, path)
            if key:
                head += chr(13) + chr(10) + "X-Admin-Key: " + key
            if data:
                head += chr(13) + chr(10) + "Content-Length: %d" % len(data)
            w.write(head.encode() + bytes([13, 10, 13, 10]) + data)
            raw = await r.read()
            w.close()
            h, b = raw.split(bytes([13, 10, 13, 10]), 1)
            return int(h.split()[1]), b

        async def sign_in(hp, sp, name, tok):
            _, t = await http(hp, "GET", "/ticket?id=%s&token=%s" % (name, tok))
            r, w = await asyncio.open_connection("127.0.0.1", sp)
            w.write(C.frame(0, 0, struct.pack(">QQIHBB", 1, 9, 0, len(t), 0, 0) + t))
            buf = b""
            while len(buf) < 20:
                buf += await asyncio.wait_for(r.read(64), 5)
            return r, w, C.parse_connect_response(C.split_frames(buf)[0][0][2])

        async def go():
            a = argparse.Namespace(host="127.0.0.1", http=0, sam=0, connect_reply="svc0", echo_keepalive=True,
                                   idle=10, min_players=2, matchmaker=None, data=d, allow_unsigned=False)
            ready = asyncio.get_running_loop().create_future()
            task = asyncio.ensure_future(gowa_server.serve(a, ready))
            hp, sp = await ready
            key = open(os.path.join(d, "admin.key")).read().strip()
            out = {"page": await http(hp, "GET", "/admin"),
                   "nokey": await http(hp, "GET", "/admin/api/overview"),
                   "badkey": await http(hp, "GET", "/admin/api/overview", key="0" * 32)}
            r1, w1, c1 = await sign_in(hp, sp, "MpTestA", "a" * 32)
            out["login"] = c1["response"]
            await asyncio.sleep(0.1)
            out["ov"] = _json.loads((await http(hp, "GET", "/admin/api/overview", key))[1])
            out["pl"] = _json.loads((await http(hp, "GET", "/admin/api/players", key))[1])["players"]
            out["ban"] = _json.loads((await http(hp, "POST", "/admin/api/ban", key, {"name": "MpTestA"}))[1])
            out["closed"] = await asyncio.wait_for(r1.read(), 5) == b""
            _, _, c2 = await sign_in(hp, sp, "MpTestA", "a" * 32)
            out["login_banned"] = c2["response"]
            await http(hp, "POST", "/admin/api/unban", key, {"name": "MpTestA"})
            _, _, c3 = await sign_in(hp, sp, "MpTestA", "a" * 32)
            out["login_unbanned"] = c3["response"]
            out["add"] = await http(hp, "POST", "/admin/api/servers/add", key,
                                    {"name": "Local test", "address": "127.0.0.1:%d" % hp, "region": "Europe"})
            out["bad_add"] = (await http(hp, "POST", "/admin/api/servers/add", key, {"name": "x", "address": "a b"}))[0]
            sid = _json.loads(out["add"][1])["servers"][0]["id"]
            out["check"] = _json.loads((await http(hp, "POST", "/admin/api/servers/check", key, {"id": sid}))[1])
            out["public"] = _json.loads((await http(hp, "GET", "/api/servers"))[1])
            out["rm"] = _json.loads((await http(hp, "POST", "/admin/api/servers/remove", key, {"id": sid}))[1])
            task.cancel()
            return out

        o = asyncio.run(go())
        self.assertEqual(o["page"][0], 200)
        self.assertIn(b"<title>Server Admin</title>", o["page"][1])
        self.assertEqual((o["nokey"][0], o["badkey"][0]), (403, 403))
        self.assertEqual(o["login"], 0)
        self.assertEqual((o["ov"]["online"], o["ov"]["accounts"], o["ov"]["lobby"]), (1, 1, 1))
        self.assertEqual([(p["name"], p["state"]) for p in o["pl"]], [("MpTestA", "lobby")])
        self.assertEqual((o["ban"]["done"], o["ban"]["kicked"]), (True, 1))
        self.assertTrue(o["closed"])                                          # the banned player was kicked
        self.assertEqual((o["login_banned"], o["login_unbanned"]), (1, 0))
        self.assertEqual((o["add"][0], o["bad_add"]), (200, 400))
        self.assertTrue(o["check"]["ok"])
        self.assertEqual(o["check"]["status"]["players_online"], 1)
        self.assertEqual(o["public"]["servers"][0]["name"], "Local test")
        self.assertNotIn("id", o["public"]["servers"][0])
        self.assertEqual(o["rm"]["servers"], [])


KNOWN_K = "8505da6637238b6e5966d0fc1429e1e2857cd12cc730126419491e65c0701a8c"
KNOWN_PROOF = "428717eb20fef482f3412b5584e06437830bc3553048ee4d7347d59be0e1a17f"


class ChallengeTests(unittest.TestCase):
    def test_challenge_sign_in_never_sends_the_token(self):
        import accounts
        acc = accounts.Accounts(":memory:")
        k = accounts.derive_key("a" * 32, "MpTestA")
        a = acc.register_key("MpTestA", k.hex())
        with self.assertRaises(accounts.AccountError):
            acc.register_key("mptesta", k.hex())                                # taken
        n = acc.new_nonce()
        self.assertEqual(acc.sign_in_proof("MpTestA", n, accounts.proof_for(k, n, "MpTestA")), a)
        with self.assertRaises(accounts.AccountError):                          # a nonce works once
            acc.sign_in_proof("MpTestA", n, accounts.proof_for(k, n, "MpTestA"))
        n2 = acc.new_nonce()
        with self.assertRaises(accounts.AccountError):                          # wrong key
            acc.sign_in_proof("MpTestA", n2, accounts.proof_for(accounts.derive_key("b" * 32, "MpTestA"), n2, "MpTestA"))
        with self.assertRaises(accounts.AccountError):                          # made-up nonce
            acc.sign_in_proof("MpTestA", "f" * 32, accounts.proof_for(k, "f" * 32, "MpTestA"))
        self.assertEqual(acc.sign_in("MpTestA", "a" * 32), a)                   # the token still derives the same K
        # an account made by the old token path can sign in by challenge too
        b = acc.sign_in("MpTestB", "c" * 32)
        n3 = acc.new_nonce()
        self.assertEqual(acc.sign_in_proof("MpTestB", n3, accounts.proof_for(accounts.derive_key("c" * 32, "MpTestB"),
                                                                              n3, "MpTestB")), b)

    def test_known_vector_for_the_runtime(self):
        """The C runtime logs these at start-up; both sides must agree."""
        import accounts
        k = accounts.derive_key("00112233445566778899aabbccddeeff", "MpTestA")
        self.assertEqual(k.hex(), KNOWN_K)
        self.assertEqual(accounts.proof_for(k, "0123456789abcdef0123456789abcdef", "MpTestA"), KNOWN_PROOF)


class SocketTests(unittest.TestCase):
    """Two clients over real loopback sockets against the server's own listener (ephemeral ports)."""

    def test_two_clients_over_loopback(self):
        async def read_frames(r, want):
            buf, out = b"", []
            while len(out) < want:
                buf += await asyncio.wait_for(r.read(65536), 5)
                fr, buf = C.split_frames(buf)
                out += fr
            return out

        async def client(port, acct, name, slot):
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.write(connect_request(acct, name))
            self.assertEqual(C.parse_connect_response((await read_frames(r, 1))[0][2])["service"], 0)
            w.write(C.session_begin(0, C.SVC_MATCH))
            (s, m, p), = await read_frames(r, 1)
            self.assertEqual((s, m), (0, 2))
            self.assertNotEqual(C.parse_session_begin(p)["value"], 0)
            w.write(C.match_request(9, slot, [player_record(acct, name)]))
            return r, w

        async def go():
            a = argparse.Namespace(host="127.0.0.1", http=0, sam=0, connect_reply="svc0", echo_keepalive=True,
                                   idle=10, min_players=2, matchmaker=None)
            ready = asyncio.get_running_loop().create_future()
            task = asyncio.ensure_future(gowa_server.serve(a, ready))
            _, sam_port = await ready
            ra, wa = await client(sam_port, 101, "MpTestA", -1)
            fa = await read_frames(ra, 1)
            rb, wb = await client(sam_port, 102, "MpTestB", -1)
            fb = await read_frames(rb, 3)
            fa += await read_frames(ra, 3)
            wa.close(), wb.close()
            task.cancel()
            return fa, fb

        fa, fb = asyncio.run(go())
        self.assertEqual([(s, m) for s, m, _ in fa], [(1, 0x17), (1, 0x17), (1, 0x15), (1, 0x16)])
        self.assertEqual([(s, m) for s, m, _ in fb], [(1, 0x17), (1, 0x15), (1, 0x16)])
        ga, gb = C.parse_game_world_reserved(fa[2][2]), C.parse_game_world_reserved(fb[1][2])
        self.assertEqual(ga["world_id"], gb["world_id"])
        self.assertEqual([struct.unpack(">Q", r[:8])[0] for r in gb["roster"]], [101, 102])


class LeaderboardTests(unittest.TestCase):
    """2026-11-14: leaderboards from the players' own TUS profile stats (leaderboard.py, /api/report, /api/leaderboard)."""

    def test_parse_and_caps(self):
        import leaderboard as L
        self.assertEqual(L.parse_stats("XP:800,Favor:590,Goat:12"), {"XP": 800, "Favor": 590})   # unknown skipped
        for bad in ("", "XP:-1", "XP:1e9", "XP:" + "9" * 13, "XP:%d" % (L.CAPS["XP"] + 1), "Goat:3", "x" * 700,
                    "XP:1;DROP", "1XP:5"):
            with self.assertRaises(L.ReportError, msg=bad):
                L.parse_stats(bad)

    def test_report_rules_and_boards(self):
        import leaderboard as L
        lb = L.Leaderboard(":memory:")
        t = 1_800_000_000
        lb.report(1, "MpTestA", {"XP": 23776, "Favor": 1025, "FFA1stPlace": 23, "GameOver": 68}, t)   # history: free
        lb.report(2, "MpTestB", {"XP": 800, "Favor": 590, "TeamVictory": 2, "FFA1stPlace": 1}, t)
        with self.assertRaises(L.ReportError):                                   # too soon
            lb.report(1, "MpTestA", {"XP": 24000}, t + 5)
        with self.assertRaises(L.ReportError):                                   # +100k XP in 10 minutes
            lb.report(1, "MpTestA", {"XP": 123776}, t + 600)
        lb.report(1, "MpTestA", {"XP": 26000, "GameOver": 69}, t + 600)          # one more match: fine
        lb.report(1, "MpTestA", {"XP": 100, "GameOver": 1}, t + 1200)            # a reset profile keeps its best
        self.assertEqual(lb.player("mptesta")["best"]["XP"], 26000)
        self.assertEqual(lb.player("MpTestA")["last"]["XP"], 100)
        self.assertEqual([(r["rank"], r["name"], r["value"]) for r in lb.top("xp")], [(1, "MpTestA", 26000), (2, "MpTestB", 800)])
        self.assertEqual([(r["name"], r["value"]) for r in lb.top("wins")], [("MpTestA", 23), ("MpTestB", 3)])
        self.assertEqual(lb.top("kills"), [])                                    # nobody reported PlayerKill
        self.assertEqual([r["name"] for r in lb.top("xp", excluded=["mptesta"])], ["MpTestB"])   # banned accounts
        self.assertEqual(lb.hide("MPTESTA"), 1)
        self.assertEqual([r["name"] for r in lb.top("xp")], ["MpTestB"])
        lb.hide("MpTestA", False)
        self.assertEqual(lb.reset("MpTestA"), 1)
        self.assertIsNone(lb.player("MpTestA"))
        pub = lb.public()
        self.assertIn("not verified", pub["note"])
        self.assertEqual([b["key"] for b in pub["boards"]], [b[0] for b in L.BOARDS])
        lb.report(3, "MpTestC", {"XP": 800}, t)
        self.assertEqual([r["rank"] for r in lb.top("xp")], [1, 1])               # ties share a rank

    def test_report_and_boards_over_loopback(self):
        import accounts, tempfile, json as _json
        d = tempfile.mkdtemp()

        async def http(port, method, path, key=None, body=None):
            r, w = await asyncio.open_connection("127.0.0.1", port)
            data = _json.dumps(body).encode() if body is not None else b""
            head = "%s %s HTTP/1.0" % (method, path)
            if key:
                head += chr(13) + chr(10) + "X-Admin-Key: " + key
            if data:
                head += chr(13) + chr(10) + "Content-Length: %d" % len(data)
            w.write(head.encode() + bytes([13, 10, 13, 10]) + data)
            raw = await r.read()
            w.close()
            h, b = raw.split(bytes([13, 10, 13, 10]), 1)
            return int(h.split()[1]), b

        async def report(hp, name, tok, stats, proof_name=None):
            k = accounts.derive_key(tok, name)
            _, n = await http(hp, "GET", "/nonce")
            n = n.decode().strip()
            pr = accounts.proof_for(k, n, proof_name or name)
            return await http(hp, "GET", "/api/report?id=%s&nonce=%s&proof=%s&s=%s" % (name, n, pr, stats))

        async def go():
            a = argparse.Namespace(host="127.0.0.1", http=0, sam=0, connect_reply="svc0", echo_keepalive=True,
                                   idle=10, min_players=2, matchmaker=None, data=d, allow_unsigned=False)
            ready = asyncio.get_running_loop().create_future()
            task = asyncio.ensure_future(gowa_server.serve(a, ready))
            hp, _ = await ready
            key = open(os.path.join(d, "admin.key")).read().strip()
            for name, tok in (("MpTestA", "a" * 32), ("MpTestB", "b" * 32)):
                await http(hp, "GET", "/ticket?id=%s&reg=%s" % (name, accounts.derive_key(tok, name).hex()))
            o = {}
            o["a"] = await report(hp, "MpTestA", "a" * 32, "XP:23776,Favor:1025,FFA1stPlace:23,GameOver:68,Goat:30")
            o["b"] = await report(hp, "MpTestB", "b" * 32, "XP:800,Favor:590,PlayerKill:4")
            o["wrongkey"] = await report(hp, "MpTestB", "c" * 32, "XP:999999")
            o["unknown"] = await report(hp, "Nobody1", "c" * 32, "XP:1")
            o["noproof"] = await http(hp, "GET", "/api/report?id=MpTestA&s=XP:1")
            o["toosoon"] = await report(hp, "MpTestA", "a" * 32, "XP:23800")
            o["cap"] = await report(hp, "MpTestB", "b" * 32, "XP:999999999")
            o["all"] = _json.loads((await http(hp, "GET", "/api/leaderboard"))[1])
            o["xp"] = _json.loads((await http(hp, "GET", "/api/leaderboard?board=xp&n=5"))[1])
            o["badboard"] = (await http(hp, "GET", "/api/leaderboard?board=nope"))[0]
            o["page"] = await http(hp, "GET", "/leaderboard")
            o["pl"] = _json.loads((await http(hp, "GET", "/admin/api/players", key))[1])["players"]
            o["nokey"] = (await http(hp, "POST", "/admin/api/lb/hide", None, {"name": "MpTestA"}))[0]
            o["hide"] = _json.loads((await http(hp, "POST", "/admin/api/lb/hide", key, {"name": "MpTestA"}))[1])
            o["xp_hidden"] = _json.loads((await http(hp, "GET", "/api/leaderboard?board=xp"))[1])
            o["show"] = _json.loads((await http(hp, "POST", "/admin/api/lb/show", key, {"name": "MpTestA"}))[1])
            o["one"] = _json.loads((await http(hp, "GET", "/admin/api/lb?name=MpTestB", key))[1])
            await http(hp, "POST", "/admin/api/ban", key, {"name": "MpTestB"})
            o["xp_banned"] = _json.loads((await http(hp, "GET", "/api/leaderboard?board=xp"))[1])
            o["reset"] = _json.loads((await http(hp, "POST", "/admin/api/lb/reset", key, {"name": "MpTestA"}))[1])
            o["xp_reset"] = _json.loads((await http(hp, "GET", "/api/leaderboard?board=xp"))[1])
            task.cancel()
            return o

        o = asyncio.run(go())
        self.assertEqual((o["a"][0], o["b"][0]), (200, 200))
        self.assertEqual((o["wrongkey"][0], o["unknown"][0], o["noproof"][0]), (403, 403, 403))
        self.assertEqual((o["toosoon"][0], o["cap"][0]), (400, 400))
        self.assertIn("not verified", o["all"]["note"])
        xp = [(r["rank"], r["name"], r["value"]) for r in o["xp"]["boards"][0]["rows"]]
        self.assertEqual(xp, [(1, "MpTestA", 23776), (2, "MpTestB", 800)])
        kills = next(b for b in o["all"]["boards"] if b["key"] == "kills")["rows"]
        self.assertEqual([(r["name"], r["value"]) for r in kills], [("MpTestB", 4)])
        self.assertEqual(o["badboard"], 404)
        self.assertEqual(o["page"][0], 200)
        self.assertIn(b"not verified", o["page"][1])
        self.assertEqual({p["name"]: p["lb"] for p in o["pl"]}, {"MpTestA": "listed", "MpTestB": "listed"})
        self.assertEqual(o["nokey"], 403)
        self.assertEqual((o["hide"]["changed"], o["show"]["changed"]), (1, 1))
        self.assertEqual([r["name"] for r in o["xp_hidden"]["boards"][0]["rows"]], ["MpTestB"])
        self.assertEqual(o["one"]["best"]["XP"], 800)
        self.assertEqual([r["name"] for r in o["xp_banned"]["boards"][0]["rows"]], ["MpTestA"])
        self.assertEqual(o["reset"]["deleted"], 1)
        self.assertEqual(o["xp_reset"]["boards"][0]["rows"], [])


if __name__ == "__main__":
    gowa_server.log = lambda *a: None      # keep the test output short
    unittest.main(verbosity=1)
