"""OTP code polling (open-source edition).

select_otp is a pure function; wait_otp polls the OTP source URL and only takes codes from after the send start time,
taking the newest 6-digit code (idempotent: retrying the same email never treats an old code as new).
"""

import json
import re
import time
from datetime import datetime, timezone

UTC = timezone.utc


def parse_iso8601(value):
    """Parse into a timezone-aware UTC time; naive input is treated as unknown and returns None."""
    try:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str):
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        else:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(UTC)
    except (TypeError, ValueError, OverflowError):
        return None


def select_otp(records, after):
    """Take the newest 6-digit numeric OTP strictly later than after; multiple different codes in the newest second count as ambiguous.

    records: [{"otp": "123456", "time": "<ISO8601>"}, ...]
    after:   the start time of this send_otp (ISO8601 string or datetime).
    """
    boundary = parse_iso8601(after)
    if boundary is None or not isinstance(records, list):
        return None
    candidates = []
    for item in records:
        if not isinstance(item, dict):
            continue
        code = item.get("otp")
        if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code):
            continue
        timestamp = parse_iso8601(item.get("time"))
        if timestamp is not None and timestamp > boundary:
            candidates.append((timestamp, code))
    if not candidates:
        return None
    newest = max(timestamp for timestamp, _ in candidates)
    codes = {code for timestamp, code in candidates if timestamp == newest}
    return next(iter(codes)) if len(codes) == 1 else None


def wait_otp(url, after, *, timeout=45, poll_interval=0.6, request_timeout=15,
             session_factory=None):
    """Poll the OTP source URL until a new OTP code appears.

    - GET once every poll_interval seconds; each request times out after request_timeout;
    - total timeout is timeout seconds (default 45s);
    - a non-200 / non-JSON-list response from the source returns None; network exceptions propagate (handled as transport_error);
    - session_factory can inject a fake session (for offline tests); by default a new curl_cffi Session is created.
    """
    if session_factory is not None:
        session = session_factory()
    else:
        from curl_cffi import requests as cr
        session = cr.Session()
    end = time.monotonic() + timeout
    try:
        while time.monotonic() < end:
            response = session.get(url, timeout=request_timeout, headers={"accept": "*/*"})
            if response.status_code != 200:
                return None
            try:
                records = json.loads(response.text)
            except (TypeError, ValueError):
                return None
            otp = select_otp(records, after)
            if otp:
                return otp
            time.sleep(poll_interval)
        return None
    finally:
        try:
            session.close()
        except Exception:
            pass
