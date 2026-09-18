#!/usr/bin/env python3
"""
Guest Scorer - client-facing web UI.

Each client gets a private URL: /c/<client_slug>/
Client data (rubric + scored guests) lives in clients/<slug>/ - you (the
operator) create/customize a client's rubric.json by hand; clients never edit
their own rubric through the UI.

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


def client_paths(slug):
    client_dir = os.path.join(CLIENTS_DIR, slug)
    if not os.path.isdir(client_dir):
        abort(404, description=f"Unknown client '{slug}'")
    return {
        "dir": client_dir,
        "rubric": os.path.join(client_dir, "rubric.json"),
        "results": os.path.join(client_dir, "guests_scored.json"),
    }


@app.route("/")
def home():
    clients = sorted(
        d for d in os.listdir(CLIENTS_DIR) if os.path.isdir(os.path.join(CLIENTS_DIR, d))
    )
    return render_template("home.html", clients=clients)


@app.route("/c/<slug>/")
def dashboard(slug):
    paths = client_paths(slug)
    tier = request.args.get("tier") or None
    source = request.args.get("source") or None
    results = engine.ranked_results(paths["results"], tier=tier, source=source)
    rubric = engine.load_json(paths["rubric"])
    return render_template(
        "dashboard.html",
        slug=slug,
        results=results,
        rubric=rubric,
        active_tier=tier,
        active_source=source,
    )


@app.route("/c/<slug>/rubric")
def rubric_view(slug):
    paths = client_paths(slug)
    rubric = engine.load_json(paths["rubric"])
    return render_template("rubric.html", slug=slug, rubric=rubric)


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


@app.route("/c/<slug>/score/new", methods=["GET", "POST"])
def score_new(slug):
    paths = client_paths(slug)
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
    try:
        record = engine.score_guest(rubric, guest, engine.DEFAULT_MODEL, "inbound", "web-form")
    except SystemExit as e:
        return render_template("score_form.html", slug=slug, error=str(e), form=guest)

    engine.save_result(record, paths["results"])
    return redirect(url_for("dashboard", slug=slug))


@app.route("/c/<slug>/find/new", methods=["GET", "POST"])
def find_new(slug):
    paths = client_paths(slug)
    if request.method == "GET":
        return render_template("find_form.html", slug=slug, error=None, form={})

    criteria = {
        "topic": request.form.get("topic", "").strip(),
        "role": request.form.get("role", "").strip(),
        "recency": request.form.get("recency", "").strip(),
        "location": request.form.get("location", "").strip(),
        "exclude": request.form.get("exclude", "").strip(),
        "max_candidates": int(request.form.get("max_candidates") or 5),
    }
    if not criteria["topic"]:
        return render_template("find_form.html", slug=slug, error="Topic is required.", form=criteria)

    rubric = engine.load_json(paths["rubric"])
    query = engine.build_find_query(criteria)
    try:
        search_results = engine.search_web(query, max_results=max(criteria["max_candidates"] * 2, 10))
        if not search_results:
            return render_template("find_form.html", slug=slug, error="No search results found - try broadening the criteria.", form=criteria)

        candidates = engine.extract_candidates(criteria, search_results, engine.DEFAULT_MODEL)[: criteria["max_candidates"]]
        if not candidates:
            return render_template("find_form.html", slug=slug, error="No named candidates could be extracted - try broadening the criteria.", form=criteria)

        for candidate in candidates:
            record = engine.score_guest(rubric, candidate, engine.DEFAULT_MODEL, "sourced", "web-search")
            engine.save_result(record, paths["results"])
    except SystemExit as e:
        return render_template("find_form.html", slug=slug, error=str(e), form=criteria)

    return redirect(url_for("dashboard", slug=slug, source="sourced"))


@app.route("/admin/clients/new", methods=["GET", "POST"])
def new_client(slug=None):
    if request.method == "GET":
        return render_template("new_client.html", error=None)

    name = request.form.get("name", "").strip()
    if not name:
        return render_template("new_client.html", error="Client name is required.")

    slug = "".join(c if c.isalnum() else "-" for c in name.lower()).strip("-") or uuid.uuid4().hex[:8]
    client_dir = os.path.join(CLIENTS_DIR, slug)
    if os.path.isdir(client_dir):
        slug = f"{slug}-{uuid.uuid4().hex[:4]}"
        client_dir = os.path.join(CLIENTS_DIR, slug)

    os.makedirs(client_dir)
    default_rubric = engine.load_json(os.path.join(BASE_DIR, "rubric.json"))
    default_rubric["name"] = f"{name} Scoring Rubric"
    with open(os.path.join(client_dir, "rubric.json"), "w", encoding="utf-8") as f:
        json.dump(default_rubric, f, indent=2)

    return redirect(url_for("dashboard", slug=slug))


if __name__ == "__main__":
    os.makedirs(CLIENTS_DIR, exist_ok=True)
    app.run(debug=True, port=5000)
