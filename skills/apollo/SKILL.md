---
name: apollo
display_name: "Apollo"
description: "Search for leads and enrich company data using Apollo.io — find companies, domains, LinkedIn profiles, phone numbers, and industry info"
category: sales
icon: search
skill_type: sandbox
catalog_type: platform
requirements: "httpx>=0.25"
resource_requirements:
  - env_var: APOLLO_API_KEY
    name: "Apollo API Key"
    description: "Master API key from Apollo.io (Settings > Integrations > API Keys). Must be a Master key, not a restricted key."
tool_schema:
  name: apollo
  description: "Search for leads and enrich company data using Apollo.io — find companies, domains, LinkedIn profiles, phone numbers, and industry info"
  parameters:
    type: object
    properties:
      action:
        type: "string"
        description: "Which operation to perform"
        enum: ['search_people', 'search_organizations', 'enrich_person', 'enrich_organization']
      person_titles:
        type: "string"
        description: "Comma-separated job titles to search for — for search_people (e.g. 'CTO, VP Engineering, Head of Engineering')"
        default: ""
      person_locations:
        type: "string"
        description: "Comma-separated locations — for search_people (e.g. 'United States, California')"
        default: ""
      organization_domains:
        type: "string"
        description: "Comma-separated company domains — for search_people (e.g. 'openai.com, anthropic.com')"
        default: ""
      organization_name:
        type: "string"
        description: "Company name — for search_organizations"
        default: ""
      locations:
        type: "string"
        description: "Comma-separated company locations — for search_organizations (e.g. 'United States', 'California, United States', 'New York')"
        default: ""
      organization_num_employees_ranges:
        type: "string"
        description: "Semicolon-separated employee count ranges — for search_people, search_organizations (e.g. '1,10;11,50;51,200'). Each range is min,max."
        default: ""
      keywords:
        type: "string"
        description: "Keyword search — for search_people (in bio/title) and search_organizations (in company description/industry)"
        default: ""
      email:
        type: "string"
        description: "Email address — for enrich_person"
        default: ""
      linkedin_url:
        type: "string"
        description: "LinkedIn profile URL — for enrich_person"
        default: ""
      first_name:
        type: "string"
        description: "First name — for enrich_person (use with last_name + organization_name or domain)"
        default: ""
      last_name:
        type: "string"
        description: "Last name — for enrich_person (use with first_name + organization_name or domain)"
        default: ""
      person_id:
        type: "string"
        description: "Apollo person ID from search_people results — for enrich_person. This is the best way to enrich a person."
        default: ""
      domain:
        type: "string"
        description: "Company domain — for enrich_organization and enrich_person (e.g. 'openai.com')"
        default: ""
      organization_name:
        type: "string"
        description: "Company name — for enrich_person (use with first_name + last_name)"
        default: ""
      page:
        type: "integer"
        description: "Page number (1-based) — for search_people, search_organizations"
        default: 1
      per_page:
        type: "integer"
        description: "Results per page (max 25) — for search_people, search_organizations"
        default: 10
    required: [action]
---
# Apollo

Search for leads and enrich company data using Apollo.io.

## Recommended Workflow for Lead Generation
1. **search_people** with `keywords`, `person_titles`, `person_locations`, `organization_num_employees_ranges` to find people at matching companies — returns person IDs and company domains.
2. **If the Hunter tool is available:** use Hunter `email_finder` with first_name + last_name + domain to find emails (cheaper and faster than Apollo enrich). Use Hunter `email_verifier` to verify before outreach.
3. **If Hunter is NOT available:** use Apollo `enrich_person` with `person_id` from search results to get emails.
4. Optionally use **search_organizations** to find companies first, then **enrich_organization** for deeper details.

IMPORTANT: Apollo search results are previews — they do NOT include emails. You MUST use Hunter (preferred) or Apollo enrich_person to get email addresses.

## When to use Apollo vs Hunter
- **Apollo**: searching for leads (search_people, search_organizations), enriching company data (enrich_organization), getting full person profile when Hunter has no result.
- **Hunter**: finding emails (email_finder), verifying emails (email_verifier), finding all emails at a domain (domain_search). Hunter is specialized for email discovery and is more accurate for this purpose.
- **Rule**: If you have a person's name and company domain, try Hunter email_finder FIRST. Only fall back to Apollo enrich_person if Hunter returns no result.

## Be Proactive
- When the user asks for "leads" or "contacts" or "emails", immediately search and enrich — do NOT ask for clarification on titles. Use common decision-maker titles: CEO, Founder, Owner, President, CMO, VP of Marketing, Director of Marketing, Head of Growth.
- When the user says "all" for titles, use the common titles above.
- After search_people returns results, IMMEDIATELY use Hunter email_finder (if available) or Apollo enrich_person for each person to get their email. Do not stop after search — the user wants contact details.
- Process multiple enrichments in sequence without asking permission.

## Company Search & Enrichment
- **search_organizations** — Search Apollo's database of companies. Filter by `organization_name`, `keywords`, `locations`, `organization_num_employees_ranges`. Returns: company name, website domain, LinkedIn URL, phone, revenue.
- **enrich_organization** — Get full details for a company by `domain`. Returns: all company data plus industry, employee count, founded year, description, technologies, social URLs.

## People Search & Enrichment
- **search_people** — Search for people by `person_titles`, `person_locations`, `organization_domains`, `organization_num_employees_ranges`, `keywords`. Returns names and titles (emails may be obfuscated — use enrich_person for full data).
- **enrich_person** — Get full contact details by `person_id` (from search_people), `email`, `linkedin_url`, or `first_name`+`last_name`+`organization_name`/`domain`. Returns: verified email, phone, title, company, LinkedIn. Requires a Master API key.

## Example: Full lead generation flow
```
Step 1: action: search_people
        person_titles: "CEO,Founder,Owner,VP of Marketing"
        keywords: "digital marketing agency"
        person_locations: "United States"
        organization_num_employees_ranges: "11,50"
        per_page: 10

Step 2: For each person in results, call:
        action: enrich_person
        person_id: "<id from search results>"

This gives you: full name, verified email, phone, LinkedIn, title, company.
```

## Example: Enrich a company for outreach
```
action: enrich_organization
domain: "webfx.com"
```

## Example: Find a person's contact info
```
action: enrich_person
first_name: "John"
last_name: "Smith"
domain: "webfx.com"
```
