"""
muse.ai registration and invite-code redemption protocol client (pure HTTP, no browser)

For protocol research and personal learning only. Before using it, please confirm the target site's terms of service yourself.
"""

import json
import random
import re
import time
import uuid
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

from curl_cffi import requests as cr

BASE = "https://muse.ai"

# Session-bootstrap endpoint: after register-completion returns frl_access_token, this must be called first;
# otherwise all subsequent hatch endpoints return 403 (confirmed by local runtime testing).
BOOTSTRAP_URL = "https://auth.meta.com/hatch/session/bootstrap/"
TRUSTED_HOSTS = frozenset({"auth.meta.com", "auth.muse.ai", "muse.ai"})

# TOS version submitted on the onboarding page, only a fallback for when confirm-otp does not return tos.content_version;
# normally the server version dynamically returned by confirm_otp should be used.
TOS_VERSION = "ntm_create_account:default_v1_meta_account_v3+docs_v2"

HEADERS_JSON = {
    "accept": "*/*",
    "content-type": "application/json",
    "origin": BASE,
    "referer": BASE + "/",
    "sec-fetch-site": "same-origin",
    "sec-fetch-mode": "cors",
    "sec-fetch-dest": "empty",
}
HEADERS_GET = {
    "accept": "*/*",
    "referer": BASE + "/",
    "sec-fetch-site": "same-origin",
    "sec-fetch-mode": "cors",
    "sec-fetch-dest": "empty",
}
HEADERS_HTML = {
    # Full browser document Accept string (derived from packet-capture comparison; a truncated version gives itself away in the bootstrap chain)
    "accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,image/apng,*/*;q=0.8,"
               "application/signed-exchange;v=b3;q=0.7"),
    "sec-fetch-site": "same-origin",
    "sec-fetch-mode": "navigate",
    "sec-fetch-dest": "document",
}

# Reference for the disclosure text returned by the server during packet capture. The accept-tos body now uses the result of activate_session()
# to parse the server's dynamic disclosure_text / disclosure_version; this constant is kept only as reference documentation.
DISCLOSURE_TEXT_JA = (
    "次に進むことで、Muse利用規約およびMetaのAI利用規約に同意するものとします。"
    "これには利用者の権利と責任に関する重要な情報が含まれています。"
    "プライバシーに関するMuseのアプローチについて詳しくはMuseプライバシーポリシーをご覧ください。"
)


class MuseError(RuntimeError):
    """Expected failures in the protocol flow"""


class MuseClient:
    """One account = one Session (one egress IP)"""

    def __init__(self, proxy=None, impersonate="chrome142", lang="ja", timeout=30):
        self.fp = impersonate
        self.lang = lang
        self.proxy = proxy
        self.timeout = timeout
        self.s = cr.Session(impersonate=impersonate,
                            proxies={"http": proxy, "https": proxy} if proxy else None,
                            timeout=timeout)
        self.csrf = None
        self.tos_content_version = None   # server TOS version dynamically returned by confirm-otp
        self.onboarding = None            # disclosure result parsed by activate_session

    # ---------- Infrastructure ----------

    def _h(self, base, extra=None):
        h = dict(base)
        h["accept-language"] = self.lang
        if extra:
            h.update(extra)
        return h

    def _json(self, resp):
        try:
            return resp.json() or {}
        except Exception:
            return {}

    def close(self):
        try:
            self.s.close()
        except Exception:
            pass

    # ---------- Registration flow ----------

    def open_home(self):
        return self.s.get(BASE + "/", impersonate=self.fp,
                          headers=self._h(HEADERS_HTML))

    def restart(self, resume=False):
        """Start the registration flow and return csrf_token"""
        r = self.s.post(BASE + "/api/auth/native/restart", impersonate=self.fp,
                        headers=self._h(HEADERS_JSON), data=json.dumps({"resume": resume}))
        if r.status_code != 201:
            raise MuseError(f"restart {r.status_code}: {r.text[:120]}")
        self.csrf = self._json(r).get("csrf_token")
        if not self.csrf:
            raise MuseError("restart did not return csrf_token")
        return self.csrf

    def _auth_headers(self):
        h = self._h(HEADERS_JSON)
        if self.csrf:
            h["X-Hatch-CSRF-Token"] = self.csrf
            h["X-Hatch-Waterfall-Id"] = _uuid()
            h["X-Hatch-Caa-Reg-Entry-Point"] = "login_home"
        return h

    def send_otp(self, email):
        r = self.s.post(BASE + "/api/auth/native/send-otp", impersonate=self.fp,
                        headers=self._auth_headers(), data=json.dumps({"contact_point": email}))
        if r.status_code not in (200, 201):
            raise MuseError(f"send-otp {r.status_code}: {r.text[:120]}")
        return self._json(r)

    def confirm_otp(self, code):
        """Key point: supports_muse_link_transparency must be true"""
        r = self.s.post(BASE + "/api/auth/native/confirm-otp", impersonate=self.fp,
                        headers=self._auth_headers(),
                        data=json.dumps({"otp_code": code,
                                         "remember_device": True,
                                         "supports_muse_link_transparency": True}))
        if r.status_code != 200:
            raise MuseError(f"confirm-otp {r.status_code}: {r.text[:120]}")
        data = self._json(r)
        tos = data.get("tos")
        if (isinstance(tos, dict) and isinstance(tos.get("content_version"), str)
                and tos["content_version"].strip()):
            # register-completion must use the version dynamically returned by the server, not a hardcoded constant
            self.tos_content_version = tos["content_version"]
        return data

    def register_completion(self, date_of_birth, first_name, last_name, remember_device=True):
        """
        Complete the registration. Note the field semantics:
          first_name = given name
          last_name  = family name
        A birthday earlier than about 18 years ago is rejected by the server (403).
        """
        body = {
            "date_of_birth": date_of_birth,
            "first_name": first_name,
            "last_name": last_name,
            "tos_content_version": self.tos_content_version or TOS_VERSION,
            "remember_device": remember_device,
        }
        r = self.s.post(BASE + "/api/auth/native/register-completion", impersonate=self.fp,
                        headers=self._auth_headers(), data=json.dumps(body))
        if r.status_code != 200:
            raise MuseError(f"register-completion {r.status_code}: {r.text[:120]}")
        data = self._json(r)
        self.account_id = (data.get("auth_meta_session") or {}).get("frl_account_id")
        return data

    def session_cookie(self, name="hatch_sess"):
        for c in self.s.cookies.jar:
            if c.name == name:
                return c.value
        return None

    # ---------- Activation ----------

    def _trusted_location(self, location, base_url):
        """Validate and resolve a 3xx Location (relative paths are resolved against the current URL).

        Only https and trusted hosts (auth.meta.com / auth.muse.ai / muse.ai) are accepted;
        targets with userinfo, non-443 ports, or backslashes/whitespace are rejected.
        """
        if (not isinstance(location, str) or not location
                or "\\" in location or any(ch.isspace() for ch in location)):
            raise MuseError("activate_session redirect Location is invalid")
        parts = urlsplit(urljoin(base_url, location))
        if (parts.scheme != "https" or parts.hostname not in TRUSTED_HOSTS
                or parts.username is not None or parts.password is not None
                or parts.port not in (None, 443)):
            raise MuseError("activate_session redirect target is not trusted")
        return urlunsplit(("https", parts.hostname, parts.path or "/", parts.query, ""))

    def activate_session(self, frl_access_token):
        """Session bootstrap (must be called after register-completion and before accept-tos).

        POST https://auth.meta.com/hatch/session/bootstrap/ with a form carrying only
        frl_access_token, allow_redirects=False, manually following 301/302/303/307/308
        redirects (307/308 keep the original method and body),
        and finally landing on a muse.ai 200 HTML document. Parse the dynamic disclosure and status gates from the document:
          - resolvedDestination=="waitlist" or isGeoGated=True
                → MuseError("region_waitlist") (the account is registered but region-restricted and has not been proven to be on the allowlist)
          - Disclosure text/version conflict or missing → MuseError("disclosure_context_unresolved")
          - Only disclosureVersion=="HATCH_ROW_v0" is supported; unknown versions are never guessed
        On success, returns a dict (containing disclosure_text / disclosure_version / source_url),
        to be passed directly to accept_tos().
        """
        if not isinstance(frl_access_token, str) or not frl_access_token.strip():
            raise MuseError("activate_session is missing frl_access_token")
        url, method, hops = BOOTSTRAP_URL, "POST", 0
        while True:
            headers = self._h(HEADERS_HTML)
            if method == "POST":
                headers.update({
                    "content-type": "application/x-www-form-urlencoded",
                    "origin": BASE,
                    "referer": BASE + "/",
                    "sec-fetch-dest": "document",
                    "sec-fetch-mode": "navigate",
                    "sec-fetch-site": "cross-site",
                    "upgrade-insecure-requests": "1",
                    "cache-control": "max-age=0",
                    "priority": "u=0, i",
                })
                r = self.s.post(url, impersonate=self.fp, headers=headers,
                                data=urlencode({"frl_access_token": frl_access_token}),
                                allow_redirects=False)
            else:
                r = self.s.get(url, impersonate=self.fp, headers=headers,
                               allow_redirects=False)
            if r.status_code in (301, 302, 303, 307, 308):
                hops += 1
                if hops > 6:
                    raise MuseError("activate_session has too many redirects")
                url = self._trusted_location(r.headers.get("location"), url)
                if r.status_code in (307, 308):
                    # 307/308 must keep the original method and body; continue with the same method
                    pass
                else:
                    method = "GET"
                continue
            break
        if r.status_code != 200:
            raise MuseError(f"activate_session bootstrap failed {r.status_code}: {r.text[:120]}")
        final = urlsplit(url)
        if final.hostname != "muse.ai":
            raise MuseError(f"activate_session landed on a non-muse.ai domain ({final.hostname})")
        onboarding = parse_onboarding(r.text)
        if onboarding["state"] == "waitlist":
            raise MuseError("region_waitlist")
        if onboarding["state"] != "disclosure":
            raise MuseError(f"disclosure_context_unresolved ({onboarding.get('reason')})")
        onboarding["source_url"] = urlunsplit(("https", final.hostname, final.path or "/", "", ""))
        self.onboarding = onboarding
        return onboarding

    def accept_tos(self, onboarding=None):
        """Must be called, otherwise all subsequent hatch endpoints return 403. A single call is enough; do not retry.

        onboarding must be the parsed result returned by activate_session(): the body takes the
        dynamically returned disclosure_text / disclosure_version from the server (no longer a hardcoded constant),
        and Referer uses the actual disclosure page URL.
        """
        text = (onboarding or {}).get("disclosure_text")
        version = (onboarding or {}).get("disclosure_version")
        if not text or not version:
            raise MuseError("accept_tos is missing the dynamic disclosure result; call activate_session() first")
        body = {
            "saw_meta_ai_tos": False,
            "client_request_timestamp": str(int(time.time())),
            "entry_point": "web_onboarding_flow",
            "nux_type": "ROW",
            "disclosure_version": version,
            "disclosure_text": text,
        }
        headers = self._h(HEADERS_JSON)
        referer = (onboarding or {}).get("source_url")
        if referer:
            headers["referer"] = referer
        r = self.s.post(BASE + "/api/hatch/accept-tos", impersonate=self.fp,
                        headers=headers, data=json.dumps(body))
        if r.status_code == 403:
            raise MuseError(f"accept-tos 403: {r.text[:200]}")
        if r.status_code != 200:
            raise MuseError(f"accept-tos {r.status_code}: {r.text[:200]}")
        return r

    def invite(self):
        return self.s.get(BASE + "/api/hatch/invite", impersonate=self.fp,
                          headers=self._h(HEADERS_GET))

    def is_live(self):
        """invite 200 = valid account; 401/403 = no hatch access"""
        try:
            return self.invite().status_code == 200
        except Exception:
            return False

    def wake_vm(self):
        """Must be called, otherwise redemption returns a misleading invalid.

        On the first connection, wake_trigger is "mount", and the body carries open_id (a new uuid) and away_ms=0;
        before waking, GET /?aymh_complete=1 first and parse vmId from the document (keeping the original logic).
        """
        r = self.s.get(BASE + "/?aymh_complete=1", impersonate=self.fp,
                       headers=self._h(HEADERS_HTML))
        m = re.search(r'vmId[\\"]*:[\\"]*([0-9a-fA-F-]{36})', r.text)
        if not m:
            return None
        vm_id = m.group(1)
        self.s.post(BASE + "/api/hatch/vm/wake", impersonate=self.fp,
                    headers=self._h(HEADERS_JSON),
                    data=json.dumps({"vm_id": vm_id,
                                     "retry_count": 0,
                                     "connect_attempt_id": _uuid(),
                                     "wake_trigger": "mount",
                                     "open_id": _uuid(),
                                     "away_ms": 0}))
        return vm_id

    def stateful_sharing(self):
        return self.s.post(BASE + "/api/hatch/stateful-sharing", impersonate=self.fp,
                           headers=self._h(HEADERS_JSON), data="")

    # ---------- Redemption ----------

    def redeem(self, code):
        """
        Redeem an invite code. Returns (ok, reason).
          reason: redeemed | already_redeemed | used_up | invalid
        """
        r = self.s.post(BASE + "/api/hatch/invite-code/redeem", impersonate=self.fp,
                        headers=self._h(HEADERS_JSON),
                        data=json.dumps({"code": code, "supportsRedemptionStatus": True}))
        if r.status_code == 200 and '"success":true' in r.text:
            return True, "redeemed"
        data = self._json(r)
        return False, data.get("reason") or f"http-{r.status_code}"

    def scratch_code(self):
        """After a successful redemption, retrieve this account's assigned invite code.

        Note: POST /api/hatch/invite only returns (your own) invite code itself;
        it is not the same as revealing the code on the page a second time.
        """
        r = self.s.post(BASE + "/api/hatch/invite", impersonate=self.fp,
                        headers=self._h(HEADERS_JSON), data="")
        if r.status_code != 200:
            return None
        return self._json(r).get("code")


def _uuid():
    return str(uuid.uuid4())


def _flight_chunks(text):
    """Extract the string data serialized in self.__next_f.push (plain-text parsing, no script execution)."""
    chunks = []
    if not isinstance(text, str):
        return chunks
    for script in re.findall(r"<script\b[^>]*>(.*?)</script\s*>", text, re.I | re.S):
        match = re.fullmatch(r"\s*self\.__next_f\.push\((.*)\)\s*;?\s*", script, re.S)
        if not match:
            continue
        try:
            value = json.loads(match.group(1))
        except (ValueError, TypeError):
            continue
        if (isinstance(value, list) and len(value) == 2 and value[0] == 1
                and isinstance(value[1], str)):
            chunks.append(value[1])
    return chunks


def parse_onboarding(html):
    """Parse the Next Flight data in the muse.ai bootstrap document (an offline pure function, no requests).

    Returns a dict: state = disclosure | waitlist | unresolved.
      - resolvedDestination=="waitlist" or isGeoGated is True → state="waitlist"
      - Disclosure text/version conflict or missing → state="unresolved" (never guess parameters)
      - Only disclosureVersion=="HATCH_ROW_v0" is supported; unknown versions are also unresolved
    The status fields (isGeoGated / hasHatchAccess / waitlistStatus) are passed through as-is.
    """
    states, disclosures = [], []
    objects = []

    def collect(value):
        if isinstance(value, dict):
            objects.append(value)
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    for row in "".join(_flight_chunks(html)).splitlines():
        row_id, separator, data = row.partition(":")
        if not separator or not re.fullmatch(r"[0-9a-f]+", row_id):
            continue
        try:
            collect(json.loads(data))
        except (ValueError, TypeError):
            continue  # non-JSON data rows such as module/text imports
    for obj in objects:
        if "resolvedDestination" in obj and isinstance(obj.get("status"), dict):
            status = obj["status"]
            states.append({
                "resolved_destination": obj.get("resolvedDestination"),
                "is_geo_gated": status.get("isGeoGated"),
                "has_hatch_access": status.get("hasHatchAccess"),
                "waitlist_status": status.get("waitlistStatus"),
            })
        if "disclosureText" in obj or "disclosureVersion" in obj:
            disclosures.append((obj.get("disclosureText"), obj.get("disclosureVersion")))
    if states:
        if len({json.dumps(s, sort_keys=True) for s in states}) > 1:
            return {"state": "unresolved", "reason": "onboarding_state_conflict"}
        if (states[0]["resolved_destination"] == "waitlist"
                or states[0]["is_geo_gated"] is True):
            return {"state": "waitlist", "reason": "onboarding_waitlist", **states[0]}
    texts = list(dict.fromkeys(t for t, _ in disclosures
                               if isinstance(t, str) and t.strip()))
    versions = list(dict.fromkeys(v for _, v in disclosures
                                  if isinstance(v, str) and v.strip()))
    if len(texts) != 1 or len(versions) != 1:
        return {"state": "unresolved", "reason": "disclosure_context_unresolved"}
    if versions[0] != "HATCH_ROW_v0":
        return {"state": "unresolved", "reason": "disclosure_version_unsupported",
                "disclosure_version": versions[0]}
    out = {"state": "disclosure", "reason": "disclosure_context_observed",
           "disclosure_text": texts[0], "disclosure_version": versions[0]}
    if states:
        out.update(states[0])
    return out


def random_dob(min_year=1970, max_year=1999):
    return (f"{random.randint(min_year, max_year)}-"
            f"{random.randint(1, 12):02d}-"
            f"{random.randint(1, 28):02d}")
