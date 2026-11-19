# muse.ai Registration / Invite-Code Redemption Protocol Analysis

One more note: remember to use a JP node. JP does not require card binding, it passes directly.

A pure-HTTP implementation of the muse.ai registration and invite-code redemption flow, with no browser dependency. This document records protocol details derived from packet capture, checked field by field.

## Quick Start

```bash
pip install -r requirements.txt
cp config.example.json config.json   # fill in your email and optional proxy
python example_register.py
```

## Directory Structure

```
musepy/
  client.py    Complete protocol client for registration + redemption
  runner.py    Multi-account batch scheduler (sticky proxy / budget / stop semantics)
  otp.py       OTP polling (idempotent: only takes the newest code after send)
  falco.py     Behavior telemetry reporting (optional)
  caa.py       Registration-funnel event reporting (optional)
example_register.py        Single-account end-to-end example
example_batch.py           Multi-account batch example
config.example.json        Config template
test_onboarding_smoke.py   Offline smoke test (pure disclosure-parsing functions)
test_runner_smoke.py       Offline smoke test (batch scheduling, mocked network)
```

## Protocol Flow

```
GET  /                                   Fetch the initial cookie
POST /api/auth/native/restart            → 201 {csrf_token}
POST /api/auth/native/send-otp           → 201
POST /api/auth/native/confirm-otp        → 200 {registration_state:"completion_required",
                                               tos:{content_version}} (dynamic TOS version)
POST /api/auth/native/register-completion→ 200 {auth_meta_session:{frl_access_token, frl_account_id}}
POST auth.meta.com/hatch/session/bootstrap/ → 302/307 redirects → muse.ai 200 HTML (session bootstrap, required)
     └ Parse the dynamic disclosure disclosure_text/disclosure_version and status gates from the muse.ai document
       (resolvedDestination/isGeoGated/waitlist → region-restricted)
POST /api/hatch/accept-tos               → 200 (single call; body uses the server's dynamic disclosure, not a hardcoded value)
GET  /api/hatch/invite                   → 200 means the account is valid
POST /api/hatch/vm/wake                  → 200 (required, otherwise redemption falsely reports invalid;
                                             wake_trigger=mount + open_id + away_ms=0）
POST /api/hatch/stateful-sharing         → 200
POST /api/hatch/invite-code/redeem       → 200 {redemptionStatus:"redeemed"}
POST /api/hatch/invite                   → 200 {code} (retrieve your own code; not the same as revealing the code on the page)
```

## Common Pitfalls

### 1. `confirm-otp` must include `supports_muse_link_transparency: true`

```json
{"otp_code":"123456","remember_device":true,"supports_muse_link_transparency":true}
```

### 2. Name-field semantics in `register-completion`

```json
{
  "date_of_birth": "1993-11-14",
  "first_name": "三郎",     // given name
  "last_name": "近藤",      // family name
  "tos_content_version": "ntm_create_account:default_v1_meta_account_v3+docs_v2",
  "remember_device": true
}
```

`first_name` is the given name and `last_name` is the family name, which is the opposite of what you might expect. A birthday earlier than about 18 years ago returns 403. In the same session with the same name, just changing the birthday from 2013 to 1993 flips the result from 403 to 200.

### 3. The `restart` body is not an empty object

```json
{"resume": false}
```

### 4. `accept-tos` is the session-activation switch, and the session must be bootstrapped first

After registration, you **must first call `POST auth.meta.com/hatch/session/bootstrap/`** (the form carries only
`frl_access_token`, manually following the 302/301/303/307/308 redirects (307/308 keep the POST method and body) to land on the muse.ai 200 document). Otherwise, all subsequent
hatch endpoints return 403. The muse.ai document you land on carries the dynamic disclosure serialized with Next Flight
(`disclosureText` / `disclosureVersion`, currently observed version `HATCH_ROW_v0`), and the `accept-tos`
body must use this dynamic copy returned by the server (**not** a hardcoded constant), with `Referer` set to the disclosure page
URL. A single call is enough, no retry; on a 403, include the first 200 characters of the response body for diagnosis.

If after bootstrapping the server reports `resolvedDestination == "waitlist"` or `isGeoGated == true`,
the account is **region-restricted**: registration is complete and the account is kept, but it has not been proven to be on the allowlist.

### 5. A missing `vm/wake` makes redemption falsely report failure

Redeeming directly without calling `vm/wake` returns the following even for a valid code:

```json
{"success":false,"reason":"invalid","detail":"この招待コードは存在しません"}
```

You first need to extract `vmId` from the logged-in page:

```python
re.search(r'vmId[\\"]*:[\\"]*([0-9a-fA-F-]{36})', html)
```

The observed `wake` body is: `wake_trigger: "mount"` (first connection), `open_id` (a new uuid),
`away_ms: 0`, not the earlier `resume`.

### 6. What redemption failure reasons mean

| reason | meaning |
|---|---|
| `redeemed` | Success |
| `already_redeemed` | This account has already redeemed a code |
| `used_up` | The code has reached its 30-use limit |
| `invalid` | The code does not exist, or `vm/wake` was not called |

## Cookie

After a successful registration, `hatch_sess` is issued via the `register-completion` response header (about 886 characters); it is the credential for all subsequent hatch endpoints. `hatch_vml` stays cleared during the registration phase (`Max-Age=0`) and is not required.

## Multi-Account Batch (musepy/runner.py)

```bash
python example_batch.py [count]
```

Batch fields in `config.json`:

| field | description |
|---|---|
| `otp_file` | Email list, one per line as `email----otp-source-URL` (the OTP source returns `[{"otp","time"},...]` JSON) |
| `proxy_file` | Sticky proxy pool, one per line as `host:port:user:pass` (or `host:port`); fallback when the proxy APIs return nothing |
| `proxy_api` | Whether to fetch fresh Japan HTTP proxies from free APIs at batch start (default true); set false to use only `proxy_file` |
| `codes_file` | Optional pool of invite codes to redeem, one per line |
| `output_dir` / `tried_file` | Per-account result JSON directory / list of already-tried emails |
| `workers` / `max_per_ip` | Number of workers (default 5) / per-proxy limit of successful verifications (default 6) |
| `resolve_ip` | Whether to resolve the egress IP via ipify (default true) |
| `send_caa` | Whether to report registration-funnel telemetry in batch mode |

Scheduling semantics (aligned with the local implementation; no bypass behavior is promised):

- **Proxy pool is fetched automatically**: at batch start the runner pulls fresh Japan HTTP proxies from free keyless APIs (Geonode, ProxyScrape v2 and v4, the ProxyScrape free-proxy-list GitHub repo http + https files, GimmeProxy which returns one freshly-checked JP proxy per call, the HProxy by-country JP file, the Databay Japan table, Proxifly JP list, ProxMint, the Databay Labs JP http file, ProxyWing, and the ClearProxy checked JP list), merges them with `proxy_file`, and deduplicates. Non-HTTP schemes (socks4/socks5) are dropped from text lists. No manual copy-paste of proxy lists is needed; `proxy_file` stays as fallback, and `proxy_api: false` disables the fetch.
- **Japan-only egress gate**: a proxy line is usable only when its egress IP resolves and exits in Japan. A line whose exit node sits in another country (the list may label it JP while the exit is elsewhere) is skipped exactly like a dead line. If the geo lookup itself fails, the line is accepted unchecked so a down geo service never kills the run.
- **A leading `http://` / `https://` scheme in a proxy line is tolerated**: it is stripped before parsing, so pasted list entries with their scheme no longer raise `proxy_line_format`.
- **Each worker exclusively holds one sticky proxy**: a proxy line belongs to only one worker at a time (claim/release),
  and workers never take another worker's line; lines are only shared (and logged) when the pool is exhausted.
- **max_per_ip counts successful verifications**: when the limit is reached, release the current line and switch to another; the old line is avoided for the rest of the round.
- **Pre-send transport-failure retries**: network failures in `bootstrap`/`restart` and similar stages **before the OTP is sent**,
  release the current proxy and **retry with a different proxy for the same email, up to 3 times**, without consuming a new email; if it still fails, mark the email as tried.
- **Business rejections / pending / failures after the OTP was sent: stop the whole batch**: no proxy switch, no retry, no replacement email.
- **region_waitlist**: the account is registered but region-restricted: record `registered_waitlist` and keep the account,
  then stop the whole batch.
- **Budget = the number of distinct emails claimed**: failures are not replaced; at the end, print the batch_summary JSON
  (queued/started/remaining/ok/pending/server_rejected/transport_error/failed/
  ip_switches/stopped/stop_reason).

Each account's result is written to `output_dir/<email>.json` with the fields: `email/status/stage/registered/
own_code/own_code_status/reveal_status/is_revealed/ip/last_error`.
`frl_access_token` and `hatch_sess` are used in memory only and are **never written to disk**.

OTP polling is idempotent: the start time is recorded before each `send_otp`, and `musepy/otp.py` only takes codes from after that time,
taking the newest 6-digit code (45-second timeout, 0.6-second interval, 15-second per-request timeout).

## Telemetry Notes

The site frontend reports two kinds of telemetry:

- **falco** (`POST /api/falco`) - performance metrics and business interaction events, with `machineId` fixed to `not_set`
- **caa-events** (`POST /api/auth/caa-events`) - registration funnel, reported step by step via `navigator.sendBeacon`, with the same `waterfall_id` running through the whole flow

Neither affects the registration decision, and omitting them does not break the flow. `musepy/falco.py` and `musepy/caa.py` keep the full implementations, which can be disabled via config.

## Usage Declaration

This project is for personal, non-commercial learning and research only and does not use the MIT or any other open-source license. It must not be used commercially or redistributed. When using it, please comply with the target site's terms of service and local laws and regulations.

Discussion group: t.me/muihbb
