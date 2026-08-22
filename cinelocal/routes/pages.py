"""Pages statiques : l'interface (index.html) et le favicon."""

from flask import Blueprint, Response, send_from_directory

from .. import config

bp = Blueprint("pages", __name__)


@bp.route("/")
def index():
    return send_from_directory(config.STATIC_DIR, "index.html")


@bp.route("/favicon.ico")
def favicon():
    return Response(status=204)
