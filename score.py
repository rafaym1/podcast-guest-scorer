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
import argparse
import urllib.request
import urllib.error
from datetime import datetime, timezone

CSV_FIELDS = ["guest_name", "scored_at", "source", "source_file", "total_score", "max_score", "tier", "summary", "criteria_json"]

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


def build_prompt(rubric, guest):
    criteria_lines = "\n".join(
        f'- "{c["key"]}" ({c["label"]}, max {c["weight"]} pts): {c["guidance"]}'
        for c in rubric["criteria"]
    )
    tier_lines = "\n".join(
        f'- {t["label"]}: score >= {t["min"]}' for t in rubric["tiers"]
    )
    guest_lines = "\n".join(f"{k}: {v}" for k, v in guest.items())

    return f"""You are scoring a podcast guest pitch for a host, using this rubric.
Score EACH criterion independently, do not just guess a total.

CRITERIA (max_score total = {rubric["max_score"]}):
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
  "max_score": {rubric["max_score"]},
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
                "search_depth": "basic",
                "max_results": max_results,
            },
            {"Content-Type": "application/json"},
            "Tavily API",
        )
    except RuntimeError as e:
        sys.exit(str(e))
    return data.get("results", [])


def build_find_query(criteria):
    parts = [criteria.get("topic", "")]
    if criteria.get("role"):
        parts.append(criteria["role"])
    if criteria.get("recency"):
        parts.append(criteria["recency"])
    if criteria.get("location"):
        parts.append(criteria["location"])
    return " ".join(p for p in parts if p).strip()


def extract_candidates(criteria, search_results, model):
    results_text = "\n\n".join(
        f"[{i}] {r.get('title', '')}\nURL: {r.get('url', '')}\n{r.get('content', '')[:600]}"
        for i, r in enumerate(search_results, 1)
    )
    exclude = f"\nEXCLUDE: {criteria['exclude']}" if criteria.get("exclude") else ""

    prompt = f"""You are helping a podcast host find real, named guest candidates from raw web search results.

SEARCH CRITERIA:
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


def score_guest(rubric, guest, model, source, source_file):
    prompt = build_prompt(rubric, guest)
    result = call_llm(prompt, model=model)
    return {
        "guest_name": guest.get("name", "Unknown"),
        "scored_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "source_file": source_file,
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


def ranked_results(results_path, tier=None, source=None):
    if not os.path.exists(results_path):
        return []
    results = load_json(results_path)
    if tier:
        results = [r for r in results if r["tier"] == tier]
    if source:
        results = [r for r in results if r.get("source") == source]
    results.sort(key=lambda r: r["total_score"], reverse=True)
    return results


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
    max_candidates = criteria.get("max_candidates", 8)

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
