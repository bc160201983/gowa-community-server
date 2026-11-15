# GoW: Ascension community server: player accounts and signed tickets (2026-11-13). Python 3 standard library only.
#
# A player is an online name (3-16 letters/digits/-/_, starting with a letter; the PSN rule) plus a secret token the
# player's runtime makes once and keeps in a file. The first sign-in with a free name registers it; later sign-ins
# must bring the same token. The server stores only a salted hash of the token. No e-mail, no password.
#
# Ticket v2 (64 bytes: the game copies the ticket into a 64-byte buffer, MEASURED "ManagerGetTicket(cap=64)"):
#   0  "MPN1"   4  u32 version 2   8  u64 account id   16  char name[16]   32  u32 issued (unix s)
#   36 u32 expiry (unix s)   40  HMAC-SHA256(server key, bytes 0..39) truncated to 24 bytes
# The game itself never checks the ticket: it only compares the account id the server echoes (NOTES.md 1.3). The
# server checks the signature, the expiry and that the account still owns the name.
import hashlib, hmac, os, re, secrets, sqlite3, struct, time

NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{2,15}$")
TICKET_LEN = 64
TICKET_LIFETIME = 24 * 3600


class AccountError(Exception):
    pass


def derive_key(token, name):
    """The per-account secret the runtime keeps (2026-11-13 challenge-response): HMAC-SHA256(token, "gowa-acct:"+name)."""
    return hmac.new(token.encode(), b"gowa-acct:" + name.lower().encode(), hashlib.sha256).digest()


def proof_for(key, nonce, name):
    """What the runtime sends instead of its token: HMAC-SHA256(K, nonce + ":" + name), hex."""
    return hmac.new(key, (nonce + ":" + name.lower()).encode(), hashlib.sha256).hexdigest()


class Accounts:
    def __init__(self, path):
        """path: a directory (created if missing) holding accounts.db and server.key, or ':memory:' for tests."""
        if path == ":memory:":
            self.db = sqlite3.connect(":memory:")
            self.key = secrets.token_bytes(32)
        else:
            os.makedirs(path, exist_ok=True)
            kf = os.path.join(path, "server.key")
            if not os.path.exists(kf):
                with open(kf, "wb") as f:
                    f.write(secrets.token_bytes(32))
            with open(kf, "rb") as f:
                self.key = f.read()
            self.db = sqlite3.connect(os.path.join(path, "accounts.db"))
        self.db.execute("CREATE TABLE IF NOT EXISTS accounts (name TEXT PRIMARY KEY COLLATE NOCASE, "
                        "acct INTEGER UNIQUE NOT NULL, salt BLOB NOT NULL, token_hash BLOB NOT NULL, "
                        "created INTEGER NOT NULL, last_seen INTEGER NOT NULL, banned INTEGER NOT NULL DEFAULT 0)")
        cols = [r[1] for r in self.db.execute("PRAGMA table_info(accounts)")]
        if "dkey" not in cols:
            self.db.execute("ALTER TABLE accounts ADD COLUMN dkey BLOB")
        self.db.commit()
        self.nonces = {}   # nonce -> expiry (monotonic); single use

    # ---- challenge-response (no token on the wire) -------------------------------------------------------------
    def new_nonce(self):
        now = time.monotonic()
        if len(self.nonces) > 20000:
            self.nonces = {n: e for n, e in self.nonces.items() if e > now}
        n = secrets.token_hex(16)
        self.nonces[n] = now + 60
        return n

    def _take_nonce(self, nonce):
        e = self.nonces.pop(nonce, None)
        if e is None or e < time.monotonic():
            raise AccountError("bad or expired challenge")

    def register_key(self, name, key_hex, now=None):
        """First sign-in of a free name with the derived key K (sent once). Returns the account id."""
        now = int(now if now is not None else time.time())
        if not NAME_RE.match(name or ""):
            raise AccountError("bad name")
        if not re.fullmatch(r"[0-9a-f]{64}", key_hex or ""):
            raise AccountError("bad key")
        if self.db.execute("SELECT 1 FROM accounts WHERE name = ?", (name,)).fetchone():
            raise AccountError("name taken")
        while True:
            acct = secrets.randbits(63) or 1
            if not self.db.execute("SELECT 1 FROM accounts WHERE acct = ?", (acct,)).fetchone():
                break
        self.db.execute("INSERT INTO accounts (name, acct, salt, token_hash, created, last_seen, banned, dkey) "
                        "VALUES (?, ?, ?, ?, ?, ?, 0, ?)", (name, acct, b"", b"", now, now, bytes.fromhex(key_hex)))
        self.db.commit()
        return acct

    def sign_in_proof(self, name, nonce, proof, now=None):
        """Sign-in with a challenge: proof = HMAC(K, nonce:name). Returns the account id."""
        now = int(now if now is not None else time.time())
        self._take_nonce(nonce)
        row = self.db.execute("SELECT acct, dkey, banned FROM accounts WHERE name = ?", (name,)).fetchone()
        if row is None or row[1] is None:
            raise AccountError("unknown name")
        acct, key, banned = row
        if not hmac.compare_digest(proof_for(key, nonce, name), (proof or "").lower()):
            raise AccountError("name taken")
        if banned:
            raise AccountError("banned")
        self.db.execute("UPDATE accounts SET last_seen = ? WHERE acct = ?", (now, acct))
        self.db.commit()
        return acct

    @staticmethod
    def _hash(salt, token):
        return hashlib.pbkdf2_hmac("sha256", token.encode(), salt, 20000)

    def sign_in(self, name, token, now=None):
        """Registers a free name or checks the token of a taken one. Returns the account id."""
        now = int(now if now is not None else time.time())
        if not NAME_RE.match(name or ""):
            raise AccountError("bad name")
        if not re.fullmatch(r"[0-9a-fA-F]{32,64}", token or ""):
            raise AccountError("bad token")
        row = self.db.execute("SELECT acct, salt, token_hash, banned FROM accounts WHERE name = ?", (name,)).fetchone()
        if row is None:
            salt = secrets.token_bytes(16)
            while True:
                acct = secrets.randbits(63) or 1
                if not self.db.execute("SELECT 1 FROM accounts WHERE acct = ?", (acct,)).fetchone():
                    break
            self.db.execute("INSERT INTO accounts (name, acct, salt, token_hash, created, last_seen, banned, dkey) "
                            "VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
                            (name, acct, salt, self._hash(salt, token), now, now, derive_key(token, name)))
            self.db.commit()
            return acct
        acct, salt, th, banned = row
        if not salt:   # registered with the challenge scheme: only K is stored
            k = self.db.execute("SELECT dkey FROM accounts WHERE acct = ?", (acct,)).fetchone()[0]
            if not hmac.compare_digest(derive_key(token, name), k or b""):
                raise AccountError("name taken")
        elif not hmac.compare_digest(self._hash(salt, token), th):
            raise AccountError("name taken")
        self.db.execute("UPDATE accounts SET dkey = ? WHERE acct = ? AND dkey IS NULL", (derive_key(token, name), acct))
        if banned:
            raise AccountError("banned")
        self.db.execute("UPDATE accounts SET last_seen = ? WHERE acct = ?", (now, acct))
        self.db.commit()
        return acct

    def name_of(self, acct):
        row = self.db.execute("SELECT name, banned FROM accounts WHERE acct = ?", (acct,)).fetchone()
        return (row[0], bool(row[1])) if row else (None, False)

    def ban(self, name, banned=True):
        self.db.execute("UPDATE accounts SET banned = ? WHERE name = ?", (1 if banned else 0, name))
        self.db.commit()

    def issue_ticket(self, acct, name, now=None):
        now = int(now if now is not None else time.time())
        body = b"MPN1" + struct.pack(">IQ", 2, acct) + name.encode().ljust(16, b"\0")[:16] + \
            struct.pack(">II", now, now + TICKET_LIFETIME)
        return body + hmac.new(self.key, body, hashlib.sha256).digest()[:24]

    def verify_ticket(self, t, now=None):
        """(acct, name) for a valid ticket this server signed, else raises AccountError."""
        now = int(now if now is not None else time.time())
        if len(t) < TICKET_LEN or t[:4] != b"MPN1":
            raise AccountError("not our ticket")
        ver, acct = struct.unpack(">IQ", t[4:16])
        if ver != 2:
            raise AccountError("unsigned ticket (version %d)" % ver)
        if not hmac.compare_digest(hmac.new(self.key, t[:40], hashlib.sha256).digest()[:24], t[40:64]):
            raise AccountError("bad signature")
        issued, expiry = struct.unpack(">II", t[32:40])
        if not issued - 300 <= now <= expiry:
            raise AccountError("expired")
        name = t[16:32].split(b"\0")[0].decode("ascii", "replace")
        owner, banned = self.name_of(acct)
        if owner is None or owner.lower() != name.lower():
            raise AccountError("unknown account")
        if banned:
            raise AccountError("banned")
        return acct, owner
