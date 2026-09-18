# Guest Scorer (MVP)

Scores podcast guest pitches against a weighted rubric (0-40) and sorts them
into tiers, so a host opens one ranked list instead of a raw inbox. Can also
actively **search for and rank candidate guests** matching criteria set,
instead of only waiting for pitches to arrive. Has both a CLI (`score.py`,
for you to test/tune) and a client-facing web UI (`webapp/`, for clients).

## What's here

- `rubric.json` — the *default* scoring criteria/weights, copied into a new
  client's folder when you create them. Generic starting point, not tuned
  to any real show yet.
- `score.py` — the scoring/search engine (also runnable as a CLI). Everything
  in `webapp/` imports and reuses this - one engine, two interfaces.
- `webapp/` — the client-facing web app. See "Web UI" below.
- `clients/<slug>/` — one folder per client: their `rubric.json` (customized
  by you) and `guests_scored.json` (their results). Fully isolated from each
  other.
- `examples/` — sample guest pitches (strong / weak / medium) and a sample
  search criteria file, to sanity-check the rubric before trusting it.

## 1. Set up

Keys go in `.env` (already created, gitignored):

```
OPENROUTER_API_KEY=...   # openrouter.ai - fallback provider, only used for "org/model" ids
GOOGLE_API_KEY=...       # ai.google.dev - powers scoring/extraction by default
SCORER_MODEL=...         # defaults to gemini-3.5-flash-lite (called directly, not via OpenRouter)
TAVILY_API_KEY=...       # tavily.com, free tier - powers web search for `find`
```

Model selection: any model name **without** a `/` (e.g. `gemini-3.5-flash-lite`)
is called directly against Google's API using `GOOGLE_API_KEY`. Any model name
**with** a `/` (e.g. `google/gemma-4-31b-it:free`, an OpenRouter id) routes
through OpenRouter using `OPENROUTER_API_KEY` instead. Direct Gemini is the
default because OpenRouter's free-tier shared pools have proven unreliable
(rate limits, capacity errors, even garbage output) in testing.

## 2. Try it on the examples first

```bash
python score.py score examples/sample_guest_1.json
python score.py score examples/sample_guest_2.json
python score.py score examples/sample_guest_3.json
python score.py rank
```

Sanity check: sample 1 (published AI founder with real war stories) should
land Tier 1-2, sample 2 (generic marketing pitch, no niche fit) should land
Tier 3-4, sample 3 (real operator, smaller following) should land Tier 2-3.
If the ranking feels wrong, fix `rubric.json` weights/guidance before doing
anything else — the rubric is the whole product.

## 3. Score a real pitch

Copy `examples/sample_guest_1.json` as a template, fill in a real pitch you've
received, then:

```bash
python score.py score my_guest.json
python score.py rank
```

## 4. Go find guests instead of waiting for pitches

Requires a free `TAVILY_API_KEY` in `.env` (get one at tavily.com — a couple
minutes, no card needed). Copy `examples/search_criteria_example.json`, edit
the fields to describe who you want, then:

```bash
python score.py find my_search.json
```

This searches the web for people matching your criteria, has the model pull
out real named candidates (only using facts found in the search results —
never invented), scores each one against your rubric, and prints a final
ranked "who to invite" list. Everything gets saved to `guests_scored.json`
tagged `"source": "sourced"`, alongside inbound pitches (tagged `"inbound"`).

```bash
python score.py rank --source sourced   # only people you went and found
python score.py rank --source inbound   # only people who pitched you
```

## 5. Web UI (what clients actually use)

```bash
python webapp/app.py
```
Opens at `http://127.0.0.1:5000`. Each client gets a private URL,
`/c/<slug>/`, with:

- **Ranked Guests** — the dashboard, filterable by tier and by inbound-vs-sourced
- **Score a Pitch** — a form for one guest (replaces hand-writing JSON)
- **Find Guests** — a form for the search criteria (replaces hand-writing JSON)
- **Rubric** — read-only view of what they're being scored on (builds trust,
  and it's clear this isn't self-serve editable)

Create a new client at `/admin/clients/new` — this is *your* screen, not a
client's. It copies the default `rubric.json` into `clients/<slug>/`. **You
then hand-edit that file to actually customize their rubric** — there's no
self-serve rubric editor yet, by design (see the prompt this was built from:
"customized from me").

This is a real Flask app, single dev server, no login/auth yet — client
separation is by (unguessable) URL slug only. Fine for early pilot clients
you're handing links to directly; add real auth before this is public-facing
or has more than a handful of clients.

## Validate before building more

Get 3-5 real podcast hosts using the web UI on their actual inbox and ask
directly: is the ranking right, and would they pay for this? Don't build the
episode-prep or content/voice modules until this piece proves itself.
