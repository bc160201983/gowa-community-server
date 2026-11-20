# GoW: Ascension community server prototype, stage 1 (2026-11-10). Python 3 standard library only.
# Clean room: written from our own static reading of the user's EBOOT (host\mpnet\NOTES.md sec. 1-4); no code from
# other projects, no Sony keys or certificates. It never opens an outgoing connection.
#
#   python -I gowa_server.py [--host 127.0.0.1] [--http 36101] [--sam 36102] [--log server.log]
#   python -I gowa_server.py --selftest          # starts on free loopback ports, talks to itself, exits 0/1
#
# STAGE 1 IS LOOPBACK ONLY: --host must be a 127.x.x.x address, anything else is refused.
#
# HTTP (--http)  GET /tss       the plaintext TSS container with our MatchConfig (tss.py); fetched by the runtime HLE
#                GET /geo...    the SamGeo location answer (latitude="..." tokens)
#                GET /status    one line of text;  GET /api/stats  JSON counters
#                anything else: 404, logged (so a request we do not serve yet is visible).
# SAM  (--sam)   the game's matchmaker protocol, plain TCP (the private build switches the client's SSL wrapper off).
#   Wire (VERIFIED static, NOTES sec. 4.2): frame = u16 BE total length (including these 2 bytes, 4..0x4000),
#   u8 serviceId, u8 messageId, payload of raw big-endian fields. The 2 bytes 00 00 alone are a keepalive.
#   Service 0 = session layer; ids from the client's table at 0x9D25AC (func_004D3674):
#     0/0 ConnectRequest (client)  u64 a, u64 b, u32 c, u16 ticketLen, u8 d, u8 e, ticket[ticketLen], [u64 f]
#                                  (field order from its serializer func_004D3160; meaning of a..f INFERRED)
#     0/1 ConnectResponse (server) u64 acct, u32 sessionId, u8 serviceId, u8 response (0 = OK), u8 logLevel,
#                                  u8 reason (< 0x40 = OK)   (serializer func_004D2DAC; checks in 0x3D5244)
#     0/2, 0/3 (session end), 0/4, 0/5, 0/6: not decoded yet, logged.
#   The client compares the response's acct with its own NP account id (the ticket's, see mpnet.c), so the server
#   reads the account from our ticket ("MPN1" format, made by the runtime) and echoes it.
#   2026-11-13: the other session messages and the Match service (search, SearchUpdate, GameWorldReserved,
#   GameWorldUpdate) are decoded in sam_codec.py and answered by sam_match.py (layouts VERIFIED static, the order
#   of the exchange INFERRED; nothing here has met a running game yet). Unknown messages are logged as hex.
import argparse, asyncio, ipaddress, itertools, json, os, secrets, struct, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tss  # noqa: E402
import sam_codec as C  # noqa: E402
import sam_match  # noqa: E402
import accounts  # noqa: E402
import limits  # noqa: E402
import admin as admin_mod  # noqa: E402
import discord_notify  # noqa: E402
import leaderboard as lb_mod  # noqa: E402

VERSION = "mpnet-stage1-2026-11-13"
T0 = time.time()
LOG = None
STATS = {"http_requests": 0, "sam_connections": 0, "sam_frames_in": 0, "sam_frames_out": 0, "sam_logins": 0}
MAX_FRAME = 0x4000
MAX_SAM_CONNS = 256
ONLINE = {}  # account id -> SamConn of the signed-in players (admin page, kick)
PEERS = {}   # online id -> (ip, p2p port), from /hello (loopback only in stage 1) or the SAM connection
DEFAULT_P2P_PORT = 3658   # the runtime's PS3_NET_P2P_PORT default
PARTIES = {}  # online id (lower case) -> private party code, from /hello?party= (2026-11-13)
PEER_LAN = {}    # online id -> (home-network ip, game port) the game reported in /hello (lan=), or None
PEER_SEEN = {}   # online id -> (ip, port) the server saw the game's UDP probe come from (outside address behind a router)
UDP_TOKENS = {}  # probe token (from /hello) -> online id


def peer_address(pid, asker_ip):
    """Where `asker_ip` should send its game packets to reach `pid` (2026-11-13 internet play). Behind the same router
    (the same outside address) -> the home-network address; else the outside address + port the probe came from (works
    through ordinary home routers once both sides send); without a probe -> the /hello address (LAN tests)."""
    seen, lan = PEER_SEEN.get(pid), PEER_LAN.get(pid)
    if seen:
        if lan and seen[0] == asker_ip:
            return lan
        if _shares_outside_ip(pid, seen[0]):
            return (stand_in_ip(pid), seen[1])
        return seen
    return PEERS[pid]


SAM_KEEPALIVE = (45, 15, 6)   # seconds idle before the first probe, seconds between probes, probes before giving up


def sam_keepalive(sock):
    """TCP keepalive on a SAM connection (2026-11-20). During a round the game sends nothing on it (rounds of 8+ min);
    MEASURED EU world 8208 (4 players, 497 s round): the host's and one guest's connections still carried the round's
    end (service 4 SessionEnd), the other two never did (one game reconnected, one went silent and stayed "in match"),
    and every 2-player world ends the same way ~9 s after the round: home routers forget a TCP connection that was
    silent for a few minutes. The probes (empty segments the other PC's TCP answers by itself, unseen by the game)
    keep the routers' entries alive, and a PC that is gone is noticed after ~2.5 min instead of never."""
    if sock is None:
        return
    import socket as _s
    try:
        sock.setsockopt(_s.SOL_SOCKET, _s.SO_KEEPALIVE, 1)
        idle, intvl, cnt = SAM_KEEPALIVE
        if hasattr(_s, "TCP_KEEPIDLE"):                       # Linux (the servers), Windows 10 1709+ with Python 3.7+
            sock.setsockopt(_s.IPPROTO_TCP, _s.TCP_KEEPIDLE, idle)
            sock.setsockopt(_s.IPPROTO_TCP, _s.TCP_KEEPINTVL, intvl)
            if hasattr(_s, "TCP_KEEPCNT"):
                sock.setsockopt(_s.IPPROTO_TCP, _s.TCP_KEEPCNT, cnt)
        elif hasattr(sock, "ioctl") and hasattr(_s, "SIO_KEEPALIVE_VALS"):
            sock.ioctl(_s.SIO_KEEPALIVE_VALS, (1, idle * 1000, intvl * 1000))
    except OSError as e:
        log("[sam] keepalive not set: %s" % e)


def _shares_outside_ip(pid, ip):
    """Another signed-in player's game socket was seen at the same outside address (two PCs behind one home router).
    2026-11-20 (EU worlds 8200/8201/8202: every world with both housemates failed, one of the two dropping ~4 s after
    GameWorldReserved; every world with only one of them played): the other players' games seem to tell peers apart
    by IP address only, so two players at one address look like one."""
    me = (pid or "").lower()
    online = {(c.online_id or "").lower() for c in list(ONLINE.values()) if c.online_id}
    return any(k.lower() != me and k.lower() in online and v[0] == ip for k, v in list(PEER_SEEN.items()))


def stand_in_ip(pid):
    """A per-player address in 100.64.0.0/10 (shared address space: never a real host on the internet) for a player
    who shares an outside address with another one. The game sends to it; the runtime only knows it as that player
    (mpnet rpeer by address) and, with no direct answer, carries the packets through this server's relay by name
    within 3 s (mpnet_p2p_wrap), where the relay delivers them to the real outside address. Same name -> same address."""
    import hashlib as _hl
    h = int.from_bytes(_hl.sha256((pid or "").lower().encode()).digest()[:4], "big") % (64 * 65536 - 2) + 1
    return "100.%d.%d.%d" % (64 + (h >> 16), (h >> 8) & 255, h & 255)


def _match_of(online_id):
    """The match (world id) a signed-in player is in or playing, else None."""
    oid = (online_id or "").lower()
    for c in list(ONLINE.values()):
        if (c.online_id or "").lower() == oid:
            pl = c.player
            if pl.world is not None:
                return pl.world.id
            return getattr(pl, "playing", None)
    return None


def relay_allowed(a, b):
    """The relay only joins two players of the same match (anti-abuse: no traffic to strangers or other hosts)."""
    m = _match_of(a)
    return m is not None and a.lower() != b.lower() and m == _match_of(b)


RELAY_MAX = 1400          # bytes of game payload in one relayed packet
RELAY_RATE = (300, 600)   # packets/s per player, burst (the game sends ~30-60/s)
TEST_RELAY_GAP = None   # (name, after_s, seconds): --test-relay-gap, loopback servers only
MATCH_QUIET_MAX = 3 * 3600   # a SAM connection may stay silent this long while its player is in a match (2026-11-14)


UDP_SECRET = [secrets.token_bytes(32)]   # replaced by <data>/udp.key at start (serve), so tokens survive restarts


def udp_token(pid):
    import hmac as _h, hashlib as _hl
    return _h.new(UDP_SECRET[0], (pid or "").lower().encode(), _hl.sha256).hexdigest()[:32]


def token_owner(tok):
    """The player a probe/relay token belongs to: issued since start, or any signed-in player (after a restart)."""
    pid = UDP_TOKENS.get(tok)
    if pid:
        return pid
    for c in list(ONLINE.values()):
        if c.online_id and udp_token(c.online_id) == tok:
            UDP_TOKENS[tok] = c.online_id
            return c.online_id
    return None


class UdpProbe(asyncio.DatagramProtocol):
    """The server's UDP port (2026-11-13):
    b"MPN1UDP <token>"                 address probe from a game's own P2P socket: records where it came from; no answer.
    b"MPR1" + token(32) + to(16) + p   relay (players whose routers block a direct path): forwarded as
                                       b"MPR2" + from(16) + p to that player's probe address, only inside one match."""
    def __init__(self, limits):
        self.limits = limits
        self.transport = None
        self.buckets = {}
        self.relayed = 0
        self.first_seen, self.gap_logged = {}, False   # --test-relay-gap

    def connection_made(self, transport):
        self.transport = transport

    def _seen(self, pid, addr):
        if PEER_SEEN.get(pid) != (addr[0], addr[1]):
            PEER_SEEN[pid] = (addr[0], addr[1])
            log("[udp]    %r seen at %s:%d" % (pid, addr[0], addr[1]))

    def datagram_received(self, data, addr):
        if data.startswith(b"MPR1"):
            self._relay(data, addr)
            return
        if len(data) > 64 or not data.startswith(b"MPN1UDP "):
            return
        if self.limits is not None and not self.limits.http_ok(addr[0]):
            return
        pid = token_owner(data[8:].strip().decode("ascii", "replace"))
        if pid:
            self._seen(pid, addr)

    def _relay(self, data, addr):
        if len(data) < 4 + 32 + 16 + 1 or len(data) > 4 + 32 + 16 + RELAY_MAX:
            return
        pid = token_owner(data[4:36].decode("ascii", "replace"))
        to = data[36:52].rstrip(b"\0").decode("ascii", "replace")
        if not pid or not to:
            return
        b = self.buckets.get(pid)
        if b is None:
            b = self.buckets[pid] = limits.Bucket(*RELAY_RATE)
        if not b.take(pid):
            return
        if TEST_RELAY_GAP and pid.lower() == TEST_RELAY_GAP[0].lower():
            # test only (--test-relay-gap NAME,AFTER,SECONDS on a loopback server): reproduce a host that goes quiet
            now = time.time()
            t0 = self.first_seen.setdefault(pid, now)
            if TEST_RELAY_GAP[1] <= now - t0 < TEST_RELAY_GAP[1] + TEST_RELAY_GAP[2]:
                if not self.gap_logged:
                    self.gap_logged = True
                    log("[relay]  TEST GAP: dropping %r's packets for %.1f s" % (pid, TEST_RELAY_GAP[2]))
                return
        self._seen(pid, addr)
        dest = PEER_SEEN.get(to) or next((v for k, v in PEER_SEEN.items() if k.lower() == to.lower()), None)
        if dest is None or not relay_allowed(pid, to):
            return
        if self.transport is not None:
            self.transport.sendto(b"MPR2" + pid.encode("ascii", "replace")[:16].ljust(16, b"\0") + data[52:], dest)
            self.relayed += 1
            STATS["relay_packets"] = STATS.get("relay_packets", 0) + 1
            if self.relayed in (1, 100, 1000, 10000) or self.relayed % 100000 == 0:
                log("[relay]  %d packets relayed (last %r -> %r, %d B)" % (self.relayed, pid, to, len(data) - 52))
PARTY_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no 0/O/1/I: codes are read out and typed


def clean_party(code):
    """A party code: 4-12 letters A-Z / digits (upper-cased; the runtime sends the same set), else None.
    Generated codes (/api/party/new) use PARTY_CHARS only, so they never contain a look-alike 0/O/1/I."""
    c = (code or "").strip().upper()
    return c if 4 <= len(c) <= 12 and all(ch.isascii() and ch.isalnum() for ch in c) else None


MODE_NAMES = {3: ("Capture the Flag", 8), 4: ("Favour of the Gods (4)", 4), 5: ("Favour of the Gods (8)", 8),
              6: ("Team Favour of the Gods (4)", 4), 7: ("Team Favour of the Gods (8)", 8),
              8: ("Trial of the Gods (2)", 2), 9: ("Trial of the Gods (1)", 1)}


def presence(online_id):
    """What a Discord helper shows for this player (2026-11-13): state, mode, players, party code. Never an address."""
    oid = (online_id or "").lower()
    conn = next((c for c in list(ONLINE.values()) if (c.online_id or "").lower() == oid), None)
    out = {"name": online_id, "state": "offline", "mode": "", "players": 0, "max": 0, "party": PARTIES.get(oid, "")}
    if conn is None:
        return out
    pl = conn.player
    if pl.world is not None:
        mode, n = pl.world.mode, len(pl.world.players)
        out["state"] = "in match"
    elif getattr(pl, "playing", None) is not None:
        mode, n = pl.playing_mode, pl.playing_count
        out["state"] = "in match"
    elif pl.request is not None:
        mode, n = (None if pl.request.get("flag6") else pl.request.get("slot")), 1
        out["state"] = "searching"
    else:
        mode, n = None, 1
        out["state"] = "lobby"
    name, mx = MODE_NAMES.get(mode, ("", 0)) if mode is not None else ("", 0)
    out.update(mode=name, players=n, max=mx)
    return out


def party_of(online_id):
    return PARTIES.get((online_id or "").lower())


def log(*a):
    s = "%9.3f %s" % (time.time() - T0, " ".join(str(x) for x in a))
    print(s, flush=True)
    if LOG:
        LOG.write(s + "\n")
        LOG.flush()


def hexs(b, n=192):
    h = b[:n].hex(" ")
    return h + (" ...(+%d)" % (len(b) - n) if len(b) > n else "")


def printable(b):
    return "".join(chr(c) if 32 <= c < 127 else "." for c in b)


# ------------------------------------------------------------------------------------------------- HTTP
def _account_from_query(acc, q, peer_ip):
    """Signs a player in from /ticket or /hello query parameters (2026-11-13). Preferred: nonce + proof (challenge
    from /nonce; proof = HMAC(K, nonce:name), the token never travels). First sign-in of a free name: reg=K (once).
    The old token=... form is only accepted from the server PC itself (older test builds)."""
    name = q.get("id", "")
    if q.get("nonce") and q.get("proof"):
        return acc.sign_in_proof(name, q["nonce"], q["proof"])
    if q.get("reg"):
        return acc.register_key(name, q["reg"])
    if q.get("token"):
        if not ipaddress.ip_address(peer_ip).is_loopback:
            raise accounts.AccountError("token sign-in only on the server PC; use a challenge")
        return acc.sign_in(name, q["token"])
    raise accounts.AccountError("no credentials")


class Http:
    def __init__(self, args):
        self.args = args
        # SamGeo sends GeoPath verbatim, without constructing an HTTP request.
        self.tss = tss.build(getattr(args, "advertise", "") or args.host, args.sam, args.http, "GET /geo HTTP/1.0\r\n\r\n",
                             with_protocol=getattr(args, "protocol_config", False))
        log("[http] TSS blob %d bytes:" % len(self.tss),
            ", ".join("%s id %d %d B" % (e[0], e[4], e[1]) for e in tss.parse(self.tss)[1]))

    async def handle(self, r, w):
        peer = w.get_extra_info("peername")
        STATS["http_requests"] += 1
        local = ipaddress.ip_address(peer[0]).is_loopback   # the HTTPS proxy on this machine (or a one-PC test)
        if not local and not self.args.limits.http_ok(peer[0]):
            STATS["http_limited"] = STATS.get("http_limited", 0) + 1
            w.close()
            return
        prefix = b""
        try:
            async with asyncio.timeout(10):
                prefix = await r.readexactly(4)
                head = prefix + await r.readuntil(b"\r\n\r\n")
        except Exception as e:  # incomplete request, too long, timeout
            data = prefix + getattr(e, "partial", b"")
            log("[http] %s incomplete request (%s): %r" % (peer, type(e).__name__, data[:200]))
            w.close()
            return
        lines = head.decode("latin1").split("\r\n")
        req = lines[0].split(" ")
        hdrs = {}
        for l in lines[1:]:
            if ":" in l:
                k, v = l.split(":", 1)
                hdrs[k.strip().lower()] = v.strip()
        body = b""
        try:
            n = int(hdrs.get("content-length", "0") or 0)
        except ValueError:
            n = 0
        if 0 < n <= 65536:
            try:
                body = await asyncio.wait_for(r.readexactly(n), 10)
            except Exception:
                pass
        # HTTPS (2026-11-14): the game's requests may come through Caddy on this machine; the player's real address is
        # then its X-Forwarded-For (trusted only from loopback). Limits, sign-in lockouts and peer addresses use it.
        real = peer
        fwd = hdrs.get("x-forwarded-for", "")
        if fwd and local:
            real = (fwd.split(",")[0].strip()[:64], peer[1])
            try:
                ipaddress.ip_address(real[0])
            except ValueError:
                w.close()
                return
            if not self.args.limits.http_ok(real[0]):
                STATS["http_limited"] = STATS.get("http_limited", 0) + 1
                w.close()
                return
        path = req[1] if len(req) > 1 else "?"
        log("[http] %s %s ua=%r host=%r body=%d %s" % (peer, lines[0][:200], hdrs.get("user-agent"),
                                                       hdrs.get("host"), len(body), printable(body[:120])))
        p = path.split("?")[0]
        extra = ""
        if p == "/admin" or p.startswith("/admin/"):
            code, ctype, out = self.args.admin.handle(req[0], path, hdrs, body, peer[0])
            if code == "CHECK":   # an outgoing status request: in a thread, so the server keeps serving meanwhile
                res = await asyncio.get_running_loop().run_in_executor(None, admin_mod.Admin.check_server, ctype)
                code, ctype, out = 200, "application/json", json.dumps(res).encode()
            extra = ("Cache-Control: no-store" + chr(13) + chr(10) + "X-Frame-Options: DENY" + chr(13) + chr(10) + "X-Content-Type-Options: nosniff" + chr(13) + chr(10))
        elif p == "/api/servers":
            # the community server list for launchers (managed on the admin page)
            code, ctype, out = 200, "application/json", json.dumps(
                {"servers": [{k: v for k, v in x.items() if k in ("name", "address", "region", "note")}
                             for x in self.args.admin.servers()]}).encode()
        elif p == "/tss":
            code, ctype, out = 200, "application/octet-stream", self.tss
        elif p == "/geo" or p.startswith("/geo/"):
            # SamGeo (func_003D0970) does one recv of <= 0x3FF bytes, strstr()s each token and reads ="%f".
            code, ctype = 200, "text/xml"
            out = b'<location latitude="0.000000" longitude="0.000000" countryCode="US" city="Local"/>'
        elif p in ("/hello", "/peer"):
            # 2026-11-13: P2P endpoints for the runtime's sceNpSignaling (kit mpnet.c): /hello?id=NAME&port=P records
            # the caller's address + P2P port, /peer?id=NAME answers "a.b.c.d port" (404 if unknown).
            q = dict(kv.split("=", 1) for kv in (path.split("?", 1) + [""])[1].split("&") if "=" in kv)
            pid = q.get("id", "")[:16]
            owner_ok = True
            if p == "/hello" and self.args.accounts is not None and not self.args.limits.may_try_sign_in(real[0]):
                owner_ok = False
                log("[http]   /hello from %s refused: too many failed sign-ins" % real[0])
            elif p == "/hello" and self.args.accounts is not None:
                try:   # only the name's owner may say where that player is (2026-11-13 accounts)
                    _account_from_query(self.args.accounts, q, real[0])
                except accounts.AccountError as e:
                    owner_ok = False
                    self.args.limits.sign_in_failed(real[0])
                    log("[http]   /hello for %r refused: %s" % (pid, e))
            if p == "/hello" and owner_ok and pid and q.get("port", "").isdigit() and 0 < int(q["port"]) < 65536:
                PEERS[pid] = (real[0], int(q["port"]))
                log("[http]   peer %r registered at %s:%d" % (pid, real[0], int(q["port"])))
                party = clean_party(q.get("party"))
                if party:
                    PARTIES[pid.lower()] = party
                    log("[http]   %r joins private party %s" % (pid, party))
                else:
                    PARTIES.pop(pid.lower(), None)
                # Internet play (2026-11-13): the home-network address the game reports (lan=), and a token the game
                # sends back by UDP from its own game port so the server sees the router's outside address + port.
                lan = q.get("lan", "")
                try:
                    lip = ipaddress.ip_address(lan)
                    PEER_LAN[pid] = (lan, int(q["port"])) if lip.version == 4 and (lip.is_private or lip.is_loopback) else None
                except ValueError:
                    PEER_LAN.pop(pid, None)
                token = udp_token(pid)   # the same after a server restart (MEASURED 2026-11-13: random tokens were lost)
                for t in [t for t, n in UDP_TOKENS.items() if n == pid]:
                    del UDP_TOKENS[t]
                UDP_TOKENS[token] = pid
                code, ctype, out = 200, "text/plain", ("ok %s %d\n" % (token, getattr(self.args, "udp", 0) or 0)).encode()
            elif p == "/peer" and pid in PEERS:
                code, ctype, out = 200, "text/plain", ("%s %d" % peer_address(pid, real[0])).encode()
            else:
                code, ctype, out = 404, "text/plain", b"unknown\n"
        elif p == "/nonce":
            # a one-time challenge for /ticket and /hello (60 s, single use)
            if self.args.accounts is None:
                code, ctype, out = 404, "text/plain", b"accounts off" + bytes([10])
            else:
                code, ctype, out = 200, "text/plain", self.args.accounts.new_nonce().encode()
        elif p == "/ticket":
            # 2026-11-13: /ticket?id=NAME&token=HEX -> a 64-byte ticket signed by this server (accounts.py); the first
            # sign-in with a free name registers it. 403 = the name belongs to another token (or is banned).
            q = dict(kv.split("=", 1) for kv in (path.split("?", 1) + [""])[1].split("&") if "=" in kv)
            if self.args.accounts is None:
                code, ctype, out = 404, "text/plain", b"accounts off" + bytes([10])
            elif not self.args.limits.may_try_sign_in(real[0]):
                code, ctype, out = 429, "text/plain", b"too many failed sign-ins, wait a minute" + bytes([10])
                log("[http]   ticket from %s refused: too many failed sign-ins" % real[0])
            else:
                try:
                    acct = _account_from_query(self.args.accounts, q, real[0])
                    code, ctype, out = 200, "application/octet-stream", self.args.accounts.issue_ticket(acct, q["id"])
                    log("[http]   ticket for %r (account %d)" % (q["id"], acct))
                except accounts.AccountError as e:
                    self.args.limits.sign_in_failed(real[0])
                    code, ctype, out = 403, "text/plain", (str(e) + chr(10)).encode()
                    log("[http]   ticket for %r refused: %s" % (q.get("id"), e))
        elif p == "/api/report":
            # 2026-11-14 leaderboards: /api/report?id=NAME&nonce=..&proof=..&s=XP:800,Favor:590 from the runtime after
            # the game saved its TUS profile. Signed in like /hello (challenge only); values capped + rate-limited
            # (leaderboard.py). Reported by the player's game, NOT verified.
            q = dict(kv.split("=", 1) for kv in (path.split("?", 1) + [""])[1].split("&") if "=" in kv)
            lbd = getattr(self.args, "leaderboard", None)
            if self.args.accounts is None or lbd is None:
                code, ctype, out = 404, "text/plain", b"leaderboards off" + bytes([10])
            elif not self.args.limits.may_try_sign_in(real[0]):
                code, ctype, out = 429, "text/plain", b"too many failed sign-ins, wait a minute" + bytes([10])
            elif not (q.get("nonce") and q.get("proof")):
                code, ctype, out = 403, "text/plain", b"challenge sign-in needed" + bytes([10])
            else:
                acct = None
                try:
                    acct = _account_from_query(self.args.accounts, q, real[0])
                except accounts.AccountError as e:
                    self.args.limits.sign_in_failed(real[0])
                    code, ctype, out = 403, "text/plain", (str(e) + chr(10)).encode()
                    log("[lb]     report for %r refused: %s" % (q.get("id"), e))
                if acct is not None:
                    name = self.args.accounts.name_of(acct)[0] or q.get("id", "")[:16]
                    try:
                        lbd.report(acct, name, lb_mod.parse_stats(q.get("s", "")))
                        STATS["lb_reports"] = STATS.get("lb_reports", 0) + 1
                        code, ctype, out = 200, "text/plain", b"ok" + bytes([10])
                        log("[lb]     report from %r: %s" % (name, q.get("s", "")[:200]))
                    except lb_mod.ReportError as e:
                        STATS["lb_refused"] = STATS.get("lb_refused", 0) + 1
                        code, ctype, out = 400, "text/plain", (str(e) + chr(10)).encode()
                        log("[lb]     report from %r refused: %s" % (name, e))
        elif p == "/api/leaderboard":
            # public JSON: every board (top n, default 10) or ?board=xp (top n, default 50); banned/hidden left out
            q = dict(kv.split("=", 1) for kv in (path.split("?", 1) + [""])[1].split("&") if "=" in kv)
            lbd = getattr(self.args, "leaderboard", None)
            board = q.get("board", "")
            n = int(q["n"]) if q.get("n", "").isdigit() and len(q["n"]) < 5 else (50 if board else 10)
            if lbd is None:
                code, ctype, out = 404, "application/json", b'{"error": "leaderboards off"}'
            elif board and board not in lb_mod.BOARD_KEYS:
                code, ctype, out = 404, "application/json", b'{"error": "unknown board"}'
            else:
                banned = []
                if self.args.accounts is not None:
                    banned = [r[0] for r in self.args.accounts.db.execute("SELECT name FROM accounts WHERE banned = 1")]
                code, ctype, out = 200, "application/json", json.dumps(
                    lbd.public(board or None, max(1, min(n, 200)), banned, q.get("period", "all"))).encode()
        elif p in ("/leaderboard", "/leaderboards"):
            # the small public page (also linked from the launcher's Online page)
            code, ctype, out = 200, "text/html; charset=utf-8", lb_mod.PAGE.encode("utf-8")
            extra = "Cache-Control: no-cache" + chr(13) + chr(10) + "X-Content-Type-Options: nosniff" + chr(13) + chr(10)
        elif p == "/status":
            code, ctype, out = 200, "text/plain", ("gowa %s ok\n" % VERSION).encode()
        elif p == "/api/presence":
            q = dict(kv.split("=", 1) for kv in (path.split("?", 1) + [""])[1].split("&") if "=" in kv)
            code, ctype, out = 200, "application/json", json.dumps(presence(q.get("id", "")[:16])).encode()
        elif p == "/api/party/new":
            # a fresh private party code for an invite (launcher / Discord join secret); the code itself grants nothing
            code, ctype, out = 200, "text/plain", ("".join(secrets.choice(PARTY_CHARS) for _ in range(6)) + "\n").encode()
        elif p == "/api/stats":
            code, ctype, out = 200, "application/json", json.dumps(STATS).encode()
        elif p == "/api/status":
            # for the launcher's Online page: server name/version and how many play right now
            mm = self.args.matchmaker
            st = {"name": getattr(self.args, "name", "GoW Ascension community server"), "version": VERSION,
                  "players_online": STATS.get("sam_online", 0),
                  "searching": sum(len(v) for v in mm.pools.values()) if mm else 0,
                  "matches": len(mm.worlds) if mm else 0,
                  "accounts": self.args.accounts is not None}
            code, ctype, out = 200, "application/json", json.dumps(st).encode()
        else:
            code, ctype, out = 404, "text/plain", b"not found\n"
        reason = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 429: "Too Many Requests"}[code]
        resp = ("HTTP/1.0 %d %s\r\nContent-Type: %s\r\nContent-Length: %d\r\nConnection: close\r\n%s\r\n"
                % (code, reason, ctype, len(out), extra)).encode() + out
        w.write(resp)
        try:
            await w.drain()
        except Exception:
            pass
        log("[http]   -> %d, %d bytes" % (code, len(out)))
        w.close()


# ------------------------------------------------------------------------------------------------- SAM
SVC = {0: "Session", 1: "Match", 2: "Profile", 3: "Data", 4: "GameTracker"}
NAMES = {(0, 0): "ConnectRequest", (0, 1): "ConnectResponse", (0, 2): "Session2", (0, 3): "Session3",
         (0, 4): "Session4", (0, 5): "Session5", (0, 6): "Session6"}
# Field layouts of the client library's other message serializers (func_004D1F48 .. func_004D309C), in wire order.
# Which (service, message) each belongs to is not mapped yet (S0-1/S0-2): they are only tried for the log.
LAYOUTS = {
    "s4D2ED4": ">IIB", "s4D2FB8": ">IBB", "s4D309C": ">IB", "s4D2DAC": ">QIBBBB",
    "s4D2B34": ">HBBBB", "s4D2C6C": ">IHB", "s4D2A3C": ">IH", "s4D2964": ">H",
}
SESSION_IDS = itertools.count(0x1001)
CONN_IDS = itertools.count(1)


def frame(svc, msg, payload=b""):
    if 4 + len(payload) > MAX_FRAME:
        raise ValueError("frame too long")
    return struct.pack(">HBB", 4 + len(payload), svc, msg) + payload


def parse_connect_request(p):
    """Returns a dict, or None if the payload is shorter than the fixed part."""
    if len(p) < 24:
        return None
    a, b, c, tlen, d, e = struct.unpack(">QQIHBB", p[:24])
    ticket = p[24:24 + tlen]
    rest = p[24 + tlen:]
    f = struct.unpack(">Q", rest[:8])[0] if len(rest) >= 8 else None
    return {"a": a, "b": b, "c": c, "ticket_len": tlen, "d": d, "e": e, "ticket": ticket, "f": f,
            "trailing": len(rest)}


def ticket_account(t):
    """Our ticket format (runtime mpnet.c): "MPN1", u32 version, u64 account, char online_id[32]."""
    if len(t) >= 48 and t[:4] == b"MPN1":
        acct = struct.unpack(">Q", t[8:16])[0]
        oid = t[16:48].split(b"\0")[0].decode("ascii", "replace")
        return acct, oid
    return None, None


def connect_response(acct, session_id, service, response=0, log_level=0, reason=0):
    return frame(0, 1, struct.pack(">QIBBBB", acct, session_id, service, response, log_level, reason))


class SamConn:
    def __init__(self, r, w, args):
        self.r, self.w, self.args = r, w, args
        self.id = next(CONN_IDS)
        self.peer = w.get_extra_info("peername")
        self.acct = None
        self.online_id = None
        self.sessions = {}
        self.mm = args.matchmaker
        self.flood = limits.Bucket(50.0, 100.0)
        self.t_login = 0.0
        self.player = sam_match.Player(self.send_raw, lambda *a: log("[sam %d]   " % self.id, *a))

    def send_raw(self, data, what):
        self.w.write(data)
        STATS["sam_frames_out"] += 1
        log("[sam %d] -> %s %s" % (self.id, what, hexs(data)))

    def on_connect_request(self, p):
        try:
            req = C.parse_connect_request(p)
        except C.CodecError as e:
            log("[sam %d]    ConnectRequest not decoded (%s): not answered" % (self.id, e))
            return
        acct, oid, why = None, None, ""
        if self.args.accounts is not None:
            try:
                acct, oid = self.args.accounts.verify_ticket(req["ticket"])
            except accounts.AccountError as e:
                why = str(e)
                if self.args.allow_unsigned and why.startswith("unsigned"):
                    acct, oid = ticket_account(req["ticket"])
        else:
            acct, oid = ticket_account(req["ticket"])
        log("[sam %d]    ConnectRequest a=%d (0x%X) b=%d c=%d d=%d e=%d ticket %d B %s f=%s trailing=%d"
            % (self.id, req["a"], req["a"], req["b"], req["c"], req["d"], req["e"], req["ticket_len"],
               ("acct %s online id %r" % (acct, oid)) if acct is not None else ("(not ours) " + hexs(req["ticket"], 48)),
               req["f"], req["trailing"]))
        if acct is None:
            # Not our ticket: no account to echo; the client would fail "acct id mismatch". Answer "failed".
            self.send_raw(connect_response(0, 0, 0, response=1),
                          "ConnectResponse (ticket refused%s: response 1)" % (": " + why if why else ""))
            return
        self.acct, self.online_id = acct, oid
        self.t_login = time.time()
        # 2026-11-13 (two-PC test): /hello is sent once at game start; after a server restart the player is still
        # signed in (the ticket survives) but PEERS is empty -> the other side's /peer gets 404 and the match fails
        # (Network Error 13:110:0x80022a05). A signed-in SAM connection fills the gap with its own address and the
        # game's default P2P port; a later /hello (with the real port) replaces it.
        if oid and oid not in PEERS and self.peer:
            PEERS[oid] = (self.peer[0], DEFAULT_P2P_PORT)
            log("[sam %d]    peer %r registered at %s:%d (from the SAM connection)" % (self.id, oid, self.peer[0], DEFAULT_P2P_PORT))
        old = ONLINE.get(acct)
        if old is not None and old is not self:
            old.kick()                     # one connection per account: the newest sign-in wins
        ONLINE[acct] = self
        STATS["sam_logins"] += 1
        self.mm.on_connect(self.player, acct, oid)
        if self.args.connect_reply == "match":
            # old experiment switch: also open the Match service at once, without waiting for SessionBegin
            self.mm.on_session_begin(self.player, C.SVC_MATCH)

    def on_frame(self, svc, msg, p):
        STATS["sam_frames_in"] += 1
        log("[sam %d] <- %d/%d %s | %d B: %s" % (self.id, svc, msg, C.describe(svc, msg, p), len(p), hexs(p)))
        try:
            if (svc, msg) == (0, 0):
                self.on_connect_request(p)
            elif self.acct is None:
                log("[sam %d]    before a ConnectRequest: ignored" % self.id)
            elif (svc, msg) == (0, 2):
                self.mm.on_session_begin(self.player, C.parse_session_begin(p)["service"])
            elif (svc, msg) == (0, 3):
                m = C.parse_session_end(p)
                ok = self.mm.on_session_end(self.player, m["session_id"], m["service"])
                log("[sam %d]    session %d of service %d %s" % (self.id, m["session_id"], m["service"],
                                                              "ended" if ok else "unknown: ignored"))
            elif (svc, msg) == (1, 1):
                self.mm.on_match_request(self.player, C.parse_match_request(p))
            elif (svc, msg) == (1, 4):
                self.mm.on_player_report(self.player, C.parse_player_report(p))
            else:
                log("[sam %d]    (no handler for %d/%d yet: not answered)" % (self.id, svc, msg))
        except C.CodecError as e:
            log("[sam %d]    not decoded (%s): not answered" % (self.id, e))

    def kick(self):
        log("[sam %d] kicked (%r)" % (self.id, self.online_id))
        try:
            self.w.close()
        except Exception:
            pass

    async def run(self):
        STATS["sam_connections"] += 1
        log("[sam %d] connect from %s" % (self.id, self.peer))
        buf = b""
        quiet = 0.0   # seconds without a message
        try:
            while True:
                try:
                    data = await asyncio.wait_for(self.r.read(65536), self.args.idle)
                    quiet = 0.0
                except asyncio.TimeoutError:
                    # 2026-11-14 (MEASURED on both servers: every round longer than ~5 min was cut, "idle 300 s"
                    # ~325 s after GameWorldReserved): during a round the game sends nothing on this connection.
                    # A player in a match keeps it for up to MATCH_QUIET_MAX; a dead PC still ends it (TCP reset).
                    quiet += self.args.idle
                    pl = getattr(self, "player", None)
                    in_match = pl is not None and (getattr(pl, "world", None) is not None or
                                                   getattr(pl, "playing", None) is not None)
                    if in_match and quiet < MATCH_QUIET_MAX:
                        continue
                    log("[sam %d] idle %d s%s: closing" % (self.id, quiet, " (in a match)" if in_match else ""))
                    break
                if not data:
                    log("[sam %d] peer closed" % self.id)
                    break
                buf += data
                if not self.flood.take("x", max(1.0, len(data) / 1024.0)):
                    # Fair play / robustness (2026-11-13): a client sends a few messages per second; a flood closes
                    # the connection.
                    log("[sam %d] flood (more than 50 messages/s): closing" % self.id)
                    break
                while len(buf) >= 2:
                    n = struct.unpack(">H", buf[:2])[0]
                    if n == 0:  # keepalive
                        buf = buf[2:]
                        log("[sam %d] <- keepalive" % self.id)
                        if self.args.echo_keepalive:
                            self.send_raw(b"\0\0", "keepalive")
                        continue
                    if n < 4 or n > MAX_FRAME:
                        log("[sam %d] bad frame length %d: %s -- closing" % (self.id, n, hexs(buf, 64)))
                        return
                    if len(buf) < n:
                        break
                    f, buf = buf[:n], buf[n:]
                    self.on_frame(f[2], f[3], f[4:])
                await self.w.drain()
        except (ConnectionError, OSError) as e:
            log("[sam %d] connection error: %s" % (self.id, e))
        finally:
            if self.acct is not None and ONLINE.get(self.acct) is self:
                del ONLINE[self.acct]
            self.mm.leave(self.player)
            try:
                self.w.close()
            except Exception:
                pass


# ------------------------------------------------------------------------------------------------- main
def check_host(h, lan=False, public=False, advertise=""):
    """Loopback by default. --lan also allows one private LAN address of this PC (10.x, 172.16-31.x, 192.168.x):
    for two PCs at home; the user starts it and answers the Windows Firewall prompt. Never 0.0.0.0 or a public
    address (internet hosting comes with accounts and the relay)."""
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        sys.exit("--host must be a numeric address, not %r" % h)
    if ip.is_loopback:
        return
    if lan and ip.version == 4 and ip.is_private and not ip.is_unspecified and not ip.is_link_local:
        print("NOTE: listening on the LAN address %s: other PCs on your network can reach this server." % h)
        return
    if public and ip.version == 4:
        # Internet hosting (2026-11-13, HOSTING.md): accounts + signed tickets + limits are on; the admin page stays
        # loopback-only (use an SSH tunnel). 0.0.0.0 needs --advertise: the address the games are told to connect to.
        if ip.is_unspecified and not advertise:
            sys.exit("--host 0.0.0.0 needs --advertise <the server's public IP or name>")
        print("NOTE: public server on %s (players connect to %s). Admin page: this machine only." % (h, advertise or h))
        return
    sys.exit("--host %s refused: loopback only (a private LAN address with --lan, a public one with --public)" % h)


LAN_DISCOVERY_PORT = 36104
LAN_ASK = b"GOWA?1"


class LanBeacon(asyncio.DatagramProtocol):
    """LAN play (2026-11-15): answers a launcher's broadcast "GOWA?1" on UDP 36104 with this server's name and
    address, so players on the same network find it without typing an IP. Only private/loopback askers get an
    answer (a public server never runs this)."""

    def __init__(self, args):
        self.args = args
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        if data.strip() != LAN_ASK:
            return
        try:
            ip = ipaddress.ip_address(addr[0])
        except ValueError:
            return
        if not (ip.is_private or ip.is_loopback):
            return
        reply = {"gowa": 1, "name": str(getattr(self.args, "name", "") or "LAN server")[:40],
                 "address": self.args.host, "http": self.args.http, "players": len(ONLINE), "version": VERSION}
        self.transport.sendto(json.dumps(reply).encode(), addr)


async def serve(args, ready=None):
    if not hasattr(args, "accounts"):
        args.accounts = accounts.Accounts(args.data) if getattr(args, "data", "") else None
    if not hasattr(args, "allow_unsigned"):
        args.allow_unsigned = False
    if not hasattr(args, "limits"):
        args.limits = limits.Limits()
    if not hasattr(args, "leaderboard"):
        args.leaderboard = lb_mod.Leaderboard(args.data) if getattr(args, "data", "") else None
    http_admin_ready = getattr(args, "admin", None)
    if not isinstance(http_admin_ready, admin_mod.Admin):
        args.admin = admin_mod.Admin(getattr(args, "data", "") or "", args.accounts, ONLINE, None, STATS, VERSION)
    http = Http(args)
    if not getattr(args, "matchmaker", None):
        loop = asyncio.get_running_loop()
        args.matchmaker = sam_match.Matchmaker(getattr(args, "min_players", 2), log=lambda *a: log("[match]", *a),
                                               later=lambda t, fn: loop.call_later(t, fn),
                                               world_mode=(None if getattr(args, "mode", None) in (None, -99) else args.mode),
                                               party_of=party_of)
    args.admin.mm = args.matchmaker
    wf = getattr(args, "discord_webhook_file", "")
    if wf:   # 2026-11-15: optional Discord posts (discord_notify.py); the only outgoing connection besides admin checks
        dn = discord_notify.Notifier(discord_notify.read_url(wf), getattr(args, "name", ""), log=log)
        args.matchmaker.on_round_start = lambda wid, mode, names: dn.match_started(
            MODE_NAMES.get(mode, ("a match", 0))[0], names)
        hour = int(getattr(args, "discord_daily_hour", 18)) % 24

        def daily():
            if args.leaderboard is not None:
                dn.weekly_top(args.leaderboard)
            schedule()

        def schedule():
            now = time.time()
            nxt = (int(now // 86400) * 86400) + hour * 3600
            if nxt <= now + 60:
                nxt += 86400
            asyncio.get_running_loop().call_later(nxt - now, daily)
        schedule()
        log("[discord] posts on: match starts + the week's leaders daily at %02d:00 UTC" % hour)
    args.admin.leaderboard = args.leaderboard
    args.admin.party_of = party_of   # private match codes (admin dashboard)
    args.admin.mode_names = MODE_NAMES
    hip = ipaddress.ip_address(args.host)
    args.admin.local_ip = "" if (hip.is_loopback or not hip.is_private) else args.host   # public: loopback only
    sam_count = [0]

    async def sam_handler(r, w):
        ip = (w.get_extra_info("peername") or ("?",))[0]
        if sam_count[0] >= MAX_SAM_CONNS or not args.limits.sam_open_ok(ip):
            log("[sam] connection from %s refused (limit)" % ip)
            w.close()
            return
        sam_count[0] += 1
        STATS["sam_online"] = sam_count[0]
        sam_keepalive(w.get_extra_info("socket"))
        try:
            await SamConn(r, w, args).run()
        finally:
            sam_count[0] -= 1
            STATS["sam_online"] = sam_count[0]
            args.limits.sam_closed(ip)

    hs = await asyncio.start_server(http.handle, args.host, args.http)
    if getattr(args, "data", ""):
        kf = os.path.join(args.data, "udp.key")
        if not os.path.exists(kf):
            os.makedirs(args.data, exist_ok=True)
            with open(kf, "wb") as f:
                f.write(secrets.token_bytes(32))
        with open(kf, "rb") as f:
            UDP_SECRET[0] = f.read(32)
    loop = asyncio.get_running_loop()
    if not hasattr(args, "udp"):
        args.udp = 0
    ut, _ = await loop.create_datagram_endpoint(lambda: UdpProbe(getattr(args, "limits", None)),
                                                local_addr=(args.host, args.udp))
    args.udp = ut.get_extra_info("sockname")[1]
    log("[main] listening on %s:%d (udp probe)" % (args.host, args.udp))
    if getattr(args, "lan", False) and not ipaddress.ip_address(args.host).is_loopback:
        try:   # broadcasts reach only a socket bound to all addresses
            await loop.create_datagram_endpoint(lambda: LanBeacon(args), local_addr=("0.0.0.0", LAN_DISCOVERY_PORT))
            log("[main] LAN discovery on UDP %d" % LAN_DISCOVERY_PORT)
        except OSError as e:
            log("[main] LAN discovery off (UDP %d: %s)" % (LAN_DISCOVERY_PORT, e))
    ss = await asyncio.start_server(sam_handler, args.host, args.sam)
    args.http = hs.sockets[0].getsockname()[1]
    args.sam = ss.sockets[0].getsockname()[1]
    for s in hs.sockets + ss.sockets:
        log("[main] listening on %s:%d" % s.getsockname()[:2])
    if ready:
        ready.set_result((args.http, args.sam))
    async with hs, ss:
        await asyncio.gather(hs.serve_forever(), ss.serve_forever())


async def selftest():
    a = argparse.Namespace(host="127.0.0.1", http=0, sam=0, connect_reply="svc0", echo_keepalive=True, idle=10, min_players=2)
    loop = asyncio.get_running_loop()
    ready = loop.create_future()
    task = asyncio.ensure_future(serve(a, ready))
    http_port, sam_port = await ready
    # (the TSS is built before the ephemeral ports are known: only its structure is checked here)
    ok = True
    r, w = await asyncio.open_connection("127.0.0.1", http_port)
    w.write(b"GET /tss HTTP/1.0\r\nHost: x\r\n\r\n")
    resp = await r.read()
    w.close()
    body = resp.split(b"\r\n\r\n", 1)[1]
    v, entries = tss.parse(body)
    mc = json.loads(entries[0][5])
    ok &= v == 1 and entries[0][0] == "MatchConfig" and entries[0][4] == 5 and "TransportConfig" in mc
    print("selftest: /tss", len(body), "bytes, version", v, entries[0][:5], "ok" if ok else "BAD")
    ticket = b"MPN1" + struct.pack(">IQ", 1, 0x123456789ABCDEF) + b"MpTestA".ljust(32, b"\0") + b"\0" * 16
    req = struct.pack(">QQIHBB", 111, 9, 3, len(ticket), 0, 1) + ticket
    r, w = await asyncio.open_connection("127.0.0.1", sam_port)
    w.write(b"\0\0" + frame(0, 0, req))
    await w.drain()
    got = await asyncio.wait_for(r.readexactly(2 + 20), 5)
    w.close()
    ka, f = got[:2], got[2:]
    n, svc, msg = struct.unpack(">HBB", f[:4])
    acct, sid, s2, resp_code, ll, reason = struct.unpack(">QIBBBB", f[4:])
    ok2 = ka == b"\0\0" and (n, svc, msg) == (20, 0, 1) and acct == 0x123456789ABCDEF and resp_code == 0
    print("selftest: SAM keepalive echo + ConnectResponse", f.hex(" "), "ok" if ok2 else "BAD")
    task.cancel()
    return ok and ok2


def admin(args):
    acc = accounts.Accounts(args.data)
    cmd = args.admin_cmd[0]
    if cmd == "list":
        for name, acct, created, seen, banned in acc.db.execute(
                "SELECT name, acct, created, last_seen, banned FROM accounts ORDER BY name"):
            print("%-16s %20d  created %s  last %s%s" % (name, acct, time.strftime("%Y-%m-%d", time.localtime(created)),
                                                        time.strftime("%Y-%m-%d %H:%M", time.localtime(seen)),
                                                        "  BANNED" if banned else ""))
        return 0
    if cmd in ("ban", "unban") and len(args.admin_cmd) == 2:
        if not acc.db.execute("SELECT 1 FROM accounts WHERE name = ?", (args.admin_cmd[1],)).fetchone():
            print("no account %r" % args.admin_cmd[1])
            return 1
        acc.ban(args.admin_cmd[1], cmd == "ban")
        print("%s %s" % (args.admin_cmd[1], "banned" if cmd == "ban" else "unbanned"))
        return 0
    print("admin commands: list | ban NAME | unban NAME")
    return 2


def main():
    global LOG
    ap = argparse.ArgumentParser(description="GoW: Ascension community server prototype (loopback only)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--http", type=int, default=36101)
    ap.add_argument("--sam", type=int, default=36102)
    ap.add_argument("--udp", type=int, default=36103, help="UDP port for the games' address probe (internet play)")
    ap.add_argument("--log", default="")
    ap.add_argument("--connect-reply", choices=["svc0", "match"], default="svc0")
    ap.add_argument("--echo-keepalive", type=int, default=1)
    ap.add_argument("--idle", type=int, default=300, help="close a SAM connection silent for this many seconds")
    ap.add_argument("--min-players", type=int, default=2, help="players needed before a match is made (1..8)")
    ap.add_argument("--mode", type=int, default=None, help="game mode for every world (default / -99: the host's)")
    ap.add_argument("--lan", action="store_true", help="allow a private LAN address for --host (two PCs at home)")
    ap.add_argument("--name", default="GoW Ascension community server", help="the server's name (status, LAN discovery)")
    ap.add_argument("--public", action="store_true", help="allow a public address or 0.0.0.0 for --host (internet server)")
    ap.add_argument("--advertise", default="", help="the address games are told to connect to (public IP or DNS name)")
    ap.add_argument("--data", default=os.path.join(HERE, "data"),
                    help="folder for accounts.db and server.key ('' = accounts off: unsigned tickets, tests only)")
    ap.add_argument("--allow-unsigned", action="store_true",
                    help="also accept the old unsigned tickets of builds before n9 (tests only)")
    ap.add_argument("--admin", nargs="+", metavar="CMD", dest="admin_cmd",
                    help="account admin on --data, server not started: list | ban NAME | unban NAME")
    ap.add_argument("--test-relay-gap", default="", metavar="NAME,AFTER,SECONDS",
                    help="tests only (loopback): drop NAME's relayed packets for SECONDS, AFTER s after its first one")
    ap.add_argument("--protocol-config", action="store_true",
                    help="tests only: also send tss.PROTOCOL_CONFIG (MEASURED 2026-11-14: the game did not apply it)")
    ap.add_argument("--discord-webhook-file", default="", help="file with a Discord webhook URL: post match starts "
                    "and the week's leaders there (optional)")
    ap.add_argument("--discord-daily-hour", type=int, default=18, help="UTC hour of the daily leaders post (default 18)")
    ap.add_argument("--test-fake-players", type=int, default=0, help="test only (loopback): add N made-up players to every match roster")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.test_relay_gap:
        global TEST_RELAY_GAP
        if args.host != "127.0.0.1":
            sys.exit("--test-relay-gap is for loopback test servers only")
        n, a, s = args.test_relay_gap.split(",")
        TEST_RELAY_GAP = (n, float(a), float(s))
    if args.test_fake_players:
        if args.host != "127.0.0.1":
            sys.exit("--test-fake-players is for loopback test servers only")
        sam_match.TEST_FAKE_PLAYERS = max(0, min(7, args.test_fake_players))
    if args.selftest:
        sys.exit(0 if asyncio.run(selftest()) else 1)
    if args.admin_cmd:
        sys.exit(admin(args))
    check_host(args.host, args.lan, args.public, args.advertise)
    if args.log:
        LOG = open(args.log, "a", encoding="utf-8")
    log("[main] %s pid %d host %s http %d sam %d reply %s" % (VERSION, os.getpid(), args.host, args.http,
                                                             args.sam, args.connect_reply))
    try:
        asyncio.run(serve(args))
    except KeyboardInterrupt:
        pass
    log("[main] stopped")


if __name__ == "__main__":
    main()
