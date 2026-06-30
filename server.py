#!/usr/bin/env python3
"""
CineLocal - Serveur de films local style Netflix
Lance avec: python server.py
"""

import os
import re
import json
import subprocess
import threading
import shutil
import time
from collections import deque
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from waitress import serve

import requests
from flask import Flask, jsonify, send_file, request, Response, send_from_directory


# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

MOVIES_DIR       = Path.home() / "storage/6TO/Films"
STATIC_DIR       = Path(__file__).parent / "static"
TMDB_CACHE_FILE  = Path.home() / "storage/6TO/.tmdb_cache.json"
TRACKS_CACHE_DIR = Path.home() / "storage/6TO/.tracks_cache"

SUPPORTED_EXTS   = {".mp4", ".mkv"}
HOST             = "0.0.0.0"
PORT             = 8765

TMDB_API_KEY     = "ba6207ce3c9ed44aa35c383f55ebab5e"

VIDEO_OK         = {"h264"}
AUDIO_OK         = {"aac", "mp3"}
BROWSER_AUDIO_OK = {"aac", "mp3", "opus", "vorbis"}
SUBS_TEXT_CODECS = {"subrip", "ass", "ssa", "mov_text", "webvtt", "srt"}
SUBS_LANG_OK     = {"fre", "fra", "fr", "francais", "français", "eng", "en", "english", "und", "vo"}

# Surveillance ffmpeg pour l'extraction des sous-titres.
# Au lieu d'un timeout fixe (qui tue ffmpeg même s'il progresse sur HDD lent),
# on observe son flux stderr : tant qu'il parle, il avance. On tue seulement
# après un silence prolongé (= vraiment bloqué) ou si le plafond absolu est
# atteint (filet de sécurité contre les boucles infinies).
SUBS_IDLE_TIMEOUT = 90      # 1 min 30 sans aucune sortie ffmpeg = bloqué
SUBS_HARD_MAX     = 1800    # 30 min absolu (sécurité, ne devrait jamais arriver)


# ══════════════════════════════════════════════════════════════════════════════
# INITIALISATION FLASK
# ══════════════════════════════════════════════════════════════════════════════

app = Flask(__name__, static_folder=str(STATIC_DIR))
TRACKS_CACHE_DIR.mkdir(parents=True, exist_ok=True)


@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Range'
    response.headers['Access-Control-Expose-Headers'] = (
        'Accept-Ranges, Content-Range, Content-Length, Content-Type'
    )
    return response


# ══════════════════════════════════════════════════════════════════════════════
# PARSING DES NOMS DE FICHIERS
# ══════════════════════════════════════════════════════════════════════════════

_YEAR_PATTERN = r'\b(19[5-9]\d|20[0-3]\d)\b'

_RELEASE_TAGS = (
    r'\b(1080p|720p|480p|2160p|4K|UHD|HDR|'
    r'BluRay|BDRip|BRRip|DVDRip|HDRip|WEBRip|WEB-DL|WEB|HDTV|HDLight|'
    r'HEVC|x264|x265|H\.?264|H\.?265|AVC|'
    r'AAC|AC3|DTS|DTS-HD|MP3|FLAC|'
    r'MULTI|VFF|VFQ|FRENCH|TRUEFRENCH|ENGLISH|'
    r'YIFY|RARBG|FGT|EVO|NTb|GHOSTS?|'
    r'mkv|mp4|avi)\b'
)

_EDITION_TAGS = (
    r'\b(Extended|Director\'?s|Uncut|Unrated|Remastered|Theatrical|Special)'
    r'\s*(Edition|Cut|Version)?\b'
)


def clean_title(filename: str) -> str:
    name = Path(filename).stem
    year_match = re.search(_YEAR_PATTERN, name)
    if year_match:
        name = name[:year_match.end()]
    name = re.sub(_RELEASE_TAGS, '', name, flags=re.IGNORECASE)
    name = re.sub(_EDITION_TAGS, '', name, flags=re.IGNORECASE)
    # Tag de groupe en fin de nom (ex. "-RARBG", "-x264") : retiré uniquement
    # après un tiret et seulement si ça ressemble à un tag (chiffres ou deux
    # majuscules consécutives), pour ne pas amputer un vrai titre
    # ("Le_Grand_Bleu", "Blade-Runner", "Spider-Man"...).
    name = re.sub(r'-[A-Za-z0-9]*(?:\d|[A-Z]{2})[A-Za-z0-9]*$', '', name)
    name = re.sub(r'\[.*?\]|\(.*?\)|\{.*?\}', '', name)
    name = re.sub(r'[\(\[\{].*$', '', name)
    name = re.sub(r'[._]+', ' ', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name.title()


def extract_year(filename: str) -> str | None:
    match = re.search(_YEAR_PATTERN, filename)
    return match.group(1) if match else None


# Qualité déduite du nom de fichier (du plus haut au plus bas).
# Chaque entrée : (motif regex, libellé affiché, hauteur indicative pour le tri).
_QUALITY_PATTERNS = [
    (r'\b(4K|2160p?|UHD)\b',      "4K",    2160),
    (r'\b1440p\b',                "1440p", 1440),
    (r'\b1080p?\b',               "1080p", 1080),
    (r'\b720p?\b',                "720p",   720),
    (r'\b480p?\b',                "480p",   480),
]


def detect_quality(filename: str) -> tuple:
    """
    Renvoie (label, hauteur) déduits du nom de fichier.
    Si aucun marqueur n'est trouvé, on suppose une version "HD" standard
    (c.-à-d. le fichier non tagué d'un couple HD/4K).
    """
    stem = Path(filename).stem
    for pattern, label, height in _QUALITY_PATTERNS:
        if re.search(pattern, stem, flags=re.IGNORECASE):
            return (label, height)
    return ("HD", 1080)


def parse_episode(filename: str) -> dict | None:
    match = re.search(r'[Ss](\d{1,2})[Ee](\d{1,2})', filename)
    if not match:
        return None
    return {"season": int(match.group(1)), "episode": int(match.group(2))}


def series_title(filename: str) -> str:
    name = Path(filename).stem
    name = re.sub(r'[Ss]\d{1,2}[Ee]\d{1,2}.*$', '', name)
    name = re.sub(r'\(?\b(19[5-9]\d|20[0-3]\d)\b\)?', '', name)
    name = re.sub(r'\[.*?\]|\(.*?\)|\{.*?\}', '', name)
    name = re.sub(r'[._\-]+', ' ', name)
    name = re.sub(r'\s+', ' ', name).strip()
    name = re.sub(r'\s*-\s*$', '', name).strip()
    return name.title()


# ══════════════════════════════════════════════════════════════════════════════
# ANALYSE FFPROBE
# ══════════════════════════════════════════════════════════════════════════════

_codec_cache = {}


def probe_codecs(filepath: str) -> dict:
    key = str(filepath)
    if key in _codec_cache:
        return _codec_cache[key]
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", str(filepath)],
            capture_output=True, text=True, timeout=10
        )
        data = json.loads(result.stdout)
        vstream = next((s for s in data.get("streams", [])
                        if s.get("codec_type") == "video"), {})
        vcodec = vstream.get("codec_name")
        vpix   = vstream.get("pix_fmt", "") or ""
        acodec = next((s["codec_name"] for s in data.get("streams", [])
                       if s.get("codec_type") == "audio"), None)
        # 10/12-bit (ex. yuv420p10le) : le Chromecast ne sait pas le décoder
        # et produit des artefacts colorés (teintes mauves).
        high_bit = bool(re.search(r'(10|12|16)(le|be)', vpix))
        codecs = {"video": vcodec, "audio": acodec,
                  "pix_fmt": vpix, "high_bit": high_bit}
    except Exception as e:
        print(f"ffprobe échec : {e}")
        codecs = {"video": None, "audio": None, "pix_fmt": "", "high_bit": False}
    _codec_cache[key] = codecs
    return codecs


# ══════════════════════════════════════════════════════════════════════════════
# EXTRACTION DES SOUS-TITRES
# ══════════════════════════════════════════════════════════════════════════════

def _lang_label(code: str) -> str:
    return {
        "fre": "Français", "fra": "Français", "fr": "Français",
        "eng": "English",  "en": "English",
        "spa": "Español",  "es": "Español",
        "ger": "Deutsch",  "deu": "Deutsch", "de": "Deutsch",
        "ita": "Italiano", "it": "Italiano",
        "jpn": "日本語",    "ja": "日本語",
        "und": "Inconnue",
    }.get((code or "und").lower(), (code or "und").upper())


_extraction_locks = {}


def _safe_remove(path: Path):
    try:
        if path.exists():
            path.unlink()
    except Exception:
        pass


def _run_ffmpeg_subs(cmd: list, vtt_path: Path, label: str,
                     idle_timeout: int = SUBS_IDLE_TIMEOUT,
                     hard_max: int = SUBS_HARD_MAX):
    """
    Lance ffmpeg en surveillant son activité plutôt qu'avec un timeout fixe.

    Renvoie un tuple (ok: bool, reason: str). En cas de succès, reason="".
    En cas d'échec, reason contient un résumé (sortie ffmpeg ou cause du kill)
    utile pour diagnostiquer.
    """
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            bufsize=1,
        )
    except Exception as e:
        _safe_remove(vtt_path)
        msg = f"lancement ffmpeg impossible : {e}"
        print(f"Échec {label} : {msg}")
        return False, msg

    stderr_tail = deque(maxlen=20)
    last_activity = time.monotonic()
    activity_lock = threading.Lock()

    def reader():
        nonlocal last_activity
        try:
            for raw in iter(proc.stderr.readline, b""):
                with activity_lock:
                    last_activity = time.monotonic()
                line = raw.decode("utf-8", errors="replace").rstrip()
                if line:
                    stderr_tail.append(line)
        except Exception:
            pass

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    start = time.monotonic()
    killed_reason = None

    while True:
        if proc.poll() is not None:
            break
        now = time.monotonic()
        with activity_lock:
            silent_for = now - last_activity
        if silent_for > idle_timeout:
            killed_reason = f"aucune progression ffmpeg depuis {int(silent_for)}s"
            break
        if now - start > hard_max:
            killed_reason = f"plafond absolu {hard_max}s atteint"
            break
        time.sleep(1)

    if killed_reason:
        try:
            proc.kill()
        except Exception:
            pass

    try:
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass

    t.join(timeout=2)

    if killed_reason:
        _safe_remove(vtt_path)
        print(f"ffmpeg interrompu {label} : {killed_reason}")
        return False, killed_reason

    if proc.returncode != 0 or not vtt_path.exists() or vtt_path.stat().st_size == 0:
        _safe_remove(vtt_path)
        tail = list(stderr_tail)[-3:]
        joined = " | ".join(tail) if tail else "(stderr vide)"
        reason = f"rc={proc.returncode} - {joined}"
        print(f"ffmpeg échec {label} : {reason}")
        return False, reason

    elapsed = int(time.monotonic() - start)
    if elapsed >= 5:
        print(f"  {label} extrait en {elapsed}s")
    return True, ""


def extract_tracks(movie: dict) -> dict:
    """
    Extrait les sous-titres en VTT + liste les pistes audio (métadonnées).
    Cache dans .tracks_cache/<movie_id>/tracks.json
    """
    movie_id = movie["id"]
    filepath = movie["path"]
    cache_dir = TRACKS_CACHE_DIR / movie_id
    metadata_file = cache_dir / "tracks.json"

    if movie_id not in _extraction_locks:
        _extraction_locks[movie_id] = threading.Lock()

    with _extraction_locks[movie_id]:
        # Vérifie les SRT externes modifiés
        if metadata_file.exists():
            cache_mtime = metadata_file.stat().st_mtime
            movie_dir = Path(filepath).parent
            movie_stem = Path(filepath).stem

            srt_changed = False
            for ext in ('.srt', '.vtt'):
                for sub_file in movie_dir.glob(f"{movie_stem}*{ext}"):
                    if sub_file.stat().st_mtime > cache_mtime:
                        srt_changed = True
                        print(f"Nouveau sous-titre détecté : {sub_file.name}")
                        break
                if srt_changed:
                    break

            if not srt_changed:
                try:
                    cached = json.loads(metadata_file.read_text(encoding="utf-8"))
                    # Auto-détection des caches incomplets (extractions interrompues
                    # par un timeout sur HDD). Si des .vtt orphelins traînent ou si
                    # une exécution précédente n'a pas marqué l'extraction complète,
                    # on relance entièrement.
                    cached_tracks = cached.get("subtitle_tracks", [])
                    vtt_files = list(cache_dir.glob("subs_*.vtt"))
                    extraction_ok = cached.get("extraction_complete", False)
                    if extraction_ok and len(vtt_files) == len(cached_tracks):
                        return cached
                    print(
                        f"Cache incomplet pour {movie['filename']} "
                        f"({len(vtt_files)} VTT sur disque, {len(cached_tracks)} pistes en cache, "
                        f"complete={extraction_ok}) - relance"
                    )
                    # Purge les VTT orphelins avant relance pour repartir propre.
                    for f in vtt_files:
                        _safe_remove(f)
                except Exception:
                    pass

        cache_dir.mkdir(parents=True, exist_ok=True)
        print(f"Extraction des pistes : {movie['filename']}")

        try:
            result = subprocess.run([
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_streams", str(filepath)
            ], capture_output=True, text=True, timeout=30)
            data = json.loads(result.stdout)
        except Exception as e:
            print(f"ffprobe échec : {e}")
            return {"audio_tracks": [], "subtitle_tracks": [], "extraction_complete": False}

        streams = data.get("streams", [])
        audio_tracks = []
        subtitle_tracks = []
        subtitle_failures = []
        audio_idx = 0
        subs_idx = 0
        extraction_complete = True

        for stream in streams:
            codec_type = stream.get("codec_type")
            tags = stream.get("tags", {})
            lang = (tags.get("language", "und") or "und").lower()
            title = tags.get("title", "")

            if codec_type == "audio":
                audio_tracks.append({
                    "index":    audio_idx,
                    "language": lang,
                    "label":    title or _lang_label(lang),
                    "codec":    stream.get("codec_name"),
                    "channels": stream.get("channels"),
                })
                audio_idx += 1

            elif codec_type == "subtitle":
                codec = stream.get("codec_name", "")
                if lang not in SUBS_LANG_OK:
                    print(f"Sous-titres {lang} ignorés (langue non voulue)")
                elif codec in SUBS_TEXT_CODECS:
                    vtt_path = cache_dir / f"subs_{subs_idx}.vtt"
                    ok, reason = _run_ffmpeg_subs(
                        [
                            "ffmpeg", "-y", "-i", str(filepath),
                            "-map", f"0:s:{subs_idx}",
                            "-c:s", "webvtt",
                            str(vtt_path),
                        ],
                        vtt_path,
                        f"sous-titres {subs_idx} ({lang})",
                    )
                    if ok:
                        subtitle_tracks.append({
                            "index":    subs_idx,
                            "language": lang,
                            "label":    title or _lang_label(lang),
                            "url":      f"/track/subs/{movie_id}/{subs_idx}",
                        })
                        print(f"Sous-titres {lang} : {title or codec}")
                    else:
                        extraction_complete = False
                        subtitle_failures.append({
                            "index":    subs_idx,
                            "language": lang,
                            "codec":    codec,
                            "source":   "embedded",
                            "reason":   reason,
                        })
                else:
                    print(f"Sous-titres image ({codec}) ignorés")
                subs_idx += 1

        # Sous-titres externes
        movie_dir = Path(filepath).parent
        movie_stem = Path(filepath).stem

        for ext in ('.srt', '.vtt'):
            for sub_file in movie_dir.glob(f"{movie_stem}.*{ext}"):
                parts = sub_file.stem.split('.')
                lang = parts[-1].lower() if len(parts) > 1 else "und"
                if len(lang) > 3 or not lang.isalpha():
                    lang = "und"
                external_idx = 1000 + subs_idx
                vtt_path = cache_dir / f"subs_{external_idx}.vtt"
                ok = False
                reason = ""
                try:
                    if ext == '.vtt':
                        shutil.copy(sub_file, vtt_path)
                        ok = vtt_path.exists() and vtt_path.stat().st_size > 0
                        if not ok:
                            reason = "copie .vtt vide"
                    else:
                        ok, reason = _run_ffmpeg_subs(
                            ["ffmpeg", "-y", "-i", str(sub_file),
                             "-c:s", "webvtt", str(vtt_path)],
                            vtt_path,
                            f"sous-titres externes {sub_file.name}",
                        )
                except Exception as e:
                    _safe_remove(vtt_path)
                    reason = f"exception : {e}"
                    print(f"Échec sous-titres externes {sub_file.name} : {e}")
                if ok:
                    subtitle_tracks.append({
                        "index":    external_idx,
                        "language": lang,
                        "label":    f"{_lang_label(lang)} (externe)",
                        "url":      f"/track/subs/{movie_id}/{external_idx}",
                    })
                    print(f"Sous-titres externes : {sub_file.name} ({lang})")
                    subs_idx += 1
                else:
                    extraction_complete = False
                    subtitle_failures.append({
                        "index":    external_idx,
                        "language": lang,
                        "codec":    ext.lstrip("."),
                        "source":   "external",
                        "file":     sub_file.name,
                        "reason":   reason or "raison inconnue",
                    })

            simple_sub = movie_dir / f"{movie_stem}{ext}"
            if simple_sub.exists():
                external_idx = 1000 + subs_idx
                vtt_path = cache_dir / f"subs_{external_idx}.vtt"
                ok = False
                reason = ""
                try:
                    if ext == '.vtt':
                        shutil.copy(simple_sub, vtt_path)
                        ok = vtt_path.exists() and vtt_path.stat().st_size > 0
                        if not ok:
                            reason = "copie .vtt vide"
                    else:
                        ok, reason = _run_ffmpeg_subs(
                            ["ffmpeg", "-y", "-i", str(simple_sub),
                             "-c:s", "webvtt", str(vtt_path)],
                            vtt_path,
                            f"sous-titres externes {simple_sub.name}",
                        )
                except Exception as e:
                    _safe_remove(vtt_path)
                    reason = f"exception : {e}"
                    print(f"Échec sous-titres externes {simple_sub.name} : {e}")
                if ok:
                    subtitle_tracks.append({
                        "index":    external_idx,
                        "language": "und",
                        "label":    "Sous-titres (externe)",
                        "url":      f"/track/subs/{movie_id}/{external_idx}",
                    })
                    print(f"Sous-titres externes : {simple_sub.name}")
                    subs_idx += 1
                else:
                    extraction_complete = False
                    subtitle_failures.append({
                        "index":    external_idx,
                        "language": "und",
                        "codec":    ext.lstrip("."),
                        "source":   "external",
                        "file":     simple_sub.name,
                        "reason":   reason or "raison inconnue",
                    })

        metadata = {
            "audio_tracks":        audio_tracks,
            "subtitle_tracks":     subtitle_tracks,
            "subtitle_failures":   subtitle_failures,
            "extraction_complete": extraction_complete,
        }
        metadata_file.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        print(f"    {len(audio_tracks)} piste(s) audio, {len(subtitle_tracks)} sous-titre(s)")
        return metadata


def clear_tracks_cache(movie_id: str):
    cache_dir = TRACKS_CACHE_DIR / movie_id
    if cache_dir.exists():
        shutil.rmtree(cache_dir)


# ──────────────────────────────────────────────────────────────────────────────
# PRÉ-EXTRACTION EN ARRIÈRE-PLAN
# ──────────────────────────────────────────────────────────────────────────────

class _BackgroundExtractor:
    """
    Précharge les pistes/sous-titres de tous les films dans une file traitée
    par un seul worker (mono-thread = pas de thrash disque sur HDD). Les films
    déjà en cache complet sont reconnus instantanément par extract_tracks().
    L'extraction à la demande reste prioritaire : si l'utilisateur clique sur
    un film pas encore traité, sa requête s'exécute en parallèle (le verrou
    par film évite la double extraction si c'est le même).
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._queue = deque()
        self._seen = set()
        self._counts = {"total": 0, "done": 0, "failed": 0, "pending": 0}
        self._current = None
        self._started_at = None
        self._worker = None
        self._failures = {}   # movie_id -> {"filename": ..., "failures": [...]}

    def enqueue(self, movies: list):
        added = 0
        with self._lock:
            for m in movies:
                mid = m.get("id")
                if not mid or mid in self._seen:
                    continue
                self._seen.add(mid)
                self._queue.append(m)
                added += 1
            self._counts["total"] = len(self._seen)
            self._counts["pending"] = len(self._queue)
            if added and self._started_at is None:
                self._started_at = time.time()
            self._ensure_worker_locked()
        if added:
            print(f"Pré-extraction : {added} film(s) ajoutés à la file (total {len(self._seen)})")

    def _ensure_worker_locked(self):
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(
                target=self._run, daemon=True, name="subs-bg"
            )
            self._worker.start()

    def _run(self):
        while True:
            with self._lock:
                if not self._queue:
                    if self._current is not None:
                        print("Pré-extraction : file vide, terminé.")
                    self._current = None
                    return
                movie = self._queue.popleft()
                self._counts["pending"] = len(self._queue)
                self._current = movie.get("filename")
            try:
                meta = extract_tracks(movie)
                ok = meta.get("extraction_complete", False)
                with self._lock:
                    if ok:
                        self._counts["done"] += 1
                        self._failures.pop(movie["id"], None)
                    else:
                        self._counts["failed"] += 1
                        self._failures[movie["id"]] = {
                            "filename": movie.get("filename"),
                            "title":    movie.get("title"),
                            "failures": meta.get("subtitle_failures", []),
                        }
            except Exception as e:
                print(f"Pré-extraction erreur sur {movie.get('filename')} : {e}")
                with self._lock:
                    self._counts["failed"] += 1
                    self._failures[movie["id"]] = {
                        "filename": movie.get("filename"),
                        "title":    movie.get("title"),
                        "failures": [{"reason": f"exception : {e}"}],
                    }

    def status(self) -> dict:
        with self._lock:
            done = self._counts["done"]
            failed = self._counts["failed"]
            total = self._counts["total"]
            processed = done + failed
            return {
                "total":       total,
                "done":        done,
                "failed":      failed,
                "pending":     self._counts["pending"],
                "in_progress": self._current is not None,
                "current":     self._current,
                "progress":    (processed / total) if total else 1.0,
                "elapsed_s":   int(time.time() - self._started_at) if self._started_at else 0,
            }

    def failures(self) -> list:
        with self._lock:
            return [
                {"movie_id": mid, **info}
                for mid, info in self._failures.items()
            ]


_bg_extractor = _BackgroundExtractor()


# ══════════════════════════════════════════════════════════════════════════════
# TMDB
# ══════════════════════════════════════════════════════════════════════════════

_tmdb_lock = threading.Lock()


def _load_tmdb_cache() -> dict:
    if TMDB_CACHE_FILE.exists():
        try:
            return json.loads(TMDB_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_tmdb_cache():
    with _tmdb_lock:
        TMDB_CACHE_FILE.write_text(
            json.dumps(_tmdb_cache, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )


_tmdb_cache = _load_tmdb_cache()


def _tmdb_format(m: dict, title_key: str = "title") -> dict:
    return {
        "poster":   f"https://image.tmdb.org/t/p/w342{m['poster_path']}"    if m.get("poster_path")   else None,
        "backdrop": f"https://image.tmdb.org/t/p/w1280{m['backdrop_path']}" if m.get("backdrop_path") else None,
        "overview": m.get("overview", ""),
        "tmdb_title": m.get(title_key, ""),
    }


def _fetch_tmdb_tv(title: str) -> dict | None:
    clean = re.sub(r'\s*[Ss]\d{1,2}[Ee]\d{1,2}.*$', '', title).strip()
    key = f"TV|{clean}"
    if key in _tmdb_cache:
        return _tmdb_cache[key]
    try:
        r = requests.get(
            "https://api.themoviedb.org/3/search/tv",
            params={"api_key": TMDB_API_KEY, "query": clean, "language": "fr-FR"},
            timeout=5
        )
        results = r.json().get("results", [])
        if results:
            result = _tmdb_format(results[0], title_key="name")
            print(f"TMDB (série) : '{clean}' → '{results[0].get('name')}'")
        else:
            result = None
            print(f"TMDB (série) : '{clean}' → aucun résultat")
        _tmdb_cache[key] = result
        return result
    except Exception as e:
        print(f"TMDB série échec : {e}")
        return None


def _fetch_tmdb_movie(title: str, year: str | None) -> dict | None:
    query = re.sub(_YEAR_PATTERN, '', title).strip()
    key = f"{query}|{year or ''}"
    if key in _tmdb_cache:
        return _tmdb_cache[key]
    try:
        params = {"api_key": TMDB_API_KEY, "query": query, "language": "fr-FR"}
        if year:
            params["year"] = year
        r = requests.get("https://api.themoviedb.org/3/search/movie", params=params, timeout=5)
        results = r.json().get("results", [])
        if not results and year:
            params.pop("year")
            r = requests.get("https://api.themoviedb.org/3/search/movie", params=params, timeout=5)
            results = r.json().get("results", [])
        if results:
            result = _tmdb_format(results[0], title_key="title")
            print(f"TMDB : '{query}' → '{results[0].get('title')}'")
        else:
            result = None
            print(f"TMDB : '{query}' ({year}) → aucun résultat")
        _tmdb_cache[key] = result
        return result
    except Exception as e:
        print(f"TMDB échec pour '{query}' : {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# SCAN
# ══════════════════════════════════════════════════════════════════════════════

def scan_movies() -> list:
    items = []
    series_groups = {}
    movie_groups = {}
    playable = {}        # id de fichier -> dict jouable (path, title, ext...)

    if not MOVIES_DIR.exists():
        print(f"Dossier introuvable : {MOVIES_DIR}")
        _set_playable(playable)
        return items

    for filepath in sorted(MOVIES_DIR.rglob("*")):
        if filepath.suffix.lower() not in SUPPORTED_EXTS:
            continue
        try:
            rel = filepath.relative_to(MOVIES_DIR)
        except ValueError:
            continue
        if any(part.startswith(".") for part in rel.parts):
            continue

        movie_id = str(hash(str(filepath)) & 0xFFFFFFFF)
        ep = parse_episode(filepath.name)

        category = rel.parts[0] if len(rel.parts) > 1 else "Films"

        common = {
            "id":         movie_id,
            "filename":   filepath.name,
            "category":   category,
            "size_mb":    round(filepath.stat().st_size / 1024 / 1024),
            "ext":        filepath.suffix.lower(),
            "path":       str(filepath),
            "stream_url": f"/stream/{movie_id}",
            "cast_url":   f"/cast/{movie_id}",
        }

        if ep:
            stitle = series_title(filepath.name)
            # Plus de séparation par langue/appareil : tous les épisodes
            # d'une même série sont regroupés ensemble.
            group_key = stitle
            episode_data = {
                **common,
                "season":  ep["season"],
                "episode": ep["episode"],
                "title":   f"S{ep['season']:02d}E{ep['episode']:02d}",
            }
            playable[movie_id] = episode_data
            if group_key not in series_groups:
                series_groups[group_key] = {
                    "stitle": stitle, "category": category,
                    "episodes": [],
                }
            series_groups[group_key]["episodes"].append(episode_data)
        else:
            title = clean_title(filepath.name)
            year  = extract_year(filepath.name)
            qlabel, qheight = detect_quality(filepath.name)
            # Les fichiers d'un même film (même titre + année) sont regroupés
            # comme variantes de qualité. clean_title retire déjà 4K/1080p/etc.
            group_key = f"{title}|{year or ''}"
            variant = {
                **common,
                "title":          title,
                "quality":        qlabel,
                "quality_height": qheight,
            }
            playable[movie_id] = variant
            if group_key not in movie_groups:
                movie_groups[group_key] = {
                    "title": title, "year": year,
                    "category": category, "variants": [],
                }
            movie_groups[group_key]["variants"].append(variant)

    for group_key, group in movie_groups.items():
        # Meilleure qualité en premier ; à hauteur égale, le plus gros fichier.
        variants = sorted(
            group["variants"],
            key=lambda v: (v["quality_height"], v["size_mb"]),
            reverse=True,
        )
        primary = variants[0]
        items.append({
            "id":        primary["id"],          # id jouable par défaut (meilleure qualité)
            "title":     group["title"],
            "year":      group["year"],
            "category":  group["category"],
            "size_mb":   primary["size_mb"],
            "ext":       primary["ext"],
            "kind":      "movie",
            "qualities": [
                {
                    "id":      v["id"],
                    "label":   v["quality"],
                    "height":  v["quality_height"],
                    "size_mb": v["size_mb"],
                    "ext":     v["ext"],
                }
                for v in variants
            ],
            "poster":    None, "backdrop": None, "overview": "",
        })

    for group_key, group in series_groups.items():
        series_id = "s_" + str(hash(group_key) & 0xFFFFFFFF)
        group["episodes"].sort(key=lambda e: (e["season"], e["episode"]))
        items.append({
            "id":          series_id,
            "title":       group["stitle"],
            "year":        None,
            "category":    group["category"],
            "size_mb":     sum(e["size_mb"] for e in group["episodes"]),
            "ext":         group["episodes"][0]["ext"],
            "kind":        "series",
            "episodes":    group["episodes"],
            "episode_count": len(group["episodes"]),
            "season_count": len({e["season"] for e in group["episodes"]}),
            "poster":      None, "backdrop": None, "overview": "",
        })

    def fetch_for_item(item):
        if item["kind"] == "series":
            return _fetch_tmdb_tv(item["title"])
        return _fetch_tmdb_movie(item["title"], item.get("year"))

    to_fetch_count = sum(1 for it in items
                         if (it["kind"] == "series" and f"TV|{it['title']}" not in _tmdb_cache)
                         or (it["kind"] == "movie" and f"{re.sub(_YEAR_PATTERN, '', it['title']).strip()}|{it.get('year') or ''}" not in _tmdb_cache))
    if to_fetch_count:
        print(f"Recherche TMDB pour {to_fetch_count} entrée(s)...")

    with ThreadPoolExecutor(max_workers=10) as ex:
        results = list(ex.map(fetch_for_item, items))

    for it, tmdb in zip(items, results):
        if tmdb:
            it["poster"]   = tmdb["poster"]
            it["backdrop"] = tmdb["backdrop"]
            it["overview"] = tmdb["overview"]

    _set_playable(playable)
    _save_tmdb_cache()
    return items


_movies_cache = None
_movies_lock = threading.Lock()
_playable_index = {}        # id de fichier -> dict jouable (rempli par scan_movies)


def _set_playable(mapping: dict):
    global _playable_index
    _playable_index = mapping


def get_movies() -> list:
    global _movies_cache
    with _movies_lock:
        if _movies_cache is None:
            print("Scan des films en cours...")
            _movies_cache = scan_movies()
            print(f" {len(_movies_cache)} film(s) trouvé(s)")
        return _movies_cache


def get_movie_by_id(movie_id: str) -> dict | None:
    # Garantit que le scan a eu lieu (remplit aussi _playable_index).
    get_movies()
    # Toutes les variantes de qualité et tous les épisodes sont indexés à plat
    # par leur id de fichier — c'est ce que la lecture/cast/pistes utilisent.
    return _playable_index.get(movie_id)


# ══════════════════════════════════════════════════════════════════════════════
# STREAMING
# ══════════════════════════════════════════════════════════════════════════════

MIME_MAP = {
    ".mp4":  "video/mp4",
    ".mkv":  "video/x-matroska",
    ".webm": "video/webm",
}


def _stream_file_ranged(filepath: str, mimetype: str):
    # Werkzeug gère les Range nativement + sendfile() noyau (zéro-copie)
    return send_file(filepath, mimetype=mimetype, conditional=True)


# ─── REMUX AUDIO À LA DEMANDE (cache disque, lecture instantanée + seek) ─────

_audio_remux_state = {}
_audio_remux_lock = threading.Lock()


def _audio_cache_path(movie_id: str, audio_idx: int) -> Path:
    return TRACKS_CACHE_DIR / movie_id / f"audio_{audio_idx}.mp4"


def _audio_tmp_path(movie_id: str, audio_idx: int) -> Path:
    return TRACKS_CACHE_DIR / movie_id / f"audio_{audio_idx}.mp4.tmp"


def _start_audio_remux(movie: dict, audio_idx: int) -> dict:
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

        state = _audio_remux_state.get(key)
        if state and state["status"] == "preparing":
            proc = state.get("process")
            if proc is not None and proc.poll() is None:
                progress = 0.0
                if tmp_path.exists():
                    try:
                        src_size = os.path.getsize(filepath)
                        progress = min(0.99, tmp_path.stat().st_size / max(src_size, 1))
                    except Exception:
                        progress = 0.0
                state["progress"] = progress
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

        if track_codec in AUDIO_OK:
            a_args = ["-c:a", "copy"]
            if track_codec == "aac":
                a_args += ["-bsf:a", "aac_adtstoasc"]
        else:
            a_args = ["-c:a", "aac", "-b:a", "192k", "-ac", "2"]

        v_args = ["-c:v", "copy"]
        if vcodec in {"hevc", "h265"}:
            v_args += ["-tag:v", "hvc1"]

        print(f"Remux piste audio {audio_idx} (vidéo={vcodec or '?'}, audio={track_codec or '?'}) : {movie['filename']}")
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
                        print(f"Remux prêt : audio_{audio_idx}.mp4")
                        return
                    except Exception as e:
                        print(f"Renommage échoué : {e}")
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
                print(f"Échec remux piste audio {audio_idx} (code {proc.returncode}){err_excerpt}")
                print(f"Log complet : {log_path}")

        threading.Thread(target=_watch, daemon=True).start()
        _audio_remux_state[key] = {"status": "preparing", "progress": 0.0, "process": proc}
        return {"status": "preparing", "progress": 0.0}


def _ensure_audio_ready(movie: dict, audio_idx: int, timeout: float = 1800.0) -> Path | None:
    """Bloque jusqu'à ce que le MP4 remuxé soit prêt. Retourne le chemin ou None."""
    deadline = time.time() + timeout
    state = _start_audio_remux(movie, audio_idx)
    while state["status"] == "preparing" and time.time() < deadline:
        time.sleep(0.5)
        state = _start_audio_remux(movie, audio_idx)
    if state["status"] == "ready":
        return _audio_cache_path(movie["id"], audio_idx)
    return None


def _transcode_stream(filepath: str, v_arg: list, a_arg: list):
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


# ══════════════════════════════════════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/favicon.ico")
def favicon():
    return Response(status=204)


@app.route("/api/movies")
def api_movies():
    return jsonify(get_movies())


@app.route("/api/movies/refresh")
def api_refresh():
    global _movies_cache
    with _movies_lock:
        _movies_cache = None
    movies = get_movies()
    _bg_extractor.enqueue(movies)
    return jsonify({"status": "ok", "count": len(movies)})


@app.route("/api/tracks/<movie_id>")
def api_tracks(movie_id):
    movie = get_movie_by_id(movie_id)
    if not movie:
        return jsonify({"error": "Film introuvable"}), 404
    return jsonify(extract_tracks(movie))


@app.route("/api/tracks/<movie_id>/refresh")
def api_tracks_refresh(movie_id):
    clear_tracks_cache(movie_id)
    movie = get_movie_by_id(movie_id)
    if not movie:
        return jsonify({"error": "Film introuvable"}), 404
    return jsonify(extract_tracks(movie))


@app.route("/api/extraction/status")
def api_extraction_status():
    return jsonify(_bg_extractor.status())


@app.route("/api/extraction/failures")
def api_extraction_failures():
    return jsonify(_bg_extractor.failures())


@app.route("/api/audio_status/<movie_id>/<int:idx>")
def api_audio_status(movie_id, idx):
    movie = get_movie_by_id(movie_id)
    if not movie:
        return jsonify({"status": "error", "message": "Film introuvable"}), 404
    state = _start_audio_remux(movie, idx)
    return jsonify({k: v for k, v in state.items() if k != "process"})


@app.route("/track/subs/<movie_id>/<int:idx>")
def serve_subs(movie_id, idx):
    vtt_path = TRACKS_CACHE_DIR / movie_id / f"subs_{idx}.vtt"
    if vtt_path.exists():
        return send_file(str(vtt_path), mimetype="text/vtt")
    return Response("Sous-titres introuvables", status=404)


# ─── STREAM PC (fichier brut + piste audio optionnelle) ───────────────────────

@app.route("/stream/<movie_id>")
def stream_video(movie_id):
    """
    Stream pour lecture navigateur.
    ?audio=N : sélectionne une piste audio spécifique (remux cache, seekable).
    Sans paramètre : fichier brut (chargement instantané).
    """
    movie = get_movie_by_id(movie_id)
    if not movie:
        return Response("Film introuvable", status=404)

    filepath = movie["path"]
    audio_idx = request.args.get("audio", default=None, type=int)

    if audio_idx is not None:
        out_path = _ensure_audio_ready(movie, audio_idx)
        if out_path is None:
            return Response("Échec préparation de la piste audio", status=500)
        print(f"PC piste {audio_idx} (remux cache) : {movie['filename']}")
        return _stream_file_ranged(str(out_path), "video/mp4")

    mimetype = MIME_MAP.get(movie["ext"], "video/mp4")
    return _stream_file_ranged(filepath, mimetype)


# ─── STREAM CHROMECAST (avec piste audio optionnelle) ────────────────────────

@app.route("/cast/<movie_id>")
def cast_video(movie_id):
    """
    Stream pour Chromecast.
    ?audio=N : sélectionne une piste audio spécifique.
    Transcode vidéo et/ou audio si nécessaire pour la compatibilité Chromecast.
    """
    movie = get_movie_by_id(movie_id)
    if not movie:
        return Response("Film introuvable", status=404)

    filepath = movie["path"]
    codecs = probe_codecs(filepath)
    audio_idx = request.args.get("audio", default=None, type=int)

    # Vidéo compatible Chromecast = H.264 ET 8-bit. Un H.264/HEVC 10-bit
    # serait décodé de travers par le Chromecast (artefacts mauves) → transcodage.
    video_ok = (codecs["video"] in VIDEO_OK) and not codecs.get("high_bit")

    # Piste audio alternative + vidéo h264 → utilise le remux cache (seekable)
    if audio_idx is not None and video_ok:
        out_path = _ensure_audio_ready(movie, audio_idx)
        if out_path is None:
            return Response("Échec préparation de la piste audio", status=500)
        print(f"Cast piste audio {audio_idx} (remux cache) : {movie['filename']}")
        return _stream_file_ranged(str(out_path), "video/mp4")

    # Argument vidéo. En transcodage on force yuv420p (8-bit) + profil/niveau
    # standard, sinon libx264 garderait le 10-bit de la source.
    v_arg = ["-map", "0:v:0", "-c:v", "copy"] if video_ok else [
        "-map", "0:v:0", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
        "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.1"
    ]

    # Argument audio : piste spécifique ou piste par défaut
    if audio_idx is not None:
        a_arg = ["-map", f"0:a:{audio_idx}", "-c:a", "aac", "-b:a", "192k", "-ac", "2"]
        print(f"Cast piste audio {audio_idx} (transcodage vidéo) : {movie['filename']}")
    else:
        audio_ok = codecs["audio"] in AUDIO_OK
        if video_ok and audio_ok:
            print(f"Cast direct : {movie['filename']}")
            return _stream_file_ranged(filepath, MIME_MAP.get(movie["ext"], "video/mp4"))
        a_arg = ["-map", "0:a:0", "-c:a", "copy"] if audio_ok else [
            "-map", "0:a:0", "-c:a", "aac", "-b:a", "192k", "-ac", "2"
        ]
        print(f"Cast transcodage : {movie['filename']}")
        print(f"Vidéo {codecs['video']} → {'copy' if video_ok else 'H.264'}")
        print(f"Audio {codecs['audio']} → {'copy' if audio_ok else 'AAC'}")

    return _transcode_stream(filepath, v_arg, a_arg)


# ─── LECTURE LOCALE HDMI (MPV) ────────────────────────────────────────────────

_current_player = None


@app.route("/play/<movie_id>", methods=["POST"])
def play_local(movie_id):
    """
    Lecture sur la sortie HDMI du Pi via MPV, en plein écran.
    Paramètres optionnels (query string) :
      ?audio=N  → index de la piste audio (0-based, tel que renvoyé par /api/tracks)
      ?sub=M    → index du sous-titre (0-based interne, ou >=1000 pour un fichier externe)
                  absent = aucun sous-titre
    """
    global _current_player
    movie = get_movie_by_id(movie_id)
    if not movie:
        return jsonify({"status": "error", "message": "Film introuvable"}), 404

    audio_idx = request.args.get("audio", default=None, type=int)
    sub_idx   = request.args.get("sub",   default=None, type=int)

    if _current_player and _current_player.poll() is None:
        _current_player.terminate()

    mpv_cmd = [
        "mpv", "--fullscreen", "--hwdec=auto", "--no-osc",
        "--no-input-default-bindings",
    ]

    # Piste audio : MPV utilise un identifiant 1-based, notre index est 0-based
    if audio_idx is not None:
        mpv_cmd.append(f"--aid={audio_idx + 1}")

    # Sous-titres
    if sub_idx is None:
        mpv_cmd.append("--sid=no")
    elif sub_idx >= 1000:
        # Sous-titre externe : on charge le VTT mis en cache
        vtt_path = TRACKS_CACHE_DIR / movie_id / f"subs_{sub_idx}.vtt"
        if vtt_path.exists():
            mpv_cmd.append(f"--sub-file={vtt_path}")
    else:
        # Sous-titre interne : MPV 1-based
        mpv_cmd.append(f"--sid={sub_idx + 1}")

    mpv_cmd.append(movie["path"])

    _current_player = subprocess.Popen(mpv_cmd, env={**os.environ, "DISPLAY": ":0"})

    print(f"Lecture locale : {movie['filename']} (audio={audio_idx}, sub={sub_idx})")
    return jsonify({"status": "ok", "playing": movie["title"]})


@app.route("/stop", methods=["POST"])
def stop_local():
    global _current_player
    if _current_player and _current_player.poll() is None:
        _current_player.terminate()
        _current_player = None
        return jsonify({"status": "stopped"})
    return jsonify({"status": "nothing_playing"})


# ══════════════════════════════════════════════════════════════════════════════
# LANCEMENT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("━" * 60)
    print("CineLocal — Serveur de films local")
    print(f"Dossier films : {MOVIES_DIR}")
    print(f"Interface     : http://localhost:{PORT}")
    print(f"Pour TV/Cast  : http://<ton-ip>:{PORT}")
    print("━" * 60)

    movies = get_movies()
    _bg_extractor.enqueue(movies)
    serve(app, host=HOST, port=PORT, threads=8)
