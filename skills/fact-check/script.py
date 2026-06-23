"""Fact-check a draft message against source evidence.

Reads {draft, evidence, strict?} from INPUT_JSON, calls Claude Haiku once
to enumerate the concrete claims in the draft and locate each in the
evidence, returns a structured pass/fail result.

`direct_relay: true` so the verifier's JSON reaches the parent agent
verbatim — the wrap_with_standalone_llm wrapper is exactly the kind of
LLM hop that has hallucinated tool results in the past, and this skill
exists to catch those hallucinations rather than introduce another one.

Note: no `from __future__ import annotations` here on purpose — the
sandbox executor prepends env-injection statements before single-file
scripts, which would put the future-import after real statements and
SyntaxError. The executor now hoists future imports, but this skill
ships without one to keep it portable to older runners.
"""
import json
import os
import re
import sys


_MODEL = "claude-haiku-4-5-20251001"
_MAX_DRAFT = 8000   # chars
_MAX_EVIDENCE = 60000  # chars — Haiku 4.5 has plenty of context but bound it


def _emit(payload: dict, exit_code: int | None = None) -> None:
    print(json.dumps(payload, ensure_ascii=False))
    sys.exit(exit_code if exit_code is not None else (0 if payload.get("passed") else 1))


def _strip_code_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        # ```json\n...\n``` or ```\n...\n```
        m = re.match(r"^```(?:json)?\s*\n(.*?)\n```\s*$", t, re.DOTALL)
        if m:
            return m.group(1).strip()
    return t


def _build_prompt(draft: str, evidence: str, strict: bool) -> str:
    strict_clause = (
        "STRICT MODE: flag any claim that requires inference, even reasonable arithmetic — "
        "the value must appear DIRECTLY in evidence."
        if strict else
        "RELAXED MODE: accept obvious restatements (e.g. summing two listed counts, "
        "trivial unit conversion). Still flag anything that materially differs from evidence."
    )
    return f"""You are a strict fact-checker. List every CONCRETE CLAIM in the DRAFT — \
numbers, percentages, counts, durations, dates, times, names, emails, IDs, URLs, \
status statements (e.g. "X completed", "Y failed", "Z is overdue").

For each claim, find a supporting span in the EVIDENCE.

{strict_clause}

Mark `supported = false` when:
- the value appears nowhere in evidence
- evidence contradicts the draft
- the claim is an inference the evidence doesn't directly state (and STRICT MODE is on)

Output ONLY this JSON (no prose, no code fence):
{{
  "claims": [
    {{
      "claim": "<exact span from draft>",
      "supported": true,
      "evidence_snippet": "<exact span from evidence>",
      "reason": "<one sentence>"
    }}
  ]
}}

If the DRAFT contains no concrete claims (pure templated text), return {{"claims": []}}.

DRAFT:
{draft}

EVIDENCE:
{evidence}"""


def main() -> None:
    inp = json.loads(os.environ.get("INPUT_JSON") or "{}")
    draft = (inp.get("draft") or "").strip()
    evidence = (inp.get("evidence") or "").strip()
    strict = bool(inp.get("strict", True))

    if not draft:
        _emit({"ok": False, "passed": False, "error": "draft_required"}, exit_code=1)
    if not evidence:
        _emit({"ok": False, "passed": False, "error": "evidence_required",
               "detail": "Pass the raw tool output the draft is derived from."}, exit_code=1)
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        _emit({"ok": False, "passed": False, "error": "no_anthropic_key"}, exit_code=1)

    # Hard cap input sizes to stay well under context limits and keep latency low.
    if len(draft) > _MAX_DRAFT:
        draft = draft[:_MAX_DRAFT] + "\n...(draft truncated)"
    if len(evidence) > _MAX_EVIDENCE:
        evidence = evidence[:_MAX_EVIDENCE] + "\n...(evidence truncated)"

    import anthropic  # type: ignore[import-not-found]
    client = anthropic.Anthropic(api_key=api_key)
    try:
        resp = client.messages.create(
            model=_MODEL,
            max_tokens=2000,
            messages=[{"role": "user", "content": _build_prompt(draft, evidence, strict)}],
        )
    except Exception as exc:
        _emit({"ok": False, "passed": False, "error": "verifier_call_failed",
               "detail": f"{type(exc).__name__}: {exc}"}, exit_code=1)

    text = ""
    for block in (resp.content or []):
        if getattr(block, "type", "") == "text":
            text += getattr(block, "text", "") or ""
    text = _strip_code_fence(text)

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        _emit({"ok": False, "passed": False, "error": "verifier_returned_non_json",
               "detail": str(e), "raw": text[:500]}, exit_code=1)

    claims = parsed.get("claims") or []
    if not isinstance(claims, list):
        _emit({"ok": False, "passed": False, "error": "verifier_bad_shape",
               "raw": text[:500]}, exit_code=1)

    unsupported = [c for c in claims if isinstance(c, dict) and not c.get("supported")]
    passed = len(unsupported) == 0
    _emit({
        "ok": True,
        "passed": passed,
        "claims": claims,
        "unsupported_count": len(unsupported),
        "unsupported_claims": unsupported,
        "summary": f"{len(claims) - len(unsupported)}/{len(claims)} claims supported"
                   if claims else "no concrete claims in draft",
        "model": _MODEL,
    })


if __name__ == "__main__":
    main()
