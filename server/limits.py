# GoW: Ascension community server: per-address limits (2026-11-13). Python 3 standard library only.
import time


class Bucket:
    """Token bucket per key: `rate` tokens per second, at most `burst`."""

    def __init__(self, rate, burst, clock=time.monotonic):
        self.rate, self.burst, self.clock = rate, burst, clock
        self.state = {}

    def take(self, key, n=1.0):
        now = self.clock()
        tokens, last = self.state.get(key, (self.burst, now))
        tokens = min(self.burst, tokens + (now - last) * self.rate)
        if tokens < n:
            self.state[key] = (tokens, now)
            return False
        self.state[key] = (tokens - n, now)
        if len(self.state) > 10000:            # forget idle addresses
            for k in [k for k, (t, l) in self.state.items() if now - l > 600][:5000]:
                del self.state[k]
        return True


class Limits:
    """HTTP: 20 requests/s per address (burst 40). Sign-in failures (/ticket, /hello with a wrong token): 5 per minute
    per address, then refused until it refills (stops token guessing). SAM: at most `sam_per_ip` connections per
    address (one-PC tests use one per window from 127.0.0.1)."""

    def __init__(self, sam_per_ip=8, clock=time.monotonic):
        self.http = Bucket(20.0, 40.0, clock)
        self.fail = Bucket(5.0 / 60.0, 5.0, clock)
        self.sam_per_ip = sam_per_ip
        self.sam_open = {}

    def http_ok(self, ip):
        return self.http.take(ip)

    def may_try_sign_in(self, ip):
        now = self.fail.clock()
        tokens, last = self.fail.state.get(ip, (self.fail.burst, now))
        return min(self.fail.burst, tokens + (now - last) * self.fail.rate) >= 1.0

    def sign_in_failed(self, ip):
        self.fail.take(ip)

    def sam_open_ok(self, ip):
        if self.sam_open.get(ip, 0) >= self.sam_per_ip:
            return False
        self.sam_open[ip] = self.sam_open.get(ip, 0) + 1
        return True

    def sam_closed(self, ip):
        n = self.sam_open.get(ip, 1) - 1
        if n <= 0:
            self.sam_open.pop(ip, None)
        else:
            self.sam_open[ip] = n
