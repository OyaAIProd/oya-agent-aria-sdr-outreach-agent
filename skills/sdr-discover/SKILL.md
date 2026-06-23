---
name: sdr-discover
display_name: "SDR Discover"
description: "Pulls raw candidate leads from Instantly SuperSearch, Apollo (paid /people/match enrichment unlocks emails inline), and LinkedIn (Unipile) — paginated, deduplicated, batch-appended to the Leads sheet. Bootstraps the sheet on first-ever run. Stops early once today's emailed-row count reaches the daily target (default 300)."
category: sales
icon: search
skill_type: sandbox
catalog_type: platform
requirements: "httpx>=0.25"
resource_requirements:
  - env_var: INSTANTLY_API_KEY
    name: "Instantly API Key"
    description: "From the Instantly gateway. Primary discovery source — SuperSearch preview is free of credit cost; emails are resolved downstream by the Hunter cascade."
  - env_var: APOLLO_API_KEY
    name: "Apollo API Key"
    description: "Optional. From the Apollo gateway. On a paid Apollo plan, /people/match enrichment unlocks emails + linkedin_url inline (1 unlock credit per row); search alone returns obfuscated previews on every tier."
    optional: true
  - env_var: GOOGLE_SHEETS_CREDENTIALS_JSON
    name: "Google Sheets OAuth"
    description: "From the Google Sheets gateway. Required — this is where raw candidates get written and also used for cross-run dedup."
  - env_var: UNIPILE_ACCOUNT_ID
    name: "LinkedIn (Unipile) Account"
    description: "Optional. From the LinkedIn gateway. Enables LinkedIn search as a third discovery source."
    optional: true
  - env_var: UNIPILE_DSN
    name: "Unipile DSN"
    description: "Optional. Companion to UNIPILE_ACCOUNT_ID."
    optional: true
  - env_var: UNIPILE_API_KEY
    name: "Unipile API Key"
    description: "Optional. Companion to UNIPILE_ACCOUNT_ID."
    optional: true
tool_schema:
  name: sdr_discover
  description: "Run multi-source discovery (Instantly SuperSearch + Apollo with /people/match enrichment + LinkedIn via Unipile). Paginates each source in Python, batch-appends per source to avoid mid-write timeouts, deduplicates against existing sheet rows. Stops early when today's emailed-row count reaches `target_emailed`. Returns a ready-to-post Slack summary and the sheet URL (which the calling routine persists to memory on first ever run)."
  parameters:
    type: object
    properties:
      icp:
        type: string
        description: "Full ICP description from memory's 'Ideal Customer Profile:' fact. Used as keyword filter when role_label is empty. Required."
        default: ""
      icp_role_label:
        type: string
        description: "Strict role label (e.g. 'LinkedIn ghostwriters', 'Founder'). When set, Instantly uses it as `title.include` (drops loose keyword_filter), and Apollo uses it as the FIRST `person_titles` slice + drops `q_keywords` (Apollo's q_keywords does strict-phrase matching that kills niche ICPs). When empty, both fall back to broad seniority/keyword filters and quality drops sharply for niche ICPs. Always pass this from memory's 'ICP Role Label:' fact."
        default: ""
      sheet_url:
        type: string
        description: "Existing 'Leads Sheet URL:' from memory, if any. Leave empty on first-ever run — the skill will create the sheet and return the URL in `created_sheet_url`."
        default: ""
      today:
        type: string
        description: "YYYY-MM-DD to stamp new rows with. Empty = UTC today."
        default: ""
      target_emailed:
        type: integer
        description: "Daily emailed-row goal. Source loop stops early once today's count of rows-with-emails reaches this. Apollo enrichment writes emails inline; Instantly + LinkedIn rows accumulate emails via the downstream research-batch cascade."
        default: 300
      target_instantly:
        type: integer
        description: "Per-run target for Instantly SuperSearch."
        default: 200
      target_apollo:
        type: integer
        description: "Per-run target for Apollo. Pagination stops when this is reached or the API returns fewer than a page."
        default: 200
      target_linkedin:
        type: integer
        description: "Per-run LinkedIn target (when Unipile creds are set)."
        default: 100
    required: [icp]
---

# SDR Discover

Multi-source discovery: Instantly SuperSearch (preview, free of credit cost) → Apollo (search + `/people/match` enrichment to unlock emails on paid plans) → LinkedIn (Unipile search, no emails — feeds the LinkedIn-first outbound channel + downstream cascade).

## First-ever run

If `sheet_url` is empty, the skill:
1. Calls Google Sheets to create a spreadsheet titled "Oya AI SDR — Leads" with sheet "Leads"
2. Appends the 19-column header row (`date, name, first_name, last_name, email, company, title, linkedin_url, signal, source, hook, email_subject, email_body, status, message_id, skip_reason, sent_at, channel, connection_note`)
3. Returns the new URL in `created_sheet_url` so the routine writes `Leads Sheet URL: <url>` back into agent memory

Subsequent runs pass the existing URL and reuse it.

## Per source

| Source | Auth | Strategy | Target |
| --- | --- | --- | --- |
| Instantly | INSTANTLY_API_KEY | When `icp_role_label` is set: `search_filters.title.include = ["<plural>", "<singular>"]` — strict, returns only people whose title contains the role. Without role_label: `keyword_filter.include = icp` (loose, used for broad ICPs). Multiple calls with `level` slices to beat the single-call cap. | 200 |
| Apollo | APOLLO_API_KEY | `/mixed_people/api_search` with `person_titles=[role_label]` and `person_email_status=["verified"]`. q_keywords is dropped when role_label is set (Apollo's q_keywords does strict-phrase matching). Each search hit is enriched via `/people/match` (1 unlock credit per row) to populate the actual email + LinkedIn URL + de-obfuscated last_name. Rows whose enrichment returns no email are dropped. | 200 |
| LinkedIn (Unipile) | UNIPILE_ACCOUNT_ID + DSN + API_KEY | 4+ `search` calls via Unipile; skipped if any cred missing. Returns name + linkedin_url + title; emails come from the downstream cascade. | 100 |

## Daily emailed-row target

After loading the sheet, counts today's rows where `email` is non-empty. If that count ≥ `target_emailed` (default 300), the skill returns immediately with a "target met" slack_line — safe to call multiple times per day on a cron without doubling work. Between sources, the same check fires; once the target is hit, later sources are skipped with status `"skipped: target met (N/300)"`.

## Deduplication

Before appending, the skill reads all existing sheet rows and builds a dedup set keyed on both `email.lower()` (when present) AND `normalized_name_plus_company`. Candidates already in the set are skipped. Append is batched per source — one `append_rows` call per source with all its surviving candidates.

## Return shape

```json
{
  "created_sheet_url": "",
  "sheet_url": "https://docs.google.com/...",
  "per_source": {
    "instantly": {"pulled": 50, "committed": 47, "status": "ok"},
    "apollo": {"pulled": 25, "committed": 25, "status": "ok"},
    "linkedin": {"pulled": 18, "committed": 18, "status": "ok"}
  },
  "total_committed": 90,
  "dedup_skipped": 3,
  "emailed_today": 72,
  "target_emailed": 300,
  "slack_line": "*Daily Discovery* · 90 raw candidates committed\nInstantly=47 · Apollo=25 · Linkedin=18\n72/300 emailed today (in progress) · 3 duplicates skipped\n<https://docs.google.com/...|Leads sheet>"
}
```

If `total_committed < 100`, the slack_line gets a `⚠️ Low yield —` prefix so the user knows something's off (bad ICP, all providers down, etc.). When the daily target is hit, the slack_line opens with `*Daily Discovery* · target met (N/300 emailed today)`.
