"""SDR Discover — deterministic multi-source candidate discovery.

Paginates Instantly SuperSearch, Apollo (verified-email-only), and LinkedIn
(via Unipile). Deduplicates against the existing sheet. Batch-appends per
source so counts reported to Slack always match what landed in storage.

Instantly SuperSearch is queried first — preview is free (no credit cost) and
returns name + title + company + LinkedIn URL but no email. The downstream
research-batch cascade resolves emails via Hunter / Findymail / web search.
Apollo runs second with `person_email_status: ["verified"]` so only candidates
Apollo has a verified email for are returned. LinkedIn (Unipile) runs third —
LI rows never have an email; outbound routes them to LinkedIn connection
requests when the daily LI cap allows, otherwise falls back to email via the
cascade.
"""
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

import httpx

INSTANTLY_BASE = "https://api.instantly.ai/api/v2"
APOLLO_BASE = "https://api.apollo.io/api/v1"
SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
TOKEN_URL = "https://oauth2.googleapis.com/token"

COLUMNS = [
    "date", "name", "first_name", "last_name", "email", "company", "title",
    "linkedin_url", "signal", "source", "hook", "email_subject", "email_body",
    "status", "message_id", "skip_reason", "sent_at", "channel", "connection_note",
]

SHEET_TITLE = "Oya AI SDR — Leads"
SHEET_TAB = "Leads"

# Title seniority slices to produce varied Apollo searches from a single ICP string.
APOLLO_SENIORITY_SLICES = [
    "VP", "Head of", "Director", "Chief", "Founder", "CEO", "CTO", "CPO", "CRO",
]


# ---------------------------------------------------------------------------
# Google Sheets
# ---------------------------------------------------------------------------


def _slack_sheet_link(sheet_url):
    """Format the sheet URL as a Slack-flavored hyperlink so channel summaries
    render a clickable 'Leads sheet' label instead of a raw
    https://docs.google.com/spreadsheets/... URL eating a whole line."""
    url = (sheet_url or "").strip()
    if not url:
        return ""
    return f"<{url}|Leads sheet>"


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
    return r.json()["access_token"]


def extract_spreadsheet_id(url_or_id):
    if not url_or_id:
        return ""
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url_or_id)
    if m:
        return m.group(1)
    if "/" not in url_or_id:
        return url_or_id
    return ""


def sheets_create(token, title, sheet_name):
    body = {"properties": {"title": title}, "sheets": [{"properties": {"title": sheet_name}}]}
    r = httpx.post(
        SHEETS_API,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=body, timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    return data["spreadsheetId"], data.get("spreadsheetUrl", "")


def sheets_read(token, sid, range_str):
    r = httpx.get(
        f"{SHEETS_API}/{sid}/values/{range_str}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("values", [])


def sheets_append(token, sid, sheet_name, values):
    """Batch-append multiple rows in one request. Returns number of rows committed.

    The range is pinned to columns A:S (the 19-column schema) so Sheets'
    table-detection heuristic doesn't drift the starting column based on the
    previous row's rightmost-filled cell. Without the explicit column range,
    rows with trailing empties (e.g. an empty `connection_note`) caused each
    successive append to land one block further right than the last.
    """
    if not values:
        return 0
    range_a1 = f"{sheet_name}!A:S"
    r = httpx.post(
        f"{SHEETS_API}/{sid}/values/{range_a1}:append",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
        json={"range": range_a1, "majorDimension": "ROWS", "values": values},
        timeout=60,
    )
    r.raise_for_status()
    return (r.json() or {}).get("updates", {}).get("updatedRows", 0)


# ---------------------------------------------------------------------------
# Candidate normalization
# ---------------------------------------------------------------------------


def _norm_name_company(name, company):
    return (re.sub(r"[^a-z0-9]", "", (name or "").lower())
            + "_" + re.sub(r"[^a-z0-9]", "", (company or "").lower()))


def _split_name(full_name):
    parts = (full_name or "").strip().split()
    if len(parts) == 0:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _candidate(name="", first_name="", last_name="", email="", company="", title="",
               linkedin_url="", source=""):
    if not first_name and name:
        first_name, last_name_from_split = _split_name(name)
        if not last_name:
            last_name = last_name_from_split
    if not name and (first_name or last_name):
        name = f"{first_name} {last_name}".strip()
    return {
        "name": name, "first_name": first_name, "last_name": last_name,
        "email": (email or "").strip(), "company": company, "title": title,
        "linkedin_url": linkedin_url, "source": source,
    }


# ---------------------------------------------------------------------------
# Instantly SuperSearch — preview-only (no email enrichment, no credit cost).
# Emails come from the downstream cascade (Hunter / Findymail / web search).
# ---------------------------------------------------------------------------

# Map seniority words found in the ICP free-text to Instantly's `level` enum.
# Preview filter accepts an array of these strings under search_filters.level.
_INSTANTLY_LEVEL_MAP = {
    "vp": ["VP-Level"],
    "head of": ["Director-Level", "VP-Level"],
    "director": ["Director-Level", "Director"],
    "chief": ["C-Level"],
    "founder": ["C-Level", "Owner"],
    "ceo": ["C-Level"],
    "cto": ["C-Level"],
    "cpo": ["C-Level"],
    "cro": ["C-Level"],
    "owner": ["Owner"],
    "manager": ["Manager-Level", "Manager"],
}


def _title_variants(role_label):
    """Build a `title.include` array from a role_label like "LinkedIn ghostwriters".
    Returns ["LinkedIn ghostwriters", "LinkedIn ghostwriter"] so Instantly's title
    filter matches both plural and singular forms. Empty role_label → empty list."""
    rl = (role_label or "").strip()
    if not rl:
        return []
    variants = [rl]
    if rl.endswith("s") and len(rl) > 1 and rl[:-1] not in variants:
        variants.append(rl[:-1])
    return variants


def instantly_discover(api_key, icp, target=200, role_label=""):
    """Returns (candidates, pulled, status). Calls Instantly SuperSearch preview.

    When `role_label` is provided (recommended for niche ICPs like "LinkedIn
    ghostwriters"), uses it as a strict `title.include` filter — Instantly's
    `keyword_filter` is too loose for niche roles and matches generic strings
    like "1-10 employees" / "Payroll Manager", returning irrelevant solo
    operators. With `title.include` set, only people whose job title actually
    contains the role label come back.

    Without `role_label`, falls back to `keyword_filter.include = icp` (loose
    free-text match, used by AI SDR / generic SDR templates where ICP is broad).

    Preview endpoint doesn't paginate, so multi-call slicing by `level` is the
    only way past its single-call cap.
    """
    if not api_key:
        return [], 0, "skipped: no-instantly-key"
    if not icp:
        return [], 0, "skipped: no-icp"

    found = []
    seen = set()
    pulled = 0

    icp_lower = icp.lower()
    detected_levels = []
    for word, levels in _INSTANTLY_LEVEL_MAP.items():
        if word in icp_lower:
            for lev in levels:
                if lev not in detected_levels:
                    detected_levels.append(lev)

    title_filter = _title_variants(role_label)

    # Build query base: title.include when role_label is set (strict; drops
    # keyword_filter so loose tokenization can't pull in junk rows), otherwise
    # keyword_filter.include with the full ICP (loose match for broad ICPs).
    if title_filter:
        base_filter = {"title": {"include": title_filter}}
    else:
        base_filter = {"keyword_filter": {"include": icp}}

    queries = [dict(base_filter)]
    for lev in detected_levels[:4]:
        q = dict(base_filter)
        q["level"] = [lev]
        queries.append(q)

    for sf in queries:
        if len(found) >= target:
            break
        body = {
            "search_filters": sf,
            "limit": min(max(target - len(found), 25), 500),
            "skip_owned_leads": True,
        }
        try:
            r = httpx.post(
                f"{INSTANTLY_BASE}/supersearch-enrichment/preview-leads-from-supersearch",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=body, timeout=30,
            )
            if r.status_code == 402:
                return found, pulled, "failure: instantly 402 (no active paid plan / supersearch credits)"
            if r.status_code == 429:
                return found, pulled, "failure: instantly 429 (rate-limited)"
            if r.status_code >= 400:
                return found, pulled, f"failure: instantly {r.status_code}"
            data = r.json() or {}
        except Exception as e:
            return found, pulled, f"failure: {str(e)[:120]}"

        leads = data.get("leads") or []
        if not leads:
            continue

        for l in leads:
            pulled += 1
            first = (l.get("firstName") or "").strip()
            last = (l.get("lastName") or "").strip()
            full_name = (l.get("fullName") or f"{first} {last}").strip()
            if not full_name:
                continue
            company = (l.get("companyName") or "").strip()
            li = (l.get("linkedIn") or "").strip()
            if li and not li.startswith(("http://", "https://")):
                li = "https://" + li
            dedup_key = _norm_name_company(full_name, company)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            found.append(_candidate(
                name=full_name, first_name=first, last_name=last,
                company=company, title=(l.get("jobTitle") or ""),
                linkedin_url=li, source="instantly",
            ))

    return found, pulled, "ok"


# ---------------------------------------------------------------------------
# Apollo — paginated search_people
# ---------------------------------------------------------------------------


def apollo_enrich_match(api_key, person_id, timeout=15):
    """Enrich a single Apollo search hit via /people/match. On a paid Apollo
    plan, this unlocks the actual email + linkedin_url + de-obfuscated last
    name (search returns obfuscated previews; match unlocks the real data and
    burns 1 unlock credit per call). Returns the enriched person dict, or None
    on any failure / if the response shape is unexpected.
    """
    if not api_key or not person_id:
        return None
    try:
        r = httpx.post(
            f"{APOLLO_BASE}/people/match",
            headers={"Cache-Control": "no-cache", "Content-Type": "application/json", "x-api-key": api_key},
            json={"id": person_id},
            timeout=timeout,
        )
        if r.status_code >= 400:
            return None
        body = r.json() or {}
        return body.get("person") or None
    except Exception:
        return None


def apollo_discover(api_key, icp, target=200, role_label="", enrich=True):
    """Returns (candidates, pulled, status). Runs multiple paginated searches with
    title-slice variations extracted from the ICP to beat per-query caps.

    When `role_label` is provided, prepends it (and its singular form) to the
    title slices — critical for niche ICPs like "LinkedIn ghostwriters" where
    the standard seniority ladder (VP / Director / Founder) returns wrong roles.

    When `enrich=True` (default), each search hit is unlocked via /people/match
    so emails + LinkedIn URLs are populated directly on paid Apollo accounts.
    Rows whose enrichment returns no email are dropped (the verified-email
    search filter already pre-selects people Apollo has emails for, so this
    rarely fires). With enrich=False the function returns the raw obfuscated
    search shape (used in tests / when the caller wants to enrich elsewhere).
    """
    if not api_key:
        return [], 0, "skipped: no-apollo-key"
    if not icp:
        return [], 0, "skipped: no-icp"

    found = []
    seen = set()
    pulled = 0

    # Derive title queries — when role_label is set (e.g. "LinkedIn ghostwriters"),
    # use it FIRST so we search for the actual role. Add seniority slices that
    # appear verbatim in the ICP. Fallback to the standard seniority ladder.
    icp_lower = icp.lower()
    icp_titles = list(_title_variants(role_label))
    for slice_ in APOLLO_SENIORITY_SLICES:
        if slice_.lower() in icp_lower and slice_ not in icp_titles:
            icp_titles.append(slice_)
    if not icp_titles:
        icp_titles = ["VP", "Head of", "Director", "Founder", "CTO"]

    for title_kw in icp_titles:
        if len(found) >= target:
            break
        for page in range(1, 6):  # up to 5 pages per title slice → 125 per slice
            if len(found) >= target:
                break
            # Apollo's `mixed_people/api_search` accepts q_keywords as a free-text
            # filter applied to the person + org. WITHOUT it, searching by title
            # When role_label is set (e.g. "Founder", "LinkedIn ghostwriters")
            # the title filter IS the ICP filter — drop q_keywords because
            # Apollo treats it as strict-phrase-match, and a long ICP string
            # like "Founders of B2B SaaS startups, 1-50 employees" matches 0
            # Apollo records (live-tested). When role_label is empty, fall
            # back to the original behavior: q_keywords disambiguates broad
            # title slices (otherwise person_titles=["VP"] returns random VPs
            # at fertilizer companies).
            body = {
                "page": page,
                "per_page": 25,
                "person_titles": [title_kw],
                # API-level email-only filter: Apollo returns ONLY candidates
                # whose email is verified in their database.
                "person_email_status": ["verified"],
            }
            if not role_label:
                body["q_keywords"] = icp
            try:
                r = httpx.post(
                    f"{APOLLO_BASE}/mixed_people/api_search",
                    headers={"Cache-Control": "no-cache", "Content-Type": "application/json", "x-api-key": api_key},
                    json=body, timeout=30,
                )
                if r.status_code >= 400:
                    return found, pulled, f"failure: apollo {r.status_code}"
                data = r.json() or {}
            except Exception as e:
                return found, pulled, f"failure: {str(e)[:120]}"

            people = data.get("people") or []
            if not people:
                break  # end of this slice

            for p in people:
                pulled += 1
                # Search returns OBFUSCATED previews on every Apollo plan
                # (last_name='', last_name_obfuscated='Sm***h', email=''). Even
                # paid plans see this in /mixed_people/api_search responses.
                # To get the actual data we have to call /people/match which
                # unlocks the person for the team and burns 1 credit per call.
                # Match returns: name, real last_name, email, email_status,
                # linkedin_url, organization. Without enrichment, Apollo rows
                # land in the sheet with empty emails — defeating the purpose
                # of having a paid plan.
                enriched = apollo_enrich_match(api_key, p.get("id", "")) if enrich else None
                if enriched:
                    first = (enriched.get("first_name") or p.get("first_name") or "").strip()
                    last = (enriched.get("last_name") or p.get("last_name") or "").strip()
                    full_name = (enriched.get("name") or f"{first} {last}").strip()
                    org_e = enriched.get("organization") or {}
                    company = (org_e.get("name") or (p.get("organization") or {}).get("name") or "").strip()
                    email = (enriched.get("email") or "").strip()
                    title = (enriched.get("title") or p.get("title") or "").strip()
                    linkedin_url = (enriched.get("linkedin_url") or p.get("linkedin_url") or "").strip()
                else:
                    first = (p.get("first_name") or "").strip()
                    last = (p.get("last_name") or "").strip()
                    full_name = (p.get("name") or f"{first} {last}").strip()
                    org = (p.get("organization") or {})
                    company = (org.get("name") or "").strip()
                    email = (p.get("email") or "").strip()
                    title = (p.get("title") or "").strip()
                    linkedin_url = (p.get("linkedin_url") or "").strip()

                # Apollo sometimes returns "email_not_unlocked@domain.com" placeholders
                # — strip those so downstream cascade can try Hunter / Findymail.
                if email.endswith("@email.unknown") or email.startswith("email_not_unlocked"):
                    email = ""

                # When enrich=True, drop rows whose enrichment didn't unlock an
                # email. The verified-email search filter already pre-selected
                # people Apollo has emails for, so this rarely fires — but when
                # it does, the row is unusable for the daily-300-emails goal
                # (no email + cascade can't help since Apollo already had its
                # turn at find/enrich).
                if enrich and not email:
                    continue

                dedup_key = email.lower() if email else _norm_name_company(full_name, company)
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)
                found.append(_candidate(
                    name=full_name, first_name=first, last_name=last, email=email,
                    company=company, title=title,
                    linkedin_url=linkedin_url, source="apollo",
                ))

            # Stop paging this slice if we got fewer than per_page
            if len(people) < 25:
                break

    return found, pulled, "ok"


# ---------------------------------------------------------------------------
# LinkedIn (via Unipile)
# ---------------------------------------------------------------------------


def linkedin_discover(dsn, api_key, account_id, icp, target=100):
    """Unipile LinkedIn search. DSN typically comes in with `https://` prefix
    already — don't double-prefix. account_id goes in query params, not body.
    Body shape matches the existing linkedin-api skill: {"api": "classic",
    "category": "people", "keywords": "..."}."""
    if not (dsn and api_key and account_id):
        return [], 0, "skipped: no-unipile-creds"
    if not icp:
        return [], 0, "skipped: no-icp"

    found = []
    seen = set()
    pulled = 0
    # Normalize DSN — accept with or without scheme + strip trailing slash
    dsn_base = dsn.rstrip("/")
    if not dsn_base.startswith(("http://", "https://")):
        dsn_base = "https://" + dsn_base
    headers = {"X-API-KEY": api_key, "accept": "application/json", "content-type": "application/json"}

    # 4 keyword variations so we don't rely on one search exhausting the target
    queries = [icp] + [f"{slice_} {icp}" for slice_ in ("VP", "Head of", "Director")]
    for query in queries:
        if len(found) >= target:
            break
        payload = {"api": "classic", "category": "people", "keywords": query.strip()}
        try:
            r = httpx.post(
                f"{dsn_base}/api/v1/linkedin/search",
                headers=headers, json=payload,
                params={"account_id": account_id},
                timeout=30,
            )
            if r.status_code >= 400:
                return found, pulled, f"failure: linkedin {r.status_code}"
            data = r.json() or {}
        except Exception as e:
            return found, pulled, f"failure: {str(e)[:120]}"

        items = data.get("items") or data.get("people") or []
        if not items:
            continue

        for p in items:
            pulled += 1
            # Unipile "people" results — field names vary by API version; try
            # a handful of common shapes.
            full_name = (p.get("name") or p.get("full_name")
                         or (f"{p.get('first_name','')} {p.get('last_name','')}").strip()
                         or "").strip()
            first = (p.get("first_name") or "").strip()
            last = (p.get("last_name") or "").strip()
            if not first or not last:
                fn, ln = _split_name(full_name)
                first = first or fn
                last = last or ln
            company = (p.get("company") or p.get("current_company")
                       or (p.get("experience") or [{}])[0].get("company", "") if p.get("experience") else ""
                       or "").strip()
            li_url = (p.get("profile_url") or p.get("public_profile_url")
                      or p.get("public_identifier") and f"https://linkedin.com/in/{p['public_identifier']}"
                      or "").strip()
            dedup_key = _norm_name_company(full_name, company)
            if dedup_key in seen or not full_name:
                continue
            seen.add(dedup_key)
            found.append(_candidate(
                name=full_name, first_name=first, last_name=last,
                company=company, title=(p.get("headline") or p.get("title") or ""),
                linkedin_url=li_url, source="linkedin",
            ))

    return found, pulled, "ok"




# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


_SHEETS_EPOCH = datetime(1899, 12, 30)


def _date_matches(cell, today_str):
    """True when the date cell represents today_str (YYYY-MM-DD).

    Tolerates legacy rows written before the RAW fix that had been coerced by
    USER_ENTERED into a date type and come back as locale strings or serials.
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


def _load_existing_dedup_keys(token, sid, today=""):
    """Read the full sheet and return:
        (keys: set, header_ok: bool, emailed_today: int)
    where `keys` is the existing (email OR name+company) dedup set and
    `emailed_today` counts rows whose date == today AND email is non-empty.
    The latter drives the daily-300-emails early-stop in run()."""
    try:
        rows = sheets_read(token, sid, f"{SHEET_TAB}!A1:S")
    except Exception:
        return set(), False, 0
    if not rows or len(rows) < 2:
        return set(), True, 0
    header = rows[0]
    try:
        idx_email = header.index("email")
        idx_name = header.index("name")
        idx_company = header.index("company")
        idx_date = header.index("date")
    except ValueError:
        return set(), False, 0
    keys = set()
    emailed_today = 0
    for r in rows[1:]:
        r = r + [""] * max(0, len(header) - len(r))
        em = (r[idx_email] if idx_email < len(r) else "").strip().lower()
        nm = r[idx_name] if idx_name < len(r) else ""
        co = r[idx_company] if idx_company < len(r) else ""
        dt = r[idx_date] if idx_date < len(r) else ""
        if em:
            keys.add(em)
        keys.add(_norm_name_company(nm, co))
        if today and _date_matches(dt, today) and em:
            emailed_today += 1
    return keys, True, emailed_today


def _candidate_key(c):
    em = (c.get("email") or "").strip().lower()
    if em:
        return em
    return _norm_name_company(c.get("name", ""), c.get("company", ""))


def _candidate_to_row(c, today):
    """Map a candidate dict to the 19-column sheet row (Daily Lead Search writes status=raw)."""
    row = {col: "" for col in COLUMNS}
    row["date"] = today
    row["name"] = c.get("name", "")
    row["first_name"] = c.get("first_name", "")
    row["last_name"] = c.get("last_name", "")
    row["email"] = c.get("email", "")
    row["company"] = c.get("company", "")
    row["title"] = c.get("title", "")
    row["linkedin_url"] = c.get("linkedin_url", "")
    row["source"] = c.get("source", "")
    row["status"] = "raw"
    return [row[col] for col in COLUMNS]


def run(inp):
    icp = (inp.get("icp") or "").strip()
    # Strict role label (e.g. "LinkedIn ghostwriters") — when present, both
    # Instantly and Apollo use it as a title filter so the search doesn't
    # fuzzy-match niche ICPs to "Payroll Manager"-shaped junk via free-text.
    role_label = (inp.get("icp_role_label") or "").strip()
    sheet_url = (inp.get("sheet_url") or "").strip()
    today = (inp.get("today") or "").strip() or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    target_instantly = int(inp.get("target_instantly") or 200)
    target_apollo = int(inp.get("target_apollo") or 200)
    target_linkedin = int(inp.get("target_linkedin") or 100)
    # Daily emailed-row goal. The source loop stops early once today's count
    # of rows-with-emails reaches this number — Apollo enrichment writes
    # emails inline, Instantly + LinkedIn rows accumulate emails via the
    # downstream research-batch cascade, both feed this counter.
    target_emailed = int(inp.get("target_emailed") or 300)

    if not icp:
        return {"error": "icp is required. Load 'Ideal Customer Profile:' from agent memory and pass it in."}

    instantly_key = os.environ.get("INSTANTLY_API_KEY", "").strip()
    apollo_key = os.environ.get("APOLLO_API_KEY", "").strip()
    gsheets_json = os.environ.get("GOOGLE_SHEETS_CREDENTIALS_JSON", "").strip()
    unipile_dsn = os.environ.get("UNIPILE_DSN", "").strip()
    unipile_api = os.environ.get("UNIPILE_API_KEY", "").strip()
    unipile_acct = os.environ.get("UNIPILE_ACCOUNT_ID", "").strip()

    if not gsheets_json:
        return {"error": "Missing credentials: GOOGLE_SHEETS_CREDENTIALS_JSON. Connect the Google Sheets gateway."}
    if not instantly_key and not apollo_key:
        return {"error": "Missing credentials: connect at least one of Instantly or Apollo (a primary discovery source is required). LinkedIn (Unipile) is the third source — it discovers but never returns emails on its own."}

    try:
        token = get_access_token(gsheets_json)
    except Exception as e:
        return {"error": f"Google Sheets auth failed: {e}"}

    # Bootstrap sheet on first ever run
    created_sheet_url = ""
    sid = extract_spreadsheet_id(sheet_url)
    if not sid:
        try:
            sid, new_url = sheets_create(token, SHEET_TITLE, SHEET_TAB)
            sheets_append(token, sid, SHEET_TAB, [COLUMNS])
            created_sheet_url = new_url
            sheet_url = new_url
        except Exception as e:
            return {"error": f"Could not bootstrap Leads sheet: {e}"}

    # Cross-run dedup set (email OR normalized name+company) plus today's
    # emailed-row count for the daily-target early-stop.
    existing_keys, header_ok, emailed_today = _load_existing_dedup_keys(token, sid, today=today)
    if not header_ok:
        return {"error": f"Leads sheet has a malformed header. Expected columns: {COLUMNS}"}

    # === Daily-target early-stop ===
    # If today's emailed-row count already meets the target, return immediately.
    # The Daily Lead Search routine can be scheduled multiple times per day
    # (e.g. every 2 hours during work hours) and each call is a no-op once 300
    # is reached.
    if emailed_today >= target_emailed:
        return {
            "created_sheet_url": created_sheet_url,
            "sheet_url": sheet_url,
            "per_source": {},
            "total_committed": 0,
            "dedup_skipped": 0,
            "emailed_today": emailed_today,
            "target_emailed": target_emailed,
            "slack_line": (
                f"*Daily Discovery* · target met ({emailed_today}/{target_emailed} emailed today) — no new discovery this run\n"
                f"{_slack_sheet_link(sheet_url)}"
            ),
        }

    per_source = {}

    # === Each source — try, record pulled/committed/status ===
    # Order matters: Instantly SuperSearch first (free preview), Apollo second
    # (paid enrichment unlocks emails inline), LinkedIn third (no emails but
    # feeds the LinkedIn-first outbound channel + downstream cascade backfill).
    # The loop checks `emailed_today` after each source and breaks early once
    # the daily target is met.
    sources = [
        ("instantly", lambda: instantly_discover(instantly_key, icp, target_instantly, role_label=role_label)),
        ("apollo",    lambda: apollo_discover(apollo_key, icp, target_apollo, role_label=role_label)),
        ("linkedin",  lambda: linkedin_discover(unipile_dsn, unipile_api, unipile_acct, icp, target_linkedin)),
    ]

    dedup_skipped_total = 0
    total_committed = 0

    for name, fn in sources:
        # Per-source early-stop: once today's emailed-row count reaches the
        # target, stop pulling more candidates from later sources.
        if emailed_today >= target_emailed:
            per_source[name] = {"pulled": 0, "committed": 0, "status": f"skipped: target met ({emailed_today}/{target_emailed})"}
            continue

        try:
            candidates, pulled, status = fn()
        except Exception as e:
            per_source[name] = {"pulled": 0, "committed": 0, "status": f"crash: {str(e)[:120]}"}
            continue

        # Batch-append only new candidates
        new_rows = []
        new_emailed = 0
        for c in candidates:
            key = _candidate_key(c)
            if key in existing_keys:
                dedup_skipped_total += 1
                continue
            existing_keys.add(key)
            new_rows.append(_candidate_to_row(c, today))
            if (c.get("email") or "").strip():
                new_emailed += 1

        committed = 0
        if new_rows:
            try:
                committed = sheets_append(token, sid, SHEET_TAB, new_rows)
            except Exception as e:
                per_source[name] = {"pulled": pulled, "committed": 0, "status": f"sheet-write-failed: {str(e)[:100]}"}
                continue

        per_source[name] = {"pulled": pulled, "committed": committed, "status": status}
        total_committed += committed
        # Update the running emailed-today count with rows this source added
        # that already had an email (Apollo enrichment lands here; Instantly
        # and LinkedIn rows arrive emailless and become emailed later via the
        # research-batch cascade).
        emailed_today += new_emailed

    # Slack summary
    def label(s):
        v = per_source.get(s, {})
        c = v.get("committed", 0)
        st = v.get("status", "")
        if st != "ok" and c == 0:
            return f"{s.capitalize()}=0 ({st})"
        return f"{s.capitalize()}={c}"

    parts = [label(s) for s in ("instantly", "apollo", "linkedin")]
    # Multi-line Slack format. "⚠️ Low yield — " prefix is preserved (existing
    # test asserts startswith) when under 100 committed; the plain run uses the
    # bold "*Daily Discovery*" heading. Each source's "Apollo=N" / "Linkedin=N"
    # label is preserved so downstream assertions still hold. The third line
    # shows progress toward the daily emailed-target so the operator can tell
    # at a glance whether we're on pace for 300/day.
    if total_committed < 100:
        heading = f"⚠️ Low yield — *Daily Discovery* · {total_committed} raw candidates committed"
    else:
        heading = f"*Daily Discovery* · {total_committed} raw candidates committed"
    target_status = "🎯 target met" if emailed_today >= target_emailed else "in progress"
    slack_line = (
        f"{heading}\n"
        + " · ".join(parts) + "\n"
        + f"{emailed_today}/{target_emailed} emailed today ({target_status}) · {dedup_skipped_total} duplicates skipped\n"
        + _slack_sheet_link(sheet_url)
    )

    return {
        "created_sheet_url": created_sheet_url,
        "sheet_url": sheet_url,
        "per_source": per_source,
        "total_committed": total_committed,
        "dedup_skipped": dedup_skipped_total,
        "emailed_today": emailed_today,
        "target_emailed": target_emailed,
        "slack_line": slack_line,
    }


try:
    inp = json.loads(os.environ.get("INPUT_JSON", "{}"))
    result = run(inp)
except Exception as e:
    result = {"error": f"sdr-discover crashed: {e}"}

print(json.dumps(result))
