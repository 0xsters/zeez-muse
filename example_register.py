"""Complete registration + redemption flow example. Fill in config.json before running (see config.example.json)."""

import json
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from musepy import caa, falco
from musepy.client import MuseClient, MuseError, random_dob


def load_config(path="config.json"):
    if not os.path.exists(path):
        raise SystemExit(f"Missing config file {path}; copy config.example.json and fill it in")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main():
    cfg = load_config()

    email = cfg["email"]
    proxy = cfg.get("proxy") or None
    invite_code = cfg.get("invite_code") or ""
    otp_file = cfg.get("otp_file")  # optional: read the current OTP code from a file

    c = MuseClient(proxy=proxy, impersonate=cfg.get("impersonate", "chrome142"),
                   lang=cfg.get("lang", "ja"))

    print(f"[1] Open home page  {email}")
    c.open_home()

    print("[2] Restart registration flow")
    c.restart()

    # ---- Optional funnel telemetry ----
    funnel = caa.Caa(c.fp, c.lang)
    if cfg.get("send_caa", True):
        funnel.page_shown(c.s)
        time.sleep(2)
        funnel.focus(c.s)
        time.sleep(0.5)
        funnel.continue_clicked(c.s)
        funnel.registration_start(c.s)

    print("[3] Send OTP code")
    c.send_otp(email)

    code = otp_file and open(otp_file).read().strip() or input("Enter the 6-digit code you received: ").strip()

    if cfg.get("send_caa", True):
        funnel.otp_typed(c.s)

    print("[4] Verify OTP code")
    c.confirm_otp(code)

    first = cfg.get("first_name") or "三郎"    # given name
    last = cfg.get("last_name") or "近藤"      # family name
    dob = cfg.get("date_of_birth") or random_dob()

    if cfg.get("send_caa", True):
        funnel.otp_success(c.s)
        funnel.birthday_start(c.s)
        time.sleep(3)
        funnel.birthday_next(c.s)

    print(f"[5] Complete registration  {last} {first}  {dob}")
    data = c.register_completion(dob, first, last)
    uid = (data.get("auth_meta_session") or {}).get("frl_account_id")
    print(f"    account_id = {uid}")

    if cfg.get("send_caa", True):
        funnel.create_success(c.s, uid)
        funnel.login_success(c.s, uid)

    # ---- falco telemetry (optional) ----
    if cfg.get("send_falco", True):
        falco.send(c.s, c.fp, c.lang, str(uuid.uuid4()), [
            falco.perf("https://muse.ai/"),
            falco.interaction("https://muse.ai/", "page_load", "main_app", "page_load"),
        ])

    print("[6] Bootstrap session (session bootstrap; if missing, the whole chain returns 403)")
    token = (data.get("auth_meta_session") or {}).get("frl_access_token")
    onboarding = c.activate_session(token)
    print(f"    resolved_destination = {onboarding.get('resolved_destination')}")
    print(f"    disclosure_version   = {onboarding.get('disclosure_version')}")

    print("[7] Activate session (accept-tos, single call, body uses the server's dynamic disclosure)")
    c.accept_tos(onboarding)
    print(f"    invite = {'200 live account' if c.is_live() else '401/403 no hatch access'}")

    print("[8] Wake VM (wake_trigger=mount, open_id, away_ms=0)")
    vm = c.wake_vm()
    print(f"    vm_id = {vm}")

    c.stateful_sharing()

    print(f"[9] Redeem invite code  {invite_code or '(not configured)'}")
    if invite_code:
        ok, reason = c.redeem(invite_code)
        print(f"    redeem = {ok} / {reason}")
        if ok:
            own = c.scratch_code()
            print(f"[10] Own invite code (retrieved via POST invite, not scraped from the page) = {own}")
        else:
            print(f"    Failure reason mapping: already_redeemed=this account already used a code  "
                  f"used_up=this code has reached its usage limit  invalid=code does not exist")
    else:
        print("    Skip redemption")

    sess = c.session_cookie()
    print(f"\nhatch_sess length = {len(sess or '')}")
    c.close()


if __name__ == "__main__":
    try:
        main()
    except MuseError as e:
        if "region_waitlist" in str(e):
            # After bootstrapping, the server reports region-restricted: registration is complete and the account is kept, but it has not been proven to be on the allowlist
            print(f"Region-restricted: {e}")
            print("    Note: the account is registered but region-restricted and not on the allowlist; please check the egress node's region")
        else:
            print(f"Protocol failure: {e}")
        sys.exit(1)
