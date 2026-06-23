"""SDR Outbound Batch — deterministic Python pipeline.

Reads queued rows from the Leads sheet, picks a channel per row, sends via
LinkedIn (Unipile invitation) when the prospect has a linkedin_url and the
daily LI cap (20 sends/day) hasn't been reached, otherwise via Brevo email.
Writes status + channel + message_id back to the sheet. No LLM control flow;
the agent just calls this once per routine run.
"""
import json
import os
import re
import traceback
from datetime import datetime, timedelta, timezone

import httpx


def _json_or_empty(r: httpx.Response) -> dict:
    """Parse response JSON, returning {} when the body is empty or unparseable.
    HTTP errors should already be raised by the caller via raise_for_status —
    this only guards against 2xx responses with empty/invalid bodies."""
    text = (r.text or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}

BREVO_BASE = "https://api.brevo.com/v3"
SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
TOKEN_URL = "https://oauth2.googleapis.com/token"

# LinkedIn account-safety cap. Going above this is what gets the LinkedIn
# account flagged or restricted — not a throughput target. 20/day is the
# widely-quoted safe ceiling for invitations from a non-premium account.
LI_DAILY_CAP = 20

COLUMNS = [
    "date", "name", "first_name", "last_name", "email", "company", "title",
    "linkedin_url", "signal", "source", "hook", "email_subject", "email_body",
    "status", "message_id", "skip_reason", "sent_at", "channel", "connection_note",
]


class BrevoError(Exception):
    def __init__(self, status, body):
        self.status = status
        try:
            j = json.loads(body) if isinstance(body, str) else body
            self.code = (j or {}).get("code", "") or ""
            self.message = (j or {}).get("message", "") or (body[:400] if isinstance(body, str) else str(body))
        except Exception:
            self.code = ""
            self.message = body[:400] if isinstance(body, str) else str(body)
        super().__init__(f"Brevo {status}: {self.code or ''} {self.message}")

    def is_cap(self):
        msg = (self.message or "").lower()
        if self.status in (402,):
            return True
        if self.code in ("account_under_validation", "plan_limit_exceeded", "not_acceptable", "credits_exhausted"):
            return True
        if "daily" in msg and ("limit" in msg or "quota" in msg):
            return True
        if "plan limit" in msg or "quota exceeded" in msg:
            return True
        return False


def get_access_token(creds_json):
    creds = json.loads(creds_json) if isinstance(creds_json, str) else creds_json
    r = httpx.post(
        TOKEN_URL,
        data={
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
            "refresh_token": creds["refresh_token"],
            "grant_type": "refresh_token",
        },
        timeout=15,
    )
    r.raise_for_status()
    token = _json_or_empty(r).get("access_token", "")
    if not token:
        raise RuntimeError("Google OAuth token refresh returned empty body")
    return token


def extract_spreadsheet_id(url_or_id):
    if not url_or_id:
        return ""
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url_or_id)
    if m:
        return m.group(1)
    if "/" not in url_or_id:
        return url_or_id
    return ""


def sheets_read(token, sid, range_str):
    r = httpx.get(
        f"{SHEETS_API}/{sid}/values/{range_str}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    r.raise_for_status()
    return _json_or_empty(r).get("values", [])


def sheets_update_row(token, sid, sheet_name, row_idx_1based, values):
    range_str = f"{sheet_name}!A{row_idx_1based}:S{row_idx_1based}"
    r = httpx.put(
        f"{SHEETS_API}/{sid}/values/{range_str}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        params={"valueInputOption": "RAW"},
        json={"range": range_str, "majorDimension": "ROWS", "values": [values]},
        timeout=30,
    )
    r.raise_for_status()
    return _json_or_empty(r)


def brevo_create_contact(api_key, email, first_name, last_name, attrs):
    headers = {"api-key": api_key, "Content-Type": "application/json"}
    contact_attrs = {"FIRSTNAME": first_name or "", "LASTNAME": last_name or ""}
    for k, v in (attrs or {}).items():
        if v:
            contact_attrs[k.upper()] = v
    payload = {"email": email, "attributes": contact_attrs, "updateEnabled": True}
    r = httpx.post(f"{BREVO_BASE}/contacts", headers=headers, json=payload, timeout=30)
    if r.status_code >= 400:
        raise BrevoError(r.status_code, r.text)


def brevo_send(api_key, sender_email, sender_name, to_email, to_name, subject, body, tags):
    headers = {"api-key": api_key, "Content-Type": "application/json"}
    to = {"email": to_email}
    if to_name:
        to["name"] = to_name
    payload = {
        "sender": {"email": sender_email, "name": sender_name or "Oya.ai"},
        "to": [to],
        "subject": subject,
        "textContent": body,
        "tags": [t.strip() for t in (tags or "").split(",") if t.strip()],
    }
    r = httpx.post(f"{BREVO_BASE}/smtp/email", headers=headers, json=payload, timeout=30)
    if r.status_code >= 400:
        raise BrevoError(r.status_code, r.text)
    return _json_or_empty(r).get("messageId", "")


def _extract_li_public_identifier(linkedin_url):
    """Pull the public_identifier slug from a LinkedIn profile URL.
    `https://www.linkedin.com/in/jane-doe/` → `jane-doe`.
    Returns "" when the URL doesn't match `/in/<slug>`."""
    if not linkedin_url:
        return ""
    m = re.search(r"linkedin\.com/in/([a-zA-Z0-9_\-%\.]+?)/?(?:[?#]|$)", linkedin_url.strip())
    return m.group(1) if m else ""


def unipile_send_invitation(dsn, api_key, account_id, linkedin_url, message):
    """Send a LinkedIn connection request via Unipile. Mirrors the flow used
    by the `linkedin-api` skill's `do_send_connection`: first GET
    `users/<public_identifier>` to resolve provider_id, then POST `users/invite`
    with the resolved provider_id + message.

    Returns a dict:
      {ok: bool, invitation_id: str, is_rate_limited: bool, error: str}

    is_rate_limited == True means LinkedIn / Unipile signalled cap-hit
    (HTTP 429, weekly-limit error, account-restricted) — caller should stop
    LI sends for the rest of the batch.
    """
    out = {"ok": False, "invitation_id": "", "is_rate_limited": False, "error": ""}
    if not (dsn and api_key and account_id):
        out["error"] = "missing-unipile-creds"
        return out
    public_identifier = _extract_li_public_identifier(linkedin_url)
    if not public_identifier:
        out["error"] = f"could not parse public_identifier from {linkedin_url!r}"
        return out

    base = dsn.rstrip("/")
    if not base.startswith(("http://", "https://")):
        base = "https://" + base
    headers = {"X-API-KEY": api_key, "Content-Type": "application/json", "Accept": "application/json"}
    params = {"account_id": account_id}

    try:
        with httpx.Client(timeout=20) as c:
            # Step 1: resolve provider_id
            r1 = c.get(f"{base}/api/v1/users/{public_identifier}", headers=headers, params=params)
            if r1.status_code == 429:
                out["is_rate_limited"] = True
                out["error"] = "unipile 429 on user-resolve"
                return out
            if r1.status_code >= 400:
                out["error"] = f"user-resolve {r1.status_code}: {r1.text[:200]}"
                return out
            user_data = _json_or_empty(r1)
            provider_id = (user_data.get("provider_id") or "").strip()
            if not provider_id:
                out["error"] = f"no provider_id in resolved user payload (keys: {list(user_data.keys())})"
                return out

            # Step 2: send the invitation
            body = {"account_id": account_id, "provider_id": provider_id}
            if message and message.strip():
                body["message"] = message.strip()[:300]
            r2 = c.post(f"{base}/api/v1/users/invite", headers=headers, json=body, params=params)
            if r2.status_code == 429:
                out["is_rate_limited"] = True
                out["error"] = "unipile 429 on invite"
                return out
            if r2.status_code >= 400:
                # Inspect body for cap-style codes
                err_text = r2.text or ""
                lower = err_text.lower()
                if any(kw in lower for kw in (
                    "weekly_limit", "weekly limit", "invitation_limit",
                    "rate_limit", "rate limit", "account_restricted",
                    "account restricted", "too_many_invites",
                )):
                    out["is_rate_limited"] = True
                out["error"] = f"invite {r2.status_code}: {err_text[:200]}"
                return out
            data = _json_or_empty(r2)
            out["ok"] = True
            out["invitation_id"] = (data.get("invitation_id") or data.get("id") or "").strip()
            return out
    except httpx.HTTPError as e:
        out["error"] = f"unipile-http-error: {str(e)[:160]}"
        return out


def _slack_sheet_link(sheet_url):
    """Format the sheet URL as a Slack-flavored hyperlink so channel summaries
    render a clickable 'Leads sheet' label instead of a raw
    https://docs.google.com/spreadsheets/... URL eating a whole line."""
    url = (sheet_url or "").strip()
    if not url:
        return ""
    return f"<{url}|Leads sheet>"


def _pad(row, n):
    return (row or []) + [""] * max(0, n - len(row or []))


_SHEETS_EPOCH = datetime(1899, 12, 30)


def _date_matches(cell, today_str):
    """True when the date cell represents today_str (YYYY-MM-DD).

    Tolerates legacy rows written before the RAW fix: USER_ENTERED had
    coerced YYYY-MM-DD into a date type, so on readback those cells come back
    as locale strings (M/D/YYYY) or — when read unformatted — as serials.
    """
    if cell is None or cell == "":
        return False
    s = str(cell).strip()
    if s == today_str:
        return True
    try:
        serial = float(s)
        if 25569 < serial < 100000:
            return (_SHEETS_EPOCH + timedelta(days=int(serial))).strftime("%Y-%m-%d") == today_str
    except (ValueError, TypeError):
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%m/%d/%Y", "%-m/%-d/%Y", "%d/%m/%Y", "%m/%d/%y", "%d/%m/%y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d") == today_str
        except ValueError:
            continue
    return False


def run(inp):
    sheet_url = (inp.get("sheet_url") or "").strip()
    try:
        batch_size = int(inp.get("batch_size") or 15)
    except (TypeError, ValueError):
        batch_size = 15
    today = (inp.get("today") or "").strip() or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if not sheet_url:
        return {"error": "sheet_url is required. Load 'Leads Sheet URL:' from agent memory and pass it in."}

    sid = extract_spreadsheet_id(sheet_url)
    if not sid:
        return {"error": f"Could not extract spreadsheet ID from sheet_url={sheet_url!r}"}

    brevo_key = os.environ.get("BREVO_API_KEY", "").strip()
    sender_email = os.environ.get("BREVO_SENDER_EMAIL", "").strip()
    sender_name = os.environ.get("BREVO_SENDER_NAME", "").strip() or "Oya.ai"
    gsheets_json = os.environ.get("GOOGLE_SHEETS_CREDENTIALS_JSON", "").strip()

    # Unipile (LinkedIn) creds — optional. When present, rows with a
    # linkedin_url are routed to LinkedIn FIRST until the daily LI cap is hit;
    # email is the fallback channel after that.
    unipile_dsn = os.environ.get("UNIPILE_DSN", "").strip()
    unipile_api = os.environ.get("UNIPILE_API_KEY", "").strip()
    unipile_acct = os.environ.get("UNIPILE_ACCOUNT_ID", "").strip()
    unipile_creds_present = bool(unipile_dsn and unipile_api and unipile_acct)

    missing = [
        k for k, v in (
            ("BREVO_API_KEY", brevo_key),
            ("BREVO_SENDER_EMAIL", sender_email),
            ("GOOGLE_SHEETS_CREDENTIALS_JSON", gsheets_json),
        ) if not v
    ]
    if missing:
        return {"error": f"Missing credentials: {', '.join(missing)}. Connect the Brevo and Google Sheets gateways."}

    try:
        token = get_access_token(gsheets_json)
    except Exception as e:
        return {"error": f"Google Sheets auth failed: {e}"}

    try:
        rows = sheets_read(token, sid, "Leads!A1:S")
    except Exception as e:
        return {"error": f"Sheet read failed: {e}"}

    now_iso = datetime.now(timezone.utc).isoformat()
    now_hm = datetime.now(timezone.utc).strftime("%H:%M")

    if not rows or len(rows) < 2:
        return {
            "sent": 0, "failed": 0, "remaining_queued": 0,
            "cumulative_sent_today": 0, "brevo_cap_hit": False, "milestone": None,
            "slack_line": (
                f"*Outbound batch* · queue empty at {now_hm} UTC (sheet has no data rows)\n"
                f"{_slack_sheet_link(sheet_url)}"
            ),
        }

    header = rows[0]
    try:
        col_idx = {c: header.index(c) for c in COLUMNS}
    except ValueError as e:
        return {"error": f"Sheet header missing expected column: {e}. Header was: {header!r}"}

    ncols = len(COLUMNS)

    # Build batch: iterate data rows, pick first N matching today+queued.
    # Sheet row numbers are 1-based; row 1 = header; first data row = row 2.
    batch = []
    all_data = []
    for i, raw in enumerate(rows[1:], start=2):
        padded = _pad(raw, ncols)
        all_data.append((i, padded))
        if _date_matches(padded[col_idx["date"]], today) and padded[col_idx["status"]] == "queued" and len(batch) < batch_size:
            batch.append((i, padded))

    # Count queued+sent BEFORE we start processing (for cumulative math)
    pre_sent_today = sum(
        1 for _, r in all_data
        if _date_matches(r[col_idx["date"]], today) and r[col_idx["status"]] == "sent"
    )

    # Seed today's LinkedIn-channel count from the sheet so the cap is
    # enforced across multiple batches in the same day, not just within one
    # batch run. Each completed batch increments this in-memory; the next
    # batch re-reads the sheet and recomputes from scratch.
    li_sent_today = sum(
        1 for _, r in all_data
        if _date_matches(r[col_idx["date"]], today)
        and r[col_idx["status"]] == "sent"
        and r[col_idx["channel"]] == "linkedin"
    )
    li_sent_this_batch = 0
    email_sent_this_batch = 0

    if not batch:
        remaining = sum(
            1 for _, r in all_data
            if _date_matches(r[col_idx["date"]], today) and r[col_idx["status"]] == "queued"
        )
        return {
            "sent": 0, "failed": 0, "remaining_queued": remaining,
            "cumulative_sent_today": pre_sent_today, "brevo_cap_hit": False, "milestone": None,
            "slack_line": (
                f"*Outbound batch* · queue empty at {now_hm} UTC\n"
                f"{_slack_sheet_link(sheet_url)}"
            ),
        }

    sent = 0
    failed = 0
    cap_hit = False

    for sheet_row_idx, row in batch:
        email = (row[col_idx["email"]] or "").strip()
        linkedin_url = (row[col_idx["linkedin_url"]] or "").strip()
        connection_note = (row[col_idx["connection_note"]] or "").strip()

        # === LinkedIn-first channel routing ===
        # Try LI when: prospect has a LinkedIn URL, today's LI cap hasn't been
        # reached, a connection note has been drafted (research-batch fills
        # this), and Unipile creds are connected. On rate-limit signal, force
        # the rest of the batch to email by capping li_sent_today.
        try_linkedin = (
            linkedin_url
            and connection_note
            and unipile_creds_present
            and li_sent_today < LI_DAILY_CAP
        )
        if try_linkedin:
            li_result = unipile_send_invitation(
                unipile_dsn, unipile_api, unipile_acct,
                linkedin_url, connection_note,
            )
            if li_result["ok"]:
                row[col_idx["status"]] = "sent"
                row[col_idx["channel"]] = "linkedin"
                row[col_idx["message_id"]] = li_result["invitation_id"]
                row[col_idx["sent_at"]] = now_iso
                try:
                    sheets_update_row(token, sid, "Leads", sheet_row_idx, row[:ncols])
                except Exception:
                    pass
                sent += 1
                li_sent_today += 1
                li_sent_this_batch += 1
                continue
            elif li_result["is_rate_limited"]:
                # LinkedIn signalled cap-hit (429 / weekly-limit / restricted).
                # Stop attempting LI for the rest of this batch — fall through
                # to the email path for this row, then email-only thereafter.
                li_sent_today = LI_DAILY_CAP
                # Note the LI fallback reason on the row for visibility.
                existing_skip = row[col_idx["skip_reason"]]
                row[col_idx["skip_reason"]] = (
                    f"li-fallback-rate-limited; {existing_skip}".rstrip("; ")
                )[:200]
            else:
                # Other LI error (parse failure, provider_id resolve failed,
                # 4xx that isn't rate-limit). Fall through to email for this
                # one row but keep trying LI for later rows in the batch.
                existing_skip = row[col_idx["skip_reason"]]
                row[col_idx["skip_reason"]] = (
                    f"li-fallback: {li_result['error'][:120]}; {existing_skip}".rstrip("; ")
                )[:200]

        # === Email channel (fallback when LI not feasible / failed) ===
        if not email:
            # No email AND we didn't / couldn't send via LI: nothing to do.
            # Mark send-failed so it doesn't retry forever and surface the reason.
            row[col_idx["status"]] = "send-failed"
            existing_skip = row[col_idx["skip_reason"]]
            row[col_idx["skip_reason"]] = (existing_skip or "no email and li unavailable")[:200]
            row[col_idx["sent_at"]] = now_iso
            try:
                sheets_update_row(token, sid, "Leads", sheet_row_idx, row[:ncols])
            except Exception:
                pass
            failed += 1
            continue

        attrs = {
            "COMPANY": row[col_idx["company"]],
            "ROLE": row[col_idx["title"]],
            "SIGNAL": row[col_idx["signal"]],
            "SOURCE": row[col_idx["source"]],
        }

        try:
            brevo_create_contact(brevo_key, email, row[col_idx["first_name"]], row[col_idx["last_name"]], attrs)
        except BrevoError as e:
            if e.is_cap():
                cap_hit = True
                break
            # Non-fatal: contact upsert failed but send may still work
        except Exception:
            pass

        try:
            msg_id = brevo_send(
                brevo_key, sender_email, sender_name,
                to_email=email, to_name=row[col_idx["name"]],
                subject=row[col_idx["email_subject"]],
                body=row[col_idx["email_body"]],
                tags=f"sdr,touch-1,{today}",
            )
        except BrevoError as e:
            if e.is_cap():
                cap_hit = True
                break
            row[col_idx["status"]] = "send-failed"
            row[col_idx["skip_reason"]] = str(e)[:200]
            row[col_idx["sent_at"]] = now_iso
            try:
                sheets_update_row(token, sid, "Leads", sheet_row_idx, row[:ncols])
            except Exception:
                pass
            failed += 1
            continue
        except Exception as e:
            row[col_idx["status"]] = "send-failed"
            row[col_idx["skip_reason"]] = str(e)[:200]
            row[col_idx["sent_at"]] = now_iso
            try:
                sheets_update_row(token, sid, "Leads", sheet_row_idx, row[:ncols])
            except Exception:
                pass
            failed += 1
            continue

        row[col_idx["status"]] = "sent"
        row[col_idx["channel"]] = "email"
        row[col_idx["message_id"]] = msg_id
        row[col_idx["sent_at"]] = now_iso
        try:
            sheets_update_row(token, sid, "Leads", sheet_row_idx, row[:ncols])
        except Exception:
            pass
        sent += 1
        email_sent_this_batch += 1

    # Recount post-batch. `all_data` holds shared references to `rows[1:]` and was
    # mutated in-place above, so status changes are reflected.
    remaining_queued = sum(
        1 for _, r in all_data
        if _date_matches(r[col_idx["date"]], today) and r[col_idx["status"]] == "queued"
    )
    cumulative_sent_today = sum(
        1 for _, r in all_data
        if _date_matches(r[col_idx["date"]], today) and r[col_idx["status"]] == "sent"
    )

    # Milestone detection: which 50/100/... line did this run cross?
    milestone = None
    for m in (300, 250, 200, 150, 100, 50):
        if pre_sent_today < m <= cumulative_sent_today:
            milestone = m
            break

    # Multi-line Slack format. Preserves the "Milestone N/300", "daily cap",
    # "N sent", "N still queued" substrings the test suite asserts on, while
    # reading as a structured update (heading, stats, link).
    milestone_line = f"🎯 *Milestone {milestone}/300 reached*\n" if milestone else ""

    # Per-channel split for this batch — visible in the Slack summary so the
    # operator sees how many went out via LinkedIn vs email.
    split_line = f"LI={li_sent_this_batch} · Email={email_sent_this_batch}"
    li_cap_note = ""
    if unipile_creds_present and li_sent_today >= LI_DAILY_CAP:
        li_cap_note = " · LI cap reached"

    if cap_hit:
        slack_line = (
            f"{milestone_line}"
            f"🛑 *Outbound — Brevo daily cap hit* · {cumulative_sent_today}/300 sent today\n"
            f"{sent} sent this batch ({split_line}{li_cap_note}) · holding remaining {remaining_queued} for tomorrow\n"
            f"{_slack_sheet_link(sheet_url)}"
        )
    else:
        slack_line = (
            f"{milestone_line}"
            f"*Outbound batch* · {sent} sent ({split_line}{li_cap_note}) · {failed} failed\n"
            f"{remaining_queued} still queued · {cumulative_sent_today}/300 sent today\n"
            f"{_slack_sheet_link(sheet_url)}"
        )

    return {
        "sent": sent,
        "li_sent": li_sent_this_batch,
        "email_sent": email_sent_this_batch,
        "li_sent_today": li_sent_today,
        "li_cap_reached": li_sent_today >= LI_DAILY_CAP,
        "failed": failed,
        "remaining_queued": remaining_queued,
        "cumulative_sent_today": cumulative_sent_today,
        "brevo_cap_hit": cap_hit,
        "milestone": milestone,
        "slack_line": slack_line,
    }


try:
    inp = json.loads(os.environ.get("INPUT_JSON") or "{}")
    result = run(inp)
except Exception as e:
    result = {
        "error": f"sdr-outbound-batch crashed: {e}",
        "trace": traceback.format_exc(),
    }

print(json.dumps(result))
