"""
ConsultBae Task 2 — small API in front of the Task 1 database.

This exposes the SAME matching logic used in db/merge.py (email > phone > name)
as an HTTP endpoint, so a no-code tool like n8n can call it without needing to
run Python itself.

Run locally with:
    pip install flask
    python3 app.py
Then it listens on http://localhost:5000

Endpoint:
    POST /check-duplicate
    body (JSON): { "name": "...", "email": "...", "phone": "..." }
    returns:      { "duplicate_found": true/false, "person_id": ..., "match_method": "..." }
"""

import re
import sqlite3
from pathlib import Path

from flask import Flask, request, jsonify

DB_PATH = Path(__file__).parent.parent / "db" / "consultbae.db"

app = Flask(__name__)


def norm_email(raw):
    if not raw or not raw.strip():
        return None
    return raw.strip().lower()


def norm_phone(raw):
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) < 10:
        return None
    return digits[-10:]


def norm_name(raw):
    if not raw:
        return None
    return re.sub(r"\s+", " ", raw.strip()).lower()


@app.route("/check-duplicate", methods=["POST"])
def check_duplicate():
    payload = request.get_json(force=True) or {}
    name = norm_name(payload.get("name"))
    email = norm_email(payload.get("email"))
    phone = norm_phone(payload.get("phone"))

    if not (name or email or phone):
        return jsonify({"error": "Provide at least one of name, email, phone"}), 400

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Same priority as the Task 1 merge: email > phone > name
    if email:
        row = conn.execute(
            "SELECT * FROM people WHERE primary_email = ?", (email,)
        ).fetchone()
        if row:
            conn.close()
            return jsonify(_found(row, "email"))

    if phone:
        row = conn.execute(
            "SELECT * FROM people WHERE primary_phone = ?", (phone,)
        ).fetchone()
        if row:
            conn.close()
            return jsonify(_found(row, "phone"))

    if name:
        rows = conn.execute(
            "SELECT * FROM people WHERE canonical_name = ?", (name,)
        ).fetchall()
        conn.close()
        if len(rows) == 1:
            return jsonify(_found(rows[0], "name (low confidence)"))
        if len(rows) > 1:
            return jsonify({
                "duplicate_found": True,
                "ambiguous": True,
                "candidate_person_ids": [r["person_id"] for r in rows],
                "match_method": "name (ambiguous — multiple different people share this name)",
            })
        return jsonify({"duplicate_found": False})

    conn.close()
    return jsonify({"duplicate_found": False})


def _found(row, method):
    return {
        "duplicate_found": True,
        "person_id": row["person_id"],
        "canonical_name": row["canonical_name"],
        "primary_email": row["primary_email"],
        "primary_phone": row["primary_phone"],
        "match_method": method,
    }


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
