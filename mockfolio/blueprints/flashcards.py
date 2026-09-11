"""Flashcard study pages, spaced-repetition progress, and user-created cards."""
from flask import Blueprint, jsonify, render_template, request

from mockfolio.blueprints.auth import current_user, login_required
from mockfolio.db import get_db

bp = Blueprint("flashcards", __name__)


@bp.route("/learn")
@login_required
def learn():
    return render_template("learn.html")


@bp.route("/flashcards")
@login_required
def flashcards_page():
    return render_template("flashcards.html")


@bp.route("/api/flashcard/progress", methods=["GET"])
@login_required
def get_flash_progress():
    db = get_db()
    user = current_user()
    rows = db.execute(
        "SELECT card_id, known, seen FROM flashcard_progress WHERE user_id=?",
        (user["id"],)
    ).fetchall()
    return jsonify({r["card_id"]: {"known": r["known"], "seen": r["seen"]} for r in rows})


@bp.route("/api/flashcard/progress", methods=["POST"])
@login_required
def update_flash_progress():
    data = request.get_json()
    card_id = data.get("card_id")
    try:
        known = int(data.get("known", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid parameters"}), 400
    db = get_db()
    user = current_user()
    db.execute("""
        INSERT INTO flashcard_progress (user_id, card_id, known, seen)
        VALUES (?, ?, ?, 1)
        ON CONFLICT(user_id, card_id) DO UPDATE SET
            known = excluded.known,
            seen = seen + 1
    """, (user["id"], card_id, known))
    db.commit()
    return jsonify({"ok": True})


@bp.route("/api/flashcard/custom", methods=["GET"])
@login_required
def get_custom_cards():
    db = get_db()
    user = current_user()
    rows = db.execute(
        "SELECT * FROM custom_cards WHERE user_id=? ORDER BY created_at DESC",
        (user["id"],)
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@bp.route("/api/flashcard/custom", methods=["POST"])
@login_required
def create_custom_card():
    data = request.get_json()
    term = (data.get("term") or "").strip()
    definition = (data.get("definition") or "").strip()
    category = (data.get("category") or "My Cards").strip()
    if not term or not definition:
        return jsonify({"error": "Term and definition are required"}), 400
    db = get_db()
    user = current_user()
    db.execute(
        "INSERT INTO custom_cards (user_id, term, definition, category) VALUES (?,?,?,?)",
        (user["id"], term, definition, category)
    )
    db.commit()
    row = db.execute(
        "SELECT * FROM custom_cards WHERE user_id=? ORDER BY id DESC LIMIT 1",
        (user["id"],)
    ).fetchone()
    return jsonify({"ok": True, "card": dict(row)})


@bp.route("/api/flashcard/custom/<int:card_id>", methods=["DELETE"])
@login_required
def delete_custom_card(card_id):
    db = get_db()
    user = current_user()
    db.execute(
        "DELETE FROM custom_cards WHERE id=? AND user_id=?",
        (card_id, user["id"])
    )
    db.commit()
    return jsonify({"ok": True})


@bp.route("/api/flashcard/custom/<int:card_id>", methods=["PUT"])
@login_required
def edit_custom_card(card_id):
    data = request.get_json()
    term = (data.get("term") or "").strip()
    definition = (data.get("definition") or "").strip()
    category = (data.get("category") or "My Cards").strip()
    if not term or not definition:
        return jsonify({"error": "Term and definition are required"}), 400
    db = get_db()
    user = current_user()
    db.execute(
        "UPDATE custom_cards SET term=?, definition=?, category=? WHERE id=? AND user_id=?",
        (term, definition, category, card_id, user["id"])
    )
    db.commit()
    return jsonify({"ok": True})
