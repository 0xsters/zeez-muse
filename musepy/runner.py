"""Multi-account batch scheduler (open-source edition, semantics aligned with the local implementation).

Scheduling rules:
- A worker exclusively holds one sticky proxy line (claim/release) and never takes another worker's line; lines are only shared (and logged) when the pool is exhausted.
- max_per_ip counts "successful verifications": when the limit is reached, release the current line and switch lines (the old line is avoided for the rest of the round).
- Transport failures in the pre-send phase (initialized/bootstrap/restart, OTP not yet sent): release the current line,
  mark pool_bad in memory, retry with a different proxy for the same email <= 3 times without consuming a new email; if it still fails, mark it tried.
- Business rejections / pending / failures after the OTP was sent: stop the whole batch: no proxy switch, no retry, no replacement email.
- region_waitlist: the account is registered but region-restricted: record registered_waitlist (keeping the account) and stop.
- Budget = the number of distinct emails claimed; failures are not replaced.

Each account's result is written to output_dir/<email>.json (simplified fields). frl_access_token and
hatch_sess are used in memory only and are never written to disk. For personal learning and research only.
"""

import concurrent.futures as cf
import json
import os
import random
import re
import ssl
import threading
import time
import urllib.request
from datetime import datetime, timezone

from . import caa as _caa
from . import otp as _otp
from .client import MuseClient, MuseError, random_dob

PRE_SEND_STAGES = frozenset({"initialized", "bootstrap", "restart"})
AUTH_STAGES = PRE_SEND_STAGES | {"send_otp", "wait_otp", "confirm_otp", "completion"}

TRANSPORT_MARKERS = (
    "curl: (35)", "curl: (28)", "curl: (7)", "curl: (56)", "curl: (52)",
    "timed out", "connectionerror", "connection reset", "connection refused",
    "certificate", "ssl",
)

# Common Japanese name pool (family name / given name); register_completion semantics: first_name = given name, last_name = family name
SURNAMES = ["佐藤", "鈴木", "高橋", "田中", "伊藤", "渡辺", "山本", "中村", "小林", "加藤",
            "吉田", "山田", "佐々木", "山口", "松本", "井上", "木村", "林", "斎藤", "清水",
            "山崎", "森", "池田", "橋本", "阿部", "石川", "前田", "藤田", "小川", "岡田",
            "後藤", "長谷川", "石井", "村上", "近藤", "坂本", "遠藤", "青木", "藤井", "西村"]
GIVEN_NAMES = ["ヨウ", "タクミ", "ユウキ", "ハルト", "ソウタ", "リク", "カイト", "ミナト", "ハル", "アオイ",
               "ヒナタ", "ツバサ", "レン", "コウ", "シュン", "ナオキ", "ケイタ", "ダイキ", "リョウ", "ユウト",
               "サクラ", "ユイ", "メイ", "アカリ", "ヒカリ", "ノゾミ", "ミサキ", "カナ", "リナ", "ユナ",
               "ソラ", "カイ", "トワ", "イツキ", "アサヒ", "マコト", "タケル", "ジン", "カズマ", "ユウマ"]


def _lines(path):
    try:
        with open(path, encoding="utf-8") as stream:
            return [line.strip() for line in stream if line.strip()]
    except OSError:
        return []


def mkproxy(line):
    """host:port:user:pass → http://user:pass@host:port；host:port → http://host:port.

    A leading http:// or https:// scheme is stripped first, so a line copied
    from a proxy list with its scheme still parses instead of raising
    proxy_line_format.
    """
    line = (line or "").strip()
    lowered = line.lower()
    for scheme in ("http://", "https://"):
        if lowered.startswith(scheme):
            line = line[len(scheme):]
            break
    parts = line.split(":")
    if len(parts) == 4:
        host, port, user, password = parts
        return f"http://{user}:{password}@{host}:{port}"
    if len(parts) == 2:
        host, port = parts
        return f"http://{host}:{port}"
    raise ValueError("proxy_line_format")


# Free proxy-list APIs (no key needed). Fetched directly, no proxy, at batch
# start; every returned line is validated again by _resolve_ip before use,
# so dead entries are skipped automatically. proxies.txt stays as fallback.
PROXY_API_SOURCES = (
    ("geonode",
     "https://proxylist.geonode.com/api/proxy-list"
     "?limit=100&page=1&sort_by=lastChecked&sort_type=desc"
     "&country=JP&protocols=http%2Chttps",
     "json"),
    ("proxyscrape",
     "https://api.proxyscrape.com/v2/?request=displayproxies"
     "&protocol=http&timeout=10000&country=JP&ssl=all&anonymity=all",
     "text"),
    ("proxyscrape_github",
     "https://raw.githubusercontent.com/ProxyScrape/free-proxy-list/main"
     "/proxies/countries/jp/http/data.txt",
     "text"),
    ("proxyscrape_github_https",
     "https://raw.githubusercontent.com/ProxyScrape/free-proxy-list/main"
     "/proxies/countries/jp/https/data.txt",
     "text"),
    ("gimmeproxy",
     "https://gimmeproxy.com/api/getProxy?country=JP&protocol=http",
     "gimme"),
    ("hproxy",
     "https://raw.githubusercontent.com/hproxy-com/free-proxy-list/main"
     "/by-country/JP.txt",
     "text"),
    ("databay",
     "https://databay.com/free-proxy-list/japan",
     "html"),
    ("proxifly",
     "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main"
     "/proxies/countries/JP/data.txt",
     "text"),
    ("proxyscrape_v4",
     "https://api.proxyscrape.com/v4/free-proxy-list/get"
     "?request=display_proxies&proxy_format=protocolipport&format=text&country=jp",
     "text"),
    ("proxmint",
     "https://proxmint.com/api/free-proxies?country=jp&format=txt",
     "text"),
    ("databay_labs",
     "https://raw.githubusercontent.com/databay-labs/free-proxy-list/master"
     "/by-country/jp/http.txt",
     "text"),
    ("proxywing",
     "https://proxywing.com/api/free-proxy-list?format=txt&country=JP&protocol=http",
     "text"),
)


_BROWSER_UA = {"User-Agent": "Mozilla/5.0"}


def _fetch_text_lines(url, timeout):
    """Plain-text proxy list: one bare "host:port" per line, scheme stripped."""
    req = urllib.request.Request(url, headers=_BROWSER_UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        text = resp.read().decode("utf-8", "ignore")
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        lowered = line.lower()
        for scheme in ("http://", "https://"):
            if lowered.startswith(scheme):
                line = line[len(scheme):]
                break
        # Drop non-HTTP proxy schemes (socks4://, socks5://, ...): the
        # runner only speaks HTTP proxies, and a "socks5" host would fail.
        if "://" in line:
            continue
        if line and ":" in line:
            lines.append(line)
    return lines


def _fetch_html_proxies(url, timeout):
    """HTML proxy table (e.g. databay): scrape bare "host:port" occurrences.

    The page renders its table server-side, so a plain fetch sees the rows;
    a browser user-agent is required (default urllib UA gets a 403).
    Duplicates are removed by the caller's _add dedup.
    """
    req = urllib.request.Request(url, headers=_BROWSER_UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        html = resp.read().decode("utf-8", "ignore")
    found = re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}:\d{2,5}\b", html)
    lines = []
    for line in found:
        if line not in lines:
            lines.append(line)
    return lines


def geo_country(ip):
    """Country code for an egress IP via ip-api.com (free, no key). None on any failure."""
    try:
        with urllib.request.urlopen(
                f"http://ip-api.com/json/{ip}?fields=status,countryCode",
                timeout=8) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        if payload.get("status") == "success":
            return payload.get("countryCode")
    except Exception:  # noqa: BLE001 - a failed lookup must not stop the batch
        pass
    return None


def fetch_proxies_from_apis(timeout=12):
    """Fetch fresh Japan HTTP proxy lines from the free APIs above.

    Returns a deduplicated list of bare "host:port" lines, freshest first.
    A source that fails (network error, bad payload) is logged and skipped;
    the other source and proxies.txt still apply. Never raises.
    """
    found = []

    def _add(source, lines):
        seen = set(found)
        fresh = [line for line in lines if line and line not in seen]
        if fresh:
            found.extend(fresh)
        print(f"[i] proxy api {source}: {len(fresh)} lines")

    for name, url, kind in PROXY_API_SOURCES:
        try:
            if kind == "json":
                # Geonode: JSON, filter to elite/anonymous, fast, high-uptime entries.
                with urllib.request.urlopen(url, timeout=timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                lines = []
                for item in payload.get("data", []):
                    protocols = item.get("protocols") or []
                    if not any(p in ("http", "https") for p in protocols):
                        continue
                    if item.get("anonymityLevel") not in ("elite", "anonymous"):
                        continue
                    if (item.get("latency") or 99999) > 3000 or (item.get("upTime") or 0) < 50:
                        continue
                    lines.append(f"{item['ip']}:{item['port']}")
                _add(name, lines)
            elif kind == "gimme":
                # GimmeProxy: one freshly-checked JP proxy per call; ask a
                # few times (1s apart, polite) and keep the distinct hits.
                # A single failed call must not kill the remaining tries.
                # GimmeProxy rejects urllib's default user-agent (403), so
                # send a browser one, like a normal visitor would.
                lines = []
                for _ in range(5):
                    try:
                        req = urllib.request.Request(
                            url, headers=_BROWSER_UA)
                        with urllib.request.urlopen(req, timeout=timeout) as resp:
                            item = json.loads(resp.read().decode("utf-8"))
                        if item.get("get") and item.get("country") == "JP" and item.get("ipPort"):
                            lines.append(item["ipPort"])
                    except Exception:
                        pass
                    time.sleep(1)
                _add(name, lines)
            elif kind == "html":
                _add(name, _fetch_html_proxies(url, timeout))
            else:
                _add(name, _fetch_text_lines(url, timeout))
        except Exception as exc:  # noqa: BLE001 - one dead source must not stop the batch
            print(f"[i] proxy api {name} failed: {exc}")

    return found


def proxy_endpoint(line):
    """Take only host:port, for logging (never print credentials)."""
    parts = (line or "").split(":")
    return f"{parts[0]}:{parts[1]}" if len(parts) >= 2 else None


def _classify_error(exc):
    """Transport-layer failures are classified as transport_error; everything else is failed. No inference about IP bans is made."""
    if isinstance(exc, (TimeoutError, ConnectionError, ssl.SSLError)):
        return "transport_error"
    text = str(exc).lower()
    if any(marker in text for marker in TRANSPORT_MARKERS):
        return "transport_error"
    return "failed"


def utc_stamp():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class BatchRunner:
    """Multi-account batch scheduler. One account = a new MuseClient instance + one current sticky proxy line."""

    def __init__(self, config, *, client_factory=None, otp_waiter=None, ip_resolver=None, geo_resolver=None):
        self.cfg = config
        self.workers = int(config.get("workers", 5) or 5)
        self.max_per_ip = int(config.get("max_per_ip", 6) or 6)
        self.lang = config.get("lang", "ja")
        self.fp = config.get("impersonate", "chrome142")
        self.otp_file = config.get("otp_file")
        self.proxy_file = config.get("proxy_file")
        self.proxy_api = bool(config.get("proxy_api", True))
        self.codes_file = config.get("codes_file")
        self.output_dir = config.get("output_dir", "output")
        self.tried_file = config.get("tried_file")
        self.send_caa = bool(config.get("send_caa", False))
        self.resolve_ip = bool(config.get("resolve_ip", True))
        # Injection points (for offline tests): client factory / OTP polling / egress IP resolution / geo lookup
        self.client_factory = client_factory or self._default_client_factory
        self.otp_waiter = otp_waiter or self._default_otp_waiter
        self.ip_resolver = ip_resolver or self._default_ip_resolver
        self.geo_resolver = geo_resolver or geo_country
        self.ROWS, self.QUEUE, self.PX, self.CODES = {}, [], [], []
        self._lk = threading.Lock()
        self._codes_lk = threading.Lock()
        self._storage_lk = threading.Lock()
        self._stop = threading.Event()
        self._stop_reason = None
        self._proxy_claims, self._pool_bad, self._ipcache, self._tried = set(), set(), {}, set()
        self._geocache = {}
        self._qi = [0]
        self._stats = {key: 0 for key in ("started", "ok", "pending", "server_rejected",
                                          "transport_error", "failed", "ip_switches")}

    # ---------- Default dependencies ----------

    def _default_client_factory(self, proxy_url):
        return MuseClient(proxy=proxy_url, impersonate=self.fp, lang=self.lang)

    @staticmethod
    def _default_otp_waiter(email, url, after):
        return _otp.wait_otp(url, after)

    @staticmethod
    def _default_ip_resolver(proxy_url):
        from curl_cffi import requests as cr
        r = cr.get("https://api.ipify.org", impersonate="chrome142",
                   proxies={"http": proxy_url, "https": proxy_url}, timeout=12)
        ip = r.text.strip()
        return ip if ip.count(".") == 3 else None

    # ---------- Pool loading (budget = count distinct emails) ----------

    def init(self, count=30):
        """Load the email/proxy/OTP pools. Failures are not replaced; emails already in tried_file are skipped."""
        count = int(count)
        if count < 1:
            raise ValueError("count_must_be_positive")
        rows = {}
        for line in _lines(self.otp_file or ""):
            if "----" not in line:
                continue
            email, url = line.split("----", 1)
            email = email.strip()
            if not email or any(ch in email for ch in "/\\:\x00"):
                continue  # invalid record name, skip it directly
            rows[email] = url.strip()
        tried = (set(_lines(self.tried_file))
                 if self.tried_file and os.path.exists(self.tried_file) else set())
        file_proxies = _lines(self.proxy_file or "")
        api_proxies = fetch_proxies_from_apis() if self.proxy_api else []
        seen = set(api_proxies)
        proxies = api_proxies + [line for line in file_proxies if line not in seen]
        print(f"[i] proxy pool: {len(api_proxies)} from APIs, "
              f"{len(file_proxies)} from file, {len(proxies)} total")
        if not proxies:
            raise ValueError("proxy_pool_empty")
        with self._lk:
            self.ROWS = rows
            self.QUEUE = [email for email in rows if email not in tried][:count]
            self.PX = proxies
            self.CODES = _lines(self.codes_file) if self.codes_file else []
            self._tried = set(tried)
            self._proxy_claims.clear()
            self._pool_bad.clear()
            self._ipcache.clear()
            self._geocache.clear()
            self._qi[0] = 0
            for key in self._stats:
                self._stats[key] = 0
            self._stop.clear()
            self._stop_reason = None
        os.makedirs(self.output_dir, exist_ok=True)
        return len(self.QUEUE)

    # ---------- Batch state ----------

    def next_job(self):
        with self._lk:
            if self._stop.is_set() or self._qi[0] >= len(self.QUEUE):
                return None
            email = self.QUEUE[self._qi[0]]
            self._qi[0] += 1
            self._stats["started"] += 1
            return email

    def request_stop(self, reason):
        with self._lk:
            if not self._stop.is_set():
                self._stop_reason = reason
            self._stop.set()

    def summary(self):
        with self._lk:
            return {**self._stats,
                    "queued": len(self.QUEUE),
                    "remaining": len(self.QUEUE) - self._stats["started"],
                    "stopped": self._stop.is_set(),
                    "stop_reason": self._stop_reason}

    # ---------- Proxy scheduling (worker-exclusive sticky) ----------

    def _country_of(self, ip):
        """Cached country code for an egress IP. None means the lookup failed."""
        with self._lk:
            if ip in self._geocache:
                return self._geocache[ip]
        country = None
        try:
            country = self.geo_resolver(ip)
        except Exception:  # noqa: BLE001 - a failed lookup must not stop the batch
            country = None
        with self._lk:
            self._geocache[ip] = country
        return country

    def _resolve_ip(self, line):
        """resolve_ip=false → (None, True): the egress IP is unknown but the line is still usable.

        With resolve_ip on, a line is usable only when its egress IP resolves
        AND exits in Japan. A non-JP exit (the proxy list may label it JP
        while the exit node sits elsewhere) is treated exactly like a dead
        line: skipped and marked pool_bad by the caller. A failed geo lookup
        accepts the line unchecked, so a down geo service never kills the run.
        """
        if not self.resolve_ip:
            return None, True
        with self._lk:
            if line in self._ipcache:
                cached = self._ipcache[line]
                return cached, cached is not None
        try:
            ip = self.ip_resolver(mkproxy(line))
            if ip is not None and not (isinstance(ip, str) and ip.count(".") == 3):
                ip = None
        except Exception:
            ip = None
        if ip is not None:
            country = self._country_of(ip)
            if country is not None and country != "JP":
                print(f"[i] proxy {proxy_endpoint(line)} exits in {country}, "
                      f"not JP, skipping")
                ip = None
        with self._lk:
            self._ipcache[line] = ip
        return ip, ip is not None

    def _pick_proxy(self, *, current=None, avoid=None):
        """Worker-exclusive sticky selection: an occupied line is not handed to another worker.

        When the pool is exhausted or all validations fail: first reuse your own current line, then look for an unoccupied line,
        and only share as a last resort (logged). avoid excludes the line just replaced (after reaching max_per_ip).
        """
        for _ in range(400):
            line = random.choice(self.PX)
            if line in self._pool_bad or line == avoid:
                continue
            if line != current:
                with self._lk:
                    if line in self._proxy_claims:
                        continue
            ip, ok = self._resolve_ip(line)
            if not ok:
                with self._lk:
                    self._pool_bad.add(line)
                continue
            with self._lk:
                self._proxy_claims.add(line)
            return line, ip
        if current is not None and current not in self._pool_bad:
            with self._lk:
                self._proxy_claims.add(current)
            return current, self._ipcache.get(current)
        for _ in range(len(self.PX)):
            with self._lk:
                free = [item for item in self.PX
                        if item not in self._pool_bad and item not in self._proxy_claims
                        and item != avoid]
                if not free:
                    break
                line = random.choice(free)
                self._proxy_claims.add(line)
            ip, ok = self._resolve_ip(line)
            if ok:
                return line, ip
            with self._lk:
                self._proxy_claims.discard(line)
        if current is not None:
            with self._lk:
                self._proxy_claims.add(current)
            return current, self._ipcache.get(current)
        with self._lk:
            shared = list(self._proxy_claims) or list(self.PX)
        line = random.choice(shared)
        print(f"[i] proxy pool exhausted; sharing {proxy_endpoint(line)}", flush=True)
        with self._lk:
            self._proxy_claims.add(line)
        return line, self._ipcache.get(line)

    def _release(self, line):
        """Release this worker's sticky claim; leave pool_bad untouched."""
        if not line:
            return
        with self._lk:
            self._proxy_claims.discard(line)

    # ---------- Records (simplified fields, no tokens/cookies) ----------

    def _save_record(self, rec):
        path = os.path.join(self.output_dir, rec["email"] + ".json")
        with self._storage_lk:
            previous = {}
            try:
                with open(path, encoding="utf-8") as stream:
                    previous = json.load(stream)
            except (OSError, ValueError):
                previous = {}
            merged = {**previous, **rec}
            for key in ("registered", "own_code"):
                if previous.get(key) and not rec.get(key):
                    merged[key] = previous[key]
            if previous.get("status") == "redeemed":
                merged["status"] = "redeemed"
            merged["updated_at"] = utc_stamp()
            with open(path, "w", encoding="utf-8") as stream:
                json.dump(merged, stream, ensure_ascii=False, indent=2)

    def _stamp_tried(self, email):
        with self._storage_lk:
            self._tried.add(email)
            if self.tried_file:
                with open(self.tried_file, "a", encoding="utf-8") as stream:
                    stream.write(email + "\n")

    def _take_code(self):
        with self._codes_lk:
            if self.CODES:
                return self.CODES.pop(0)
        return None

    def _finish(self, email, rec, outcome, reason, stage, *, status=None):
        """Single exit point: write to disk, then decide whether to stop the whole batch. Returns (outcome, detail)."""
        rec.update(stage=stage, last_error=reason)
        if status is None:
            if rec.get("registered") and outcome == "ok":
                status = "redeemed"
            elif rec.get("registered"):
                status = "registered_pending_redemption"
            else:
                status = outcome
        rec["status"] = status
        self._save_record(rec)
        stop = (outcome in ("server_rejected", "pending", "failed")
                or (outcome == "transport_error" and stage not in PRE_SEND_STAGES))
        if stop:
            self.request_stop("authentication_paused" if stage in AUTH_STAGES
                              else "workflow_paused")
        return outcome, {"stage": stage, "status": status,
                         "registered": bool(rec.get("registered")), "reason": reason}

    # ---------- Single-account full chain (order aligned with the local implementation) ----------

    def register_one(self, email, proxy_line):
        """open→restart→send_otp→wait_otp→confirm→completion→activate_session
        (onboarding gate) → accept_tos → wake → sharing → invite → redeem → own_code.

        Returns (outcome, detail); outcome: ok|server_rejected|pending|transport_error|failed
        """
        rec = {"email": email, "status": "pending", "stage": "initialized",
               "registered": False, "own_code": None, "own_code_status": None,
               "reveal_status": "not_attempted", "is_revealed": None,
               "ip": self._ipcache.get(proxy_line), "last_error": None}
        stage = "initialized"
        client = None
        try:
            if not self.ROWS.get(email):
                return self._finish(email, rec, "failed", "otp_source_missing", stage)
            client = self.client_factory(mkproxy(proxy_line))
            caa = _caa.Caa(client.fp, client.lang) if self.send_caa else None
            stage = "bootstrap"
            client.open_home()
            if caa:
                caa.page_shown(client.s)
                caa.focus(client.s)
                caa.continue_clicked(client.s)
            stage = "restart"
            client.restart()
            if caa:
                caa.registration_start(client.s)
            stage = "send_otp"
            client.send_otp(email)
            sent_at = utc_stamp()
            if caa:
                caa.otp_typed(client.s)
            stage = "wait_otp"
            otp_code = self.otp_waiter(email, self.ROWS.get(email), sent_at)
            if not otp_code:
                return self._finish(email, rec, "pending", "otp_wait_timeout", stage)
            if caa:
                caa.otp_success(client.s)
                caa.birthday_start(client.s)
            stage = "confirm_otp"
            client.confirm_otp(otp_code)
            stage = "completion"
            data = client.register_completion(random_dob(),
                                              random.choice(GIVEN_NAMES),
                                              random.choice(SURNAMES))
            auth = data.get("auth_meta_session") or {}
            token, uid = auth.get("frl_access_token"), auth.get("frl_account_id")
            if not token:
                return self._finish(email, rec, "pending", "completion_result_unknown", stage)
            rec["registered"] = True  # token is used in memory only, never written to disk
            if caa:
                caa.create_success(client.s, uid)
                caa.login_success(client.s, uid)
            stage = "session_bootstrap"
            onboarding = client.activate_session(token)
            stage = "accept_tos"
            client.accept_tos(onboarding)
            stage = "wake"
            if client.wake_vm() is None:
                return self._finish(email, rec, "pending", "vm_unresolved", stage)
            stage = "sharing"
            client.stateful_sharing()
            stage = "invite"
            if not client.is_live():
                return self._finish(email, rec, "pending", "invite_result_unverified", stage)
            code = self._take_code()
            if not code:
                return self._finish(email, rec, "pending", "redemption_code_unavailable", stage)
            stage = "redeem"
            ok, reason = client.redeem(code)
            if not ok:
                return self._finish(email, rec, "server_rejected",
                                    f"redemption_unverified:{reason}", stage)
            stage = "own_code"
            own = client.scratch_code()
            if own:
                rec["own_code"] = own
                rec["own_code_status"] = "available"
                return self._finish(email, rec, "ok", "redemption_verified", stage)
            rec["own_code_status"] = "pending"
            return self._finish(email, rec, "ok", "own_code_pending", stage)
        except MuseError as exc:
            text = str(exc)
            if "region_waitlist" in text:
                return self._finish(email, rec, "pending", "region_waitlist", stage,
                                    status="registered_waitlist")
            if text.startswith(("restart ", "send-otp ", "confirm-otp ",
                                "register-completion ", "accept-tos ")):
                return self._finish(email, rec, "server_rejected", text, stage)
            return self._finish(email, rec, "pending", text, stage)
        except Exception as exc:
            return self._finish(email, rec, _classify_error(exc), type(exc).__name__, stage)
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    # ---------- worker ----------

    def _worker(self, wid):
        proxy_line, avoid = None, None
        uses, retries = 0, 0
        email = None
        while True:
            if email is None:
                email = self.next_job()
                if email is None:
                    self._release(proxy_line)
                    return
                retries = 0
            if proxy_line is None:
                proxy_line, _ = self._pick_proxy(current=None, avoid=avoid)
                avoid = None
            outcome, detail = self.register_one(email, proxy_line)
            message = json.dumps(detail, ensure_ascii=False, sort_keys=True)
            print(f"  [W{wid}] {outcome} {message}", flush=True)
            with self._lk:
                self._stats[outcome if outcome in self._stats else "failed"] += 1
            if outcome == "ok":
                self._stamp_tried(email)
                uses += 1  # max_per_ip only counts successful verifications
                email, retries = None, 0
                if uses >= self.max_per_ip and not self._stop.is_set():
                    self._release(proxy_line)
                    with self._lk:
                        self._stats["ip_switches"] += 1
                    avoid = proxy_line  # when switching lines, avoid the one just used up
                    proxy_line, uses = None, 0
            elif (outcome == "transport_error" and retries < 3
                    and detail.get("stage") in PRE_SEND_STAGES):
                # pre-send transport failure, zero side effects: release the current proxy and retry with a different proxy for the same email,
                # without consuming a new email; pool_bad is only marked in memory, no blacklist file is written.
                retries += 1
                self._release(proxy_line)
                with self._lk:
                    self._pool_bad.add(proxy_line)
                proxy_line = None
                with self._lk:
                    self._stats["ip_switches"] += 1
                print(f"  [W{wid}] proxy_retry#{retries} {email}", flush=True)
            else:
                # Business rejections / pending / failures after the OTP was sent: no proxy switch, no retry, no replacement email;
                # stopping the whole batch takes effect via request_stop in register_one's _finish.
                self._stamp_tried(email)
                email, retries = None, 0
            if self._stop.is_set():
                self._release(proxy_line)
                return

    def run(self):
        workers = max(1, self.workers)
        with cf.ThreadPoolExecutor(max_workers=workers) as executor:
            list(executor.map(self._worker, range(workers)))
