import os
import json
import httpx

BASE = "https://api.apollo.io/api/v1"


def _headers(key):
    return {
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
        "x-api-key": key,
    }


def api(key, method, path, body=None, params=None, timeout=15):
    with httpx.Client(timeout=timeout) as c:
        r = c.request(method, f"{BASE}/{path}", json=body, params=params, headers=_headers(key))
        if r.status_code >= 400:
            try:
                err_body = r.json()
            except Exception:
                err_body = r.text
            raise Exception(f"Apollo API {r.status_code} on {path}: {json.dumps(err_body) if isinstance(err_body, dict) else err_body}")
        return r.json()


def _split_csv(val):
    """Split comma-separated string into list, filtering empty values."""
    if not val or not isinstance(val, str):
        return []
    return [x.strip() for x in val.split(",") if x.strip()]


def _split_semi(val):
    """Split semicolon-separated employee range string. Each range is 'min,max' like '11,50'.
    Multiple ranges separated by semicolons: '1,10;11,50;51,200'.
    If no semicolon present, treat the whole value as a single range."""
    if not val or not isinstance(val, str):
        return []
    parts = [x.strip() for x in val.split(";") if x.strip()]
    # Validate each part looks like a range (contains comma with digits)
    return [p for p in parts if p]


def _int(val, default):
    """Safely convert to int with default."""
    try:
        v = int(val)
        return v if v >= 1 else default
    except (TypeError, ValueError):
        return default


def _format_person(p):
    result = {
        "id": p.get("id", ""),
        "name": f"{p.get('first_name', '')} {p.get('last_name', '')}".strip(),
        "title": p.get("title", ""),
        "linkedin_url": p.get("linkedin_url", ""),
        "company": p.get("organization", {}).get("name", "") if p.get("organization") else "",
        "company_domain": p.get("organization", {}).get("primary_domain", "") if p.get("organization") else "",
    }
    if p.get("email"):
        result["email"] = p["email"]
    if p.get("email_status"):
        result["email_status"] = p["email_status"]
    if p.get("phone_numbers"):
        result["phone"] = p["phone_numbers"][0].get("sanitized_number", "")
    if p.get("city"):
        result["city"] = p["city"]
    if p.get("state"):
        result["state"] = p["state"]
    if p.get("country"):
        result["country"] = p["country"]
    return result


def _format_search_person(p):
    """Format obfuscated person from api_search — includes id for enrichment."""
    return {
        "id": p.get("id", ""),
        "first_name": p.get("first_name", ""),
        "title": p.get("title", ""),
        "has_email": p.get("has_email", False),
        "has_direct_phone": p.get("has_direct_phone", ""),
        "company": p.get("organization", {}).get("name", "") if p.get("organization") else "",
    }


def _format_org(o):
    result = {
        "id": o.get("id", ""),
        "name": o.get("name", ""),
        "domain": (o.get("primary_domain") or o.get("website_url") or "").replace("http://www.", "").replace("https://www.", "").replace("http://", "").replace("https://", "").rstrip("/"),
        "website": o.get("website_url", ""),
        "linkedin_url": o.get("linkedin_url", ""),
        "twitter_url": o.get("twitter_url", ""),
        "facebook_url": o.get("facebook_url", ""),
        "phone": o.get("phone", ""),
        "founded_year": o.get("founded_year"),
    }
    # These fields are only present in enrichment responses, not in search
    if o.get("industry"):
        result["industry"] = o["industry"]
    if o.get("estimated_num_employees"):
        result["estimated_num_employees"] = o["estimated_num_employees"]
    if o.get("city"):
        result["city"] = o["city"]
    if o.get("state"):
        result["state"] = o["state"]
    if o.get("country"):
        result["country"] = o["country"]
    if o.get("short_description"):
        result["short_description"] = o["short_description"]
    if o.get("organization_revenue_printed"):
        result["revenue"] = o["organization_revenue_printed"]
    if o.get("keywords"):
        result["keywords"] = o["keywords"][:10]
    if o.get("current_technologies"):
        result["technologies"] = o["current_technologies"][:10]
    return result


def do_search_people(key, inp):
    body = {"page": _int(inp.get("page"), 1), "per_page": min(_int(inp.get("per_page"), 10), 25)}
    titles = _split_csv(inp.get("person_titles"))
    if titles:
        body["person_titles"] = titles
    locations = _split_csv(inp.get("person_locations"))
    if locations:
        body["person_locations"] = locations
    domains = _split_csv(inp.get("organization_domains"))
    if domains:
        body["q_organization_domains"] = "\n".join(domains)
    ranges = _split_semi(inp.get("organization_num_employees_ranges"))
    if ranges:
        body["organization_num_employees_ranges"] = ranges
    kw = _split_csv(inp.get("keywords"))
    if kw:
        body["q_organization_keyword_tags"] = kw

    data = api(key, "POST", "mixed_people/api_search", body)
    people = data.get("people", [])
    return {
        "people": [_format_search_person(p) for p in people],
        "total": data.get("pagination", {}).get("total_entries", data.get("total_entries", 0)),
        "page": data.get("pagination", {}).get("page", 1),
        "per_page": data.get("pagination", {}).get("per_page", 10),
        "note": "Results are previews. Use enrich_person with the person id to get full contact details (email, phone, LinkedIn).",
    }


def do_search_organizations(key, inp):
    body = {"page": _int(inp.get("page"), 1), "per_page": min(_int(inp.get("per_page"), 10), 25)}
    if inp.get("organization_name"):
        body["q_organization_name"] = inp["organization_name"]
    kw = _split_csv(inp.get("keywords"))
    if kw:
        body["q_organization_keyword_tags"] = kw
    locations = _split_csv(inp.get("locations"))
    if locations:
        body["organization_locations"] = locations
    ranges = _split_semi(inp.get("organization_num_employees_ranges"))
    if ranges:
        body["organization_num_employees_ranges"] = ranges

    data = api(key, "POST", "mixed_companies/search", body)
    orgs = data.get("organizations", [])
    return {
        "organizations": [_format_org(o) for o in orgs],
        "total": data.get("pagination", {}).get("total_entries", 0),
        "page": data.get("pagination", {}).get("page", 1),
    }


def do_enrich_person(key, inp):
    body = {}
    if inp.get("person_id"):
        body["id"] = inp["person_id"]
    if inp.get("email"):
        body["email"] = inp["email"]
    if inp.get("linkedin_url"):
        body["linkedin_url"] = inp["linkedin_url"]
    if inp.get("first_name"):
        body["first_name"] = inp["first_name"]
    if inp.get("last_name"):
        body["last_name"] = inp["last_name"]
    if inp.get("organization_name"):
        body["organization_name"] = inp["organization_name"]
    if inp.get("domain"):
        body["domain"] = inp["domain"]
    if not body:
        return {"error": "Provide person_id, email, linkedin_url, or first_name+last_name+organization_name"}

    try:
        data = api(key, "POST", "people/match", body)
    except Exception as e:
        msg = str(e)
        if "403" in msg:
            return {"error": "Person enrichment requires a Master API key. Go to Apollo Settings > API Keys and create a Master key (not restricted)."}
        return {"error": msg}
    person = data.get("person")
    if not person:
        return {"error": "Person not found", "detail": data.get("message", "")}
    return _format_person(person)


def do_enrich_organization(key, inp):
    domain = inp.get("domain", "")
    if not domain:
        return {"error": "Provide domain"}

    data = api(key, "GET", "organizations/enrich", params={"domain": domain})
    org = data.get("organization")
    if not org:
        return {"error": "Organization not found"}
    return _format_org(org)


try:
    key = os.environ["APOLLO_API_KEY"]
    inp = json.loads(os.environ.get("INPUT_JSON", "{}"))
    action = inp.get("action", "")

    if action == "search_people":
        result = do_search_people(key, inp)
    elif action == "search_organizations":
        result = do_search_organizations(key, inp)
    elif action == "enrich_person":
        result = do_enrich_person(key, inp)
    elif action == "enrich_organization":
        result = do_enrich_organization(key, inp)
    else:
        result = {"error": f"Unknown action: {action}. Available: search_people, search_organizations, enrich_person, enrich_organization"}

    print(json.dumps(result))

except Exception as e:
    print(json.dumps({"error": str(e)}))
