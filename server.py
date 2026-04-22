#!/usr/bin/env python3
"""
CineLocal - Serveur de films local style Netflix
Lance avec: python server.py
"""

import os
import re
import json
import queue
import subprocess
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import requests
from flask import Flask, jsonify, send_file, request, Response, send_from_directory, redirect


# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

MOVIES_DIR       = Path.home() / "nvme_data"
STATIC_DIR       = Path(__file__).parent / "static"
TMDB_CACHE_FILE  = Path(__file__).parent / ".tmdb_cache.json"

SUPPORTED_EXTS   = {".mp4", ".mkv"}
HOST             = "0.0.0.0"
PORT             = 8765

TMDB_API_KEY     = "ba6207ce3c9ed44aa35c383f55ebab5e"

# Codecs compatibles Chromecast (récepteur par défaut)
# Ajoute "hevc" à VIDEO_OK si tu as un Chromecast Ultra / Google TV 4K
VIDEO_OK         = {"h264"}
AUDIO_OK         = {"aac", "mp3"}

# Codecs audio décodables par les navigateurs web
BROWSER_AUDIO_OK = {"aac", "mp3", "opus", "vorbis"}


# ══════════════════════════════════════════════════════════════════════════════
# INITIALISATION FLASK
# ══════════════════════════════════════════════════════════════════════════════

app = Flask(__name__, static_folder=str(STATIC_DIR))


@app.after_request
def add_cors_headers(response):
    """Headers CORS pour compatibilité Chromecast et lecture navigateur."""
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
    """Nettoie un nom de fichier pour afficher un titre lisible."""
    name = Path(filename).stem

    # Retire les tags personnels _PC / _TV / _VF / _EN (EN PREMIER)
    # Le `\b` ne fonctionne pas après _, on utilise une lookahead
    name = re.sub(r'_(?:PC|TV|VF|EN)(?=_|$|\.)', '', name, flags=re.IGNORECASE)

    # Tronque après l'année (vire tous les tags techniques qui suivent)
    year_match = re.search(_YEAR_PATTERN, name)
    if year_match:
        name = name[:year_match.end()]

    # Supprime les tags de release et d'édition
    name = re.sub(_RELEASE_TAGS, '', name, flags=re.IGNORECASE)
    name = re.sub(_EDITION_TAGS, '', name, flags=re.IGNORECASE)

    # Supprime les suffixes de releaser (ex: -gismo65)
    name = re.sub(r'[-_][a-zA-Z0-9]+$', '', name)

    # Supprime crochets/parenthèses et leur contenu
    name = re.sub(r'\[.*?\]|\(.*?\)|\{.*?\}', '', name)
    name = re.sub(r'[\(\[\{].*$', '', name)

    # Séparateurs → espaces
    name = re.sub(r'[._]+', ' ', name)
    name = re.sub(r'\s+', ' ', name).strip()

    return name.title()


def extract_year(filename: str) -> str | None:
    """Extrait l'année du nom de fichier."""
    match = re.search(_YEAR_PATTERN, filename)
    return match.group(1) if match else None

def extract_tags(filename: str) -> dict:
    """Extrait les tags _VF / _EN / _TV / _PC du nom de fichier."""
    stem = Path(filename).stem
    
    # Langue : _VF explicite, sinon VO par défaut
    lang = "vf" if re.search(r'_VF(?=_|$|\.)', stem, re.IGNORECASE) else "en"
    
    # Type : _TV explicite OU pattern SxxEyy → série, sinon film
    is_tv = (
        bool(re.search(r'_TV(?=_|$|\.)', stem, re.IGNORECASE)) or
        bool(re.search(r'[Ss]\d{1,2}[Ee]\d{1,2}', stem))
    )
    
    return {
        "lang": lang,
        "kind": "tv" if is_tv else "movie",
    }

# ══════════════════════════════════════════════════════════════════════════════
# ANALYSE FFPROBE DES CODECS
# ══════════════════════════════════════════════════════════════════════════════

def probe_codecs(filepath: str) -> dict:
    """Retourne les codecs vidéo et audio d'un fichier."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", str(filepath)],
            capture_output=True, text=True, timeout=10
        )
        data = json.loads(result.stdout)
        vcodec = next((s["codec_name"] for s in data.get("streams", [])
                       if s.get("codec_type") == "video"), None)
        acodec = next((s["codec_name"] for s in data.get("streams", [])
                       if s.get("codec_type") == "audio"), None)
        return {"video": vcodec, "audio": acodec}
    except Exception as e:
        print(f"⚠  ffprobe échec : {e}")
        return {"video": None, "audio": None}


# ══════════════════════════════════════════════════════════════════════════════
# TMDB — AFFICHES ET BACKDROPS
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
    """Formate un résultat brut TMDB en dict standard."""
    return {
        "poster":   f"https://image.tmdb.org/t/p/w342{m['poster_path']}"    if m.get("poster_path")   else None,
        "backdrop": f"https://image.tmdb.org/t/p/w1280{m['backdrop_path']}" if m.get("backdrop_path") else None,
        "overview": m.get("overview", ""),
        "tmdb_title": m.get(title_key, ""),
    }


def _fetch_tmdb_tv(title: str) -> dict | None:
    """Recherche une série sur TMDB."""
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
            print(f"📺 TMDB (série) : '{clean}' → '{results[0].get('name')}'")
        else:
            result = None
            print(f"❌ TMDB (série) : '{clean}' → aucun résultat")
        _tmdb_cache[key] = result
        return result
    except Exception as e:
        print(f"⚠  TMDB série échec : {e}")
        return None


def _fetch_tmdb_movie(title: str, year: str | None) -> dict | None:
    """Recherche un film sur TMDB."""
    query = re.sub(_YEAR_PATTERN, '', title).strip()
    key = f"{query}|{year or ''}"
    if key in _tmdb_cache:
        return _tmdb_cache[key]

    try:
        params = {"api_key": TMDB_API_KEY, "query": query, "language": "fr-FR"}
        if year:
            params["year"] = year

        r = requests.get("https://api.themoviedb.org/3/search/movie",
                         params=params, timeout=5)
        results = r.json().get("results", [])

        # Fallback sans l'année si rien n'a matché
        if not results and year:
            params.pop("year")
            r = requests.get("https://api.themoviedb.org/3/search/movie",
                             params=params, timeout=5)
            results = r.json().get("results", [])

        if results:
            result = _tmdb_format(results[0], title_key="title")
            print(f"✅ TMDB : '{query}' → '{results[0].get('title')}'")
        else:
            result = None
            print(f"❌ TMDB : '{query}' ({year}) → aucun résultat")
        _tmdb_cache[key] = result
        return result
    except Exception as e:
        print(f"⚠  TMDB échec pour '{query}' : {e}")
        return None


def fetch_tmdb(title: str, year: str | None) -> dict | None:
    """Recherche TMDB avec détection automatique série/film."""
    if not TMDB_API_KEY:
        return None
    # Détecte le pattern SxxEyy (série)
    if re.search(r'[Ss]\d{1,2}[Ee]\d{1,2}', title):
        return _fetch_tmdb_tv(title)
    return _fetch_tmdb_movie(title, year)


# ══════════════════════════════════════════════════════════════════════════════
# SCAN DE LA BIBLIOTHÈQUE
# ══════════════════════════════════════════════════════════════════════════════

def scan_movies() -> list:
    """Scanne le dossier films et enrichit via TMDB."""
    movies = []
    if not MOVIES_DIR.exists():
        print(f"⚠  Dossier introuvable : {MOVIES_DIR}")
        return movies

    # Construction de la liste
    for filepath in sorted(MOVIES_DIR.rglob("*")):
        if filepath.suffix.lower() not in SUPPORTED_EXTS:
            continue
        if filepath.name.startswith("."):
            continue

        movie_id = str(hash(str(filepath)) & 0xFFFFFFFF)
        tags = extract_tags(filepath.name)
        try:
            rel = filepath.relative_to(MOVIES_DIR)
            category = rel.parts[0] if len(rel.parts) > 1 else "Films"
        except ValueError:
            category = "Films"

        movies.append({
            "id":         movie_id,
            "title":      clean_title(filepath.name),
            "filename":   filepath.name,
            "year":       extract_year(filepath.name),
            "category":   category,
            "size_mb":    round(filepath.stat().st_size / 1024 / 1024),
            "ext":        filepath.suffix.lower(),
            "path":       str(filepath),
            "stream_url": f"/stream/{movie_id}",
            "cast_url":   f"/cast/{movie_id}",
            "poster":     None,
            "backdrop":   None,
            "overview":   "",
            "lang":       tags["lang"],
            "kind":       tags["kind"],
        })

    # Enrichissement TMDB en parallèle
    to_fetch = [m for m in movies
                if f"{re.sub(_YEAR_PATTERN, '', m['title']).strip()}|{m['year'] or ''}"
                not in _tmdb_cache]
    if to_fetch:
        print(f"🎞  Recherche des affiches TMDB pour {len(to_fetch)} film(s)...")

    with ThreadPoolExecutor(max_workers=10) as ex:
        results = list(ex.map(lambda m: fetch_tmdb(m["title"], m["year"]), movies))

    for m, tmdb in zip(movies, results):
        if tmdb:
            m["poster"]   = tmdb["poster"]
            m["backdrop"] = tmdb["backdrop"]
            m["overview"] = tmdb["overview"]

    _save_tmdb_cache()
    return movies


# Cache mémoire global
_movies_cache = None
_movies_lock = threading.Lock()


def get_movies() -> list:
    global _movies_cache
    with _movies_lock:
        if _movies_cache is None:
            print("🔍 Scan des films en cours...")
            _movies_cache = scan_movies()
            print(f"✅ {len(_movies_cache)} film(s) trouvé(s)")
        return _movies_cache


def get_movie_by_id(movie_id: str) -> dict | None:
    return next((m for m in get_movies() if m["id"] == movie_id), None)


# ══════════════════════════════════════════════════════════════════════════════
# STREAMING VIDÉO
# ══════════════════════════════════════════════════════════════════════════════

MIME_MAP = {
    ".mp4":  "video/mp4",
    ".mkv":  "video/x-matroska",
    ".avi":  "video/x-msvideo",
    ".mov":  "video/quicktime",
    ".webm": "video/webm",
    ".m4v":  "video/mp4",
    ".wmv":  "video/x-ms-wmv",
    ".ts":   "video/mp2t",
}


def _stream_file_ranged(filepath: str, mimetype: str):
    """Sert un fichier avec support des Range requests (seek, Chromecast)."""
    file_size = os.path.getsize(filepath)
    range_header = request.headers.get("Range")

    if not range_header:
        return send_file(filepath, mimetype=mimetype, conditional=True)

    match = re.match(r"bytes=(\d+)-(\d*)", range_header)
    start = int(match.group(1))
    end   = int(match.group(2)) if match.group(2) else file_size - 1
    length = end - start + 1

    def generate():
        with open(filepath, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    headers = {
        "Content-Range":  f"bytes {start}-{end}/{file_size}",
        "Accept-Ranges":  "bytes",
        "Content-Length": str(length),
        "Content-Type":   mimetype,
    }
    return Response(generate(), status=206, headers=headers)


def _transcode_stream(filepath: str, v_arg: list, a_arg: list):
    """Stream un fichier transcodé à la volée via ffmpeg."""
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
    return jsonify({"status": "ok", "count": len(get_movies())})


# ─── STREAM PC ────────────────────────────────────────────────────────────────

@app.route("/stream/<movie_id>")
def stream_video(movie_id):
    """
    Stream pour lecture navigateur (PC/mobile).
    Transcode l'audio à la volée si incompatible (AC3/DTS/FLAC → AAC).
    La vidéo est toujours copiée (pas de re-encodage).
    """
    movie = get_movie_by_id(movie_id)
    if not movie:
        return Response("Film introuvable", status=404)

    filepath = movie["path"]
    codecs = probe_codecs(filepath)

    # Si l'audio est compatible navigateur → fichier brut (Range OK, scrubbing OK)
    if codecs["audio"] in BROWSER_AUDIO_OK:
        mimetype = MIME_MAP.get(movie["ext"], "video/mp4")
        return _stream_file_ranged(filepath, mimetype)

    # Sinon → transcodage audio uniquement, vidéo copiée
    print(f"🖥️  PC transcodage audio : {movie['filename']} ({codecs['audio']} → AAC)")
    return _transcode_stream(
        filepath,
        v_arg=["-c:v", "copy"],
        a_arg=["-c:a", "aac", "-b:a", "192k", "-ac", "2"]
    )


# ─── STREAM CHROMECAST ────────────────────────────────────────────────────────

@app.route("/cast/<movie_id>")
def cast_video(movie_id):
    """
    Stream pour Chromecast.
    Transcode ce qui est nécessaire pour le récepteur par défaut.
    Si tout est compatible → redirige vers /stream/ (fichier brut).
    """
    movie = get_movie_by_id(movie_id)
    if not movie:
        return Response("Film introuvable", status=404)

    filepath = movie["path"]
    codecs = probe_codecs(filepath)

    video_ok = codecs["video"] in VIDEO_OK
    audio_ok = codecs["audio"] in AUDIO_OK

    # Tout compatible → redirection vers le fichier brut
    if video_ok and audio_ok:
        print(f"📺 Cast direct : {movie['filename']}")
        return redirect(f"/stream/{movie_id}", code=302)

    # Transcodage adapté
    v_arg = ["-c:v", "copy"] if video_ok else [
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23"
    ]
    a_arg = ["-c:a", "copy"] if audio_ok else [
        "-c:a", "aac", "-b:a", "192k", "-ac", "2"
    ]

    print(f"📺 Cast transcodage : {movie['filename']}")
    print(f"   Vidéo {codecs['video']} → {'copy' if video_ok else 'H.264'}")
    print(f"   Audio {codecs['audio']} → {'copy' if audio_ok else 'AAC'}")

    return _transcode_stream(filepath, v_arg, a_arg)


# ══════════════════════════════════════════════════════════════════════════════
# LANCEMENT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("━" * 60)
    print("🎬  CineLocal — Serveur de films local")
    print(f"📁  Dossier films : {MOVIES_DIR}")
    print(f"🌐  Interface     : http://localhost:{PORT}")
    print(f"📺  Pour TV/Cast  : http://<ton-ip>:{PORT}")
    print("━" * 60)

    get_movies()  # Pré-charge les films au démarrage

    app.run(host=HOST, port=PORT, debug=False, threaded=True)
