import os, json, base64, time, httpx, re
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
try:
    from google.oauth2 import credentials, service_account
    from google.auth.transport.requests import Request as AuthRequest
except ImportError:
    print(json.dumps({"error": "google-auth not installed. pip install google-auth"}))
    raise SystemExit(1)


def _wrap_html(html: str) -> str:
    """Wrap an HTML fragment in a clean, minimal, email-client-safe document so
    emails look good by default. If the model already provided a full document
    (<html>/<!doctype>), pass it through untouched."""
    if re.search(r"<!doctype|<html", html, re.IGNORECASE):
        return html
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "</head>"
        '<body style="margin:0;padding:0;background:#f4f5f7;">'
        '<div style="max-width:600px;margin:0 auto;padding:24px;">'
        '<div style="background:#ffffff;border-radius:12px;padding:28px 32px;'
        'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,Helvetica,Arial,sans-serif;'
        'font-size:15px;line-height:1.6;color:#1a1a1a;">'
        f"{html}"
        "</div></div></body></html>"
    )


def _strip_tags(html: str) -> str:
    """Crude HTML→text fallback for the multipart plain-text part."""
    text = re.sub(r"<(br|/p|/div|/h[1-6]|/li)\s*/?>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


try:
    inp = json.loads(os.environ.get("INPUT_JSON", "{}"))
    creds_json = json.loads(os.environ["GMAIL_CREDENTIALS_JSON"])
    user_email = os.environ.get("GMAIL_USER_EMAIL", "")
    if creds_json.get("type") == "authorized_user":
        creds = credentials.Credentials.from_authorized_user_info(
            creds_json, scopes=["https://www.googleapis.com/auth/gmail.send"]
        )
    else:
        creds = service_account.Credentials.from_service_account_info(
            creds_json, scopes=["https://www.googleapis.com/auth/gmail.send"], subject=user_email
        )
    creds.refresh(AuthRequest())
    requested_to = inp["to"]
    requested_subject = inp["subject"]
    html_body = (inp.get("html") or "").strip()
    text_body = inp.get("body") or ""

    if html_body:
        # multipart/alternative: text part (fallback) + styled HTML part. Good clients
        # render the HTML; text-only clients still get a readable message.
        msg = MIMEMultipart("alternative")
        plain = text_body or _strip_tags(html_body)
        msg.attach(MIMEText(plain, "plain", "utf-8"))
        msg.attach(MIMEText(_wrap_html(html_body), "html", "utf-8"))
    else:
        msg = MIMEText(text_body, "plain", "utf-8")
    msg["to"] = requested_to
    msg["subject"] = requested_subject
    msg["from"] = user_email
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    response_json = None
    for _attempt in range(4):
        with httpx.Client(timeout=15) as c:
            r = c.post(f"https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
                headers={"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"},
                json={"raw": raw})
        if r.status_code == 429 and _attempt < 3:
            time.sleep(min(2 ** _attempt, 30))
            continue
        r.raise_for_status()
        response_json = r.json()
        break

    # Auto-verify against the send response itself. Gmail's API returns the
    # full Message resource on success, including `labelIds` — if SENT is in
    # there, the message landed in the user's Sent folder. The wrapper LLM
    # cannot claim "sent" if verified is false.
    message_id = (response_json or {}).get("id", "")
    thread_id = (response_json or {}).get("threadId", "")
    label_ids = (response_json or {}).get("labelIds", []) or []
    sent_label_present = "SENT" in label_ids
    mismatches = []
    if not message_id:
        mismatches.append("no message_id returned from Gmail API")
    if not sent_label_present:
        mismatches.append(f"SENT label missing from response (got: {label_ids})")
    out = {
        "ok": bool(message_id) and sent_label_present,
        "verified": bool(message_id) and sent_label_present,
        "message_id": message_id,
        "thread_id": thread_id,
        "format": "html" if html_body else "plain",
        "verification": {
            "label_ids": label_ids,
            "sent_label_present": sent_label_present,
            "requested_to": requested_to,
            "requested_subject": requested_subject,
            "from": user_email,
            "mismatch": "; ".join(mismatches) if mismatches else "",
        },
    }
    print(json.dumps(out))
except Exception as e:
    print(json.dumps({"error": str(e)}))
