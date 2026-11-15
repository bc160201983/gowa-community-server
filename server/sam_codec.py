# GoW: Ascension community server: SAM wire codec (2026-11-13). Python 3 standard library only.
# Clean room: every layout here was read from the game's own serializers and handlers in the user's EBOOT
# (static reading, host\mpnet\NOTES.md "2026-11-13"). No game run has checked them yet: VERIFIED = read from the
# code, INFERRED = my reading of what a field means. No code, keys or data from Sony or other projects.
#
# Frame (VERIFIED): u16 BE total length (incl. these 2 bytes), u8 serviceId, u8 messageId, payload (big-endian).
# A bare 00 00 is a keepalive.
import struct

MAX_FRAME = 0x4000
SVC_SESSION, SVC_MATCH, SVC_PROFILE, SVC_DATA, SVC_GAMETRACKER = 0, 1, 2, 3, 4

# (service, message) -> name, direction (C = client sends, S = server sends, B = both). Ids VERIFIED from the
# client's id tables (0x9D25AC session, 0x9D2598 match) and its two receive factories (0x4D5D84, 0x4D59B8).
MESSAGES = {
    (0, 0): ("ConnectRequest", "C"),
    (0, 1): ("ConnectResponse", "S"),
    (0, 2): ("SessionBegin", "B"),       # name INFERRED: asks for a session on one service
    (0, 3): ("SessionEnd", "B"),
    (0, 4): ("Session4", "S"),           # u32, u32, u8: meaning not read yet
    (0, 5): ("SessionData", "C"),        # u16 len, u8, bytes: meaning not read yet
    (1, 1): ("MatchRequest", "C"),       # search / quickmatch request
    (1, 4): ("MatchPlayerReport", "C"),  # name INFERRED: world id + n flags + n account ids
    (1, 0x14): ("Match14", "S"),         # u32 n, u32 len, u32[n], bytes[len]: no handler in the match context
    (1, 0x15): ("GameWorldReserved", "S"),
    (1, 0x16): ("GameWorldUpdate", "S"),
    (1, 0x17): ("SearchUpdate", "S"),
}

RESULT_CODES = ["Success", "Timeout", "Unmatchable", "NoGameSlots", "InvalidGameName", "BadMaxGameSize",
                "InvalidPassword", "GameNotFound", "OptionMiss", "GenericError", "InvalidRequest",
                "NoApplicationData", "ApplicationDataInvalid", "MissingAccountInfo", "InvalidMaxPlayers",
                "InvalidNATType"]  # VERIFIED: the client's own names for GameWorldReserved's result byte

PLAYER_REC = 0x61      # the record a client sends about a player (VERIFIED size)
ROSTER_REC = 0x67      # the same record + 6 bytes the server adds (VERIFIED size)
MAX_PLAYERS = 8        # VERIFIED: every roster parser refuses more than 8
RESERVED_HDR = 0x13


class CodecError(ValueError):
    pass


def frame(svc, msg, payload=b""):
    if 4 + len(payload) > MAX_FRAME:
        raise CodecError("frame too long: %d" % (4 + len(payload)))
    return struct.pack(">HBB", 4 + len(payload), svc, msg) + payload


def split_frames(buf):
    """Returns (list of (svc, msg, payload) or None for a keepalive, rest). Raises CodecError on a bad length."""
    out = []
    while len(buf) >= 2:
        n = struct.unpack(">H", buf[:2])[0]
        if n == 0:
            out.append(None)
            buf = buf[2:]
            continue
        if n < 4 or n > MAX_FRAME:
            raise CodecError("bad frame length %d" % n)
        if len(buf) < n:
            break
        out.append((buf[2], buf[3], buf[4:n]))
        buf = buf[n:]
    return out, buf


def _need(p, n, what):
    if len(p) < n:
        raise CodecError("%s: %d bytes, need %d" % (what, len(p), n))


# ---------------------------------------------------------------------------------------------- session service
def parse_connect_request(p):
    """(0,0) VERIFIED order (serializer 0x4D3160): u64 a, u64 b, u32 c, u16 ticketLen, u8 d, u8 e, ticket, [u64 f]."""
    _need(p, 24, "ConnectRequest")
    a, b, c, tlen, d, e = struct.unpack(">QQIHBB", p[:24])
    _need(p, 24 + tlen, "ConnectRequest ticket")
    rest = p[24 + tlen:]
    f = struct.unpack(">Q", rest[:8])[0] if len(rest) >= 8 else None
    return {"a": a, "b": b, "c": c, "ticket_len": tlen, "d": d, "e": e, "ticket": p[24:24 + tlen], "f": f,
            "trailing": len(rest)}


def connect_response(acct, session_id, service, response=0, log_level=0, reason=0):
    """(0,1) VERIFIED (serializer 0x4D2DAC, checks 0x3D5244): the client needs acct == its own, response == 0;
    service 0 must carry session 0; service 1 (Match) / 2 (Profile) with a non-zero session opens that service;
    any other case with reason >= 0x40 is 'cannot establish session'."""
    return frame(0, 1, struct.pack(">QIBBBB", acct, session_id, service, response, log_level, reason))


def parse_connect_response(p):
    _need(p, 16, "ConnectResponse")
    acct, sid, svc, resp, ll, reason = struct.unpack(">QIBBBB", p[:16])
    return {"acct": acct, "session_id": sid, "service": svc, "response": resp, "log_level": ll, "reason": reason}


def parse_session_begin(p):
    """(0,2) VERIFIED layout u32, u8 (serializer 0x4D309C; built by 0x4D3428 with r4 = service 1/2/4).
    INFERRED: the u32 is a client value (agent config +0x110), the u8 the service asked for."""
    _need(p, 5, "SessionBegin")
    v, svc = struct.unpack(">IB", p[:5])
    return {"value": v, "service": svc}


def session_begin(value, service):
    return frame(0, 2, struct.pack(">IB", value, service))


def parse_session_end(p):
    """(0,3) VERIFIED layout u32 sessionId, u8 service, u8 reason (serializer 0x4D2FB8; built by 0x4D3334)."""
    _need(p, 6, "SessionEnd")
    sid, svc, reason = struct.unpack(">IBB", p[:6])
    return {"session_id": sid, "service": svc, "reason": reason}


def session_end(session_id, service, reason=0):
    return frame(0, 3, struct.pack(">IBB", session_id, service, reason))


def parse_session_data(p):
    """(0,5) VERIFIED layout u16 len, u8, bytes[len] (serializer 0x4D38D4)."""
    _need(p, 3, "SessionData")
    n, b = struct.unpack(">HB", p[:3])
    _need(p, 3 + n, "SessionData body")
    return {"kind": b, "data": p[3:3 + n]}


# ------------------------------------------------------------------------------------------------ match service
def parse_player_record(r):
    """One 0x61-byte player record. VERIFIED: u64 account at +0, a 36-byte SceNpId-sized block at +8 whose first 16
    bytes are the name shown in search lists. The rest (+0x2C..+0x60, network data) is carried, not interpreted."""
    _need(r, PLAYER_REC, "player record")
    return {"acct": struct.unpack(">Q", r[:8])[0], "name16": r[8:24],
            "name": r[8:24].split(b"\0")[0].decode("ascii", "replace"), "raw": bytes(r[:PLAYER_REC])}


def parse_match_request(p):
    """(1,1) envelope VERIFIED (serializer 0x4D2B34): u16 len, u8 a, u8 b, u8 c, u8 d, blob[len].
    Blob VERIFIED (writer 0x27A860): 16-byte header {u64 shard, u32 key, i8 slot, u8 0, u8 nplayers,
    u8 flags: bit7 f1, bit6 f2, bits0-4 n64}, n64 * u64, nplayers * 0x61 player records, application data.
    MEASURED 2026-11-13 (run 114525): shard = the client's shard key (0xB92E156B800038D0, the same value as
    ConnectRequest's first u64), key 0, slot 8 = the quickmatch game mode (log: "defaultGameMode: 8", THUN2),
    nplayers 1, flags 0x40, envelope a,b,c,d = 2,2,2,0 (log: "defaultMinPlayers: 2 defaultMaxPlayers: 2").
    'slot' is the game mode (ctx+0x31, or its default ctx+0x32 when -1); GameWorldReserved must echo it."""
    _need(p, 6, "MatchRequest")
    n, a, b, c, d = struct.unpack(">HBBBB", p[:6])
    _need(p, 6 + n, "MatchRequest blob")
    blob = p[6:6 + n]
    _need(blob, 16, "MatchRequest header")
    shard, key, slot, _z, nplayers, flags = struct.unpack(">QIbBBB", blob[:16])
    n64 = flags & 0x1F
    if nplayers > MAX_PLAYERS:
        raise CodecError("MatchRequest: %d players" % nplayers)
    off = 16
    _need(blob, off + 8 * n64 + PLAYER_REC * nplayers, "MatchRequest lists")
    ids = list(struct.unpack(">%dQ" % n64, blob[off:off + 8 * n64]))
    off += 8 * n64
    players = [parse_player_record(blob[off + i * PLAYER_REC:off + (i + 1) * PLAYER_REC]) for i in range(nplayers)]
    off += PLAYER_REC * nplayers
    return {"a": a, "b": b, "c": c, "d": d, "shard": shard, "key": key, "slot": slot,
            "pool": key, "flag7": flags >> 7, "flag6": (flags >> 6) & 1,
            "ids": ids, "players": players, "app_data": blob[off:]}


def match_request(key, slot, players, ids=(), app_data=b"", a=0, b=0, c=0, d=0, flag7=0, flag6=0, shard=0):
    """Builds a client's (1,1) frame; used by the tests and the self-test, never sent by the server."""
    flags = (flag7 << 7) | (flag6 << 6) | (len(ids) & 0x1F)
    blob = struct.pack(">QIbBBB", shard, key, slot, 0, len(players), flags)
    blob += b"".join(struct.pack(">Q", i) for i in ids) + b"".join(players) + app_data
    return frame(1, 1, struct.pack(">HBBBB", len(blob), a, b, c, d) + blob)


def parse_player_report(p):
    """(1,4) VERIFIED (serializer 0x4D2864): u32 worldId, u8 n, bytes[n], u64[n]."""
    _need(p, 5, "MatchPlayerReport")
    wid, n = struct.unpack(">IB", p[:5])
    _need(p, 5 + 9 * n, "MatchPlayerReport lists")
    return {"world_id": wid, "flags": list(p[5:5 + n]), "accts": list(struct.unpack(">%dQ" % n, p[5 + n:5 + 9 * n]))}


def search_update(status, names16):
    """(1,0x17) VERIFIED (handler 0x3D25FC): u16 len, then i8 status, u8 count (1..8), count * 16-byte names;
    len must be 2 + 16 * count. The client logs 'playerCount' and keeps the status byte."""
    if not 1 <= len(names16) <= MAX_PLAYERS:
        raise CodecError("SearchUpdate needs 1..8 players")
    blob = struct.pack(">bB", status, len(names16)) + b"".join(n.ljust(16, b"\0")[:16] for n in names16)
    return frame(1, 0x17, struct.pack(">H", len(blob)) + blob)


def parse_search_update(p):
    _need(p, 2, "SearchUpdate")
    n = struct.unpack(">H", p[:2])[0]
    blob = p[2:2 + n]
    _need(blob, 2, "SearchUpdate blob")
    status, count = struct.unpack(">bB", blob[:2])
    if len(blob) != 2 + 16 * count or not 1 <= count <= MAX_PLAYERS:
        raise CodecError("SearchUpdate size")
    return {"status": status, "names16": [blob[2 + 16 * i:18 + 16 * i] for i in range(count)]}


HOST_MARK = 0x01000000   # +0x61 bytes 01 00 00 00


def roster_record(player_raw, peer_index, host=False, extra8=0):
    """0x67 roster record = the client's own 0x61 record + 4 bytes at +0x61, u8 at +0x65, u8 at +0x66.
    VERIFIED 2026-11-13: +0x61 marks the match host. ProcessNewPeer 0x1A33C0, own record: host state -> connected
    (0x27AE1C) only if +0x61 == 01 00 00 00; the host state machine copies its own +0x61 at start (0x27AED4) and a
    new peer whose +0x61 equals it gives state 3, otherwise 2 (both = connected). +0x65 = peer index (duplicates:
    'SamPeerIndexCollusion'). +0x66 not read yet (0)."""
    if len(player_raw) != PLAYER_REC:
        raise CodecError("player record must be 0x61 bytes")
    return player_raw + struct.pack(">IBB", HOST_MARK if host else 0, peer_index, extra8)


def reserved_header(world_key16, slot, count, flags=0):
    """0x13-byte header of GameWorldReserved's blob (VERIFIED parser 0x27AB88, users 0x3D2870/0x3D3B5C):
    16 bytes kept by the client as the world's key, i8 slot (must equal the slot the client asked for unless it
    asked for -1), u8 count, u8 flags (bit 2 set = no roster follows)."""
    return world_key16.ljust(16, b"\0")[:16] + struct.pack(">bBB", slot, count, flags)


def game_world_reserved(world_id, result, world_key16=b"", slot=-1, roster=()):
    """(1,0x15) VERIFIED (serializer 0x4D2C6C, handler 0x3D2D28): u32 worldId, u16 len, u8 result, blob[len].
    result 0 = Success (blob = header + count * 0x67 records), 1 = Timeout, others = error (see RESULT_CODES).
    The client accepts it only while it is searching."""
    if len(roster) > MAX_PLAYERS:
        raise CodecError("roster too long")
    blob = b""
    if result == 0:
        blob = reserved_header(world_key16, slot, len(roster)) + b"".join(roster)
    return frame(1, 0x15, struct.pack(">IHB", world_id, len(blob), result) + blob)


def parse_game_world_reserved(p):
    _need(p, 7, "GameWorldReserved")
    wid, n, result = struct.unpack(">IHB", p[:7])
    blob = p[7:7 + n]
    out = {"world_id": wid, "result": result, "roster": []}
    if result == 0:
        _need(blob, RESERVED_HDR, "GameWorldReserved header")
        slot, count, flags = struct.unpack(">bBB", blob[16:19])
        out.update(world_key16=blob[:16], slot=slot, count=count, flags=flags)
        if count and not flags & 2:
            if len(blob) != RESERVED_HDR + ROSTER_REC * count or count > MAX_PLAYERS:
                raise CodecError("GameWorldReserved roster size")
            out["roster"] = [blob[RESERVED_HDR + ROSTER_REC * i:RESERVED_HDR + ROSTER_REC * (i + 1)]
                             for i in range(count)]
    return out


def game_world_update(world_id, roster):
    """(1,0x16) VERIFIED (serializer 0x4D2A3C, handler 0x3D33CC, parser 0x27AAD8): u32 worldId, u16 len, blob =
    u8 count + count * 0x67 records. The client accepts it only for the world it is in."""
    if len(roster) > MAX_PLAYERS:
        raise CodecError("roster too long")
    blob = bytes([len(roster)]) + b"".join(roster)
    return frame(1, 0x16, struct.pack(">IH", world_id, len(blob)) + blob)


def parse_game_world_update(p):
    _need(p, 6, "GameWorldUpdate")
    wid, n = struct.unpack(">IH", p[:6])
    blob = p[6:6 + n]
    _need(blob, 1, "GameWorldUpdate blob")
    count = blob[0]
    if len(blob) != 1 + ROSTER_REC * count or count > MAX_PLAYERS:
        raise CodecError("GameWorldUpdate roster size")
    return {"world_id": wid, "roster": [blob[1 + ROSTER_REC * i:1 + ROSTER_REC * (i + 1)] for i in range(count)]}


def describe(svc, msg, p):
    """A short text for the log; never raises."""
    name = MESSAGES.get((svc, msg), ("svc%d/%d" % (svc, msg), "?"))[0]
    try:
        if (svc, msg) == (0, 2):
            return "%s %r" % (name, parse_session_begin(p))
        if (svc, msg) == (0, 3):
            return "%s %r" % (name, parse_session_end(p))
        if (svc, msg) == (0, 5):
            d = parse_session_data(p)
            return "%s kind %d, %d bytes" % (name, d["kind"], len(d["data"]))
        if (svc, msg) == (1, 1):
            m = parse_match_request(p)
            return "%s key %d slot %d players %s ids %s app %d B abcd %d,%d,%d,%d" % (
                name, m["key"], m["slot"], [(q["acct"], q["name"]) for q in m["players"]], m["ids"],
                len(m["app_data"]), m["a"], m["b"], m["c"], m["d"])
        if (svc, msg) == (1, 4):
            return "%s %r" % (name, parse_player_report(p))
    except CodecError as e:
        return "%s (not decoded: %s)" % (name, e)
    return name
