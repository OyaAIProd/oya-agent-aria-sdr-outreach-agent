import os, json

def normalize(text: str) -> str:
    return text.lower().strip()

def score_lead(lead: dict, icp: dict) -> tuple[int, str]:
    reasons = []
    score = 0

    title = normalize(lead.get("title", ""))
    company = normalize(lead.get("company", ""))
    domain = normalize(lead.get("domain", ""))
    signals = [normalize(s) for s in lead.get("signals", [])]

    # --- Industry match (0-25 points) ---
    industries = [normalize(i) for i in icp.get("industries", [])]
    industry_matched = False
    for ind in industries:
        if ind in company or ind in domain:
            score += 25
            reasons.append(f"Industry match: '{ind}'")
            industry_matched = True
            break
    if not industry_matched:
        reasons.append("No industry match")

    # --- Seniority match (0-25 points) ---
    seniority_keywords = [normalize(k) for k in icp.get("seniority_keywords", [])]
    seniority_matched = False
    for kw in seniority_keywords:
        if kw in title:
            score += 25
            reasons.append(f"Seniority match: '{kw}' in title")
            seniority_matched = True
            break
    if not seniority_matched:
        reasons.append("Seniority below target")

    # --- Company size match (0-20 points) ---
    size_range = icp.get("company_size", {})
    size_min = size_range.get("min", 0)
    size_max = size_range.get("max", -1)
    size_estimate_map = icp.get("company_size_estimate", {})
    estimated_size = None
    for key, est in size_estimate_map.items():
        if normalize(key) in company or normalize(key) in domain:
            estimated_size = est
            break
    if estimated_size is not None:
        in_range = (estimated_size >= size_min) and (size_max == -1 or estimated_size <= size_max)
        if in_range:
            score += 20
            reasons.append(f"Company size ~{estimated_size} fits range {size_min}-{'∞' if size_max == -1 else size_max}")
        else:
            reasons.append(f"Company size ~{estimated_size} outside target range")
    else:
        # No size data — give partial credit
        score += 10
        reasons.append("Company size unknown (partial credit)")

    # --- Domain targeting bonus (0-5 points) ---
    target_domains = [normalize(d) for d in icp.get("target_domains", [])]
    if target_domains and domain in target_domains:
        score += 5
        reasons.append(f"Domain '{domain}' is a named target")

    # --- Intent signals (0-25 points) ---
    high_signals = [normalize(s) for s in icp.get("high_intent_signals", [])]
    med_signals = [normalize(s) for s in icp.get("medium_intent_signals", [])]
    signal_score = 0
    matched_signals = []
    for sig in signals:
        for hs in high_signals:
            if hs in sig:
                signal_score += 15
                matched_signals.append(f"high-intent: '{sig}'")
                break
        else:
            for ms in med_signals:
                if ms in sig:
                    signal_score += 8
                    matched_signals.append(f"mid-intent: '{sig}'")
                    break
    signal_score = min(signal_score, 25)
    score += signal_score
    if matched_signals:
        reasons.append("Signals: " + "; ".join(matched_signals[:3]))
    else:
        reasons.append("No intent signals detected")

    score = min(score, 100)
    fit_reason = " | ".join(reasons)
    return score, fit_reason


try:
    inp = json.loads(os.environ.get("INPUT_JSON", "{}"))

    leads = inp.get("leads")
    icp = inp.get("icp")

    if not isinstance(leads, list) or len(leads) == 0:
        raise ValueError("'leads' must be a non-empty array of lead objects.")
    if not isinstance(icp, dict):
        raise ValueError("'icp' must be an object with ICP definition fields.")
    if not icp.get("seniority_keywords"):
        raise ValueError("'icp.seniority_keywords' is required (e.g. ['VP', 'Director', 'Chief']).")
    if "high_intent_signals" not in icp or "medium_intent_signals" not in icp:
        raise ValueError("'icp.high_intent_signals' and 'icp.medium_intent_signals' are required.")

    scored = []
    for lead in leads:
        if not isinstance(lead, dict):
            continue
        lead_score, fit_reason = score_lead(lead, icp)
        if lead_score >= 50:
            scored.append({
                "name": lead.get("name", "Unknown"),
                "company": lead.get("company", ""),
                "title": lead.get("title", ""),
                "domain": lead.get("domain", ""),
                "signals": lead.get("signals", []),
                "score": lead_score,
                "fit_reason": fit_reason,
            })

    scored.sort(key=lambda x: x["score"], reverse=True)

    result = {
        "total_leads_evaluated": len(leads),
        "qualified_leads_count": len(scored),
        "dropped_count": len(leads) - len(scored),
        "qualified_leads": scored,
    }

    print(json.dumps(result))

except Exception as e:
    print(json.dumps({"error": str(e)}))