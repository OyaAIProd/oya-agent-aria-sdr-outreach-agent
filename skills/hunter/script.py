import os
import json
import httpx

BASE = "https://api.hunter.io/v2"


def api(key, endpoint, params=None, timeout=15):
    params = params or {}
    params["api_key"] = key
    with httpx.Client(timeout=timeout) as c:
        r = c.get(f"{BASE}/{endpoint}", params=params)
        if r.status_code >= 400:
            try:
                err_body = r.json()
                errors = err_body.get("errors", [])
                if errors:
                    raise Exception(f"Hunter API {r.status_code}: {'; '.join(e.get('details', str(e)) for e in errors)}")
            except Exception as e:
                if "Hunter API" in str(e):
                    raise
                pass
            raise Exception(f"Hunter API {r.status_code}: {r.text[:500]}")
        return r.json()


def _split_csv(val):
    if not val or not isinstance(val, str):
        return []
    return [x.strip() for x in val.split(",") if x.strip()]


def _int(val, default):
    try:
        v = int(val)
        return v if v >= 0 else default
    except (TypeError, ValueError):
        return default


def _format_email(e):
    result = {
        "email": e.get("value", ""),
        "type": e.get("type", ""),
        "confidence": e.get("confidence", 0),
    }
    if e.get("first_name"):
        result["first_name"] = e["first_name"]
    if e.get("last_name"):
        result["last_name"] = e["last_name"]
    if e.get("position"):
        result["position"] = e["position"]
    if e.get("seniority"):
        result["seniority"] = e["seniority"]
    if e.get("department"):
        result["department"] = e["department"]
    if e.get("linkedin"):
        result["linkedin_url"] = e["linkedin"]
    if e.get("phone_number"):
        result["phone"] = e["phone_number"]
    v = e.get("verification", {})
    if v and v.get("status"):
        result["verified"] = v["status"]
    return result


def do_domain_search(key, inp):
    domain = inp.get("domain", "")
    if not domain:
        return {"error": "Provide domain (e.g. 'stripe.com')"}

    params = {
        "domain": domain,
        "limit": min(_int(inp.get("limit"), 10), 100),
        "offset": _int(inp.get("offset"), 0),
    }
    seniority = _split_csv(inp.get("seniority"))
    if seniority:
        params["seniority"] = ",".join(seniority)
    department = _split_csv(inp.get("department"))
    if department:
        params["department"] = ",".join(department)

    data = api(key, "domain-search", params)
    d = data.get("data", {})
    emails = d.get("emails", [])
    meta = data.get("meta", {})

    result = {
        "domain": d.get("domain", domain),
        "organization": d.get("organization", ""),
        "emails": [_format_email(e) for e in emails],
        "total": meta.get("results", len(emails)),
    }
    if d.get("pattern"):
        result["email_pattern"] = d["pattern"]
    return result


def do_email_finder(key, inp):
    domain = inp.get("domain", "")
    first_name = inp.get("first_name", "")
    last_name = inp.get("last_name", "")

    if not domain:
        return {"error": "Provide domain"}
    if not first_name or not last_name:
        return {"error": "Provide first_name and last_name"}

    params = {
        "domain": domain,
        "first_name": first_name,
        "last_name": last_name,
    }

    data = api(key, "email-finder", params)
    d = data.get("data", {})

    result = {
        "email": d.get("email", ""),
        "score": d.get("score", 0),
        "domain": d.get("domain", domain),
    }
    if d.get("first_name"):
        result["first_name"] = d["first_name"]
    if d.get("last_name"):
        result["last_name"] = d["last_name"]
    if d.get("position"):
        result["position"] = d["position"]
    if d.get("linkedin_url"):
        result["linkedin_url"] = d["linkedin_url"]
    if d.get("phone_number"):
        result["phone"] = d["phone_number"]
    if d.get("company"):
        result["company"] = d["company"]
    v = d.get("verification", {})
    if v and v.get("status"):
        result["verified"] = v["status"]
    return result


def do_email_verifier(key, inp):
    email = inp.get("email", "")
    if not email:
        return {"error": "Provide email"}

    data = api(key, "email-verifier", {"email": email})
    d = data.get("data", {})

    return {
        "email": email,
        "status": d.get("status", ""),
        "result": d.get("result", ""),
        "score": d.get("score", 0),
        "mx_records": d.get("mx_records", False),
        "smtp_check": d.get("smtp_check", False),
        "accept_all": d.get("accept_all", False),
        "disposable": d.get("disposable", False),
        "webmail": d.get("webmail", False),
    }


def do_email_count(key, inp):
    domain = inp.get("domain", "")
    if not domain:
        return {"error": "Provide domain"}

    data = api(key, "email-count", {"domain": domain})
    d = data.get("data", {})

    result = {
        "domain": domain,
        "total": d.get("total", 0),
        "personal_emails": d.get("personal_emails", 0),
        "generic_emails": d.get("generic_emails", 0),
    }
    dept = d.get("department", {})
    if dept:
        result["by_department"] = {k: v for k, v in dept.items() if v > 0}
    sen = d.get("seniority", {})
    if sen:
        result["by_seniority"] = {k: v for k, v in sen.items() if v > 0}
    return result


try:
    key = os.environ["HUNTER_API_KEY"]
    inp = json.loads(os.environ.get("INPUT_JSON", "{}"))
    action = inp.get("action", "")

    if action == "domain_search":
        result = do_domain_search(key, inp)
    elif action == "email_finder":
        result = do_email_finder(key, inp)
    elif action == "email_verifier":
        result = do_email_verifier(key, inp)
    elif action == "email_count":
        result = do_email_count(key, inp)
    else:
        result = {"error": f"Unknown action: {action}. Available: domain_search, email_finder, email_verifier, email_count"}

    print(json.dumps(result))

except Exception as e:
    print(json.dumps({"error": str(e)}))
