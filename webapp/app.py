#!/usr/bin/env python3
"""
Guest Scorer - client-facing web UI.

Each client gets a private URL: /c/<client_slug>/. A new client goes through
a short guided setup first time in: /admin/clients/new (your screen) creates
the client, then they land in onboarding - profile (who they are / what the
show covers) -> scoring criteria (fully editable, not hardcoded) -> Find
Guests. After that, both profile and criteria stay editable any time.

Run:
    python webapp/app.py
Then open http://127.0.0.1:5000/c/demo/
"""

import os
import sys
import json
import io
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import score as engine  # noqa: E402

from flask import Flask, render_template, request, redirect, url_for, abort, Response

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENTS_DIR = os.path.join(BASE_DIR, "clients")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10MB upload cap


@app.template_filter("first_link")
def first_link_filter(links_str):
    """First URL out of a comma-separated links string, normalized to be clickable."""
    if not links_str:
        return None
    first = links_str.split(",")[0].strip()
    if not first:
        return None
    if first.startswith("http://") or first.startswith("https://"):
        return first
    if "@" in first and "/" not in first:
        return f"mailto:{first}"
    return f"https://{first}"

EMPTY_PROFILE = {
    "host_name": "", "show_name": "", "industry": "",
    "show_description": "", "target_guest_profile": "", "onboarded": True,
}


def client_paths(slug):
    client_dir = os.path.join(CLIENTS_DIR, slug)
    if not os.path.isdir(client_dir):
        abort(404, description=f"Unknown client '{slug}'")
    return {
        "dir": client_dir,
        "rubric": os.path.join(client_dir, "rubric.json"),
        "results": os.path.join(client_dir, "guests_scored.json"),
        "profile": os.path.join(client_dir, "profile.json"),
    }


def load_profile(paths):
    # Clients created before profile.json existed are grandfathered in as
    # already-onboarded, so they aren't suddenly bounced into the wizard.
    if not os.path.exists(paths["profile"]):
        return dict(EMPTY_PROFILE)
    return engine.load_json(paths["profile"])


def save_profile(paths, profile):
    with open(paths["profile"], "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=2)


@app.route("/")
def home():
    clients = sorted(
        d for d in os.listdir(CLIENTS_DIR) if os.path.isdir(os.path.join(CLIENTS_DIR, d))
    )
    return render_template("home.html", clients=clients)


@app.route("/c/<slug>/")
def dashboard(slug):
    paths = client_paths(slug)
    profile = load_profile(paths)
    if not profile.get("onboarded", True):
        return redirect(url_for("onboarding_profile", slug=slug))

    tier = request.args.get("tier") or None
    source = request.args.get("source") or None
    status = request.args.get("status") or None
    results = engine.ranked_results(paths["results"], tier=tier, source=source, status=status)
    rubric = engine.load_json(paths["rubric"])
    return render_template(
        "dashboard.html",
        slug=slug,
        results=results,
        rubric=rubric,
        profile=profile,
        active_tier=tier,
        active_source=source,
        active_status=status,
    )


@app.route("/c/<slug>/guest/<guest_id>/status", methods=["POST"])
def guest_status(slug, guest_id):
    paths = client_paths(slug)
    new_status = request.form.get("status")
    if new_status not in ("kept", "discarded", "new"):
        abort(400, description="Invalid status")
    try:
        engine.set_guest_status(paths["results"], guest_id, new_status)
    except ValueError:
        abort(404, description="Unknown guest")
    return redirect(request.referrer or url_for("dashboard", slug=slug))


@app.route("/c/<slug>/onboarding/profile", methods=["GET", "POST"])
def onboarding_profile(slug):
    paths = client_paths(slug)
    profile = load_profile(paths)

    if request.method == "GET":
        return render_template("onboarding_profile.html", slug=slug, form=profile, error=None)

    profile.update({
        "host_name": request.form.get("host_name", "").strip(),
        "show_name": request.form.get("show_name", "").strip(),
        "industry": request.form.get("industry", "").strip(),
        "show_description": request.form.get("show_description", "").strip(),
        "target_guest_profile": request.form.get("target_guest_profile", "").strip(),
    })
    if not profile["show_name"] or not profile["industry"]:
        return render_template(
            "onboarding_profile.html", slug=slug, form=profile,
            error="Show name and industry/field are required - this is what makes scoring actually relevant to you.",
        )

    save_profile(paths, profile)
    return redirect(url_for("rubric_edit", slug=slug))


@app.route("/c/<slug>/rubric", methods=["GET", "POST"])
def rubric_edit(slug):
    paths = client_paths(slug)
    profile = load_profile(paths)
    onboarding = not profile.get("onboarded", True)

    if request.method == "GET":
        rubric = engine.load_json(paths["rubric"])
        return render_template(
            "rubric.html", slug=slug, rubric=rubric, onboarding=onboarding,
            error=None, saved=request.args.get("saved"),
        )

    labels = request.form.getlist("label")
    weights = request.form.getlist("weight")
    guidances = request.form.getlist("guidance")
    keys = request.form.getlist("key")
    name = request.form.get("name", "")

    try:
        new_rubric = engine.normalize_rubric(name, labels, weights, guidances, keys)
    except ValueError as e:
        rubric = engine.load_json(paths["rubric"])
        return render_template("rubric.html", slug=slug, rubric=rubric, onboarding=onboarding, error=str(e), saved=None)

    with open(paths["rubric"], "w", encoding="utf-8") as f:
        json.dump(new_rubric, f, indent=2)

    if onboarding:
        profile["onboarded"] = True
        save_profile(paths, profile)
        return redirect(url_for("find_new", slug=slug, welcome=1))

    return redirect(url_for("rubric_edit", slug=slug, saved=1))


@app.route("/c/<slug>/export/csv")
def export_csv(slug):
    # Render's free tier wipes local disk on every redeploy - this is the
    # backup step to run before redeploying, and import_csv is how it comes back.
    paths = client_paths(slug)
    buf = io.StringIO()
    engine.write_results_csv(paths["results"], buf)
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={slug}-guests-backup.csv"},
    )


@app.route("/c/<slug>/import/csv", methods=["GET", "POST"])
def import_csv(slug):
    paths = client_paths(slug)
    if request.method == "GET":
        return render_template("import_csv.html", slug=slug, error=None)

    file = request.files.get("csv_file")
    if not file or file.filename == "":
        return render_template("import_csv.html", slug=slug, error="Choose a CSV file first.")

    try:
        text = file.read().decode("utf-8")
        engine.read_results_csv(paths["results"], io.StringIO(text))
    except Exception as e:
        return render_template("import_csv.html", slug=slug, error=f"Couldn't read that CSV: {e}")

    return redirect(url_for("dashboard", slug=slug))


@app.route("/c/<slug>/score/parse", methods=["POST"])
def score_parse(slug):
    client_paths(slug)  # 404s if the client doesn't exist
    file = request.files.get("upload_file")
    if not file or file.filename == "":
        return render_template("score_form.html", slug=slug, error="Choose a file first.", form={})

    mime_type = file.mimetype
    try:
        extracted = engine.extract_guest_from_file(file.read(), mime_type, filename=file.filename)
    except (ValueError, SystemExit) as e:
        return render_template("score_form.html", slug=slug, error=str(e), form={})

    return render_template(
        "score_form.html",
        slug=slug,
        error=None,
        form=extracted,
        notice=f"Extracted from {file.filename} - review before scoring.",
    )


@app.route("/c/<slug>/score/new", methods=["GET", "POST"])
def score_new(slug):
    paths = client_paths(slug)
    if not load_profile(paths).get("onboarded", True):
        return redirect(url_for("onboarding_profile", slug=slug))
    if request.method == "GET":
        return render_template("score_form.html", slug=slug, error=None, form={})

    guest = {
        "name": request.form.get("name", "").strip(),
        "bio": request.form.get("bio", "").strip(),
        "pitch_text": request.form.get("pitch_text", "").strip(),
        "links": request.form.get("links", "").strip(),
    }
    if not guest["name"]:
        return render_template("score_form.html", slug=slug, error="Name is required.", form=guest)

    rubric = engine.load_json(paths["rubric"])
    profile = load_profile(paths)
    try:
        record = engine.score_guest(rubric, guest, engine.DEFAULT_MODEL, "inbound", "web-form", profile=profile)
    except SystemExit as e:
        return render_template("score_form.html", slug=slug, error=str(e), form=guest)

    engine.save_result(record, paths["results"])
    return redirect(url_for("dashboard", slug=slug))


@app.route("/c/<slug>/find/new", methods=["GET", "POST"])
def find_new(slug):
    paths = client_paths(slug)
    if not load_profile(paths).get("onboarded", True):
        return redirect(url_for("onboarding_profile", slug=slug))
    if request.method == "GET":
        prefill = {"topic": request.args.get("topic", "")} if request.args.get("topic") else {}
        return render_template("find_form.html", slug=slug, error=None, form=prefill, welcome=request.args.get("welcome"))

    criteria = {
        "topic": request.form.get("topic", "").strip(),
        "role": request.form.get("role", "").strip(),
        "recency": request.form.get("recency", "").strip(),
        "location": request.form.get("location", "").strip(),
        "exclude": request.form.get("exclude", "").strip(),
        "max_candidates": engine.clamp_max_candidates(request.form.get("max_candidates")),
    }
    rubric = engine.load_json(paths["rubric"])
    profile = load_profile(paths)
    if not criteria["topic"] and not profile.get("target_guest_profile") and not profile.get("industry"):
        return render_template("find_form.html", slug=slug, error="Topic is required (or set your show profile first).", form=criteria, welcome=None)

    query = engine.build_find_query(criteria, profile=profile)
    try:
        search_results = engine.search_web(query, max_results=max(criteria["max_candidates"] * 2, 10))
        if not search_results:
            explanation = engine.diagnose_find_failure(criteria, profile, query, search_results, engine.DEFAULT_MODEL)
            return render_template(
                "find_form.html", slug=slug, form=criteria, welcome=None,
                error=explanation or "No search results found for this combination of fields - try broadening one of them.",
            )

        candidates = engine.extract_candidates(criteria, search_results, engine.DEFAULT_MODEL, profile=profile)[: criteria["max_candidates"]]
        if not candidates:
            explanation = engine.diagnose_find_failure(criteria, profile, query, search_results, engine.DEFAULT_MODEL)
            return render_template(
                "find_form.html", slug=slug, welcome=None, form=criteria, searched=search_results,
                error=explanation or "Found pages, but none named a specific person to extract - try a more person-focused topic.",
            )

        for candidate in candidates:
            record = engine.score_guest(rubric, candidate, engine.DEFAULT_MODEL, "sourced", "web-search", profile=profile)
            engine.save_result(record, paths["results"])
    except SystemExit as e:
        return render_template("find_form.html", slug=slug, error=str(e), form=criteria, welcome=None)

    return redirect(url_for("dashboard", slug=slug, source="sourced"))


@app.route("/c/<slug>/topics/new", methods=["GET", "POST"])
def topics_new(slug):
    paths = client_paths(slug)
    if not load_profile(paths).get("onboarded", True):
        return redirect(url_for("onboarding_profile", slug=slug))
    profile = load_profile(paths)

    if request.method == "GET":
        return render_template("topics_form.html", slug=slug, error=None, form={}, topics=None)

    seed = request.form.get("seed", "").strip()
    avoid = request.form.get("avoid", "").strip()
    recency = request.form.get("recency", "trending")
    form = {"seed": seed, "avoid": avoid, "recency": recency}

    news_query, discussion_query = engine.build_topic_search_queries(profile, seed)
    try:
        news_results = engine.search_web(news_query, max_results=8, topic="news", days=14)
        discussion_results = engine.search_web(discussion_query, max_results=8)
        topics = engine.generate_topic_ideas(profile, seed, avoid, recency, news_results, discussion_results, engine.DEFAULT_MODEL)
    except SystemExit as e:
        return render_template("topics_form.html", slug=slug, error=str(e), form=form, topics=None)

    if not topics:
        return render_template(
            "topics_form.html", slug=slug, form=form, topics=None,
            error="Couldn't generate topics from what was found - try a more specific seed, or check your show profile has enough detail.",
        )

    return render_template("topics_form.html", slug=slug, error=None, form=form, topics=topics)


@app.route("/admin/clients/new", methods=["GET", "POST"])
def new_client(slug=None):
    if request.method == "GET":
        return render_template("new_client.html", error=None)

    host_name = request.form.get("name", "").strip()
    if not host_name:
        return render_template("new_client.html", error="Your name is required.")

    slug = "".join(c if c.isalnum() else "-" for c in host_name.lower()).strip("-") or uuid.uuid4().hex[:8]
    client_dir = os.path.join(CLIENTS_DIR, slug)
    if os.path.isdir(client_dir):
        slug = f"{slug}-{uuid.uuid4().hex[:4]}"
        client_dir = os.path.join(CLIENTS_DIR, slug)

    os.makedirs(client_dir)
    default_rubric = engine.load_json(os.path.join(BASE_DIR, "rubric.json"))
    with open(os.path.join(client_dir, "rubric.json"), "w", encoding="utf-8") as f:
        json.dump(default_rubric, f, indent=2)

    profile = dict(EMPTY_PROFILE)
    profile["host_name"] = host_name
    profile["onboarded"] = False
    with open(os.path.join(client_dir, "profile.json"), "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=2)

    return redirect(url_for("onboarding_profile", slug=slug))


if __name__ == "__main__":
    os.makedirs(CLIENTS_DIR, exist_ok=True)
    app.run(debug=True, port=5000)
