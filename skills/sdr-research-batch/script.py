"""SDR Research Batch — deterministic Python pipeline.

Converts raw discovery rows (status='raw') into fully-researched, verified, and
pre-drafted queued leads (status='queued'). Runs the 7-step email cascade and
composes the email body in Python. No LLM control flow.
"""
import json
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx

APOLLO_BASE = "https://api.apollo.io/api/v1"
HUNTER_BASE = "https://api.hunter.io/v2"
FINDYMAIL_BASE = "https://app.findymail.com/api"
SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
TOKEN_URL = "https://oauth2.googleapis.com/token"
DDG_URL = "https://html.duckduckgo.com/html/"

COLUMNS = [
    "date", "name", "first_name", "last_name", "email", "company", "title",
    "linkedin_url", "signal", "source", "hook", "email_subject", "email_body",
    "status", "message_id", "skip_reason", "sent_at", "channel", "connection_note",
]

# Platform / aggregator / freelance-marketplace / generic-employer domains that
# are NEVER the prospect's actual company domain. When Instantly preview returns
# `company="LinkedIn"` for a solo ghostwriter or `company="Upwork"` for a
# freelancer, the domain extractor used to slugify those into `linkedin.com` /
# `upwork.com` — at which point Hunter's domain_search returned LinkedIn's own
# corporate employees as "matches", and pattern-gen produced fake addresses
# like `first.last@linkedin.com`. Filter those out at every step:
#   1. domain_from_row() returns "" when the slug resolves to one of these
#   2. The cascade rejects any returned/generated email whose domain is here
NON_COMPANY_DOMAINS = frozenset({
    # Job + freelance marketplaces (where solo operators "work")
    "linkedin.com", "upwork.com", "fiverr.com", "contra.com", "toptal.com",
    "freelancer.com", "guru.com", "peopleperhour.com", "indeed.com",
    "glassdoor.com", "ziprecruiter.com", "monster.com", "angel.co", "wellfound.com",
    "behance.net", "dribbble.com", "github.com", "gitlab.com",
    # Free webmail / personal email — not "company emails", but Brevo / cold
    # outreach to these has dramatically worse deliverability and reply rates
    "gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com",
    "aol.com", "live.com", "msn.com", "protonmail.com", "proton.me", "mail.com",
    "gmx.com", "yandex.com", "zoho.com",
    # Generic "self-employed" placeholders Instantly returns
    "selfemployed.com", "self-employed.com", "freelance.com", "freelancer.com",
})


def is_non_company_domain(domain):
    """True when `domain` is a platform / marketplace / webmail domain — i.e.
    NOT a real company domain we'd want to send cold outreach to."""
    if not domain:
        return False
    d = domain.strip().lower().lstrip("@")
    return d in NON_COMPANY_DOMAINS


# Nickname equivalences for the hunter.domain_search name-matching step.
# Each set means: any of these first names refer to the same person.
NICKNAME_GROUPS = [
    {"bob", "robert", "rob", "bobby"},
    {"mike", "michael", "mick"},
    {"jim", "james", "jimmy"},
    {"bill", "william", "will", "billy"},
    {"tom", "thomas", "tommy"},
    {"dick", "richard", "rich", "rick", "ricky"},
    {"dave", "david"},
    {"joe", "joseph", "joey"},
    {"sam", "samuel", "sammy", "samantha"},
    {"chris", "christopher", "christine", "christina"},
    {"dan", "daniel", "danny"},
    {"ben", "benjamin", "benji"},
    {"alex", "alexander", "alexandra", "alexis"},
    {"liz", "elizabeth", "beth", "betty", "eliza"},
    {"kat", "kathryn", "katherine", "kate", "katie", "cathy", "cat"},
    {"nick", "nicholas", "nico"},
    {"tony", "anthony", "antonio"},
    {"steve", "steven", "stephen"},
    {"jack", "john", "johnny"},
    {"matt", "matthew", "matty"},
]

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

# A DDG snippet is only usable as "news" when it contains a real trigger event.
# Generic press-hub blurbs ("ServiceNow news Welcome to your source for…") lack a
# signal and must fall through to a title+company hook.
NEWS_SIGNAL_RE = re.compile(
    r"(?i)\b("
    r"raised|raise|raising|funding|seed|pre-seed|series\s+[a-e]\b|"
    r"\$\s?\d+(?:\.\d+)?\s?(?:m|b|million|billion)\b|"
    r"launch(?:ed|es|ing)?|unveil(?:ed|s|ing)?|debut(?:ed|s|ing)?|"
    r"announc(?:ed|es|ing)|releas(?:ed|es|ing)|"
    r"acqui(?:red|res|ring|sition)|merg(?:ed|er|ing)|ipo\b|listed\b|"
    r"hir(?:ed|es|ing)|appoint(?:ed|s|ment)|joins|joined|promoted|"
    r"partnership|partners\s+with|integrat(?:ed|es|ion|ing)|"
    r"expand(?:ed|s|ing|sion)|secured\s+\$|milestone|went\s+live"
    r")\b"
)
_PRESS_HUB_RE = re.compile(
    r"(?i)(welcome\s+to\s+(?:your\s+)?source\s+for|"
    r"your\s+source\s+for\s+[\w\s]+\s+(?:updates|news)|"
    r"press\s+releases?\s+from|"
    r"official\s+(?:news|blog|press)\s+(?:room|page|site))"
)

# Domains that aren't company indicators when extracted from a URL
_NOT_COMPANY_DOMAINS = {
    "news", "github", "gitlab", "twitter", "x", "medium", "notion",
    "substack", "linkedin", "ycombinator", "reddit", "youtube", "vimeo",
    "google", "docs", "calendar", "drive", "amazon", "wikipedia",
    "mailchimp", "typeform", "airtable", "tinyletter",
}

# Name suffixes that shouldn't become part of last_name
_NAME_SUFFIXES = (", MBA", ", PhD", ", MD", ", JD", ", CPA", ", DDS",
                  ", P.E.", ", PE", ", Jr.", ", Sr.", ", III", ", II")


# ---------------------------------------------------------------------------
# Auth helpers (Google Sheets OAuth)
# ---------------------------------------------------------------------------


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


def sheets_read(token, sid, range_str):
    r = httpx.get(
        f"{SHEETS_API}/{sid}/values/{range_str}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("values", [])


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
    return r.json()


# ---------------------------------------------------------------------------
# Domain / name utilities
# ---------------------------------------------------------------------------


def _strip_name_suffix(s):
    """Strip titular suffixes (', MBA', ', PhD', ' Jr.', …) so they don't contaminate last_name."""
    s = (s or "").strip()
    for sfx in _NAME_SUFFIXES:
        if s.endswith(sfx):
            return s[: -len(sfx)].strip()
    return s


def infer_company_from_title(title):
    """Extract a probable company name from a title string.

    Handles patterns like:
      'Founder @ RevTectonic | Fractional RevOps Leader ...'       -> 'RevTectonic'
      'AI & Automation Engineer at Acme Corp'                      -> 'Acme Corp'
      'Founder @ Shodwe – AI Automation Studio'                    -> 'Shodwe – AI Automation Studio'
      'Owner of Meant2Flourish | Automation Expert'                -> 'Meant2Flourish'
      'Founder & CEO at GEOPHEX'                                   -> 'GEOPHEX'
    Returns '' if nothing plausible is found — callers may then fall back to a
    personal-brand treatment for solopreneurs (use the person's full name).
    """
    if not title:
        return ""
    # Stop characters: pipe, slash, bracket, bullet, trailing paren, newline,
    # comma. Deliberately exclude em/en-dash so "Shodwe – AI Automation Studio"
    # captures the full company name. Comma is a terminator so "Nebula.io,
    # repeat Co-Founder of AI" doesn't capture the comma-trailing text.
    STOP = r"[|/\[(•\n,]"
    # When the source title repeats "at X at X" (happens in raw LinkedIn data),
    # stop at the second " at " so we don't concatenate both halves into one
    # company name.
    AT_STOP = r"\s+at\s+"

    # Generic descriptor phrases that look like companies but aren't. Returned
    # captures that exactly match any of these get rejected so callers fall back
    # to personal-brand treatment instead.
    GENERIC = {
        "scale", "size", "level", "speed", "pace", "volume", "velocity",
        "enterprise scale", "fortune 100 scale", "fortune 500 scale",
        "fortune 100 companies", "fortune 500 companies",
        "startup speed", "startup scale", "enterprise", "startups",
    }

    def _clean(s):
        return s.strip(" .-–—")

    # 1. 'Role @ Company' — the @ is explicit, so be permissive about what follows.
    m = re.search(rf"@\s+([^|/\[(•\n,]+?)(?:\s*{STOP}|\s*$)", title)
    if m:
        out = _clean(m.group(1))
        if len(out) > 1 and not out.lower().startswith(("http", "linkedin")) and out.lower() not in GENERIC:
            return out

    # 2. 'Role at Company' — require leading capital on the company to avoid
    # matching prose like "I help businesses at scale". Stops at pipes / brackets
    # / commas / a second "at X" (duplicated-title case).
    m = re.search(rf"\bat\s+([A-Z][^|/\[(•\n,]*?)(?:\s*{STOP}|{AT_STOP}|\s*$)", title)
    if m:
        out = _clean(m.group(1))
        if len(out) > 1 and out.lower() not in GENERIC:
            return out

    # 3. '{Role} of Company' — restricted to founder/owner/C-suite roles. "VP of
    # Technology", "Head of Operations", "Director of Marketing" are department
    # titles — treating the 'of X' group as a company would produce false
    # positives like company='Technology'. "President" is excluded because it's
    # almost always preceded by "Vice " in practice.
    m = re.search(
        rf"\b(?:owner|founder|co-?founder|ceo|cto|coo|cfo)(?:\s*&\s*\w+)?\s+of\s+([A-Z][^|/\[(•\n,]*?)(?:\s*{STOP}|\s*$)",
        title,
        flags=re.IGNORECASE,
    )
    if m:
        out = _clean(m.group(1))
        if len(out) > 1 and out.lower() not in GENERIC:
            return out

    return ""


def infer_company_from_url(url):
    """Extract a probable company name from a URL's domain.

    HN discovery rows carry the person's submitted URL in `linkedin_url` — that URL
    is very often the person's own project/company (e.g. `pumpups.com` → 'Pumpups').
    Skips known non-company domains (news, github, etc.) and LinkedIn profile URLs.
    """
    if not url:
        return ""
    u = url.lower()
    if "linkedin.com/in/" in u or "news.ycombinator.com" in u:
        return ""
    try:
        from urllib.parse import urlparse
        netloc = urlparse(url).netloc.lower().replace("www.", "")
        if not netloc or "." not in netloc:
            return ""
        slug = netloc.split(".")[0]
        if slug in _NOT_COMPANY_DOMAINS:
            return ""
        # Title-case, preserving internal dashes as spaces: 'make-it-future' -> 'Make It Future'
        return " ".join(p.capitalize() for p in slug.replace("_", "-").split("-") if p)
    except Exception:
        return ""


def domain_from_row(row_dict):
    """Derive a probable company domain from existing row fields. Falls back through:
    (0) signal='domain=...' seeded by Apollo enrichment (real, authoritative),
    (1) explicit email's domain,
    (2) linkedin_url's company slug → guess .com,
    (3) company name slugified → guess .com.
    Returns "" when no source is available OR when every candidate resolves to
    a platform / marketplace / webmail domain (linkedin.com, upwork.com, etc.) —
    those are never the prospect's real company domain, and letting them through
    causes Hunter to return LinkedIn's own corporate employees and pattern-gen
    to produce fake `first.last@linkedin.com` addresses.
    """
    # (0) Apollo enrichment stashes the REAL domain here as 'domain=acme.io'
    signal = (row_dict.get("signal") or "").strip()
    m = re.match(r"domain=([a-z0-9.\-]+)", signal, re.IGNORECASE)
    if m:
        dom = m.group(1).lower()
        if not is_non_company_domain(dom):
            return dom

    email = (row_dict.get("email") or "").strip()
    if "@" in email:
        dom = email.split("@", 1)[1].strip().lower()
        if dom and "." in dom and not is_non_company_domain(dom):
            return dom

    li = (row_dict.get("linkedin_url") or "").strip().lower()
    # e.g. https://www.linkedin.com/company/acme-corp/about
    m = re.search(r"linkedin\.com/company/([a-z0-9-]+)", li)
    if m:
        slug = m.group(1)
        candidate = f"{slug}.com"
        if not is_non_company_domain(candidate):
            return candidate

    company = (row_dict.get("company") or "").strip().lower()
    if company:
        slug = re.sub(r"[^a-z0-9]+", "", company)
        if slug:
            candidate = f"{slug}.com"
            if not is_non_company_domain(candidate):
                return candidate
    return ""


def names_match(candidate_first, candidate_last, lead_first, lead_last):
    """Case-insensitive exact match on last name + first-name-or-nickname match."""
    if not candidate_last or not lead_last:
        return False
    if candidate_last.strip().lower() != lead_last.strip().lower():
        return False
    cf = (candidate_first or "").strip().lower()
    lf = (lead_first or "").strip().lower()
    if not cf or not lf:
        return bool(cf or lf)  # permit one-sided match rather than reject
    if cf == lf:
        return True
    for group in NICKNAME_GROUPS:
        if cf in group and lf in group:
            return True
    return False


def generate_email_patterns(first_name, last_name, domain):
    """Ordered list of candidate emails to try via hunter.email_verifier."""
    f = (first_name or "").strip().lower()
    l = (last_name or "").strip().lower()
    d = (domain or "").strip().lower()
    if not d:
        return []
    out = []
    seen = set()
    def add(x):
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    if f and l:
        add(f"{f}.{l}@{d}")
        add(f"{f}{l}@{d}")
        add(f"{f[0]}{l}@{d}")
        add(f"{f}.{l[0]}@{d}")
        add(f"{l}.{f}@{d}")
    if f:
        add(f"{f}@{d}")
    if l:
        add(f"{l}@{d}")
    return out


# ---------------------------------------------------------------------------
# External API calls — each returns None / empty on error so callers continue
# ---------------------------------------------------------------------------


def apollo_enrich_person(api_key, first_name, last_name, company, domain):
    """Enrich a person via Apollo's people/match endpoint.

    Returns (email, enriched_dict). Apollo's `mixed_people/api_search` (used by
    Discovery) only returns first_name + company — no last_name, no linkedin_url,
    no real domain. Enrichment here fills those in so the rest of the email
    cascade has real data to work with. `email` is only present on paid Apollo
    tiers (reveal_personal_emails=True). `enriched_dict` keys that aren't already
    on the row can be used to update the row in place.
    """
    if not api_key or not (first_name or last_name) or not (company or domain):
        return None, {}
    payload = {
        "first_name": first_name or "",
        "last_name": last_name or "",
        "organization_name": company or "",
        "domain": domain or "",
        "reveal_personal_emails": True,
    }
    try:
        r = httpx.post(
            f"{APOLLO_BASE}/people/match",
            headers={"Cache-Control": "no-cache", "Content-Type": "application/json", "x-api-key": api_key},
            json=payload,
            timeout=20,
        )
        if r.status_code >= 400:
            return None, {}
        data = r.json() or {}
    except Exception:
        return None, {}

    person = data.get("person") or {}
    org = person.get("organization") or {}

    enriched = {}
    ln = (person.get("last_name") or "").strip()
    if ln:
        enriched["last_name"] = ln
    li = (person.get("linkedin_url") or "").strip()
    if li:
        enriched["linkedin_url"] = li
    # Apollo returns `primary_domain` ('acme.io') OR `website_url` ('https://acme.io/home')
    dom = (org.get("primary_domain") or org.get("website_url") or "").strip()
    if dom:
        dom = re.sub(r"^https?://", "", dom).rstrip("/").split("/")[0].lower()
        if dom:
            enriched["domain"] = dom
    title = (person.get("title") or "").strip()
    if title:
        enriched["title"] = title

    email = (person.get("email") or "").strip()
    if email.endswith("@email.unknown") or email.startswith("email_not_unlocked"):
        email = ""
    return (email or None), enriched


def hunter_email_finder(api_key, first_name, last_name, domain):
    if not api_key or not domain or not (first_name or last_name):
        return None
    try:
        r = httpx.get(
            f"{HUNTER_BASE}/email-finder",
            params={
                "api_key": api_key, "domain": domain,
                "first_name": first_name or "", "last_name": last_name or "",
            },
            timeout=20,
        )
        if r.status_code >= 400:
            return None
        data = (r.json() or {}).get("data") or {}
        email = (data.get("email") or "").strip()
        score = data.get("score") or 0
        if email and score >= 70:
            return email
        return None
    except Exception:
        return None


def hunter_domain_search(api_key, domain, limit=10):
    """Hunter's free plan caps `limit` at 10 — any higher returns a 400
    'pagination_error'. Default to 10 so free-tier keys don't silently fail."""
    if not api_key or not domain:
        return []
    try:
        r = httpx.get(
            f"{HUNTER_BASE}/domain-search",
            params={"api_key": api_key, "domain": domain, "limit": min(limit, 10)},
            timeout=25,
        )
        if r.status_code >= 400:
            return []
        data = (r.json() or {}).get("data") or {}
        return data.get("emails") or []
    except Exception:
        return []


def findymail_find_by_name(api_key, full_name, domain):
    """Findymail's search-by-name: POST /api/search/name with {name, domain}.
    Credit-charged only on successful finds. Returns (email, enriched_dict).
    enriched_dict carries last_name / linkedin_url / domain / job_title pulled
    from Findymail's response — useful when upstream Apollo didn't have them."""
    if not api_key or not full_name or not domain:
        return None, {}
    try:
        r = httpx.post(
            f"{FINDYMAIL_BASE}/search/name",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "Accept": "application/json"},
            json={"name": full_name, "domain": domain},
            timeout=25,
        )
        if r.status_code >= 400:
            return None, {}
        data = r.json() or {}
    except Exception:
        return None, {}

    contact = data.get("contact") or {}
    email = (contact.get("email") or "").strip() or None
    enriched = {}
    # Findymail returns a full `name` string; try to split it into first/last
    ret_name = (contact.get("name") or "").strip()
    if ret_name and " " in ret_name:
        parts = ret_name.split(None, 1)
        enriched["first_name"] = parts[0]
        enriched["last_name"] = parts[1]
    li = (contact.get("linkedin_url") or "").strip()
    if li:
        # Findymail returns bare `linkedin.com/in/...` without the scheme sometimes
        if not li.startswith(("http://", "https://")):
            li = "https://" + li.lstrip("/")
        enriched["linkedin_url"] = li
    dom = (contact.get("domain") or "").strip().lower()
    if dom:
        enriched["domain"] = dom
    title = (contact.get("job_title") or "").strip()
    if title:
        enriched["title"] = title
    return email, enriched


def findymail_find_by_linkedin(api_key, linkedin_url):
    """Reverse lookup: given a LinkedIn profile URL, get email + full profile.
    Best used when Apollo/Hunter missed but we have a LinkedIn URL from
    discovery (LinkedIn source, or Apollo enrichment that gave us the URL)."""
    if not api_key or not linkedin_url:
        return None, {}
    try:
        r = httpx.post(
            f"{FINDYMAIL_BASE}/search/linkedin",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "Accept": "application/json"},
            json={"linkedin_url": linkedin_url},
            timeout=25,
        )
        if r.status_code >= 400:
            return None, {}
        data = r.json() or {}
    except Exception:
        return None, {}
    contact = data.get("contact") or {}
    email = (contact.get("email") or "").strip() or None
    enriched = {}
    ret_name = (contact.get("name") or "").strip()
    if ret_name and " " in ret_name:
        parts = ret_name.split(None, 1)
        enriched["first_name"] = parts[0]
        enriched["last_name"] = parts[1]
    dom = (contact.get("domain") or "").strip().lower()
    if dom:
        enriched["domain"] = dom
    title = (contact.get("job_title") or "").strip()
    if title:
        enriched["title"] = title
    return email, enriched


def hunter_resolve_domain_from_company(api_key, company):
    """Hunter's domain-search accepts either `domain=` or `company=`. When given
    a company name it returns the matched real domain. This is the ground-truth
    we should use in preference to slugified guesses like 'paymentslab.com'
    (the real one is 'payments-lab.com'). Returns ('', []) on no-match or error."""
    if not api_key or not company:
        return "", []
    try:
        r = httpx.get(
            f"{HUNTER_BASE}/domain-search",
            params={"api_key": api_key, "company": company, "limit": 10},
            timeout=25,
        )
        if r.status_code >= 400:
            return "", []
        data = (r.json() or {}).get("data") or {}
        return (data.get("domain") or "").strip(), (data.get("emails") or [])
    except Exception:
        return "", []


def hunter_verify(api_key, email):
    """Returns one of: 'deliverable', 'risky', 'undeliverable', 'unknown'.

    Hunter's email-verifier returns `status` in its own vocabulary — 'valid',
    'invalid', 'accept_all', 'webmail', 'disposable'. Normalize those to the
    email-industry triad this cascade uses:
      valid                                   → 'deliverable'
      accept_all | webmail                    → 'risky'
      invalid | disposable                    → 'undeliverable'
      anything else                           → 'unknown'
    """
    if not api_key or not email or "@" not in email:
        return "unknown"
    try:
        r = httpx.get(
            f"{HUNTER_BASE}/email-verifier",
            params={"api_key": api_key, "email": email},
            timeout=15,
        )
        if r.status_code >= 400:
            return "unknown"
        data = (r.json() or {}).get("data") or {}
        raw = (data.get("status") or data.get("result") or "unknown").lower().strip()
    except Exception:
        return "unknown"
    if raw in ("deliverable", "valid"):
        return "deliverable"
    if raw in ("risky", "accept_all", "webmail"):
        return "risky"
    if raw in ("undeliverable", "invalid", "disposable"):
        return "undeliverable"
    return "unknown"


def ddg_search(query, n=5):
    """Free-tier web search via DuckDuckGo HTML endpoint. No API key needed.
    Returns a list of (title, url, snippet) tuples, best-effort parsed."""
    if not query:
        return []
    try:
        r = httpx.post(
            DDG_URL,
            data={"q": query},
            headers={"User-Agent": "Mozilla/5.0 (compatible; Oya-SDR/1.0)"},
            timeout=20,
            follow_redirects=True,
        )
        if r.status_code >= 400:
            return []
        html = r.text or ""
    except Exception:
        return []
    # Very light parse — DDG's /html/ returns plain HTML with <a class="result__a" ...
    results = []
    for m in re.finditer(
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
        html, re.DOTALL,
    ):
        url = m.group(1)
        title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        snippet = re.sub(r"<[^>]+>", "", m.group(3)).strip()
        results.append((title, url, snippet))
        if len(results) >= n:
            break
    return results


def fetch_url_text(url, timeout=15):
    """Fetch a URL and return text content (HTML stripped). Returns '' on error."""
    if not url:
        return ""
    try:
        r = httpx.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; Oya-SDR/1.0)"},
            timeout=timeout,
            follow_redirects=True,
        )
        if r.status_code >= 400:
            return ""
        html = r.text or ""
    except Exception:
        return ""
    # Strip scripts/styles, then tags
    html = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.IGNORECASE)
    html = re.sub(r"<style[\s\S]*?</style>", " ", html, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_meta_description(url, timeout=12):
    """Pull a one-sentence overview from a company homepage."""
    if not url:
        return ""
    try:
        r = httpx.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; Oya-SDR/1.0)"},
            timeout=timeout,
            follow_redirects=True,
        )
        if r.status_code >= 400:
            return ""
        html = r.text or ""
    except Exception:
        return ""
    m = re.search(
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']',
        html, re.IGNORECASE,
    )
    if m:
        return m.group(1).strip()
    m = re.search(
        r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']',
        html, re.IGNORECASE,
    )
    if m:
        return m.group(1).strip()
    m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return ""


# ---------------------------------------------------------------------------
# The 7-step email cascade
# ---------------------------------------------------------------------------


def _email_domain_ok(email):
    """True when `email` looks deliverable AND its domain isn't a known
    platform/marketplace/webmail domain. Used to defensively reject results
    like `first.last@linkedin.com` or `support@upwork.com` that any step in
    the cascade might surface — those are never the prospect's real address."""
    if not email or "@" not in email:
        return False
    dom = email.rsplit("@", 1)[1].strip().lower()
    if not dom or "." not in dom:
        return False
    return not is_non_company_domain(dom)


def email_cascade(row_dict, apollo_key, hunter_key, findymail_key=""):
    """Runs the 7-step email cascade. Returns (email, step_that_won) or (None, None).

    `step_that_won` is a short string used for skip_reason / debugging:
    'existing' / 'apollo' / 'hunter-finder' / 'hunter-domain' / 'pattern' /
    'web' / 'scrape'. On total failure returns (None, 'cascade-exhausted').

    A defensive post-filter rejects any returned email whose domain is in
    NON_COMPANY_DOMAINS (linkedin.com, upwork.com, gmail.com, etc.) — those
    are never the prospect's real company address, regardless of which step
    surfaced them.
    """
    email, step = _email_cascade_impl(row_dict, apollo_key, hunter_key, findymail_key=findymail_key)
    if email and not _email_domain_ok(email):
        # Strip the bad email off the row so downstream code doesn't reuse it
        row_dict["email"] = ""
        return None, f"rejected-non-company-domain ({step})"
    return email, step


def _email_cascade_impl(row_dict, apollo_key, hunter_key, findymail_key=""):
    """Internal cascade body. Public callers go through email_cascade() which
    applies the non-company-domain post-filter."""
    first_name = row_dict.get("first_name") or ""
    last_name = row_dict.get("last_name") or ""
    company = row_dict.get("company") or ""
    domain = domain_from_row(row_dict)
    name_full = f"{first_name} {last_name}".strip()

    # Step (a) — verify existing email if one is already on the row
    existing = (row_dict.get("email") or "").strip()
    if existing and "@" in existing:
        status = hunter_verify(hunter_key, existing)
        if status in ("deliverable", "risky"):
            return existing, "existing"

    # Step (b) — Apollo enrich_person (returns (email, enriched_dict))
    # deep_research already called this on Apollo-sourced rows to populate
    # last_name/linkedin_url/domain, but calling again here is idempotent and
    # catches the case where this cascade is invoked on a non-Apollo row.
    _b = apollo_enrich_person(apollo_key, first_name, last_name, company, domain)
    if isinstance(_b, tuple):
        email, _ = _b
    else:
        # Backward-compat for any monkeypatched test that returns a bare string
        email = _b
    if email:
        return email, "apollo"

    # Step (c) — Hunter email_finder
    if domain:
        email = hunter_email_finder(hunter_key, first_name, last_name, domain)
        if email:
            return email, "hunter-finder"

    # Step (d.0) — If the guessed domain has no emails in Hunter, ask Hunter to
    # resolve the REAL domain from the company name ("Payments Lab" →
    # "payments-lab.com", which our company-slug guess would miss). Use that
    # better domain for the rest of the cascade.
    if domain and company:
        probe = hunter_domain_search(hunter_key, domain, limit=10)
        if not probe:
            real_domain, real_emails = hunter_resolve_domain_from_company(hunter_key, company)
            if real_domain and real_domain != domain:
                domain = real_domain
                # Update the signal so future cascade re-runs reuse this domain
                if not re.match(r"domain=", row_dict.get("signal") or ""):
                    existing_sig = (row_dict.get("signal") or "").strip()
                    row_dict["signal"] = f"domain={real_domain}" + (f" | {existing_sig}" if existing_sig else "")

    # Step (d) — Hunter domain_search + nickname-aware name match.
    # When lead_last_name is empty (common for Apollo free-tier rows), fall back to
    # unambiguous first-name match: accept only if EXACTLY ONE candidate at the
    # domain has a matching first name. This unlocks leads like Shawn@Vodyssey
    # that Hunter already has at 99% confidence, without risking false positives.
    if domain and (first_name or last_name):
        candidates = hunter_domain_search(hunter_key, domain, limit=10)
        # First try strict name match (requires both first and last)
        for c in candidates:
            if names_match(c.get("first_name"), c.get("last_name"), first_name, last_name):
                email = (c.get("value") or "").strip()
                if email:
                    # Update the row with last_name we just learned from Hunter
                    if not last_name and c.get("last_name"):
                        row_dict["last_name"] = c["last_name"].strip()
                    return email, "hunter-domain"

        # Fall back: first-name-only match if lead_last_name is missing.
        # Require exactly-one match to avoid picking the wrong "Shawn" at a
        # company that has two Shawns. Prefer higher confidence.
        if not last_name and first_name:
            matches = []
            for c in candidates:
                cf = (c.get("first_name") or "").strip().lower()
                lf = first_name.strip().lower()
                if cf == lf:
                    matches.append(c)
                    continue
                for group in NICKNAME_GROUPS:
                    if cf in group and lf in group:
                        matches.append(c)
                        break
            if len(matches) == 1 and (matches[0].get("value") or "").strip():
                best = matches[0]
                # Update the row with the last_name we just learned
                if best.get("last_name"):
                    row_dict["last_name"] = best["last_name"].strip()
                return best["value"].strip(), "hunter-domain"

    # Step (e) — pattern generation + verifier
    for candidate in generate_email_patterns(first_name, last_name, domain):
        if hunter_verify(hunter_key, candidate) == "deliverable":
            return candidate, "pattern"

    # Step (e.5) — Findymail. Purpose-built long-tail finder that waterfalls
    # across 10+ providers internally, so it hits sources Hunter/Apollo miss
    # (small YC-style companies, niche B2B, EU founders). Credit-charged ONLY
    # on a successful find — failures are free. Runs AFTER pattern-gen so we
    # don't burn credits on leads Hunter's $0.00 lookups could have solved.
    # Try linkedin-first (higher hit rate when we have the URL from Apollo
    # enrichment or LinkedIn discovery), then fall back to name-search.
    if findymail_key:
        li_url = (row_dict.get("linkedin_url") or "").strip()
        if li_url and "linkedin.com/" in li_url.lower():
            email, enriched = findymail_find_by_linkedin(findymail_key, li_url)
            if email:
                # Update row with anything Findymail told us that we didn't have
                for k in ("last_name", "linkedin_url", "title"):
                    if enriched.get(k) and not (row_dict.get(k) or "").strip():
                        row_dict[k] = enriched[k]
                return email, "findymail-linkedin"

        if name_full and domain:
            email, enriched = findymail_find_by_name(findymail_key, name_full, domain)
            if email:
                for k in ("last_name", "linkedin_url", "title"):
                    if enriched.get(k) and not (row_dict.get(k) or "").strip():
                        row_dict[k] = enriched[k]
                return email, "findymail-name"

    # Step (f) — DDG search for "name company email"
    if domain and name_full:
        query = f'"{name_full}" "{company}" email'
        for title, url, snippet in ddg_search(query, n=5):
            for candidate in EMAIL_RE.findall(f"{title} {snippet}"):
                if candidate.lower().endswith("@" + domain):
                    if hunter_verify(hunter_key, candidate) in ("deliverable", "risky"):
                        return candidate, "web"

    # Step (g) — site scrape of the usual suspects
    if domain:
        for path in ("team", "about", "leadership", "contact", "people"):
            url = f"https://{domain}/{path}"
            page_text = fetch_url_text(url, timeout=10)
            if not page_text:
                continue
            # Scan for lead's name with an email nearby
            lname_lower = (last_name or "").lower()
            for candidate in EMAIL_RE.findall(page_text):
                if not candidate.lower().endswith("@" + domain):
                    continue
                # Only accept if the lead's last name appears in the page
                if lname_lower and lname_lower in page_text.lower():
                    if hunter_verify(hunter_key, candidate) in ("deliverable", "risky"):
                        return candidate, "scrape"

    return None, "cascade-exhausted"


# ---------------------------------------------------------------------------
# Deep research — produce a personalization hook for Compose
# ---------------------------------------------------------------------------


def deep_research(row_dict, apollo_key=""):
    """Returns (signal, hook, company_overview). Best-effort — skips sub-steps
    that fail rather than aborting the whole research. Also fills in missing
    first_name / last_name / linkedin_url / domain / company by (1) Apollo enrichment
    if the row came from Apollo discovery with preview-only data, (2) inferring
    company from title text or URL domain, (3) stripping name suffixes."""
    # --- Pre-research normalization ---
    for k in ("first_name", "last_name", "name"):
        v = (row_dict.get(k) or "").strip()
        if v:
            row_dict[k] = _strip_name_suffix(v)

    # Apollo `mixed_people/api_search` returns preview-only records (first_name +
    # company only — no last_name, no linkedin_url, no real domain). Call Apollo
    # `people/match` enrichment to fill those in. Without this, Hunter finder fails
    # (needs last_name), pattern gen is crippled, and domain guessing from
    # company-name ('Payments Lab' → 'paymentslab.com') is usually wrong.
    source = (row_dict.get("source") or "").strip().lower()
    if (apollo_key and source == "apollo" and
            (not (row_dict.get("last_name") or "").strip() or
             not (row_dict.get("linkedin_url") or "").strip())):
        try:
            _email, enriched = apollo_enrich_person(
                apollo_key,
                row_dict.get("first_name") or "",
                row_dict.get("last_name") or "",
                row_dict.get("company") or "",
                "",
            )
            for k in ("last_name", "linkedin_url", "title"):
                if enriched.get(k) and not (row_dict.get(k) or "").strip():
                    row_dict[k] = enriched[k]
            # Apollo-provided domain is real — stash in signal so email_cascade
            # can read it. Only set if signal is empty (don't clobber news signals).
            if enriched.get("domain") and not (row_dict.get("signal") or "").strip():
                row_dict["signal"] = f"domain={enriched['domain']}"
            # Rebuild name if we just added a last_name and current name doesn't include it
            ln = (row_dict.get("last_name") or "").strip()
            current_name = (row_dict.get("name") or "").strip()
            if ln and ln not in current_name:
                row_dict["name"] = f"{row_dict.get('first_name','')} {ln}".strip()
            if _email and not (row_dict.get("email") or "").strip():
                row_dict["email"] = _email
        except Exception:
            pass  # enrichment failure is non-fatal

    if not (row_dict.get("company") or "").strip():
        guess = infer_company_from_title(row_dict.get("title") or "")
        # For HN rows the submitted URL is the person's own project/portfolio —
        # the domain is THEM, not an employer. Skip URL-based company inference
        # so the downstream personal-brand fallback fires instead of producing
        # nonsensical hooks like "Noticed you're HN participant at Jameshard".
        if not guess and (row_dict.get("source") or "").strip().lower() != "hn":
            guess = infer_company_from_url(row_dict.get("linkedin_url") or "")
        if guess:
            row_dict["company"] = guess

    # Personal-brand fallback: solopreneurs (LinkedIn search often surfaces
    # "AI Automation Expert | …", "Owner & Operator", "Founder @ {thing}")
    # frequently lack a distinct company — they ARE the brand. Rather than
    # skip these (the ICP is often dead-on), fall back to using the full name
    # as the company so downstream quality-gate and hook logic have something
    # to work with. The LLM compose step gets `_is_personal_brand=True` as a
    # hint so it doesn't write "At OyaAI, we help Anas Azam scale…" weirdly.
    is_personal_brand = False
    if not (row_dict.get("company") or "").strip():
        fn = (row_dict.get("first_name") or "").strip()
        ln = (row_dict.get("last_name") or "").strip()
        full = (row_dict.get("name") or "").strip()
        if fn and ln:
            row_dict["company"] = f"{fn} {ln}"
            is_personal_brand = True
        elif full:
            row_dict["company"] = full
            is_personal_brand = True
    row_dict["_is_personal_brand"] = is_personal_brand

    first_name = row_dict.get("first_name") or ""
    company = row_dict.get("company") or ""
    title = row_dict.get("title") or ""
    existing_signal = (row_dict.get("signal") or "").strip()
    existing_hook = (row_dict.get("hook") or "").strip()

    # Try to pull a company overview from the homepage. For HN-discovered rows,
    # the `linkedin_url` field actually holds the URL the person submitted to
    # Hacker News — usually their personal project, portfolio, or GitHub page
    # (e.g. https://jameshard.ing/pilot). That URL is the best signal we have
    # about what this person works on, so fetch the meta description there
    # instead of guessing a domain from their username. The email cascade's
    # site-scrape step (step g) will also try this same domain for a contact
    # address, so HN-sourced leads can yield an email via the project site.
    overview = ""
    source_kind = (row_dict.get("source") or "").strip().lower()
    submitted_url = (row_dict.get("linkedin_url") or "").strip()
    if source_kind == "hn" and submitted_url:
        try:
            overview = extract_meta_description(submitted_url)
        except Exception:
            overview = ""
    if not overview:
        domain = domain_from_row(row_dict)
        if domain:
            overview = extract_meta_description(f"https://{domain}")
        if not overview and domain:
            # Retry with www.
            overview = extract_meta_description(f"https://www.{domain}")

    # Try DDG for recent company news to build a trigger-based hook. Only accept a
    # snippet as "news" when it contains an actual signal keyword (raised, launched,
    # acquired, hired, etc.) AND isn't the homepage press-hub blurb. Skip entirely
    # for personal-brand fallbacks (company = person's name) — DDG will surface
    # unrelated people with the same name.
    news_snippet = ""
    if company and not is_personal_brand:
        for _, _, snippet in ddg_search(f"{company} news OR funding OR hiring", n=5):
            if not snippet or len(snippet) < 40:
                continue
            if _PRESS_HUB_RE.search(snippet):
                continue
            if not NEWS_SIGNAL_RE.search(snippet):
                continue
            news_snippet = snippet[:200]
            break

    # Stash raw research on the row so compose()/LLM can use them as separate fields
    # (not just the summarized `hook`). Underscore-prefixed keys are intentionally
    # not in COLUMNS — they're in-memory only, not written to the sheet.
    row_dict["_overview"] = overview
    row_dict["_news_snippet"] = news_snippet

    # Compose the hook — prefer news snippet, then title+company, then company-only.
    # Personal-brand rows get a title-first hook (no "at {name}" phrasing).
    hook = existing_hook
    if not hook:
        if news_snippet:
            hook = f"Saw news about {company}: \"{news_snippet.rstrip('.')}.\""
        elif source_kind == "hn" and overview:
            # HN rows have title="HN participant" (placeholder); the meaningful
            # positioning comes from the submitted project page's meta description.
            snippet = overview.split(".")[0][:160].strip().rstrip(".")
            hook = f"Saw your project: {snippet}" if snippet else f"Came across your HN submission"
        elif is_personal_brand and title and title.lower() != "hn participant":
            # Use the first title segment (before the first pipe) as the hook —
            # LinkedIn titles are pipe-separated self-descriptions.
            first_segment = title.split("|")[0].strip()[:160].rstrip(".")
            if first_segment:
                hook = f"Saw your work: {first_segment}"
            else:
                hook = f"Came across your profile on {(row_dict.get('source') or 'LinkedIn').title()}"
        elif title and company and not is_personal_brand:
            # Corporate rows only: "Noticed you're {title} at {company}" only
            # reads correctly when company is a real employer, not the person's name.
            hook = f"Noticed you're {title} at {company}"
            if overview:
                # Add a short positioning clause
                first_sentence = overview.split(".")[0][:140].strip()
                if first_sentence:
                    hook += f", where {first_sentence.lower().rstrip('.')}"
        elif is_personal_brand:
            src_label = (row_dict.get("source") or "linkedin").upper() if (row_dict.get("source") or "").lower() == "hn" else (row_dict.get("source") or "LinkedIn").title()
            hook = f"Came across your work via {src_label}"
        elif company:
            hook = f"Came across {company} recently"

    signal = existing_signal
    if not signal:
        if news_snippet:
            signal = news_snippet[:100]
        elif title:
            signal = title

    return signal, hook, overview


# ---------------------------------------------------------------------------
# Quality gate
# ---------------------------------------------------------------------------


def quality_gate(row_dict):
    """Returns (passed: bool, reason_if_failed: str)."""
    first_name = (row_dict.get("first_name") or "").strip()
    name = (row_dict.get("name") or "").strip()
    company = (row_dict.get("company") or "").strip()
    hook = (row_dict.get("hook") or "").strip()
    if not first_name and not name:
        return False, "no-name"
    if not company:
        return False, "no-company"
    if not hook or len(hook) < 20:
        return False, "weak-hook"
    return True, ""


# ---------------------------------------------------------------------------
# Compose — Python template
# ---------------------------------------------------------------------------


# Values that look like role labels or LLM-substituted persona descriptions
# rather than an actual human first name. When the Research Batch routine was
# originally shipped, its prompt passed `sender_name=<your persona name>` — the
# orchestrating LLM would substitute the persona's description ("Sales
# Development Representative", "AI SDR", etc.), which then landed in the email
# sign-off. This sanitizer rejects those values so the skill falls back to the
# safer "the team" default — existing deployed agents pick up the correct
# behavior on next routine run without any migration or re-deploy.
_PERSONA_LABEL_RE = re.compile(
    r"(?i)\b(representative|assistant|manager|rep|agent|bot|sdr|ai|employee|"
    r"team|staff|persona|executive|officer|coordinator|specialist|consultant|"
    r"the\s+name|the\s+team)\b"
)


def _slack_sheet_link(sheet_url):
    """Format the sheet URL as a Slack-flavored hyperlink so channel summaries
    render a clickable 'Leads sheet' label instead of a 100-char
    https://docs.google.com/spreadsheets/... blob taking up a whole line.
    Slack's link syntax is `<URL|display text>`."""
    url = (sheet_url or "").strip()
    if not url:
        return ""
    return f"<{url}|Leads sheet>"


def _sanitize_sender_name(raw):
    """Normalize a sender_name input. Returns the cleaned value, or empty
    string if the input looks like a role label rather than a person's name."""
    v = (raw or "").strip()
    if not v:
        return ""
    # Literal unsubstituted placeholders like "<your persona name>"
    if v.startswith("<") or v.lower() in {"your persona name", "your name", "the team"}:
        return ""
    # Excessively long values are almost always persona descriptions, not names
    if len(v) > 40:
        return ""
    # Role/title keywords — "Sales Development Representative", "AI SDR", etc.
    if _PERSONA_LABEL_RE.search(v):
        return ""
    return v


def _build_cta(my_calendar):
    if my_calendar and my_calendar.strip():
        return f"Worth a 15-min chat? Grab a time that works: {my_calendar.strip()}"
    return "Worth a 15-min chat this week? Reply with a time that works and I'll send an invite."


def _build_signature(source, seller, company):
    src = (source or "").strip() or "the web"
    return (
        f"---\n"
        f"This is an AI Employee from Oya.ai — I found you on {src}, researched "
        f"{seller}'s fit with {company}, then wrote this email. If you like it, "
        f"build one like me for {seller} in 3 minutes at https://oya.ai"
    )


def _llm_compose(row_dict, my_company, my_value_prop, sender_name, my_site_overview=""):
    """Ask Claude to write the personalized opener + pitch paragraph PLUS a
    280-char LinkedIn connection note. Returns (subject, body_main, connection_note)
    on success, or (None, None, None) on failure. The caller appends the
    deterministic CTA and signature to body_main so Calendly URL and disclosure
    footer never depend on LLM compliance. connection_note is sent verbatim as a
    LinkedIn invite message — no CTA appended.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return None, None, None

    first_name = (row_dict.get("first_name") or "").strip() or "there"
    company = (row_dict.get("company") or "").strip() or "their company"
    title = (row_dict.get("title") or "").strip()
    hook = (row_dict.get("hook") or "").strip()
    signal = (row_dict.get("signal") or "").strip()
    overview = (row_dict.get("_overview") or "").strip()
    news_snippet = (row_dict.get("_news_snippet") or "").strip()
    is_personal_brand = bool(row_dict.get("_is_personal_brand"))
    signer = (sender_name or "").strip() or "the team"
    seller = (my_company or "").strip() or "our team"
    pitch_raw = (my_value_prop or "").strip() or "we help teams like yours hit their goals."
    seller_overview = (my_site_overview or "").strip()

    # Compact the available research into a single block. The LLM is told to
    # ground the opener in one concrete detail from here, or acknowledge when
    # research is thin rather than fabricating a generic industry observation.
    research_lines = []
    if is_personal_brand:
        # `company` is actually the person's own name — tell the LLM explicitly
        # so it doesn't write "At OyaAI, we help Anas Azam …" as if Anas were a
        # corporate buyer. Solopreneurs get a peer-to-peer framing instead.
        research_lines.append(
            f"Recipient is a SOLOPRENEUR / PERSONAL BRAND. The 'company' field "
            f"is just their own name ({company}) — they don't work at a "
            f"separate org. Treat the title below as their self-description / "
            f"what they offer. Pitch peer-to-peer, not B2B-enterprise."
        )
    if title:
        research_lines.append(
            f"Their self-description / title{' (this IS their positioning)' if is_personal_brand else ''}: {title}"
        )
    if news_snippet:
        research_lines.append(f"Recent news about {company}: {news_snippet}")
    if overview and not is_personal_brand:
        research_lines.append(f"What {company} does (from their homepage): {overview}")
    if hook and hook not in (news_snippet, overview):
        research_lines.append(f"Hook drafted from the above: {hook}")
    if signal and signal not in research_lines:
        research_lines.append(f"Signal: {signal}")
    research_block = "\n".join(f"- {ln}" for ln in research_lines) or "- (no specific research available — the opener should acknowledge this honestly, not fabricate detail)"

    system = (
        "You write concise, human-sounding cold outreach emails for a B2B SDR. "
        "Your job is to make THIS email feel like it was written only for THIS "
        "recipient — if the pitch paragraph could be sent to any other prospect "
        "unchanged, you have failed.\n\n"
        "Non-negotiable rules:\n"
        "1. PERSONALIZATION IS MANDATORY. Every opener must ground in a specific "
        "detail from the provided research. Never write generic filler like "
        "'you're building something ambitious' or 'teams in your space' or 'most "
        "founders like you' — those lines apply to anyone. If the research is "
        "thin, write a shorter, honest opener ('Came across {company} and "
        "wanted to reach out') rather than padding with industry observations.\n"
        "2. PITCH MUST DIAGNOSE A SPECIFIC PAIN AND OFFER ONE CONCRETE USE CASE "
        "for this recipient. Infer what this specific person probably struggles "
        "with given their role + company + vertical, then offer ONE specific "
        "thing the sender can do to solve it — named concretely (e.g. 'an AI "
        "employee that triages your L1 support tickets' or 'an agent that drafts "
        "RFP responses against your pricing rules'). Do NOT repeat the sender's "
        "generic value proposition verbatim. Do NOT describe the product's "
        "high-level positioning ('consolidates your stack', 'one platform', "
        "'better margins'). Those phrases mean nothing when the recipient has "
        "no idea which one of their problems you would actually solve.\n"
        "3. NEVER fabricate. No invented metrics, results, percentages, "
        "timelines, or customer quotes. If a fact isn't in the research, the "
        "value proposition, or the sender's homepage summary, don't claim it.\n"
        "4. NEVER name-drop customers from the sender's value proposition "
        "unless the recipient is clearly in the SAME vertical as that customer. "
        "Dropping 'we work with JumperMedia, SZ Accounting, and Nafham AI' "
        "into an email to a ServiceNow VP makes the sender look sloppy — "
        "cross-industry references are worse than none. When in doubt, omit.\n"
        "5. If the sender's value proposition is a 'Pain point / Differentiator "
        "/ Social proof' scaffold, treat it as reference material — NOT as "
        "language to paste. Use the sender's homepage summary to understand "
        "what they actually do, then write a prospect-specific pitch.\n"
        "6. No marketing language, no em-dash decoration, no emoji, no stacked "
        "adjectives, no hype ('revolutionary', 'game-changing', 'transforming').\n"
        "7. BANNED PHRASES (because they appear in every lazy cold email and "
        "could target any recipient): 'consolidates X tools into one', "
        "'stitching 5-10 tools', 'tool sprawl', 'one platform', 'real margin', "
        "'lock-in', 'worth a chat', 'hope this finds you well', 'quick question', "
        "'circle back'. If the sender's value prop contains these phrases, "
        "you must REWRITE them into specific, recipient-relevant language — "
        "do not paste them through.\n"
        "8. SOLOPRENEUR HANDLING: when the research block says the recipient "
        "is a solopreneur / personal brand, NEVER write things like 'At OyaAI "
        "we help {FullName} scale their business' — that reads as if their "
        "name were a company. Instead, address them as a peer practitioner: "
        "'You're doing {X from their title}; the bottleneck most people in "
        "that flow hit is Y. Here's the specific thing we do about it: Z.' "
        "Reference their WORK, not a made-up company."
    )

    seller_context = (
        f"What {seller} does (from their homepage): {seller_overview}"
        if seller_overview else
        f"(Sender's homepage summary not available — rely on the value proposition below.)"
    )

    user_prompt = f"""Write the first two paragraphs of a cold email. A call-to-action and signature will be appended after your output — do NOT write them.

Recipient:
- First name: {first_name}
- Company: {company}
- Title: {title or "(unknown)"}

Research on the recipient:
{research_block}

Sender ({seller}):
- {seller_context}
- Value proposition (reference material — may be a rough scaffold; do not paste verbatim): {pitch_raw}
- Signing off as: {signer}

How to write the pitch (paragraph 2):
1. First, SILENTLY reason about what {first_name} specifically struggles with given their role ({title or "unknown"}) at {company} and what the research reveals. What is ONE concrete operational pain they likely feel week-to-week?
2. Then, from what {seller} does, pick ONE specific use case / service / workflow that would solve that exact pain. Name it concretely — don't describe the product category. "An AI employee that X" is better than "a platform that does X-ish things".
3. Write paragraph 2 as: (a) one sentence naming that specific pain in their terms, (b) one sentence offering the one specific thing {seller} would build/do to solve it. That's it. Two sentences.
4. Do NOT write a pitch that could be sent unchanged to a different prospect. If it reads as generic, rewrite.

Output format:
- Return strict JSON: {{"subject": "...", "body": "...", "connection_note": "..."}} and nothing else.
- Subject: 4-8 words, references their company or role specifically, no clickbait, no "Re:".
- Body:
  * Line 1: "Hi {first_name},"
  * Paragraph 1 (opener, 1-2 sentences): anchor in one concrete detail from the Research block. If "(no specific research available…)", write ONE short honest sentence.
  * Paragraph 2 (pitch, 2 sentences max): pain + one specific offer, per the steps above.
- End the body after paragraph 2. Do NOT add a CTA, sign-off, "Thanks", signature block, or disclaimer in the body — those are appended deterministically.
- Total body length: 45-80 words. Shorter and sharper beats padded.
- connection_note: a LinkedIn connection request note that will be sent verbatim. HARD CAP 280 characters (count carefully). Address {first_name} by first name. Anchor in ONE specific detail from the Research block (or, if research is thin, name their work / role honestly). End with a soft connect ask like "would love to connect" or "open to connecting?". NO pitch, NO product mention, NO calendar URL, NO sign-off / "Thanks,". A single short paragraph.
"""

    try:
        r = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5",
                # 1024 not 600 — 600 occasionally truncated mid-JSON on multi-paragraph
                # pitches, causing json.loads to fail and the row to fall through to the
                # deterministic "At Oya.ai, we help agencies stitching 5-10 tools..."
                # fallback template. 1024 is still well under the 80-word body cap.
                "max_tokens": 1024,
                "system": system,
                "messages": [{"role": "user", "content": user_prompt}],
            },
            timeout=25,
        )
        r.raise_for_status()
        data = r.json()
        text = "".join(
            block.get("text", "")
            for block in data.get("content", [])
            if block.get("type") == "text"
        ).strip()
        # Strip any ```json fences the model may add
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S).strip()
        # Lenient JSON parse: strict first, then fall back to extracting the first
        # balanced {...} block. Haiku occasionally prefixes the JSON with a lead-in
        # ("Here's the email:") or embeds it in a longer response — without this
        # extraction those responses were all counted as compose failures.
        parsed = None
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            m = re.search(r"\{.*\}", text, flags=re.S)
            if m:
                try:
                    parsed = json.loads(m.group(0))
                except (json.JSONDecodeError, ValueError):
                    parsed = None
        if not isinstance(parsed, dict):
            return None, None, None
        subject = (parsed.get("subject") or "").strip()
        body_main = (parsed.get("body") or "").strip()
        connection_note = (parsed.get("connection_note") or "").strip()
        if not subject or not body_main:
            return None, None, None
        # Guard against models that ignore instructions and include a sign-off.
        body_main = re.sub(r"(?is)\n+(thanks|best|cheers|regards|sincerely)\b.*$", "", body_main).rstrip()
        # Guard against a stray signature block leaking in.
        body_main = re.sub(r"(?is)\n+-{2,}.*$", "", body_main).rstrip()
        # Connection note: enforce LI's 300-char invite-message cap with a 280
        # safety margin. Strip stray sign-offs the model may add despite the
        # explicit "no sign-off" instruction. Empty connection_note is OK —
        # caller substitutes a deterministic fallback.
        if connection_note:
            connection_note = re.sub(r"(?is)\n+(thanks|best|cheers|regards|sincerely|warmly)\b.*$", "", connection_note).rstrip()
            if len(connection_note) > 280:
                connection_note = connection_note[:280].rstrip()
        return subject, body_main, connection_note
    except Exception:
        return None, None, None


def _sanitize_pitch(pitch):
    """Collapse 'Messaging Angles' / ICP-template scaffolding into one clause.
    Used by the deterministic fallback when no LLM key is available.
    """
    if not pitch:
        return ""
    p = pitch.strip()
    scaffold = re.search(
        r"(?i)(?:pain\s*point|the\s+problem|problem)\s*[:\-—–]\s*"
        r"(.+?)(?=\s*\d+\.\s|\s*(?:differentiator|solution|outcome|social\s*proof)\s*[:\-—–]|$)",
        p,
        flags=re.S,
    )
    if scaffold:
        clause = scaffold.group(1).strip().rstrip(".,;:").strip()
        if clause and clause[0].isupper() and not (len(clause) > 1 and clause[1].isupper()):
            clause = clause[0].lower() + clause[1:]
        p = f"we help {clause}"
    p = re.sub(r"(?i)\bmessaging\s+angles\s*[:.\-—]?\s*", "", p)
    p = re.sub(
        r"(?i)\s*\d+\.\s*\*{0,2}(pain\s*point|differentiator|social\s*proof|problem|solution|outcome)\*{0,2}\s*[:\-—–]\s*",
        " ",
        p,
    )
    p = re.sub(r"(?i)\b(pain\s*point|differentiator|social\s*proof)\s*[:\-—–]\s*", "", p)
    p = re.sub(r"\s+", " ", p).strip()
    if p and not p.endswith((".", "!", "?")):
        p += "."
    return p


def compose(row_dict, my_company, my_value_prop, my_calendar, sender_name, my_site_overview=""):
    """Returns (subject, body, connection_note).

    LLM-first: Claude Haiku composes the personalized opener + pitch paragraph
    AND a 280-char LinkedIn connection note in the same call. The CTA and
    signature block are appended deterministically so the Calendly URL and
    Oya disclosure footer are never dropped by a sloppy model output. The
    connection_note is sent verbatim (no CTA / no signature / no Calendly).

    If ANTHROPIC_API_KEY isn't available or the call fails, falls back to a
    deterministic template for body AND a deterministic connection note built
    from the hook + first_name.
    """
    first_name = (row_dict.get("first_name") or "").strip()
    if not first_name:
        name = (row_dict.get("name") or "").strip()
        first_name = name.split()[0] if name else "there"
    company = (row_dict.get("company") or "").strip() or "your company"
    source = (row_dict.get("source") or "").strip() or "the web"
    signer = (sender_name or "").strip() or "the team"
    seller = (my_company or "").strip() or "our team"
    cta = _build_cta(my_calendar)
    signature = _build_signature(source, seller, company)

    subject, body_main, connection_note = _llm_compose(
        row_dict, my_company, my_value_prop, sender_name,
        my_site_overview=my_site_overview,
    )

    # Deterministic fallback for connection_note when the LLM didn't return one
    # (or when the LLM call failed entirely). Built from the hook so it still
    # references prospect-specific research when available.
    if not connection_note:
        hook_short = (row_dict.get("hook") or "").strip()
        if hook_short:
            # Trim hook to fit "Hi {first_name}, {hook} Would love to connect." under 280
            available = 280 - len(f"Hi {first_name}, ") - len(" Would love to connect.")
            hook_clipped = hook_short[:max(available, 40)].rstrip(" ,.;:")
            connection_note = f"Hi {first_name}, {hook_clipped}. Would love to connect."
        else:
            connection_note = f"Hi {first_name}, came across your work and thought it'd be valuable to connect."
        if len(connection_note) > 280:
            connection_note = connection_note[:280].rstrip()

    if subject and body_main:
        body = f"{body_main}\n\n{cta}\n\nThanks,\n{signer}\n\n{signature}"
        return subject, body, connection_note

    # Deterministic fallback for body — only hits when the LLM call fails.
    hook = (row_dict.get("hook") or "").strip() or f"I came across {company} recently"
    pitch = _sanitize_pitch(my_value_prop) or "we help teams like yours hit their goals."
    subject = f"{first_name} — quick thought for {company}"
    body = (
        f"Hi {first_name},\n\n"
        f"{hook}\n\n"
        f"At {seller}, {pitch} {cta}\n\n"
        f"Thanks,\n{signer}\n\n"
        f"{signature}"
    )
    return subject, body, connection_note


# ---------------------------------------------------------------------------
# Per-row orchestration
# ---------------------------------------------------------------------------


def research_row(row_dict, apollo_key, hunter_key, my_company, my_value_prop, my_calendar, sender_name, findymail_key="", my_site_overview=""):
    """Process one raw row. Mutates row_dict (sets status, signal, hook, email,
    email_subject, email_body, connection_note, skip_reason). Returns the
    outcome bucket: 'queued' / 'skipped' / 'no-email' / 'research-failed'.

    A row queues for outbound if it has a deliverable channel — either a
    verified email (cascade-resolved or already present) OR a linkedin_url
    (LinkedIn connection request path). Only rows with neither end up
    'no-email'.
    """
    try:
        signal, hook, _overview = deep_research(row_dict, apollo_key=apollo_key)
        row_dict["signal"] = signal or row_dict.get("signal", "")
        row_dict["hook"] = hook or row_dict.get("hook", "")
    except Exception as e:
        row_dict["status"] = "research-failed"
        row_dict["skip_reason"] = f"research-error: {str(e)[:150]}"
        return "research-failed"

    passed, reason = quality_gate(row_dict)
    if not passed:
        row_dict["status"] = "skipped"
        row_dict["skip_reason"] = reason
        return "skipped"

    try:
        email, won_step = email_cascade(row_dict, apollo_key, hunter_key, findymail_key=findymail_key)
    except Exception as e:
        row_dict["status"] = "research-failed"
        row_dict["skip_reason"] = f"cascade-error: {str(e)[:150]}"
        return "research-failed"

    has_li_url = bool((row_dict.get("linkedin_url") or "").strip())

    if not email and not has_li_url:
        row_dict["status"] = "no-email"
        row_dict["skip_reason"] = won_step or "cascade-exhausted"
        return "no-email"

    if email:
        row_dict["email"] = email

    try:
        subject, body, connection_note = compose(
            row_dict, my_company, my_value_prop, my_calendar, sender_name,
            my_site_overview=my_site_overview,
        )
        row_dict["email_subject"] = subject
        row_dict["email_body"] = body
        row_dict["connection_note"] = connection_note
    except Exception as e:
        row_dict["status"] = "research-failed"
        row_dict["skip_reason"] = f"compose-error: {str(e)[:150]}"
        return "research-failed"

    row_dict["status"] = "queued"
    # If only the LI channel is available, log it in skip_reason for visibility.
    # Outbound will route based on linkedin_url + email + LI cap; this just
    # surfaces the cascade outcome on rows that queue without an email.
    if not email and has_li_url:
        row_dict["skip_reason"] = f"li-only ({won_step or 'cascade-exhausted'})"
    return "queued"


def _pad(row, n):
    return (row or []) + [""] * max(0, n - len(row or []))


_SHEETS_EPOCH = datetime(1899, 12, 30)


def _date_matches(cell, today_str):
    """True when the date cell represents today_str (YYYY-MM-DD).

    Tolerates legacy rows written before the RAW fix: Google Sheets had
    coerced YYYY-MM-DD via valueInputOption=USER_ENTERED into a date type, so
    on readback those cells come back as locale-formatted strings (M/D/YYYY)
    or — when the column is read unformatted — as Excel serial numbers.
    """
    if cell is None or cell == "":
        return False
    s = str(cell).strip()
    if s == today_str:
        return True
    try:
        serial = float(s)
        if 25569 < serial < 100000:  # plausible date serial range
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
        batch_size = int(inp.get("batch_size") or 10)
    except (TypeError, ValueError):
        batch_size = 10
    today = (inp.get("today") or "").strip() or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    my_company = (inp.get("my_company") or "").strip()
    my_site = (inp.get("my_site") or "").strip()
    my_value_prop = (inp.get("my_value_prop") or "").strip()
    my_calendar = (inp.get("my_calendar") or "").strip()
    # Sanitize sender_name at the entry point so every downstream consumer
    # (compose, LLM prompt, deterministic fallback) sees either a real human
    # name or empty. Existing AI SDR agents were shipped with a routine prompt
    # that passed the PERSONA'S label ("Sales Development Representative") —
    # this strips those so the sign-off reads as "the team" rather than a
    # corporate-title sign-off prospects obviously interpret as a bot.
    sender_name = _sanitize_sender_name(inp.get("sender_name"))

    if not sheet_url:
        return {"error": "sheet_url is required. Load 'Leads Sheet URL:' from agent memory and pass it in."}

    sid = extract_spreadsheet_id(sheet_url)
    if not sid:
        return {"error": f"Could not extract spreadsheet ID from sheet_url={sheet_url!r}"}

    apollo_key = os.environ.get("APOLLO_API_KEY", "").strip()
    hunter_key = os.environ.get("HUNTER_API_KEY", "").strip()
    findymail_key = os.environ.get("FINDYMAIL_API_KEY", "").strip()
    gsheets_json = os.environ.get("GOOGLE_SHEETS_CREDENTIALS_JSON", "").strip()

    missing = [
        k for k, v in (
            ("APOLLO_API_KEY", apollo_key),
            ("HUNTER_API_KEY", hunter_key),
            ("GOOGLE_SHEETS_CREDENTIALS_JSON", gsheets_json),
        ) if not v
    ]
    if missing:
        return {"error": f"Missing credentials: {', '.join(missing)}. Connect the Apollo, Hunter, and Google Sheets gateways."}

    try:
        token = get_access_token(gsheets_json)
    except Exception as e:
        return {"error": f"Google Sheets auth failed: {e}"}

    try:
        rows = sheets_read(token, sid, "Leads!A1:S")
    except Exception as e:
        return {"error": f"Sheet read failed: {e}"}

    # Fetch the seller's homepage once per batch so the LLM compose step knows
    # what THIS company actually does — critical for picking a prospect-specific
    # use case instead of pasting the generic value prop into every email.
    my_site_overview = ""
    if my_site:
        url = my_site if my_site.startswith("http") else f"https://{my_site}"
        try:
            my_site_overview = extract_meta_description(url)
        except Exception:
            my_site_overview = ""

    now_hm = datetime.now(timezone.utc).strftime("%H:%M")

    if not rows or len(rows) < 2:
        return {
            "researched": 0, "queued": 0, "skipped": 0, "no_email": 0, "research_failed": 0,
            "raw_pool_remaining": 0,
            "slack_line": (
                f"*Research batch* · raw pool empty at {now_hm} UTC (sheet has no data rows)\n"
                f"{_slack_sheet_link(sheet_url)}"
            ),
        }

    header = rows[0]
    try:
        col_idx = {c: header.index(c) for c in COLUMNS}
    except ValueError as e:
        return {"error": f"Sheet header missing expected column: {e}"}

    ncols = len(COLUMNS)

    batch = []
    all_data = []
    for i, raw in enumerate(rows[1:], start=2):
        padded = _pad(raw, ncols)
        all_data.append((i, padded))
        if _date_matches(padded[col_idx["date"]], today) and padded[col_idx["status"]] == "raw" and len(batch) < batch_size:
            batch.append((i, padded))

    if not batch:
        raw_remaining = sum(
            1 for _, r in all_data
            if _date_matches(r[col_idx["date"]], today) and r[col_idx["status"]] == "raw"
        )
        return {
            "researched": 0, "queued": 0, "skipped": 0, "no_email": 0, "research_failed": 0,
            "raw_pool_remaining": raw_remaining,
            "slack_line": (
                f"*Research batch* · raw pool empty at {now_hm} UTC\n"
                f"{_slack_sheet_link(sheet_url)}"
            ),
        }

    counts = {"queued": 0, "skipped": 0, "no-email": 0, "research-failed": 0}

    for sheet_row_idx, row_arr in batch:
        row_dict = {c: row_arr[col_idx[c]] for c in COLUMNS}
        outcome = research_row(
            row_dict, apollo_key, hunter_key,
            my_company, my_value_prop, my_calendar, sender_name,
            findymail_key=findymail_key,
            my_site_overview=my_site_overview,
        )
        counts[outcome] = counts.get(outcome, 0) + 1

        # Write back all dirty fields
        for c in COLUMNS:
            row_arr[col_idx[c]] = row_dict.get(c, row_arr[col_idx[c]])
        try:
            sheets_update_row(token, sid, "Leads", sheet_row_idx, row_arr[:ncols])
        except Exception:
            pass  # best-effort — next run sees the raw row again

    # Count remaining raw rows AFTER the batch (we mutated all_data in place via shared refs)
    raw_remaining = sum(
        1 for _, r in all_data
        if _date_matches(r[col_idx["date"]], today) and r[col_idx["status"]] == "raw"
    )

    # Multi-line Slack format: bold heading with headline metric, stat line,
    # remaining-work line, clickable sheet link. Preserves every substring the
    # test suite asserts ("N queued", "N skipped", "N no-email", "N failed",
    # "raw pool remaining: N") so downstream assertions still hold.
    slack_line = (
        f"*Research batch* · {counts['queued']} queued\n"
        f"{counts['skipped']} skipped · {counts['no-email']} no-email · {counts['research-failed']} failed\n"
        f"raw pool remaining: {raw_remaining}\n"
        f"{_slack_sheet_link(sheet_url)}"
    )

    return {
        "researched": sum(counts.values()),
        "queued": counts["queued"],
        "skipped": counts["skipped"],
        "no_email": counts["no-email"],
        "research_failed": counts["research-failed"],
        "raw_pool_remaining": raw_remaining,
        "slack_line": slack_line,
    }


try:
    inp = json.loads(os.environ.get("INPUT_JSON", "{}"))
    result = run(inp)
except Exception as e:
    result = {"error": f"sdr-research-batch crashed: {e}"}

print(json.dumps(result))
