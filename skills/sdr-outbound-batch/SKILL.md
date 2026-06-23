---
name: sdr-outbound-batch
display_name: "SDR Outbound Batch"
description: "Pulls queued leads from the Leads sheet and sends them via Brevo — one bounded batch per run. Pure-Python control flow: the LLM calls this once and gets a Slack-ready summary back."
category: sales
icon: send
skill_type: sandbox
catalog_type: platform
requirements: "httpx>=0.25"
resource_requirements:
  - env_var: BREVO_API_KEY
    name: "Brevo API Key"
    description: "From the Brevo gateway. Required."
  - env_var: BREVO_SENDER_EMAIL
    name: "Brevo Sender Email"
    description: "Verified sender on Brevo. Required."
  - env_var: BREVO_SENDER_NAME
    name: "Brevo Sender Name"
    description: "Display name on outbound mail."
    optional: true
  - env_var: GOOGLE_SHEETS_CREDENTIALS_JSON
    name: "Google Sheets OAuth"
    description: "From the Google Sheets gateway. Required — this is how the skill reads the queue and writes sent/failed status back."
tool_schema:
  name: sdr_outbound_batch
  description: "Drain up to batch_size queued leads from the Leads sheet via Brevo. Deterministic loop — the skill itself handles row selection, contact creation, sending, daily-cap detection, and sheet writebacks. Returns a ready-to-post Slack line and numeric counts. Call this once per routine run."
  parameters:
    type: object
    properties:
      sheet_url:
        type: string
        description: "Full Google Sheets URL from memory's 'Leads Sheet URL:' fact. Required."
        default: ""
      batch_size:
        type: integer
        description: "Max queued rows to process this run. Default 15 — tuned to finish inside the 15-min completion timeout with a safety margin."
        default: 15
      today:
        type: string
        description: "YYYY-MM-DD filter for which leads are 'today's'. Empty/omitted = UTC today."
        default: ""
    required: [sheet_url]
---

# SDR Outbound Batch

Drains the `status="queued"` rows of the Leads sheet via Brevo. Each invocation processes up to `batch_size` rows, flips each to `sent` / `send-failed`, halts cleanly on Brevo's daily cap, and returns a single Slack-ready summary line.

## Expected sheet layout

Sheet tab name: `Leads`. Columns (row 1 is the header):

`date | name | first_name | last_name | email | company | title | linkedin_url | signal | source | hook | email_subject | email_body | status | message_id | skip_reason | sent_at`

Daily Lead Search writes rows with `status="raw"`. Research Batch flips them to `queued` (filling subject + body). This skill flips queued → sent / send-failed.

## Return shape

```json
{
  "sent": 12,
  "failed": 1,
  "remaining_queued": 47,
  "cumulative_sent_today": 124,
  "brevo_cap_hit": false,
  "milestone": 100,
  "slack_line": "🎯 Milestone 100/300 — Outbound batch: 12 sent / 1 failed / 47 still queued. Sheet: https://..."
}
```

`milestone` is non-null only when the run crossed 50/100/150/200/250/300 cumulative sent-today rows. When `brevo_cap_hit=true`, the slack_line names the cap and the carry-over count.
