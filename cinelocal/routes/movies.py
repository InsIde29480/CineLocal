"""API du catalogue : liste des films/séries et rechargement de la bibliothèque."""

from flask import Blueprint, jsonify

from .. import library

bp = Blueprint("movies", __name__)


@bp.route("/api/movies")
def api_movies():
    return jsonify(library.get_movies())


@bp.route("/api/movies/refresh")
def api_refresh():
    library.invalidate_cache()
    return jsonify({"status": "ok", "count": len(library.get_movies())})
