"""
Routes de lecture : stream navigateur, stream/HLS Chromecast et état du
remux audio.
"""

import logging

from flask import Blueprint, Response, jsonify, request, send_file

from .. import config, library
from ..media import streaming
from ..media.ffprobe import probe_codecs

log = logging.getLogger(__name__)

bp = Blueprint("streaming", __name__)


@bp.route("/api/audio_status/<movie_id>/<int:idx>")
def api_audio_status(movie_id, idx):
    movie = library.get_movie_by_id(movie_id)
    if not movie:
        return jsonify({"status": "error", "message": "Film introuvable"}), 404
    state = streaming.start_audio_remux(movie, idx)
    return jsonify({k: v for k, v in state.items() if k != "process"})


# ─── STREAM PC (fichier brut + piste audio optionnelle) ───────────────────────

@bp.route("/stream/<movie_id>")
def stream_video(movie_id):
    """
    Stream pour lecture navigateur.
    ?audio=N : sélectionne une piste audio spécifique (remux cache, seekable).
    Sans paramètre : fichier brut (chargement instantané).
    """
    movie = library.get_movie_by_id(movie_id)
    if not movie:
        return Response("Film introuvable", status=404)

    filepath = movie["path"]
    audio_idx = request.args.get("audio", default=None, type=int)

    if audio_idx is not None:
        out_path = streaming.ensure_audio_ready(movie, audio_idx)
        if out_path is None:
            return Response("Échec préparation de la piste audio", status=500)
        log.info("PC piste %d (remux cache) : %s", audio_idx, movie['filename'])
        return streaming.stream_file_ranged(str(out_path), "video/mp4")

    mimetype = config.MIME_MAP.get(movie["ext"], "video/mp4")
    return streaming.stream_file_ranged(filepath, mimetype)


# ─── STREAM CHROMECAST (avec piste audio optionnelle) ────────────────────────

@bp.route("/cast/<movie_id>")
def cast_video(movie_id):
    """
    Stream pour Chromecast.
    ?audio=N : sélectionne une piste audio spécifique.
    Transcode vidéo et/ou audio si nécessaire pour la compatibilité Chromecast.
    """
    movie = library.get_movie_by_id(movie_id)
    if not movie:
        return Response("Film introuvable", status=404)

    filepath = movie["path"]
    codecs = probe_codecs(filepath)
    audio_idx = request.args.get("audio", default=None, type=int)

    # Vidéo compatible Chromecast = H.264 ET 8-bit. Un H.264/HEVC 10-bit
    # serait décodé de travers par le Chromecast (artefacts mauves) → transcodage.
    video_ok = (codecs["video"] in config.VIDEO_OK) and not codecs.get("high_bit")

    # Piste audio alternative + vidéo h264 → utilise le remux cache (seekable)
    if audio_idx is not None and video_ok:
        out_path = streaming.ensure_audio_ready(movie, audio_idx)
        if out_path is None:
            return Response("Échec préparation de la piste audio", status=500)
        log.info("Cast piste audio %d (remux cache) : %s", audio_idx, movie['filename'])
        return streaming.stream_file_ranged(str(out_path), "video/mp4")

    # Argument vidéo. En transcodage on force yuv420p (8-bit) + profil/niveau
    # standard, sinon libx264 garderait le 10-bit de la source.
    v_arg = ["-map", "0:v:0", "-c:v", "copy"] if video_ok else [
        "-map", "0:v:0", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
        "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.1"
    ]

    # Argument audio : piste spécifique ou piste par défaut
    if audio_idx is not None:
        a_arg = ["-map", f"0:a:{audio_idx}", "-c:a", "aac", "-b:a", "192k", "-ac", "2"]
        log.info("Cast piste audio %d (transcodage vidéo) : %s", audio_idx, movie['filename'])
    else:
        audio_ok = codecs["audio"] in config.AUDIO_OK
        if video_ok and audio_ok:
            log.info("Cast direct : %s", movie['filename'])
            return streaming.stream_file_ranged(
                filepath, config.MIME_MAP.get(movie["ext"], "video/mp4"))
        a_arg = ["-map", "0:a:0", "-c:a", "copy"] if audio_ok else [
            "-map", "0:a:0", "-c:a", "aac", "-b:a", "192k", "-ac", "2"
        ]
        log.info("Cast transcodage : %s", movie['filename'])
        log.info("Vidéo %s → %s", codecs['video'], 'copy' if video_ok else 'H.264')
        log.info("Audio %s → %s", codecs['audio'], 'copy' if audio_ok else 'AAC')

    return streaming.transcode_stream(filepath, v_arg, a_arg)


# ─── INFO CAST : le frontend demande quelle URL / quel mime utiliser ─────────

@bp.route("/api/cast_info/<movie_id>")
def cast_info(movie_id):
    """
    Décide du mode de diffusion Chromecast et retourne {url, mime, mode} :
      - direct : fichier brut compatible (send_file avec Range → robuste)
      - remux  : piste audio alternative, MP4 cache seekable (Range → robuste)
      - hls    : transcodage nécessaire → playlist HLS (segments → robuste)
    Le pipe MP4 fragmenté n'est plus utilisé pour le cast : il perdait la
    session à la première reconnexion réseau (plantage après ~1 h).
    """
    movie = library.get_movie_by_id(movie_id)
    if not movie:
        return jsonify({"error": "Film introuvable"}), 404

    codecs = probe_codecs(movie["path"])
    audio_idx = request.args.get("audio", default=None, type=int)
    video_ok = (codecs["video"] in config.VIDEO_OK) and not codecs.get("high_bit")
    audio_ok = codecs["audio"] in config.AUDIO_OK

    if video_ok and audio_idx is None and audio_ok:
        return jsonify({
            "url": f"/cast/{movie_id}",
            "mime": config.MIME_MAP.get(movie["ext"], "video/mp4"),
            "mode": "direct",
        })

    if video_ok and audio_idx is not None:
        return jsonify({
            "url": f"/cast/{movie_id}?audio={audio_idx}",
            "mime": "video/mp4",
            "mode": "remux",
        })

    # Transcodage nécessaire → HLS
    v_arg, a_arg = streaming.cast_transcode_args(codecs, audio_idx)
    variant = f"a{audio_idx}" if audio_idx is not None else "auto"
    playlist = streaming.start_cast_hls(movie, v_arg, a_arg, variant)
    if playlist is None:
        return jsonify({"error": "Échec du démarrage du transcodage"}), 500
    return jsonify({
        "url": f"/cast_hls/{movie_id}/{variant}/index.m3u8",
        "mime": "application/x-mpegURL",
        "mode": "hls",
    })


@bp.route("/cast_hls/<movie_id>/<variant>/<path:fname>")
def cast_hls_files(movie_id, variant, fname):
    """Sert la playlist et les segments HLS depuis le cache."""
    if "/" in fname or ".." in fname:
        return Response("Chemin invalide", status=400)
    f = streaming.hls_dir(movie_id, variant) / fname
    if not f.exists():
        return Response("Introuvable", status=404)
    if fname.endswith(".m3u8"):
        resp = send_file(str(f), mimetype="application/vnd.apple.mpegurl")
        # La playlist grandit pendant le transcodage : jamais de cache.
        resp.headers["Cache-Control"] = "no-store"
        return resp
    return send_file(str(f), mimetype="video/mp2t", conditional=True)
