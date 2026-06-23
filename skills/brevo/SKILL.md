---
name: brevo
display_name: "Brevo"
description: "Send personalized transactional emails, manage contacts, and track engagement (opens, clicks, bounces, replies) via Brevo"
category: sales
icon: send
skill_type: sandbox
catalog_type: platform
requirements: "httpx>=0.25"
resource_requirements:
  - env_var: BREVO_API_KEY
    name: "Brevo API Key"
    description: "API key from Brevo (SMTP & API > API Keys > Generate a new API key)"
  - env_var: BREVO_SENDER_EMAIL
    name: "Sender email"
    description: "Verified sender email in Brevo (must be confirmed under Senders & IP > Senders before sending)"
  - env_var: BREVO_SENDER_NAME
    name: "Sender name"
    description: "Display name on outbound emails (e.g. 'Jane from Oya')"
tool_schema:
  name: brevo
  description: "Send personalized transactional emails, manage contacts, and track engagement events via Brevo"
  parameters:
    type: object
    properties:
      action:
        type: "string"
        description: "Which operation to perform"
        enum: ['send_email', 'get_email_events', 'create_contact', 'add_contact_to_list', 'list_contact_lists']
      to_email:
        type: "string"
        description: "Recipient email — for send_email, get_email_events"
        default: ""
      to_name:
        type: "string"
        description: "Recipient name — for send_email (optional, improves deliverability)"
        default: ""
      subject:
        type: "string"
        description: "Email subject — for send_email. Keep under 50 chars for cold outreach."
        default: ""
      html_body:
        type: "string"
        description: "HTML email body — for send_email. Use this OR text_body. Wrap paragraphs in <p> tags."
        default: ""
      text_body:
        type: "string"
        description: "Plain text email body — for send_email. Use this OR html_body. Plain text has better deliverability for cold outreach."
        default: ""
      reply_to:
        type: "string"
        description: "Reply-to email — for send_email. Defaults to the sender email. Set this if the user should receive replies at a different inbox."
        default: ""
      tags:
        type: "string"
        description: "Comma-separated tags for segmentation and analytics — for send_email (e.g. 'sdr,touch-1,series-b-signal')"
        default: ""
      event_type:
        type: "string"
        description: "Filter events by type — for get_email_events. One of: sent, delivered, opened, clicks, bounced, spam, unsubscribed"
        default: ""
      days:
        type: "integer"
        description: "How many days back to look — for get_email_events"
        default: 7
      email:
        type: "string"
        description: "Contact email — for create_contact, add_contact_to_list"
        default: ""
      first_name:
        type: "string"
        description: "Contact first name — for create_contact"
        default: ""
      last_name:
        type: "string"
        description: "Contact last name — for create_contact"
        default: ""
      attributes_json:
        type: "string"
        description: "JSON object of custom contact attributes — for create_contact. Use UPPERCASE keys per Brevo convention (e.g. '{\"COMPANY\":\"Acme\",\"ROLE\":\"CTO\",\"INDUSTRY\":\"SaaS\"}')"
        default: ""
      list_id:
        type: "integer"
        description: "Brevo list ID — for add_contact_to_list. Discover IDs via list_contact_lists."
        default: 0
      limit:
        type: "integer"
        description: "Max results — for list_contact_lists, get_email_events"
        default: 50
    required: [action]
---
# Brevo

Send personalized transactional emails, manage contacts, and track engagement via Brevo (formerly Sendinblue).

## Why Brevo for cold outreach

- **Deliverability:** dedicated IPs, DKIM/SPF handled, established sending reputation
- **Per-email tracking:** opens, clicks, bounces as structured events — not IMAP scraping
- **Tags:** attach arbitrary labels to each send (`sequence-id`, `touch-number`, `icp-segment`) for clean analytics
- **Scale:** 300/day on free tier, 20k+/day on paid plans

## Recommended Workflow for AI SDR

1. Find and verify leads (Apollo + Hunter).
2. Call **create_contact** to add each lead with custom attributes (company, role, industry, trigger signal).
3. Call **send_email** with a fully personalized subject + body. Always include `tags` like `sdr,touch-1,<YYYY-MM-DD>` for analytics.
4. Every few hours, call **get_email_events** to detect opens, clicks, and bounces before replies land.
5. Use **add_contact_to_list** to move leads between sequence stages (cold → touched → replied → booked).

## Be Proactive

- When you have verified leads, create their contacts in Brevo immediately — before sending, not after.
- Every outbound send MUST include `tags` — without them, analytics are useless.
- Check `get_email_events` every 3 hours during business hours to surface engagement signals.
- If an email bounces (event `bounced` or `spam`), stop sending to that address and flag the lead as invalid in your Sheet.
- Do not send the same lead the same subject line twice — check prior events first.

## Actions

### send_email
Send a single personalized transactional email.
```
action: send_email
to_email: "sarah@acme.com"
to_name: "Sarah Lee"
subject: "Noticed Acme just raised Series B"
html_body: "<p>Hi Sarah,</p><p>Congrats on the $25M round — saw the TechCrunch piece.</p><p>We help Series B founders automate their SDR pipeline. Worth a 15-min look?</p><p>Two slots that work: Thu 2pm PT, Fri 10am PT. Or book whatever suits: https://cal.com/oya/sdr-intro</p>"
tags: "sdr,touch-1,series-b-signal"
```
Returns: `{message_id, status, to, subject}`. Save `message_id` to your Sheet for later reply correlation.

### get_email_events
Query recent engagement events (opens, clicks, bounces, etc.). Filter by recipient, type, and date range.
```
action: get_email_events
to_email: "sarah@acme.com"
event_type: "opened"
days: 7
```
Returns: `{events: [{date, email, event, subject, message_id, tags}], total}`. The `event` field is one of: sent, delivered, opened, clicks, bounced, spam, unsubscribed.

### create_contact
Create or update a contact in Brevo. Idempotent — safe to call for existing emails.
```
action: create_contact
email: "sarah@acme.com"
first_name: "Sarah"
last_name: "Lee"
attributes_json: "{\"COMPANY\":\"Acme\",\"ROLE\":\"CTO\",\"INDUSTRY\":\"SaaS\",\"SIGNAL\":\"series-b-apr-2026\"}"
```
Returns: `{email, contact_id, status}`.

### add_contact_to_list
Add an existing contact to a list by list ID. Use this to track sequence stages.
```
action: add_contact_to_list
email: "sarah@acme.com"
list_id: 42
```

### list_contact_lists
List all contact lists in the account. Use this once to discover the IDs of your sequence lists.
```
action: list_contact_lists
limit: 50
```
Returns: `{lists: [{id, name, total_subscribers, total_blacklisted}], total}`.
