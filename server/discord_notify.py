# GoW Ascension community server: optional posts to a Discord channel through a webhook (2026-11-15).
# The server owner makes the webhook (Discord: channel settings > Integrations > Webhooks > New Webhook > Copy URL)
# and puts its URL in a file: gowa_server.py --discord-webhook-file <file>. Without that option nothing is ever sent.
# Posts: "match started" (mode + players) and once a day the week's top 3 (XP, wins, trial waves).
# Mentions are switched off (allowed_mentions: none) and posts are rate limited, so a player name cannot ping anyone
# and a busy server cannot flood the channel.
import json, re, threading, time, urllib.request

URL_RE = re.compile(r"^https://(discord\.com|discordapp\.com|ptb\.discord\.com|canary\.discord\.com)/api/webhooks/\d+/[\w-]+$")
MIN_GAP = 5.0          # seconds between two posts
MAX_PER_HOUR = 30


def read_url(path):
    with open(path, encoding="utf-8") as f:
        url = f.read().strip()
    if not URL_RE.match(url):
        raise ValueError("not a Discord webhook URL (https://discord.com/api/webhooks/<id>/<token>)")
    return url


def clean(s, n=40):
    """A player name / server name as plain text: no markdown, no mentions, no links."""
    return re.sub(r"[^\w .'()-]", "", str(s))[:n] or "?"


class Notifier:
    def __init__(self, url, server_name="", log=lambda *a: None, send=None):
        self.url, self.server_name, self.log = url, clean(server_name, 60), log
        self.sent = []          # post times (rate limit)
        self.lock = threading.Lock()
        self._send = send or self._http

    def _http(self, payload):
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": "gowa-server"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status

    def post(self, text):
        """Send one message (in a thread). Returns False when the rate limit drops it."""
        now = time.time()
        with self.lock:
            self.sent = [t for t in self.sent if now - t < 3600]
            if len(self.sent) >= MAX_PER_HOUR or (self.sent and now - self.sent[-1] < MIN_GAP):
                return False
            self.sent.append(now)
        payload = {"content": text[:1900], "allowed_mentions": {"parse": []}}
        if self.server_name:
            payload["username"] = self.server_name

        def work():
            try:
                self._send(payload)
            except Exception as e:      # Discord down, URL revoked: logged, never fatal
                self.log("[discord] post failed: %s" % e)
        threading.Thread(target=work, daemon=True).start()
        return True

    def match_started(self, mode_name, names):
        players = ", ".join(clean(n, 16) for n in names) or "?"
        return self.post("**Match started**: %s (%s)" % (clean(mode_name, 40), players))

    def weekly_top(self, lb):
        """The week's top 3 of a few boards (lb = leaderboard.Leaderboard)."""
        lines = []
        for key, title in (("xp", "XP"), ("wins", "Wins"), ("waves", "Trial waves")):
            rows = lb.top(key, 3, period="week")
            if rows:
                lines.append("**%s**: " % title + ", ".join("%d. %s %s" % (r["rank"], clean(r["name"], 16),
                                                                         format(r["value"], ",")) for r in rows))
        if not lines:
            return False
        return self.post("**This week's leaders**\n" + "\n".join(lines))
