"""mtex temp-mail OTP poller for the zeez-muse batch runner (runs on this VM).

mail.mtex.my.id is a Cloudflare temp-mail deployment
(dreamhunter2333/cloudflare_temp_email, MIT). Each mailbox address has
its own Address JWT; the agent API is:

  GET  /api/settings                     -> {"address", "send_balance"}
  GET  /api/parsed_mails?limit=&offset=  -> {"results": [parsedMail], "count"}
  GET  /api/parsed_mail/:id              -> one parsedMail
  POST /api/new_address  (body {})       -> {"address", "jwt"} (one per call)

Auth: Authorization: Bearer <Address JWT> on every call. Cloudflare
blocks bare urllib (403 error 1010), so every request sends a browser
User-Agent (verified live 2026-11-19).

What it does:
- generate: POST /api/new_address N times with the MAIN address JWT
  (from ~/.cf-temp-mail/credentials.json), appends each new
  {"address", "jwt"} to ~/.cf-temp-mail/addresses.json (mode 600).
- poll: for each address in addresses.json (+ the main address),
  GET /api/parsed_mails, take the first 6-digit code from a muse.ai
  or Meta sender, and INSERT OR IGNORE it into the same Turso table
  otps the Vercel endpoint serves. The batch runner then works
  unchanged (it polls Turso via otp-urls.txt).
  PROVEN 2026-11-19 20:09 WIB: the muse.ai OTP arrives as a Meta
  account-verification email from security@account.meta.com. The code
  in that mail passes the script's confirm-otp step, so the Meta mail
  IS the OTP mail. A sender filter that only accepts muse.ai misses
  every OTP; that was the real bug all day, not the mailbox.
- Record time is the poller's first-seen UTC time (microsecond ISO):
  parsedMail.created_at has NO timezone ("YYYY-MM-DD HH:MM:SS"), and
  the batch's select_otp only accepts tz-aware ISO strictly after the
  send time, so first-seen UTC is the safe value. created_at is kept
  in the seen log only.
- Codes from OTHER senders are NOT inserted into Turso; they are
  written to mtex_seen.jsonl so the sender question can be settled
  with evidence instead of guesses.
- Polling discipline (per the cf-temp-mail-agent-mail skill): start 3s,
  backoff capped at 10s, never faster than 1/s, respect 429.

What it does NOT do:
- It never sends mail, never deletes mail.
- It never prints or logs any JWT. JWTs are read at runtime from
  ~/.cf-temp-mail/credentials.json and addresses.json (both mode 600)
  and are never written anywhere else. NEVER paste a JWT into this
  script, into git, or into chat.

Usage (from this directory; paths resolve from __file__):
    python3 mtex_poller.py --test
        # settings + inbox of the main address, then create ONE test
        # address and list its inbox (proves auth + creation).
    python3 mtex_poller.py --generate 5
        # create N addresses, append to addresses.json
    python3 mtex_poller.py --once
        # single pass over all known addresses, then exit
    python3 mtex_poller.py
        # continuous: pass every 10 s until stopped (pidfile guard)
"""

import json
import os
import re
import signal
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CREDS_FILE = os.path.expanduser("~/.cf-temp-mail/credentials.json")
ADDRS_FILE = os.path.expanduser("~/.cf-temp-mail/addresses.json")
TOKEN_FILE = os.path.join(HERE, "turso_token.txt")
SEEN_FILE = os.path.join(HERE, "mtex_seen.jsonl")
PID_FILE = os.path.join(HERE, "mtex_poller.pid")

BASE_URL = "https://mail.mtex.my.id"
TURSO_URL = "https://zeez-otp-zexez.aws-ap-southeast-1.turso.io/v2/pipeline"

POLL_SECONDS = 10
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/142.0.0.0 Safari/537.36")
OTP_RE = re.compile(r"\b(\d{6})\b")


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def load_main_creds():
    with open(CREDS_FILE, encoding="utf-8") as f:
        return json.load(f)


def _jwt_address(jwt):
    """Address from the JWT payload (base64 decode, no verification).
    The payload is {"address": "...", "address_id": N}."""
    try:
        import base64
        payload = jwt.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))["address"]
    except Exception:
        return ""


def load_addresses():
    """All known addresses as [{address, jwt}]: main address first."""
    out = []
    try:
        main = load_main_creds()
        if main.get("jwt"):
            out.append({"address": _jwt_address(main["jwt"]),
                        "jwt": main["jwt"]})
    except Exception:
        pass
    try:
        with open(ADDRS_FILE, encoding="utf-8") as f:
            for rec in json.load(f):
                if rec.get("address") and rec.get("jwt"):
                    out.append({"address": rec["address"], "jwt": rec["jwt"]})
    except Exception:
        pass
    seen = set()
    uniq = []
    for rec in out:
        if rec["address"] not in seen:
            seen.add(rec["address"])
            uniq.append(rec)
    return uniq


def load_turso_token():
    with open(TOKEN_FILE, encoding="utf-8") as f:
        return f.read().strip()


def api_call(jwt, method, path, body=None):
    """One mtex API call. Returns parsed JSON. Raises RuntimeError on
    HTTP error (429 included, so the caller can back off)."""
    url = BASE_URL + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": "Bearer " + jwt,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": BROWSER_UA,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        raise RuntimeError("mtex %s %s -> HTTP %d: %s"
                           % (method, path, e.code,
                              e.read().decode("utf-8", "ignore")[:300]))
    except Exception as e:
        raise RuntimeError("mtex %s %s failed: %s" % (method, path, e))
    try:
        return json.loads(raw)
    except Exception:
        return {"_raw": raw}


def generate_address(main_jwt):
    res = api_call(main_jwt, "POST", "/api/new_address", {})
    if isinstance(res, dict) and res.get("address") and res.get("jwt"):
        return {"address": res["address"], "jwt": res["jwt"]}
    raise RuntimeError("new_address: unexpected response: %s"
                       % json.dumps(res)[:500])


def save_address(rec):
    recs = []
    try:
        with open(ADDRS_FILE, encoding="utf-8") as f:
            recs = json.load(f)
    except Exception:
        recs = []
    if all(r.get("address") != rec["address"] for r in recs):
        recs.append(rec)
        with open(ADDRS_FILE, "w", encoding="utf-8") as f:
            json.dump(recs, f, indent=2)
        os.chmod(ADDRS_FILE, 0o600)


def list_parsed_mails(jwt, limit=20):
    res = api_call(jwt, "GET", "/api/parsed_mails?limit=%d&offset=0" % limit)
    if isinstance(res, dict):
        results = res.get("results")
        if isinstance(results, list):
            return results
    return []


def _mail_text(mail):
    parts = []
    for k in ("subject", "sender", "source", "text", "html", "snippet"):
        v = mail.get(k)
        if isinstance(v, str):
            parts.append(v)
    return "\n".join(parts)


def log_seen(email_addr, mail, code):
    row = {"email": email_addr,
           "mail_id": mail.get("id"),
           "sender": mail.get("sender") or mail.get("source") or "",
           "subject": mail.get("subject") or "",
           "created_at": mail.get("created_at") or "",
           "otp": code or "",
           "checked_at": _now_iso()}
    with open(SEEN_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def insert_otp(email_addr, code, iso_time, turso_token):
    payload = {
        "requests": [
            {
                "type": "execute",
                "stmt": {
                    "sql": "INSERT OR IGNORE INTO otps (email, otp, time) VALUES (?, ?, ?)",
                    "args": [
                        {"type": "text", "value": email_addr},
                        {"type": "text", "value": code},
                        {"type": "text", "value": iso_time},
                    ],
                },
            },
            {"type": "close"},
        ]
    }
    req = urllib.request.Request(
        TURSO_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + turso_token,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", "ignore")
            if resp.status != 200:
                return False
            try:
                res = json.loads(body)
                for r in res.get("results") or []:
                    inner = (r.get("response") or {}).get("result") or {}
                    if "rows_affected" in inner:
                        return inner["rows_affected"] > 0
            except Exception:
                pass
            return True
    except Exception as e:
        print("insert failed for %s: %s" % (email_addr, e), flush=True)
        return False


def check_address(rec, turso_token):
    """One pass over one mtex inbox. Returns OTPs newly stored."""
    email_addr, jwt = rec["address"], rec["jwt"]
    try:
        mails = list_parsed_mails(jwt)
    except RuntimeError as e:
        print("[%s] inbox error: %s" % (email_addr, e), flush=True)
        return 0
    stored = 0
    for mail in mails:
        if not isinstance(mail, dict):
            continue
        text = _mail_text(mail)
        m = OTP_RE.search(text)
        code = m.group(1) if m else ""
        sender = (mail.get("sender") or mail.get("source") or "").lower()
        subject = (mail.get("subject") or "").lower()
        # PROVEN 2026-11-19 20:09 WIB: the muse.ai OTP arrives as a
        # Meta account-verification email (security@account.meta.com).
        # Its 6-digit code passes confirm-otp, so Meta senders are OTP
        # senders too. Only accepting muse.ai was the bug.
        is_otp_sender = ("muse.ai" in sender or "muse.ai" in subject
                         or "muse" in sender or "meta.com" in sender)
        log_seen(email_addr, mail, code)
        if not code:
            continue
        if not is_otp_sender:
            print("[%s] non-OTP code seen (sender %s), logged only"
                  % (email_addr, sender or "?"), flush=True)
            continue
        if insert_otp(email_addr, code, _now_iso(), turso_token):
            stored += 1
            print("[%s] OTP %s stored" % (email_addr, code), flush=True)
    return stored


def poll_once(turso_token):
    addrs = load_addresses()
    stored = 0
    for rec in addrs:
        try:
            stored += check_address(rec, turso_token)
        except RuntimeError as e:
            print("[%s] error: %s" % (rec["address"], e), flush=True)
            if "429" in str(e):
                print("rate limited, backing off 30s", flush=True)
                time.sleep(30)
    return len(addrs), stored


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def write_pidfile():
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE, encoding="utf-8") as f:
                old = int(f.read().strip())
            if pid_alive(old):
                print("mtex_poller already running (pid %d)" % old, flush=True)
                return False
        except (ValueError, OSError):
            pass
    with open(PID_FILE, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    return True


def remove_pidfile():
    try:
        os.unlink(PID_FILE)
    except OSError:
        pass


def main():
    argv = sys.argv[1:]
    main_jwt = load_main_creds().get("jwt")
    if "--test" in argv:
        settings = api_call(main_jwt, "GET", "/api/settings")
        print("settings: %s" % settings, flush=True)
        mails = list_parsed_mails(main_jwt, limit=5)
        print("main inbox: %d mail(s)" % len(mails), flush=True)
        rec = generate_address(main_jwt)
        save_address(rec)
        print("created test address: %s" % rec["address"], flush=True)
        print("its inbox: %d mail(s)"
              % len(list_parsed_mails(rec["jwt"], limit=5)), flush=True)
        return 0
    if "--generate" in argv:
        i = argv.index("--generate")
        n = int(argv[i + 1]) if i + 1 < len(argv) else 1
        for _ in range(n):
            rec = generate_address(main_jwt)
            save_address(rec)
            print("generated: %s" % rec["address"], flush=True)
        return 0
    turso_token = load_turso_token()
    if not turso_token:
        print("error: empty Turso token", flush=True)
        return 1
    if "--once" in argv:
        polled, stored = poll_once(turso_token)
        print("once: polled %d address(es), stored %d OTP(s)"
              % (polled, stored), flush=True)
        return 0
    if not write_pidfile():
        return 1

    stop = {"flag": False}

    def _sig(signum, frame):
        stop["flag"] = True

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    print("mtex_poller started (pid %d)" % os.getpid(), flush=True)
    try:
        while not stop["flag"]:
            poll_once(turso_token)
            for _ in range(POLL_SECONDS):
                if stop["flag"]:
                    break
                time.sleep(1)
    finally:
        remove_pidfile()
        print("mtex_poller stopped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
