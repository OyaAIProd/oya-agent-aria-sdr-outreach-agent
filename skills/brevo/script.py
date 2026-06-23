import os
import json
import httpx
from datetime import datetime, timedelta, timezone

BASE = "https://api.brevo.com/v3"


def api(key, method, endpoint, params=None, payload=None, timeout=30):
    headers = {"api-key": key, "Content-Type": "application/json", "accept": "application/json"}
    with httpx.Client(timeout=timeout) as c:
        r = c.request(method, f"{BASE}/{endpoint}", headers=headers, params=params, json=payload)
        if r.status_code >= 400:
            try:
                body = r.json()
                msg = body.get("message") or body.get("code") or r.text[:500]
            except Exception:
                msg = r.text[:500]
            raise Exception(f"Brevo API {r.status_code}: {msg}")
        if r.status_code == 204 or not r.text:
            return {}
        return r.json()


def _int(val, default):
    try:
        v = int(val)
        return v if v >= 0 else default
    except (TypeError, ValueError):
        return default


def do_send_email(key, inp):
    to_email = (inp.get("to_email") or "").strip()
    subject = inp.get("subject") or ""
    html_body = inp.get("html_body") or ""
    text_body = inp.get("text_body") or ""

    if not to_email:
        return {"error": "Provide to_email"}
    if not subject:
        return {"error": "Provide subject"}
    if not html_body and not text_body:
        return {"error": "Provide html_body or text_body"}

    sender_email = (os.environ.get("BREVO_SENDER_EMAIL") or "").strip()
    sender_name = (os.environ.get("BREVO_SENDER_NAME") or "").strip() or "Oya.ai"
    if not sender_email:
        return {"error": "BREVO_SENDER_EMAIL not set. Verify a sender email in Brevo and set the env var."}

    to_obj = {"email": to_email}
    to_name = (inp.get("to_name") or "").strip()
    if to_name:
        to_obj["name"] = to_name

    payload = {
        "sender": {"email": sender_email, "name": sender_name},
        "to": [to_obj],
        "subject": subject,
    }
    if html_body:
        payload["htmlContent"] = html_body
    if text_body:
        payload["textContent"] = text_body

    reply_to = (inp.get("reply_to") or "").strip()
    if reply_to:
        payload["replyTo"] = {"email": reply_to}

    tags = [t.strip() for t in (inp.get("tags") or "").split(",") if t.strip()]
    if tags:
        payload["tags"] = tags

    data = api(key, "POST", "smtp/email", payload=payload)
    return {
        "message_id": data.get("messageId", ""),
        "status": "sent",
        "to": to_email,
        "subject": subject,
    }


def do_get_email_events(key, inp):
    params = {"limit": min(_int(inp.get("limit"), 100), 2500)}
    days = _int(inp.get("days"), 7) or 7
    now = datetime.now(timezone.utc)
    params["startDate"] = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    params["endDate"] = now.strftime("%Y-%m-%d")

    to_email = (inp.get("to_email") or "").strip()
    if to_email:
        params["email"] = to_email
    event_type = (inp.get("event_type") or "").strip()
    if event_type:
        params["event"] = event_type

    data = api(key, "GET", "smtp/statistics/events", params=params)
    events = data.get("events", [])
    formatted = [{
        "date": e.get("date", ""),
        "email": e.get("email", ""),
        "event": e.get("event", ""),
        "subject": e.get("subject", ""),
        "message_id": e.get("messageId", ""),
        "tags": e.get("tags", []),
    } for e in events]
    return {"events": formatted, "total": len(formatted)}


def do_create_contact(key, inp):
    email = (inp.get("email") or "").strip()
    if not email:
        return {"error": "Provide email"}

    payload = {"email": email, "updateEnabled": True}
    attrs = {}
    first_name = (inp.get("first_name") or "").strip()
    last_name = (inp.get("last_name") or "").strip()
    if first_name:
        attrs["FIRSTNAME"] = first_name
    if last_name:
        attrs["LASTNAME"] = last_name

    attrs_json = inp.get("attributes_json") or ""
    if attrs_json:
        try:
            extra = json.loads(attrs_json)
            if not isinstance(extra, dict):
                return {"error": "attributes_json must be a JSON object"}
            attrs.update(extra)
        except json.JSONDecodeError as e:
            return {"error": f"attributes_json invalid JSON: {e}"}

    if attrs:
        payload["attributes"] = attrs

    data = api(key, "POST", "contacts", payload=payload)
    return {
        "email": email,
        "contact_id": data.get("id", 0),
        "status": "created_or_updated",
    }


def do_add_contact_to_list(key, inp):
    email = (inp.get("email") or "").strip()
    list_id = _int(inp.get("list_id"), 0)
    if not email:
        return {"error": "Provide email"}
    if not list_id:
        return {"error": "Provide list_id (use list_contact_lists to find IDs)"}

    data = api(key, "POST", f"contacts/lists/{list_id}/contacts/add", payload={"emails": [email]})
    return {
        "email": email,
        "list_id": list_id,
        "contacts_added": data.get("contacts", {}).get("success", []),
        "contacts_failed": data.get("contacts", {}).get("failure", []),
        "status": "added",
    }


def do_list_contact_lists(key, inp):
    params = {"limit": min(_int(inp.get("limit"), 50), 50)}
    data = api(key, "GET", "contacts/lists", params=params)
    lists = data.get("lists", [])
    formatted = [{
        "id": lst.get("id", 0),
        "name": lst.get("name", ""),
        "total_subscribers": lst.get("totalSubscribers", 0),
        "total_blacklisted": lst.get("totalBlacklisted", 0),
    } for lst in lists]
    return {"lists": formatted, "total": data.get("count", len(formatted))}


try:
    key = os.environ["BREVO_API_KEY"]
    inp = json.loads(os.environ.get("INPUT_JSON", "{}"))
    action = inp.get("action", "")

    if action == "send_email":
        result = do_send_email(key, inp)
    elif action == "get_email_events":
        result = do_get_email_events(key, inp)
    elif action == "create_contact":
        result = do_create_contact(key, inp)
    elif action == "add_contact_to_list":
        result = do_add_contact_to_list(key, inp)
    elif action == "list_contact_lists":
        result = do_list_contact_lists(key, inp)
    else:
        result = {"error": f"Unknown action: {action}. Available: send_email, get_email_events, create_contact, add_contact_to_list, list_contact_lists"}

    print(json.dumps(result))

except Exception as e:
    print(json.dumps({"error": str(e)}))
