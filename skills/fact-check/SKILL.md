---
name: fact-check
display_name: "Fact Check"
description: "Verify every concrete claim in a draft message against source evidence BEFORE posting (Slack, email, ClickUp, etc.). Catches hallucinated numbers, dates, names, and metrics. Use this any time the LLM is composing a message that includes specific values pulled from a tool result."
category: utility
icon: shield-check
skill_type: sandbox
catalog_type: addon
direct_relay: true
entry_point: script.py
requirements: "anthropic>=0.40"
resource_requirements:
  - env_var: ANTHROPIC_API_KEY
    name: "Anthropic API Key"
    description: "Used by the verifier (Claude Haiku 4.5). Auto-injected on Oya billing; bring your own key to skip platform billing."
    secret: true
    required: true
tool_schema:
  name: fact_check
  description: "Check whether every CONCRETE claim in `draft` (numbers, dates, names, percentages, counts, status statements) is directly supported by `evidence`. Returns {passed, claims, unsupported_claims}. Call this BEFORE any Slack/email post that contains values pulled from a tool result. If passed=false, rewrite the draft using only supported claims and re-check."
  parameters:
    type: object
    properties:
      draft:
        type: string
        description: "The exact message you're about to post (Slack text, email body, etc.). Pass it verbatim — what goes in here is what gets verified."
      evidence:
        type: string
        description: "The raw tool output / source data the draft is supposed to be derived from. Paste the JSON or text returned by the previous tool call. The verifier matches each claim in the draft against this string. Multiple tool outputs can be concatenated with newlines."
      strict:
        type: boolean
        description: "When true (default), even REASONABLE inferences are flagged as unsupported — the claim must be DIRECTLY stated in evidence. When false, the verifier accepts obvious arithmetic/restatement (e.g. summing two listed counts). Use strict for compliance/audit posts; non-strict for status summaries."
        default: true
    required: [draft, evidence]
---
# Fact Check

Compares every concrete claim in a draft message against source evidence using
Claude Haiku 4.5. Designed to gate LLM-generated posts (Slack, email, ClickUp
comments) so hallucinated metrics never leave the agent.

## When to use

Call this BEFORE any tool that posts to a human-facing channel, whenever the
draft includes:

- counts, percentages, ratios, durations
- dates, times, deadlines
- names, emails, IDs, URLs
- status statements ("X completed", "Y failed", "Z is overdue")

If the draft is purely templated text with no values, this skill is unnecessary.

## How it works

The script calls Claude Haiku with a structured prompt: list every concrete
claim in `draft`, then for each, locate the supporting span in `evidence`.
Output is JSON only:

```json
{
  "passed": true | false,
  "claims": [
    {
      "claim": "<exact span from draft>",
      "supported": true | false,
      "evidence_snippet": "<exact span from evidence, or empty>",
      "reason": "<why supported or not>"
    }
  ],
  "unsupported_count": 0,
  "unsupported_claims": [...],
  "summary": "N/M claims supported"
}
```

If `passed` is false, do NOT post. Rewrite the draft using only supported
claims (drop or genericize the rest), then call this skill again. Posting
unsupported claims is the failure mode this skill exists to prevent.

## Cost

One Haiku call per check (~$0.0005). Cheap enough to run on every status
post; the cost of one fabricated metric in a customer-facing channel is
much higher.
