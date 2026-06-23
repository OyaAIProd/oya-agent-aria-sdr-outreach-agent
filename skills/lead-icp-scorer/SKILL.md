---
name: lead-icp-scorer
display_name: "Lead ICP Scorer"
description: "Scores and ranks raw leads against a stored ICP definition, returning only qualified leads (score ≥ 50) with fit-reason strings."
category: sales
icon: target
skill_type: sandbox
catalog_type: addon
tool_schema:
  name: lead_icp_scorer
  description: "Score and rank a list of raw leads against an ICP definition. Drops leads scoring below 50. Returns a sorted array of qualified leads with scores and fit-reason strings."
  parameters:
    type: object
    properties:
      leads:
        type: array
        description: "Array of raw lead objects to score."
        items:
          type: object
          properties:
            name:
              type: string
              description: "Full name of the lead."
            company:
              type: string
              description: "Company the lead works at."
            title:
              type: string
              description: "Job title of the lead."
            domain:
              type: string
              description: "Company domain (e.g. 'acme.com')."
            signals:
              type: array
              description: "List of intent signal strings (e.g. ['visited pricing page', 'downloaded whitepaper'])."
              items:
                type: string
      icp:
        type: object
        description: "ICP definition object used to score leads."
        properties:
          industries:
            type: array
            description: "Target industries (e.g. ['SaaS', 'FinTech'])."
            items:
              type: string
          seniority_levels:
            type: array
            description: "Target seniority levels (e.g. ['VP', 'Director', 'C-Level'])."
            items:
              type: string
          seniority_keywords:
            type: array
            description: "Keywords in job titles that indicate target seniority (e.g. ['VP', 'Head', 'Chief', 'Director', 'President'])."
            items:
              type: string
          company_size:
            type: object
            description: "Target company size range by employee count."
            properties:
              min:
                type: integer
                description: "Minimum number of employees."
              max:
                type: integer
                description: "Maximum number of employees (-1 for unlimited)."
          target_domains:
            type: array
            description: "Optional list of specific target domains."
            items:
              type: string
          high_intent_signals:
            type: array
            description: "Signal strings that indicate strong buying intent."
            items:
              type: string
          medium_intent_signals:
            type: array
            description: "Signal strings that indicate moderate buying intent."
            items:
              type: string
          company_size_estimate:
            type: object
            description: "Map of domain keywords or company name fragments to estimated employee counts."
            additionalProperties:
              type: integer
        required: [industries, seniority_keywords, high_intent_signals, medium_intent_signals]
    required: [leads, icp]
---
# Lead ICP Scorer
Score and rank raw leads against your Ideal Customer Profile, returning only qualified leads with scores and fit-reason explanations.

## Be Proactive
When the user has a batch of new leads and an ICP definition and wants to know which leads to prioritize, call this skill immediately to produce a ranked, qualified shortlist.