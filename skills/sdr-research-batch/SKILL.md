---
name: sdr-research-batch
display_name: "SDR Research Batch"
description: "Processes raw discovery candidates into send-ready queued leads. Deep-research + 7-step email cascade + email composition — all pure-Python control flow, bounded per run."
category: sales
icon: search
skill_type: sandbox
catalog_type: platform
requirements: "httpx>=0.25"
resource_requirements:
  - env_var: APOLLO_API_KEY
    name: "Apollo API Key"
    description: "From the Apollo gateway. Used for enrich_person (step b of the email cascade) and optional company enrichment."
  - env_var: HUNTER_API_KEY
    name: "Hunter API Key"
    description: "From the Hunter gateway. Drives steps c–e of the email cascade: email_finder, domain_search, email_verifier."
  - env_var: GOOGLE_SHEETS_CREDENTIALS_JSON
    name: "Google Sheets OAuth"
    description: "From the Google Sheets gateway. The skill reads raw rows and writes back queued / skipped / no-email / research-failed."
tool_schema:
  name: sdr_research_batch
  description: "Convert up to batch_size raw discovery rows into fully-researched, verified, pre-drafted queued leads ready for Outbound Batch. Deterministic pipeline — the LLM calls this once per routine fire and posts the returned slack_line."
  parameters:
    type: object
    properties:
      sheet_url:
        type: string
        description: "Full Google Sheets URL from memory's 'Leads Sheet URL:' fact. Required."
        default: ""
      batch_size:
        type: integer
        description: "Max raw rows to process this run. Default 10 — tuned to finish inside the 15-min completion timeout with a safety margin."
        default: 10
      today:
        type: string
        description: "YYYY-MM-DD filter. Empty = UTC today."
        default: ""
      my_company:
        type: string
        description: "Sender's company name. From memory's 'Company Name:' fact. Used in the email signature."
        default: ""
      my_site:
        type: string
        description: "Sender's product/website. From memory's 'Company Website:' fact."
        default: ""
      my_value_prop:
        type: string
        description: "Sender's one-sentence value proposition. From memory's 'Value Proposition:' fact. Used as the email's core pitch."
        default: ""
      my_calendar:
        type: string
        description: "Optional calendar / meeting link. From memory's 'Calendar / Meeting Link:' fact. If present, rendered as an inline URL in the CTA."
        default: ""
      sender_name:
        type: string
        description: "Name to sign emails with. Defaults to persona name if empty."
        default: ""
    required: [sheet_url]
---

# SDR Research Batch

Converts `status="raw"` rows into `status="queued"` rows by running deep research, the 7-step email cascade, and email composition. Bounded batch (default 10 rows / run) to fit inside the 15-minute completion timeout.

## Email cascade (in order, stop at first verified email)

1. **Verify existing** — if the raw row already has an email (from Apollo discovery), verify via `hunter.email_verifier`.
2. **Apollo enrich_person** — different DB than Hunter; often wins where Hunter misses.
3. **Hunter email_finder** — primary finder.
4. **Hunter domain_search** — list all emails at the company domain, match by name with nickname equivalence (Bob↔Robert, Mike↔Michael, …).
5. **Pattern + verifier** — generate `first.last@`, `firstlast@`, `first@`, `flast@`, `f.last@`, `last@`, `last.first@`; verify each; take the first `deliverable`.
6. **Web search (DuckDuckGo)** — `"<first last>" "<company>" email` and scan results for any `@<domain>` string.
7. **Site scrape** — fetch `<domain>/team`, `<domain>/about`, `<domain>/leadership`, `<domain>/contact`, `<domain>/people`; extract any email mentioning the lead's name.

If all 7 fail, the row is marked `status="no-email"` with `skip_reason="cascade-exhausted"`.

## Quality gate

Before sending a row to Compose, require: (a) first name, (b) company, (c) a personalization hook. If any is missing, row is marked `status="skipped"` with `skip_reason="weak-research"`.

## Compose

LLM-first: Claude Haiku writes the personalized opener + pitch paragraph from the researched row (first_name, company, title, hook) and the sender config (company, value prop, signer). The deterministic CTA (with the Calendly URL when set) and the Oya disclosure signature are appended in Python after the LLM output, so those critical parts are never dropped or mangled by a sloppy response. A pitch sanitizer collapses 'Messaging Angles' / ICP-template scaffolding into one clause before it reaches the prompt. If `ANTHROPIC_API_KEY` isn't injected into the sandbox or the call fails, the skill falls back to a sanitized deterministic template.

## Expected sheet layout

Row columns match Daily Lead Search and Outbound Batch (17 columns). This skill reads rows with `status="raw"` for `today`, writes the following fields on update: `signal`, `hook`, `email`, `email_subject`, `email_body`, `status`, `skip_reason`.

## Return shape

```json
{
  "researched": 10,
  "queued": 6,
  "skipped": 2,
  "no_email": 1,
  "research_failed": 1,
  "raw_pool_remaining": 42,
  "slack_line": "Research batch: 6 queued / 2 skipped / 1 no-email / 1 failed — raw pool remaining: 42. Sheet: https://..."
}
```
