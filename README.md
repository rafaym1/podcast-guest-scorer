# Guest Scorer (MVP)

Scores podcast guest pitches against a weighted rubric (0-40) and sorts them
into tiers, so a host opens one ranked list instead of a raw inbox. Can also
actively **search for and rank candidate guests** matching criteria set,
instead of only waiting for pitches to arrive. Has both a CLI (`score.py`,
for you to test/tune) and a client-facing web UI (`webapp/`, for clients).

## Design

`/` is a standalone marketing landing page (`webapp/templates/landing.html`),
separate from the app itself - no client list, just a bold hero and a "Start
Here" CTA into client creation. The actual client list lives at `/admin`
(your screen, not shown publicly). Visual style is neo-brutalist: cream
background with a dot texture, thick black borders, hard offset shadows, pill
buttons/badges, Lexend Mega + Public Sans. All defined as CSS custom
properties at the top of `webapp/static/style.css` - the whole palette/
border/shadow system is driven from there, so it's consistent across every
page (landing, dashboard, forms, editors) without per-page overrides.

## What's here

- `rubric.json` — the *default* scoring criteria/weights, copied into a new
  client's folder when you create them. Generic starting point - clients
  customize it themselves during onboarding (see "Web UI" below).
- `score.py` — the scoring/search engine (also runnable as a CLI). Everything
  in `webapp/` imports and reuses this - one engine, two interfaces.
- `webapp/` — the client-facing web app. See "Web UI" below.
- `clients/<slug>/` — one folder per client: `profile.json` (their show/niche),
  `rubric.json` (their scoring criteria, self-edited), `guests_scored.json`
  (their results). Fully isolated from each other.
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
`http://127.0.0.1:5000` is the public landing page. Create a client at
`/admin/clients/new` (your screen - one click, just **their name**, remembered
as the host so it's never asked again) and they land in a short guided setup:

1. **Show profile** — show name, industry/field, what the show covers, what
   an ideal guest looks like. This isn't cosmetic - it gets injected directly
   into every prompt sent to the model (`profile_section()` in `score.py`),
   so a real estate show and an AI show get judged on genuinely different
   terms, not the same generic rubric.
2. **Scoring criteria** — fully editable, not hardcoded: add/remove criteria,
   change weights, rewrite the guidance text per criterion. The rubric's max
   score is always `sum(weights)`, recomputed live as they edit (see the point
   counter on the page) - no way for it to drift out of sync.
3. Lands on a choice screen: **"I already have a topic"** (go straight to Find
   Guests) or **"Help me find a topic first"** (go to Find Topics, then hand
   off into Find Guests once one's picked). This is the same hub the "Find
   Guests" nav link always points to, not just a one-time onboarding step.

After setup, everything stays editable any time from the nav - "Scoring
Criteria" is the same page as onboarding step 2, just without the wizard
framing. Each client's data lives in `clients/<slug>/`:
`profile.json` (their niche), `rubric.json` (their criteria),
`guests_scored.json` (their results) - fully isolated from other clients.

Pages:
- **Ranked Guests** — the dashboard, filterable by tier, inbound-vs-sourced,
  and kept-vs-discarded. Each card's name links out to wherever it came from
  (their submitted link, or the first link found during a search) when one's
  available. **Keep**/**Discard** buttons per guest - discard hides it from
  the default view without deleting it (see the Discarded filter), reversible
  any time via Undo.
- **Score a Pitch** — fill the fields manually, *or* upload a screenshot, PDF,
  or `.docx` of the pitch and it extracts the fields for review/edit before
  scoring (never auto-scores straight from the upload). Images/PDFs go
  through Gemini's vision directly; `.docx` gets text-extracted first
  (`python-docx`) then read the same way as a manual pitch.
- **Find Topics** — before you even have a guest in mind: enter what's on
  your mind right now and what you've already covered recently (both
  **required** - vague/blank inputs are what produce generic suggestions),
  plus a trending-vs-evergreen preference. Runs two real searches - Tavily in
  news mode (`topic=news`, last 14 days) for what's actually happening, and a
  second discussion-flavored search for what people are debating - then the
  model proposes 4-5 specific episode angles grounded in that real signal,
  not generic categories. Each topic shows its actual source(s) as clickable
  links, cited by index into the real search results (never asks the model
  to reproduce a URL from memory - that's how you get broken/hallucinated
  links). If a topic is the model's own synthesis rather than tied to one
  specific article, it's labeled **"AI idea"** instead of faking a citation.
  Each comes with a "Find a Guest for This" button that hands off straight
  into Find Guests with the topic pre-filled (`build_topic_search_queries()`
  / `generate_topic_ideas()` in `score.py`).
- **Find Guests** — search criteria form; leave "Topic" blank and it falls
  back to the show profile's industry/target guest description. The query
  sent to the web is phrased as "profile or interview with a &lt;role&gt;
  working on &lt;topic&gt;..." rather than raw keywords - this is what
  actually finds individuals instead of market reports. If a search comes up
  empty, the error isn't a canned message - the model looks at your specific
  fields and what was actually found, then explains why and suggests a
  concrete edit (`diagnose_find_failure()` in `score.py`).
- **Scoring Criteria** — the editor described above
- **Export CSV / Import CSV** — backup/restore a client's scored guest list.
  Needed before/after every Render redeploy (see below) since results aren't
  tracked in git.

This is a real Flask app, single dev server, no login/auth yet — client
separation is by (unguessable) URL slug only. Fine for early pilot clients
you're handing links to directly; add real auth before this is public-facing
or has more than a handful of clients.

## Validate before building more

Get 3-5 real podcast hosts using the web UI on their actual inbox and ask
directly: is the ranking right, and would they pay for this? Don't build the
episode-prep or content/voice modules until this piece proves itself.
