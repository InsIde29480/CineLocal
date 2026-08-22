"""
API des sous-titres : pistes d'un film, extraction en masse, catalogue pour
la resynchronisation et décalage des timecodes.
"""

import logging
import os
from pathlib import Path

from flask import Blueprint, Response, jsonify, request, send_file

from .. import library, scanner, state
from ..media import subtitles as subs

log = logging.getLogger(__name__)

bp = Blueprint("subtitles", __name__)


@bp.route("/api/tracks/<movie_id>")
def api_tracks(movie_id):
    movie = library.get_movie_by_id(movie_id)
    if not movie:
        return jsonify({"error": "Film introuvable"}), 404
    data = subs.extract_tracks(movie)
    # Sous-titres partagés entre versions (HD/4K) : on fusionne ceux des autres
    # variantes déjà en cache. Leur URL /track/subs/<autre_id>/… reste valide,
    # un VTT étant un fichier de texte indépendant de la vidéo.
    data = subs.with_sibling_subs(movie, data)
    # Durée du film (pour l'affichage de la fiche), calculée si absente.
    data = subs.with_duration(movie, data)
    return jsonify(data)


@bp.route("/api/tracks/<movie_id>/refresh")
def api_tracks_refresh(movie_id):
    subs.clear_tracks_cache(movie_id)
    movie = library.get_movie_by_id(movie_id)
    if not movie:
        return jsonify({"error": "Film introuvable"}), 404
    return jsonify(subs.extract_tracks(movie))


@bp.route("/api/subtitles/scan", methods=["POST"])
def api_subtitles_scan():
    """
    Lance l'extraction en masse des sous-titres de tout le dossier Films.
    ?mode=new    : vérifie tout, saute les fichiers déjà complets (défaut).
    ?mode=retry  : ne reprend que les échecs et les fichiers sans sous-titre.
    ?mode=force  : purge tous les caches puis ré-extrait tout.
    (?force=1 reste accepté comme alias de mode=force.)
    """
    mode = request.args.get("mode", "new")
    if request.args.get("force", "0") in ("1", "true", "yes"):
        mode = "force"
    started = scanner.start_subtitle_scan(mode=mode)
    return jsonify({
        "status": "started" if started else "already_running",
        **scanner.public_scan_state(),
    })


@bp.route("/api/subtitles/status")
def api_subtitles_status():
    """État courant de l'extraction en masse (pour la fenêtre de progression)."""
    return jsonify(scanner.public_scan_state())


@bp.route("/api/subtitles/catalog")
def api_subtitles_catalog():
    """
    Liste tous les sous-titres disponibles (films + épisodes de séries) pour
    l'outil de resynchronisation. Chaque entrée : titre lisible + langue +
    (movie_id, idx) pour cibler le VTT à décaler.
    """
    out = []
    for item in library.get_movies():
        if item["kind"] == "movie":
            ids = [q["id"] for q in item.get("qualities", [])] or [item["id"]]
            title = item["title"] + (f" ({item['year']})" if item.get("year") else "")
            for s in subs.collect_subs_for_ids(ids):
                out.append({
                    "title":    title,
                    "sub":      s["label"],
                    "language": s["language"],
                    "movie_id": s["movie_id"],
                    "idx":      s["idx"],
                })
        else:
            for ep in item.get("episodes", []):
                ids = [q["id"] for q in ep.get("qualities", [])] or [ep["id"]]
                title = (item["title"]
                         + f" - S{ep['season']:02d}E{ep['episode']:02d}")
                for s in subs.collect_subs_for_ids(ids):
                    out.append({
                        "title":    title,
                        "sub":      s["label"],
                        "language": s["language"],
                        "movie_id": s["movie_id"],
                        "idx":      s["idx"],
                    })
    out.sort(key=lambda e: e["title"].lower())
    return jsonify(out)


@bp.route("/api/subtitles/shift", methods=["POST"])
def api_subtitles_shift():
    """
    Décale tous les timecodes d'un sous-titre de `offset` secondes (± décimal).
    Body JSON : { movie_id, idx, offset }. offset>0 = retarde, offset<0 = avance.
    """
    body = request.get_json(silent=True) or {}
    movie_id = str(body.get("movie_id", ""))
    try:
        idx = int(body.get("idx"))
        offset = float(body.get("offset"))
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "paramètres invalides"}), 400
    if offset == 0:
        return jsonify({"status": "error", "message": "décalage nul"}), 400

    cache_dir = state.TRACKS_CACHE_DIR / movie_id
    vtt = cache_dir / f"subs_{idx}.vtt"
    if not vtt.exists():
        return jsonify({"status": "error", "message": "sous-titre introuvable"}), 404

    # 1) Décale le VTT servi au lecteur (effet immédiat).
    n_vtt = subs.shift_subtitle_file(vtt, offset)
    if n_vtt < 0:
        return jsonify({"status": "error", "message": "échec de l'écriture du VTT"}), 500
    if n_vtt == 0:
        return jsonify({"status": "error",
                        "message": "aucun timecode reconnu dans ce sous-titre"}), 422

    # 2) Pour un sous-titre externe : décale aussi le .srt/.vtt SOURCE (à côté de
    #    la vidéo) pour que la modif persiste et que le fichier d'origine change.
    n_src, src_name = 0, None
    meta = subs.read_cached_meta(movie_id)
    track = None
    if meta:
        track = next((t for t in meta.get("subtitle_tracks", []) if t.get("index") == idx), None)
    source = track.get("source") if track else None
    if not source and idx >= 1000:
        mv = library.get_movie_by_id(movie_id)
        if mv:
            lang = track.get("language") if track else None
            found = subs.find_external_source(mv["path"], lang)
            source = str(found) if found else None
    if source and Path(source).exists() and Path(source).suffix.lower() in (".srt", ".vtt"):
        r = subs.shift_subtitle_file(Path(source), offset)
        if r > 0:
            n_src, src_name = r, Path(source).name

    # 3) Évite une ré-extraction parasite : on rend le cache plus récent que le
    #    .srt source (sinon le prochain scan croirait le sous-titre « modifié »).
    try:
        meta_file = cache_dir / "tracks.json"
        if meta_file.exists():
            os.utime(meta_file, None)
    except Exception:
        pass

    log.info("Sous-titre décalé de %+.3fs : %s/subs_%d.vtt (%d timecodes%s)",
             offset, movie_id, idx, n_vtt,
             f", source {src_name}: {n_src}" if src_name else "")
    return jsonify({"status": "ok", "offset": offset,
                    "shifted": n_vtt, "source_shifted": n_src, "source": src_name})


@bp.route("/track/subs/<movie_id>/<int:idx>")
def serve_subs(movie_id, idx):
    vtt_path = state.TRACKS_CACHE_DIR / movie_id / f"subs_{idx}.vtt"
    if vtt_path.exists():
        return send_file(str(vtt_path), mimetype="text/vtt")
    return Response("Sous-titres introuvables", status=404)
