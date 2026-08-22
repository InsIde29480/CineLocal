"""
Streaming et transcodage.

- Streaming du fichier brut avec support des Range (lecture navigateur).
- Remux à la demande d'une piste audio alternative dans un MP4 cache
  (lecture instantanée + seek, indispensable pour le Cast).
- Transcodage à la volée en MP4 fragmenté (repli).
- Transcodage HLS pour le Chromecast (segments sur disque, robuste aux
  micro-coupures réseau).
"""

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from flask import Response, send_file

from .. import config, state

log = logging.getLogger(__name__)


def stream_file_ranged(filepath: str, mimetype: str):
    # Werkzeug gère les Range nativement + sendfile() noyau (zéro-copie)
    return send_file(filepath, mimetype=mimetype, conditional=True)


# ─── REMUX AUDIO À LA DEMANDE (cache disque, lecture instantanée + seek) ─────

_audio_remux_state = {}
_audio_remux_lock = threading.Lock()


def _audio_cache_path(movie_id: str, audio_idx: int) -> Path:
    return state.TRACKS_CACHE_DIR / movie_id / f"audio_{audio_idx}.mp4"


def _audio_tmp_path(movie_id: str, audio_idx: int) -> Path:
    return state.TRACKS_CACHE_DIR / movie_id / f"audio_{audio_idx}.mp4.tmp"


def start_audio_remux(movie: dict, audio_idx: int) -> dict:
    """
    Lance (ou poursuit) le remux d'une piste audio alternative dans un MP4 cache.
    Retourne l'état courant : {status: ready|preparing|error, progress: 0..1}.
    """
    movie_id = movie["id"]
    filepath = movie["path"]
    out_path = _audio_cache_path(movie_id, audio_idx)
    tmp_path = _audio_tmp_path(movie_id, audio_idx)
    key = f"{movie_id}:{audio_idx}"

    with _audio_remux_lock:
        if out_path.exists() and out_path.stat().st_size > 0:
            return {"status": "ready", "progress": 1.0}

        st = _audio_remux_state.get(key)
        if st and st["status"] == "preparing":
            proc = st.get("process")
            if proc is not None and proc.poll() is None:
                progress = 0.0
                if tmp_path.exists():
                    try:
                        src_size = os.path.getsize(filepath)
                        progress = min(0.99, tmp_path.stat().st_size / max(src_size, 1))
                    except Exception:
                        progress = 0.0
                st["progress"] = progress
                return {"status": "preparing", "progress": progress}
            if out_path.exists() and out_path.stat().st_size > 0:
                _audio_remux_state[key] = {"status": "ready", "progress": 1.0}
                return {"status": "ready", "progress": 1.0}
            _audio_remux_state[key] = {"status": "error", "progress": 0.0}
            return {"status": "error", "progress": 0.0}

        out_path.parent.mkdir(parents=True, exist_ok=True)
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass

        try:
            result = subprocess.run([
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-select_streams", f"a:{audio_idx}",
                "-show_streams", str(filepath)
            ], capture_output=True, text=True, timeout=10)
            track_codec = json.loads(result.stdout).get("streams", [{}])[0].get("codec_name", "")
        except Exception:
            track_codec = ""

        try:
            vresult = subprocess.run([
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-select_streams", "v:0",
                "-show_streams", str(filepath)
            ], capture_output=True, text=True, timeout=10)
            vcodec = json.loads(vresult.stdout).get("streams", [{}])[0].get("codec_name", "")
        except Exception:
            vcodec = ""

        if track_codec in config.AUDIO_OK:
            a_args = ["-c:a", "copy"]
            if track_codec == "aac":
                a_args += ["-bsf:a", "aac_adtstoasc"]
        else:
            a_args = ["-c:a", "aac", "-b:a", "192k", "-ac", "2"]

        v_args = ["-c:v", "copy"]
        if vcodec in {"hevc", "h265"}:
            v_args += ["-tag:v", "hvc1"]

        log.info("Remux piste audio %d (vidéo=%s, audio=%s) : %s",
                 audio_idx, vcodec or '?', track_codec or '?', movie['filename'])
        log_path = out_path.parent / f"audio_{audio_idx}.log"
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(filepath),
            "-map", "0:v:0", *v_args,
            "-map", f"0:a:{audio_idx}", *a_args,
            "-sn", "-dn",
            "-movflags", "+faststart",
            "-f", "mp4",
            str(tmp_path)
        ]
        try:
            log_fh = open(log_path, "wb")
        except Exception:
            log_fh = subprocess.DEVNULL
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=log_fh)

        def _watch():
            proc.wait()
            if hasattr(log_fh, "close"):
                try:
                    log_fh.close()
                except Exception:
                    pass
            with _audio_remux_lock:
                if tmp_path.exists() and tmp_path.stat().st_size > 0 and proc.returncode == 0:
                    try:
                        tmp_path.rename(out_path)
                        _audio_remux_state[key] = {"status": "ready", "progress": 1.0}
                        log.info("Remux prêt : audio_%d.mp4", audio_idx)
                        return
                    except Exception as e:
                        log.warning("Renommage échoué : %s", e)
                if tmp_path.exists():
                    try:
                        tmp_path.unlink()
                    except Exception:
                        pass
                _audio_remux_state[key] = {"status": "error", "progress": 0.0}
                err_excerpt = ""
                try:
                    if log_path.exists():
                        err_excerpt = log_path.read_text(errors="replace").strip().splitlines()[-5:]
                        err_excerpt = "\n      " + "\n      ".join(err_excerpt) if err_excerpt else ""
                except Exception:
                    pass
                log.warning("Échec remux piste audio %d (code %s)%s",
                            audio_idx, proc.returncode, err_excerpt)
                log.warning("Log complet : %s", log_path)

        threading.Thread(target=_watch, daemon=True).start()
        _audio_remux_state[key] = {"status": "preparing", "progress": 0.0, "process": proc}
        return {"status": "preparing", "progress": 0.0}


def ensure_audio_ready(movie: dict, audio_idx: int, timeout: float = 1800.0) -> Path | None:
    """Bloque jusqu'à ce que le MP4 remuxé soit prêt. Retourne le chemin ou None."""
    deadline = time.time() + timeout
    st = start_audio_remux(movie, audio_idx)
    while st["status"] == "preparing" and time.time() < deadline:
        time.sleep(0.5)
        st = start_audio_remux(movie, audio_idx)
    if st["status"] == "ready":
        return _audio_cache_path(movie["id"], audio_idx)
    return None


# ─── TRANSCODAGE À LA VOLÉE (MP4 fragmenté) ──────────────────────────────────

def transcode_stream(filepath: str, v_arg: list, a_arg: list):
    cmd = [
        "ffmpeg", "-i", str(filepath),
        *v_arg, *a_arg,
        "-f", "mp4",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof+faststart",
        "-"
    ]

    def generate():
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            proc.kill()
            proc.wait()

    return Response(generate(), mimetype="video/mp4")


# ─── TRANSCODAGE HLS POUR LE CAST ─────────────────────────────────────────────
# Le pipe MP4 fragmenté (transcode_stream) est fragile : pas de Content-Length,
# pas de Range, pas de durée. À la moindre micro-coupure Wi-Fi ou stall TCP, le
# Chromecast re-demande la ressource avec un en-tête Range… que le pipe ne peut
# pas honorer : il repart de zéro et la session est perdue (typiquement après
# ~1 h de lecture). Le HLS règle ça : ffmpeg écrit des segments de 4 s sur
# disque, servis en fichiers statiques. Le Chromecast peut re-télécharger un
# segment, se reconnecter, re-buffériser… sans jamais perdre la session.

_hls_lock  = threading.Lock()
_hls_procs = {}   # "movie_id:variant" -> Popen


def hls_dir(movie_id: str, variant: str) -> Path:
    return state.TRACKS_CACHE_DIR / movie_id / f"hls_{variant}"


def _hls_finished(playlist: Path) -> bool:
    try:
        return playlist.exists() and "#EXT-X-ENDLIST" in playlist.read_text(errors="ignore")
    except Exception:
        return False


def start_cast_hls(movie: dict, v_arg: list, a_arg: list, variant: str) -> Path | None:
    """
    Lance (ou réutilise) un transcodage HLS vers le cache.
    Bloque jusqu'à ce que la playlist contienne ses premiers segments
    (démarrage en quelques secondes), puis retourne son chemin — ou None
    si ffmpeg meurt avant.
    """
    out_dir  = hls_dir(movie["id"], variant)
    playlist = out_dir / "index.m3u8"
    key      = f"{movie['id']}:{variant}"

    with _hls_lock:
        if _hls_finished(playlist):
            return playlist                      # déjà transcodé entièrement

        proc = _hls_procs.get(key)
        if proc is None or proc.poll() is not None:
            # (Re)démarrage propre : on purge les restes d'une session tuée.
            shutil.rmtree(out_dir, ignore_errors=True)
            out_dir.mkdir(parents=True, exist_ok=True)
            cmd = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(movie["path"]),
                *v_arg, *a_arg,
                "-sn", "-dn",
                "-f", "hls",
                "-hls_time", "4",
                "-hls_playlist_type", "event",
                "-hls_flags", "independent_segments+temp_file",
                "-hls_segment_filename", str(out_dir / "seg_%05d.ts"),
                str(playlist),
            ]
            log_fh = open(out_dir / "ffmpeg.log", "wb")
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=log_fh)
            _hls_procs[key] = proc
            log.info("HLS transcodage démarré (%s) : %s", variant, movie['filename'])

    # Attend les premiers segments pour que le Chromecast démarre sans 404.
    deadline = time.time() + 90
    while time.time() < deadline:
        if playlist.exists() and playlist.stat().st_size > 0:
            return playlist
        if proc.poll() is not None and not _hls_finished(playlist):
            log.warning("HLS échec (voir %s)", out_dir / 'ffmpeg.log')
            return None
        time.sleep(0.25)
    return None


def cast_transcode_args(codecs: dict, audio_idx: int | None):
    """Arguments ffmpeg vidéo/audio pour le transcodage Chromecast."""
    video_ok = (codecs["video"] in config.VIDEO_OK) and not codecs.get("high_bit")
    v_arg = ["-map", "0:v:0", "-c:v", "copy"] if video_ok else [
        "-map", "0:v:0", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
        "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.1"
    ]
    if audio_idx is not None:
        a_arg = ["-map", f"0:a:{audio_idx}", "-c:a", "aac", "-b:a", "192k", "-ac", "2"]
    else:
        audio_ok = codecs["audio"] in config.AUDIO_OK
        a_arg = ["-map", "0:a:0", "-c:a", "copy"] if audio_ok else [
            "-map", "0:a:0", "-c:a", "aac", "-b:a", "192k", "-ac", "2"
        ]
    return v_arg, a_arg
