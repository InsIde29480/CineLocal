"""API des Paramètres : lecture et enregistrement de la configuration."""

import re
from pathlib import Path

from flask import Blueprint, jsonify, request

from .. import library, state
from .. import settings as settings_mod
from ..settings import SETTINGS, SETTINGS_LOCK, save_settings

bp = Blueprint("settings", __name__)


@bp.route("/api/settings", methods=["GET"])
def api_get_settings():
    """
    Renvoie les paramètres pour préremplir le formulaire.
    Le mot de passe n'est jamais renvoyé : on indique seulement s'il est défini.
    """
    with SETTINGS_LOCK:
        return jsonify({
            "tmdb_api_key":            SETTINGS["tmdb_api_key"],
            "opensubtitles_api_key":   SETTINGS["opensubtitles_api_key"],
            "opensubtitles_username":  SETTINGS["opensubtitles_username"],
            "opensubtitles_langs":     ", ".join(SETTINGS["opensubtitles_langs"] or []),
            "opensubtitles_password_set": bool(SETTINGS["opensubtitles_password"]),
            "movies_dirs":             SETTINGS["movies_dirs"],
            "tracks_cache_dir":        SETTINGS["tracks_cache_dir"],
            "movies_dirs_status": [
                {"path": p, "exists": Path(p).expanduser().exists()}
                for p in SETTINGS["movies_dirs"]
            ],
            "auto_scan_enabled":          bool(SETTINGS["auto_scan_enabled"]),
            "auto_scan_interval_minutes": SETTINGS["auto_scan_interval_minutes"],
        })


@bp.route("/api/settings", methods=["POST"])
def api_save_settings():
    """
    Enregistre les paramètres (persistés dans SETTINGS_FILE).
    Le mot de passe n'est mis à jour que si un nouveau est fourni (non vide).
    Les langues sont acceptées en chaîne « fr, en » ou en liste.
    """
    body = request.get_json(silent=True) or {}
    new = {}

    for key in ("tmdb_api_key", "opensubtitles_api_key", "opensubtitles_username"):
        if key in body:
            new[key] = (body[key] or "").strip()

    # Dossiers des films : liste (ou texte multi-lignes / séparé par des virgules).
    if "movies_dirs" in body:
        raw = body["movies_dirs"]
        if isinstance(raw, list):
            dirs = [str(x).strip() for x in raw if str(x).strip()]
        else:
            dirs = [line.strip() for line in re.split(r"[\r\n]+", str(raw)) if line.strip()]
        if dirs:
            new["movies_dirs"] = dirs

    # Dossier de stockage des sous-titres (unique).
    if "tracks_cache_dir" in body and (body["tracks_cache_dir"] or "").strip():
        new["tracks_cache_dir"] = body["tracks_cache_dir"].strip()

    # Langues : "fr, en" → ["fr", "en"]
    if "opensubtitles_langs" in body:
        raw = body["opensubtitles_langs"]
        if isinstance(raw, list):
            langs = [str(x).strip().lower() for x in raw if str(x).strip()]
        else:
            langs = [p.strip().lower() for p in re.split(r"[,\s]+", str(raw)) if p.strip()]
        new["opensubtitles_langs"] = langs or ["fr", "en"]

    # Mot de passe : seulement s'il est fourni non vide (sinon on garde l'ancien).
    pwd = (body.get("opensubtitles_password") or "").strip()
    if pwd:
        new["opensubtitles_password"] = pwd

    # Analyse automatique
    if "auto_scan_enabled" in body:
        new["auto_scan_enabled"] = bool(body["auto_scan_enabled"])
    if "auto_scan_interval_minutes" in body:
        try:
            new["auto_scan_interval_minutes"] = max(5, int(body["auto_scan_interval_minutes"]))
        except (TypeError, ValueError):
            pass

    ok = save_settings(new)

    # Si un chemin a changé : on rebinde les chemins partagés et on force un
    # rescan de la bibliothèque au prochain /api/movies (nouveaux dossiers).
    paths_changed = ok and any(k in new for k in ("movies_dirs", "tracks_cache_dir"))
    if paths_changed:
        settings_mod.apply_paths_from_settings()
        library.invalidate_cache()

    return jsonify({
        "status":         "ok" if ok else "error",
        "paths_changed":  paths_changed,
        "movies_dirs_status": [
            {"path": str(d), "exists": d.exists()} for d in state.MOVIES_DIRS
        ],
    }), (200 if ok else 500)
