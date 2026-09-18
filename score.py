#!/usr/bin/env python3
"""
Guest Scorer - MVP prototype.

Scores podcast guest pitches against a weighted rubric, and can also go find
candidate guests matching search criteria (instead of waiting for pitches to
land in your inbox). Keeps a running ranked list on disk (guests_scored.json).

Usage:
    python score.py score examples/sample_guest_1.json
    python score.py find examples/search_criteria_example.json
    python score.py rank
    python score.py rank --tier "Tier 1 - Book Immediately"
    python score.py rank --source sourced

Requires:
    OPENROUTER_API_KEY  - get one at openrouter.ai (used for scoring/extraction)
    TAVILY_API_KEY      - get one at tavily.com, free tier (used for `find` only)
Both can live in .env instead of real env vars.
"""

import base64
import csv
import io
import json
import os
import sys
import time
import uuid
import argparse
import urllib.request
import urllib.error
from datetime import datetime, timezone

CSV_FIELDS = ["id", "guest_name", "links", "scored_at", "source", "source_file", "status", "total_score", "max_score", "tier", "summary", "criteria_json"]

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
TAVILY_API_URL = "https://api.tavily.com/search"
RUBRIC_PATH = os.path.join(os.path.dirname(__file__), "rubric.json")
RESULTS_PATH = os.path.join(os.path.dirname(__file__), "guests_scored.json")
ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")


def load_dotenv(path):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


load_dotenv(ENV_PATH)
DEFAULT_MODEL = os.environ.get("SCORER_MODEL", "gemini-3.5-flash-lite")


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def rubric_max_score(rubric):
    return sum(c["weight"] for c in rubric["criteria"])


DEFAULT_TIERS = [
    {"label": "Tier 1 - Book Immediately", "min_pct": 0.825},
    {"label": "Tier 2 - Strong Candidate", "min_pct": 0.625},
    {"label": "Tier 3 - Maybe / Backlog", "min_pct": 0.375},
    {"label": "Tier 4 - Pass", "min_pct": 0.0},
]


def slugify_key(label, used_keys):
    base = "".join(c if c.isalnum() else "_" for c in label.lower()).strip("_") or "criterion"
    key = base
    i = 2
    while key in used_keys:
        key = f"{base}_{i}"
        i += 1
    return key


MAX_CANDIDATES_CAP = 10


def clamp_max_candidates(value, default=5):
    try:
        return min(MAX_CANDIDATES_CAP, max(1, int(value)))
    except (TypeError, ValueError):
        return default


def normalize_rubric(name, labels, weights, guidances, keys):
    """Builds a valid rubric dict from parallel form-submitted lists (used by the criteria editor)."""
    criteria = []
    used_keys = set()
    for label, weight, guidance, key in zip(labels, weights, guidances, keys):
        label = (label or "").strip()
        if not label:
            continue
        try:
            weight = min(10, max(1, int(weight)))
        except (TypeError, ValueError):
            weight = 5
        key = (key or "").strip() or slugify_key(label, used_keys)
        if key in used_keys:
            key = slugify_key(label, used_keys)
        used_keys.add(key)
        criteria.append({"key": key, "label": label, "weight": weight, "guidance": (guidance or "").strip()})

    if not criteria:
        raise ValueError("At least one criterion is required.")

    return {"name": name.strip() or "Guest Scoring Rubric", "criteria": criteria, "tiers": DEFAULT_TIERS}


def rubric_tier_lines(rubric, max_score):
    lines = []
    for t in rubric["tiers"]:
        abs_min = round(t["min_pct"] * max_score)
        lines.append(f'- {t["label"]}: score >= {abs_min}')
    return "\n".join(lines)


def profile_section(profile):
    if not profile or not any(profile.get(k) for k in ("show_name", "industry", "show_description", "target_guest_profile")):
        return ""
    lines = ["SHOW PROFILE (use this to judge topical/niche fit - this show is NOT generic):"]
    if profile.get("show_name"):
        lines.append(f'- Show: {profile["show_name"]}')
    if profile.get("industry"):
        lines.append(f'- Industry/field: {profile["industry"]}')
    if profile.get("show_description"):
        lines.append(f'- What the show covers: {profile["show_description"]}')
    if profile.get("target_guest_profile"):
        lines.append(f'- Ideal guest looks like: {profile["target_guest_profile"]}')
    return "\n".join(lines) + "\n\n"


def build_prompt(rubric, guest, profile=None):
    max_score = rubric_max_score(rubric)
    criteria_lines = "\n".join(
        f'- "{c["key"]}" ({c["label"]}, max {c["weight"]} pts): {c["guidance"]}'
        for c in rubric["criteria"]
    )
    tier_lines = rubric_tier_lines(rubric, max_score)
    guest_lines = "\n".join(f"{k}: {v}" for k, v in guest.items())

    return f"""You are scoring a podcast guest pitch for a host, using this rubric.
Score EACH criterion independently, do not just guess a total.

{profile_section(profile)}CRITERIA (max_score total = {max_score}):
{criteria_lines}

TIERS:
{tier_lines}

GUEST SUBMISSION:
{guest_lines}

Respond with ONLY valid JSON, no markdown fences, matching exactly this shape:
{{
  "criteria": [
    {{"key": "authority", "score": <int 0-weight>, "rationale": "<one sentence>"}},
    ...
  ],
  "total_score": <int>,
  "max_score": {max_score},
  "tier": "<tier label from the TIERS list>",
  "summary": "<one sentence overall verdict for the host>"
}}"""


def post_json(url, payload, headers, label):
    """Single attempt. Raises RuntimeError on failure - callers decide whether/how to retry."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{label} error {e.code}: {e.read().decode('utf-8')}")


def extract_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object in response:\n{text[:300]}")
    return json.loads(text[start:end + 1])


def call_gemini_once(prompt, model, file_part=None):
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        sys.exit("ERROR: set the GOOGLE_API_KEY environment variable (or add it to .env) first.")

    parts = [{"text": prompt}]
    if file_part:
        parts.append(file_part)

    data = post_json(
        GEMINI_API_URL.format(model=model, key=api_key),
        {
            "contents": [{"parts": parts}],
            "generationConfig": {"maxOutputTokens": 1500},
        },
        {"Content-Type": "application/json"},
        "Gemini API",
    )
    if "error" in data:
        raise RuntimeError(f"Gemini API error: {data['error']}")
    candidates = data.get("candidates") or []
    if not candidates:
        raise RuntimeError(f"Gemini returned no candidates: {data}")
    parts_out = candidates[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts_out)
    return extract_json(text)


def call_openrouter_once(prompt, model):
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        sys.exit("ERROR: set the OPENROUTER_API_KEY environment variable (or add it to .env) first.")

    data = post_json(
        OPENROUTER_API_URL,
        {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": 1500},
        {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        "OpenRouter API",
    )
    if "error" in data:
        raise RuntimeError(f"OpenRouter API error: {data['error']}")
    text = data["choices"][0]["message"]["content"]
    return extract_json(text)


def with_retry(fn, label, attempts=4):
    backoffs = [0, 5, 15, 30]
    last_error = None
    for attempt in range(attempts):
        time.sleep(backoffs[min(attempt, len(backoffs) - 1)])
        try:
            return fn()
        except (RuntimeError, ValueError, json.JSONDecodeError) as e:
            last_error = str(e)
            continue
    sys.exit(f"{label} failed {attempts}x.\nLast error: {last_error}")


def call_llm(prompt, model=DEFAULT_MODEL, attempts=4):
    # OpenRouter model ids are always "org/model" (e.g. "google/gemma-4-31b-it:free").
    # A bare name like "gemini-3.5-flash-lite" means: call Google directly.
    call_once = call_openrouter_once if "/" in model else call_gemini_once
    return with_retry(lambda: call_once(prompt, model), f"Model '{model}'", attempts)


def search_web(query, max_results=10):
    api_key = os.environ.get("TAVILY_API_KEY")
    if not api_key:
        sys.exit("ERROR: set the TAVILY_API_KEY environment variable (or add it to .env) first. Free key at tavily.com.")

    try:
        data = post_json(
            TAVILY_API_URL,
            {
                "api_key": api_key,
                "query": query,
                "search_depth": "advanced",
                "max_results": max_results,
            },
            {"Content-Type": "application/json"},
            "Tavily API",
        )
    except RuntimeError as e:
        sys.exit(str(e))
    return data.get("results", [])


def build_find_query(criteria, profile=None):
    """Builds a natural-language, person-seeking query - not keyword soup.
    Plain concatenation (e.g. "AI edtech founder funding US") reads like a
    research question to a search engine and surfaces market reports/investor
    listicles instead of individuals. Framing it as "profile of a <role>..."
    biases results toward content about one identifiable person."""
    profile = profile or {}
    topic = criteria.get("topic") or profile.get("target_guest_profile") or profile.get("industry", "")
    role = criteria.get("role") or "person"

    sentence = f"profile or interview with a {role}"
    if topic:
        sentence += f" working on {topic}"
    if profile.get("industry") and profile.get("industry", "").lower() not in sentence.lower():
        sentence += f" in {profile['industry']}"
    if criteria.get("recency"):
        sentence += f", who {criteria['recency']}"
    if criteria.get("location"):
        sentence += f", based in {criteria['location']}"
    return sentence.strip()


def extract_candidates(criteria, search_results, model, profile=None):
    results_text = "\n\n".join(
        f"[{i}] {r.get('title', '')}\nURL: {r.get('url', '')}\n{r.get('content', '')[:600]}"
        for i, r in enumerate(search_results, 1)
    )
    exclude = f"\nEXCLUDE: {criteria['exclude']}" if criteria.get("exclude") else ""

    prompt = f"""You are helping a podcast host find real, named guest candidates from raw web search results.

{profile_section(profile)}SEARCH CRITERIA:
- Topic: {criteria.get("topic", "")}
- Desired role: {criteria.get("role", "any")}
- Recency requirement: {criteria.get("recency", "none")}
- Location preference: {criteria.get("location", "any")}{exclude}

RAW SEARCH RESULTS:
{results_text}

From these results, identify distinct REAL, NAMED individuals (not companies, not generic articles) who plausibly
fit the criteria. Skip anything that isn't clearly about one identifiable person. For each, write a short bio and
pitch angle using ONLY what's in the search results above - do not invent facts.

Respond with ONLY valid JSON, no markdown fences, matching exactly this shape:
{{
  "candidates": [
    {{
      "name": "<full name>",
      "bio": "<1-2 sentence bio, only facts found in the results>",
      "pitch_text": "<1 sentence on why they'd make a good guest for this topic>",
      "links": "<comma-separated URLs from the results that mention them>"
    }}
  ]
}}"""
    result = call_llm(prompt, model=model)
    return result.get("candidates", [])


GUEST_FIELDS_INSTRUCTION = """Extract information about a potential podcast guest from this document (could be an
email pitch, a LinkedIn profile screenshot, a bio page, a one-pager, etc). Use ONLY facts present in the document -
never invent anything. Leave a field as an empty string if it isn't present.

Respond with ONLY valid JSON, no markdown fences, matching exactly this shape:
{
  "name": "<their full name>",
  "bio": "<1-3 sentence background/credentials, facts only>",
  "pitch_text": "<what they're pitching or proposing to talk about, if present - else empty string>",
  "links": "<comma-separated URLs/handles found in the document, if any>"
}"""

# File uploads always parse via direct Gemini (multimodal), regardless of SCORER_MODEL,
# since that's the only provider path wired up for image/PDF understanding here.
PARSE_MODEL = DEFAULT_MODEL if "/" not in DEFAULT_MODEL else "gemini-3.5-flash-lite"

SUPPORTED_UPLOAD_TYPES = {
    "image/png", "image/jpeg", "image/jpg", "image/webp",
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def extract_docx_text(file_bytes):
    import docx
    doc = docx.Document(io.BytesIO(file_bytes))
    return "\n".join(p.text for p in doc.paragraphs if p.text.strip())


def extract_guest_from_file(file_bytes, mime_type, filename=""):
    if mime_type not in SUPPORTED_UPLOAD_TYPES:
        raise ValueError(f"Unsupported file type '{mime_type}'. Upload an image, PDF, or .docx.")

    if mime_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        doc_text = extract_docx_text(file_bytes)
        if not doc_text.strip():
            raise ValueError(f"Couldn't find any text in {filename or 'that document'}.")
        prompt = f"{GUEST_FIELDS_INSTRUCTION}\n\nDOCUMENT TEXT:\n{doc_text[:8000]}"
        return with_retry(lambda: call_gemini_once(prompt, PARSE_MODEL), "Gemini file parsing")

    file_part = {"inline_data": {"mime_type": mime_type, "data": base64.b64encode(file_bytes).decode("ascii")}}
    return with_retry(
        lambda: call_gemini_once(GUEST_FIELDS_INSTRUCTION, PARSE_MODEL, file_part=file_part),
        "Gemini file parsing",
    )


def score_guest(rubric, guest, model, source, source_file, profile=None):
    prompt = build_prompt(rubric, guest, profile=profile)
    result = call_llm(prompt, model=model)
    return {
        "id": uuid.uuid4().hex[:12],
        "guest_name": guest.get("name", "Unknown"),
        "links": guest.get("links", ""),
        "scored_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "source_file": source_file,
        "status": "new",
        **result,
    }


def save_result(record, results_path=RESULTS_PATH):
    results = load_json(results_path) if os.path.exists(results_path) else []
    results.append(record)
    with open(results_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)


def results_to_csv_rows(results):
    rows = []
    for r in results:
        row = {k: r.get(k, "") for k in CSV_FIELDS if k != "criteria_json"}
        row["criteria_json"] = json.dumps(r.get("criteria", []))
        rows.append(row)
    return rows


def csv_rows_to_results(rows):
    results = []
    for row in rows:
        rec = {k: row.get(k, "") for k in CSV_FIELDS if k != "criteria_json"}
        rec["id"] = rec["id"] or uuid.uuid4().hex[:12]
        rec["status"] = rec["status"] or "new"
        rec["total_score"] = int(rec["total_score"] or 0)
        rec["max_score"] = int(rec["max_score"] or 0)
        rec["criteria"] = json.loads(row.get("criteria_json") or "[]")
        results.append(rec)
    return results


def write_results_csv(results_path, csv_file):
    results = load_json(results_path) if os.path.exists(results_path) else []
    writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS)
    writer.writeheader()
    writer.writerows(results_to_csv_rows(results))


def read_results_csv(results_path, csv_file):
    reader = csv.DictReader(csv_file)
    results = csv_rows_to_results(list(reader))
    with open(results_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    return results


def backfill_result_fields(results_path):
    """Records saved before id/status/links existed need these before Keep/Discard can target them."""
    if not os.path.exists(results_path):
        return
    results = load_json(results_path)
    changed = False
    for r in results:
        if "id" not in r:
            r["id"] = uuid.uuid4().hex[:12]
            changed = True
        if "status" not in r:
            r["status"] = "new"
            changed = True
        if "links" not in r:
            r["links"] = ""
            changed = True
    if changed:
        with open(results_path, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2)


def ranked_results(results_path, tier=None, source=None, status=None):
    if not os.path.exists(results_path):
        return []
    backfill_result_fields(results_path)
    results = load_json(results_path)
    if tier:
        results = [r for r in results if r["tier"] == tier]
    if source:
        results = [r for r in results if r.get("source") == source]
    if status:
        results = [r for r in results if r.get("status", "new") == status]
    else:
        # Default view hides discarded guests without deleting them - reversible.
        results = [r for r in results if r.get("status", "new") != "discarded"]
    results.sort(key=lambda r: r["total_score"], reverse=True)
    return results


def set_guest_status(results_path, guest_id, status):
    results = load_json(results_path) if os.path.exists(results_path) else []
    found = False
    for r in results:
        if r.get("id") == guest_id:
            r["status"] = status
            found = True
            break
    if not found:
        raise ValueError(f"No guest with id '{guest_id}'")
    with open(results_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)


def print_record(record):
    print(f"\n{record['guest_name']}  ->  {record['total_score']}/{record['max_score']}  ({record['tier']})")
    print(record["summary"])
    for c in record["criteria"]:
        print(f"  - {c['key']}: {c['score']}  ({c['rationale']})")


def cmd_score(args):
    rubric = load_json(RUBRIC_PATH)
    guest = load_json(args.guest_file)

    record = score_guest(rubric, guest, args.model, "inbound", os.path.basename(args.guest_file))
    save_result(record, RESULTS_PATH)
    print_record(record)
    print(f"\nSaved to {RESULTS_PATH}")


def cmd_find(args):
    rubric = load_json(RUBRIC_PATH)
    criteria = load_json(args.criteria_file)
    max_candidates = clamp_max_candidates(criteria.get("max_candidates"), default=5)

    query = build_find_query(criteria)
    print(f"Searching for: {query}\n")
    search_results = search_web(query, max_results=max(max_candidates * 2, 10))
    if not search_results:
        print("No search results found - try broadening the criteria.")
        return

    candidates = extract_candidates(criteria, search_results, args.model)[:max_candidates]
    if not candidates:
        print("No named candidates could be extracted from the search results - try broadening the criteria.")
        return

    print(f"Found {len(candidates)} candidate(s). Scoring each...\n")
    scored = []
    for candidate in candidates:
        record = score_guest(rubric, candidate, args.model, "sourced", os.path.basename(args.criteria_file))
        save_result(record, RESULTS_PATH)
        print_record(record)
        scored.append(record)

    scored.sort(key=lambda r: r["total_score"], reverse=True)
    print("\n\n=== PEOPLE TO INVITE (ranked) ===")
    print(f"{'RANK':<5}{'SCORE':<8}{'TIER':<30}{'GUEST'}")
    for i, r in enumerate(scored, 1):
        print(f"{i:<5}{r['total_score']:<8}{r['tier']:<30}{r['guest_name']}")
    print(f"\nSaved to {RESULTS_PATH}")


def cmd_rank(args):
    if not os.path.exists(RESULTS_PATH):
        print("No guests scored yet. Run: python score.py score <guest_file.json>")
        return
    results = ranked_results(RESULTS_PATH, tier=args.tier, source=args.source)

    print(f"{'RANK':<5}{'SCORE':<8}{'TIER':<30}{'SOURCE':<10}{'GUEST'}")
    for i, r in enumerate(results, 1):
        print(f"{i:<5}{r['total_score']:<8}{r['tier']:<30}{r.get('source', '-'):<10}{r['guest_name']}")


def main():
    parser = argparse.ArgumentParser(description="Podcast guest scorer MVP")
    sub = parser.add_subparsers(dest="command", required=True)

    p_score = sub.add_parser("score", help="Score a single guest pitch JSON file")
    p_score.add_argument("guest_file")
    p_score.add_argument("--model", default=DEFAULT_MODEL)
    p_score.set_defaults(func=cmd_score)

    p_find = sub.add_parser("find", help="Search for candidate guests matching criteria, then score them")
    p_find.add_argument("criteria_file")
    p_find.add_argument("--model", default=DEFAULT_MODEL)
    p_find.set_defaults(func=cmd_find)

    p_rank = sub.add_parser("rank", help="Print all scored guests, ranked")
    p_rank.add_argument("--tier", default=None)
    p_rank.add_argument("--source", default=None, choices=["inbound", "sourced"])
    p_rank.set_defaults(func=cmd_rank)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
