# mpnet server: the TSS ("title small storage") blob the game downloads at boot, built from our own config.
# The runtime hands it to the game as PLAINTEXT (the game's own decrypt at 0x3319BC is skipped by the private
# build's source patch 2), so the blob is just the container the game walks (VERIFIED static, func_003316B8 and
# func_00331838; NOTES.md sec. 2 + 3):
#   u32 version = 1 (BE; the game rejects anything else, 0x331A64), u32 0, u32 0, u32 count,
#   count x 0x2C-byte entries { char name[32]; u16 size; u16 offset (from the blob start); u32 version; u32 id },
#   then the documents (NUL-terminated JSON text). Max 0x10000 bytes (the game's buffer).
# MatchConfig (listener id 5) is parsed STRICTLY (MatchConfig_parse 0x3F2260): every key below must be present with
# the right JSON type (int / float with a decimal point / bool / string) and in range, or the whole config is
# rejected. Keys, types and ranges come from tools\mcschema.py; values default to the game's own built-in
# defaults (func_005D2F88) where we read them, otherwise to small sane numbers. Durations are microseconds.
import json, struct

MAX_BLOB = 0x10000


# The client's shard key is func_00327720 = (data hash << 32) | 0x80000000 | (0x81B2B0 << 14) | code version.
# MEASURED 2026-11-13: data hash a5a9b090 / b92e156b (top bit set) -> the key is above 2**63, so the full u64 range.
# The parser stores RangeHigh as a u64 (peek: 0x7FFFFFFFFFFFFFFF arrived intact at 0x93A9B8), but the game's JSON
# reader is signed: 18446744073709551615 -> "too big integer", whole MatchConfig rejected (error 2:122, MEASURED).
# -1 is stored as 0xFFFFFFFFFFFFFFFF and the selection compares unsigned (cmpld), so -1 = u64 max (INFERRED).
TARGET_RANGE_HIGH = -1


def match_config(host, sam_port, geo_port, geo_path="/geo"):
    mc = {
        "ShardStrategy": 1,                      # 0..2
        "RoundRobinIfReconnectEnabled": True,
        "GeoServerAddr": host,                   # str[0x40]
        "GeoPath": geo_path,                     # str[0x40]
        "GeoPort": geo_port,                     # 1024..65535
        "GeoTimeoutSecs": 10,
        "GeoLatitudeToken": "latitude",
        "GeoLongitudeToken": "longitude",
        "GeoCountryCodeToken": "countryCode",
        "GeoCityToken": "city",
        "GeoLookupPeriod": 3600000000,           # >= 1e6
        "BandwidthProbeLookupPeriod": 3600000000,
        "BandwidthProbeEnable": False,           # no bandwidth probe server
        "BandwidthProbeBufferSize": 65536,
        "BandwidthProbeMaxAge": 3600000000,
        "StatsFileName": "samstats.json",        # str[0x20]
        "StatsVersion": 3,                       # >= 3
        "StatsDecayDurationTicks": 2592000000000,
        "GameTrackerDecayDurationTicks": 2592000000000,
        "StatsCntSmoothingBits": 3,              # 0..255 (the six *SmoothingBits)
        "GameTrackerCntSmoothingBits": 3,
        "StatsRTTSmoothingBits": 3,
        "StatsPacketLossSmoothingBits": 3,
        "StatsBandwidthSmoothingBits": 3,
        "StatsP2PBandwidthSmoothingBits": 3,
        "MaxReconnectResessionLimit": 32,
        "SAMTotalSearchTime": 300,               # >= 1 (default 120); 300 = players wait 5 min before "Matchmaker Error 2:86" (2026-11-13)
        "SAMConnectTime": 20,                    # >= 1 (default 20)
        "SAMRetryDelay": 2,                      # >= 1 (default 2)
        "SAMDefaultMsgSize": 1024,               # 1024..65535 (default 0x400)
        "SAMBwProbeTime": 20,
        "SAMTusTime": 20,
    }
    # The SAM server list: target0..target3 are ALL required (0x3F1CE0 loops 4 times, a missing one is an error).
    # 2026-11-13 (static, SamStateStart 0x3D48F4): the client picks a target whose [RangeLow, RangeHigh] (u64,
    # stored at target +0x48/+0x50) holds a key from func_00327720 = (global 0x81B2A8 << 32) | 0x80000000 | ...;
    # that key always has bit 31 set, so 0..0 never holds it and only a 0/0 fallback could pick a target. One game
    # run with 0/0 ended in "Matchmaker Error 2:97" (= no target chosen, line 208). A full range lets every target
    # match directly. INFERRED fix: not checked by a game run yet.
    for i in range(4):
        mc["target%d" % i] = {"SAMServerHostAddr": host, "SAMServerPort": sam_port,
                              "RangeLow": 0, "RangeHigh": TARGET_RANGE_HIGH, "Probability": 100}
    # Elo skill model: elo0..elo7 all required (0x3F09C0 loops 8 times). Floats must be JSON floats.
    for i in range(8):
        mc["elo%d" % i] = {
            "EloVersion": 1, "EloMinSkill": 100, "EloMaxSkill": 6000, "EloMinK": 1, "EloMaxK": 116,
            "EloKX1": 116, "EloKX2": 10, "EloKY1": 100, "EloKY2": 2700, "EloMinA": 1, "EloMaxA": 200,
            "EloAX1": 200, "EloAX2": 70, "EloAY1": 100, "EloAY2": 2700,
            "EloDeflationAdjust": -0.016, "EloEuler": 2.718282, "EloDefaultSkill": 1000,
            "EloWinScore": 1.0, "EloDrawScore": 0.5, "EloLossScore": 0.0,
        }
    # TransportConfig is a sibling of MatchConfig at the ROOT (read from the root object at 0x3F3434).
    tc = {
        "GameMeshReceiveTimeOutSecs": 10, "GameMeshKeepAliveSendSecs": 1, "GameMeshTimeOutLogSecs": 5,
        "GameMeshRemotePeerActivationTimeOutSecs": 20, "GameMeshRestrictedKeepAlives": False,
        "MeshStateUpdateListSizeLocal": 32, "MeshStateUpdateListSizePerCon": 32,
        "MeshDisconnectProcessInterval": 1, "MeshDisconnectBufferTime": 5,
        "EnableNPSigBandwidthProbe": False, "BandwidthPerNode": 32768,
        "EnablePacketForwarding": True, "MinForwardLimit": 1, "MaxForwardLimit": 4,
        "EnableHostKickOut": False, "MeshActivationMinDelay": 0, "MeshActivationMaxDelay": 1,
        "RosterCheckFreqUpdateCount": 30,
    }
    return {"MatchConfig": mc, "TransportConfig": tc}


def container(docs):
    """docs: list of (name, id, version, text). Returns the plaintext TSS blob."""
    n = len(docs)
    head = struct.pack(">IIII", 1, 0, 0, n)
    off = 0x10 + 0x2C * n
    dirs, body = b"", b""
    for name, ident, ver, text in docs:
        data = text.encode("utf-8") if isinstance(text, str) else bytes(text)
        if not data.endswith(b"\0"):
            data += b"\0"
        while (off + len(body)) % 4:
            body += b"\0"
        o = off + len(body)
        nb = name.encode("ascii")
        assert len(nb) < 32 and len(data) < 0x10000 and o < 0x10000
        dirs += nb.ljust(32, b"\0") + struct.pack(">HHII", len(data), o, ver, ident)
        body += data
    blob = head + dirs + body
    if len(blob) > MAX_BLOB:
        raise ValueError("TSS blob %d bytes > %d" % (len(blob), MAX_BLOB))
    return blob


# The Playlist the original PS3 servers sent (2026-11-13, parser func_004E24D0, key order read from the code): per game
# mode its maps, matchmaking settings and the lobby countdowns. Without it the game uses built-in defaults (~60 s of
# lobby before the round, MEASURED). Seconds; decimals until a run shows the type (a wrong field = "Parse error:" in
# the game log and the whole Playlist is ignored, i.e. the old defaults - harmless).
PLAYLIST_MODES = [   # (mode, maps, players needed, in Quick Play) - host/mp/NOTES.md mode table (STATIC)
    ("TCTF8", ["Bog8CTF", "Lab8CTF", "Try8CTF", "Des8CTF"], 6, True),
    ("FFR4", ["HRC4FR", "PHT4FR", "Per4FR", "Oly4FR"], 4, True),
    ("FFR8", ["Bog8FR", "Lab8FR", "Try8FR", "Des8FR"], 6, True),
    ("TFR4", ["Oly4TFR", "Hrc4TFR", "Pht4TFR", "Per4TFR"], 4, True),
    ("TFR8", ["Bog8TFR", "Des8TFR", "Lab8TFR", "Try8TFR"], 6, True),
    ("THUN2", ["Des2GNT", "Hrc2GNT", "Oly2GNT"], 2, True),
]
LOBBY = {"TotalCountdown": 30, "AdditionalTotalForPlayerJoin": 5, "MaxAdditionalTotal": 15}   # whole seconds (MEASURED: 30.0 = "Parse error")
COUNTDOWN = {"Countdown": 15, "UnskippableCountdown": 5, "SkipAmount": 5, "FinalCountdown": 5}


def playlist():
    modes = []
    for mode, maps, need, quick in PLAYLIST_MODES:
        mm = {"NumRequiredPlayers": need, "NumRequiredPlayersForSAM": need}
        mm.update(LOBBY)
        mm["AvailableInQuickPlay"] = quick
        modes.append({"GameMode": mode, "Levels": maps, "MatchmakingSettings": mm,
                      "CountdownFirstRound": dict(COUNTDOWN), "CountdownIntermission": dict(COUNTDOWN)})
    return {"GameModes": modes}


# ProtocolConfig (2026-11-14): TSS listener "ProtocolConfig", id 1 (registration func_004E307C), parsed leniently
# by func_004E3118 (every key optional; a number is stored as a float in the table at 0x885D28 through setters
# 0x139644..0x139958). Game defaults MEASURED with PS3_PEEK (run n2-us-lobby-20261114-104549/105348): BadRTT 400,
# BadPacketLoss 10, DisconnectNoPacketsTime 1, HostCutoffRTT 400 (ms), HostCutoffSeconds 5, LagSwitchDetectTime 1,
# LagSwitchPenaltyTime 1. A laptop froze when the host's packets came back after a 4.4 s gap over the relay
# (2026-11-14 10:37, user + friend Pakistan <-> USA, ~300 ms): the lag-switch / host-cutoff paths are raised so a
# few seconds of trouble on a long route no longer trigger them.
PROTOCOL_CONFIG = {"Interface": {"LagSwitchDetectTime": 30.0, "LagSwitchPenaltyTime": 1.0,
                                 "HostCutoffRTT": 3000.0, "HostCutoffSeconds": 30.0}}


def build(host, sam_port, geo_port, geo_path="/geo", version=1, with_playlist=True, with_protocol=False):
    mc = json.dumps(match_config(host, sam_port, geo_port, geo_path), separators=(",", ":"))
    entries = [("MatchConfig", 5, version, mc)]
    if with_playlist:
        entries.append(("Playlist", 2, version, json.dumps(playlist(), separators=(",", ":"))))
    if with_protocol:
        entries.append(("ProtocolConfig", 1, version, json.dumps(PROTOCOL_CONFIG, separators=(",", ":"))))
    return container(entries)


def parse(blob):
    """For tests/logging: [(name, size, offset, version, id, text)]."""
    v, _, _, n = struct.unpack(">IIII", blob[:16])
    out = []
    for i in range(n):
        e = blob[16 + 0x2C * i: 16 + 0x2C * (i + 1)]
        name = e[:32].split(b"\0")[0].decode()
        size, off, ver, ident = struct.unpack(">HHII", e[32:44])
        out.append((name, size, off, ver, ident, blob[off:off + size].rstrip(b"\0").decode()))
    return v, out


if __name__ == "__main__":
    b = build("127.0.0.1", 36102, 36101)
    v, es = parse(b)
    print("blob", len(b), "bytes, version", v)
    for e in es:
        print(e[:5], len(e[5]), "chars JSON, top keys:", list(json.loads(e[5]))[:3])
