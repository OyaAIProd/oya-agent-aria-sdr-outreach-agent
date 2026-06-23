---
name: hunter
display_name: "Hunter.io"
description: "Find verified business emails by company domain, find a specific person's email, and verify email deliverability"
category: sales
icon: mail
skill_type: sandbox
catalog_type: platform
requirements: "httpx>=0.25"
resource_requirements:
  - env_var: HUNTER_API_KEY
    name: "Hunter.io API Key"
    description: "API key from Hunter.io (Dashboard > API & Integrations > API Keys)"
tool_schema:
  name: hunter
  description: "Find verified business emails by company domain, find a specific person's email, and verify email deliverability"
  parameters:
    type: object
    properties:
      action:
        type: "string"
        description: "Which operation to perform"
        enum: ['domain_search', 'email_finder', 'email_verifier', 'email_count']
      domain:
        type: "string"
        description: "Company domain — for domain_search, email_finder, email_count (e.g. 'stripe.com')"
        default: ""
      first_name:
        type: "string"
        description: "First name — for email_finder (e.g. 'John')"
        default: ""
      last_name:
        type: "string"
        description: "Last name — for email_finder (e.g. 'Smith')"
        default: ""
      email:
        type: "string"
        description: "Email address — for email_verifier"
        default: ""
      seniority:
        type: "string"
        description: "Filter by seniority — for domain_search. Comma-separated: junior, senior, executive"
        default: ""
      department:
        type: "string"
        description: "Filter by department — for domain_search. Comma-separated: executive, it, finance, management, sales, legal, support, hr, marketing, communication, education, design, health, operations"
        default: ""
      limit:
        type: "integer"
        description: "Max results (1-100) — for domain_search"
        default: 10
      offset:
        type: "integer"
        description: "Results offset — for domain_search pagination"
        default: 0
    required: [action]
---
# Hunter.io

Find verified business emails by company domain, locate specific people's emails, and verify email deliverability.

## Recommended Workflow for Lead Outreach
1. Use Apollo **search_people** to find leads with person IDs and company domains.
2. Use Hunter **domain_search** to find all emails at a company domain — faster and cheaper than Apollo enrichment.
3. Use Hunter **email_finder** with first_name + last_name + domain to find a specific person's email.
4. Use Hunter **email_verifier** to check deliverability before sending outreach.

## Be Proactive
- When the user has leads from Apollo with company domains, immediately use domain_search to find emails at those domains.
- When you have a person's name and domain, use email_finder directly — don't ask for confirmation.
- Always verify emails before adding to outreach lists.
- Process multiple domains/emails in sequence without asking permission.

## Actions

### domain_search
Find all email addresses at a company domain. Filter by seniority (executive, senior, junior) and department (sales, marketing, executive, etc.).
```
action: domain_search
domain: "intercom.com"
seniority: "executive,senior"
department: "executive,marketing"
limit: 10
```
Returns: email, first_name, last_name, position, seniority, department, confidence score, verification status.

### email_finder
Find a specific person's email by name + domain.
```
action: email_finder
domain: "intercom.com"
first_name: "John"
last_name: "Smith"
```
Returns: email, confidence score, position, LinkedIn URL, phone number, verification status.

### email_verifier
Verify if an email address is deliverable.
```
action: email_verifier
email: "john@intercom.com"
```
Returns: status (valid/invalid/accept_all/unknown), result (deliverable/undeliverable/risky), confidence score.

### email_count
Quick count of available emails at a domain (free, no credits used).
```
action: email_count
domain: "intercom.com"
```
Returns: total emails, personal vs generic breakdown, count by department and seniority.
