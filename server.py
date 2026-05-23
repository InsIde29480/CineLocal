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
TRACKS_CACHE_DIR = Path(__file__).parent / ".tracks_cache"

SUPPORTED_EXTS   = {".mp4", ".mkv"}
HOST             = "0.0.0.0"
PORT             = 8765

TMDB_API_KEY     = "ba6207ce3c9ed44aa35c383f55ebab5e"

VIDEO_OK         = {"h264"}
AUDIO_OK         = {"aac", "mp3"}
BROWSER_AUDIO_OK = {"aac", "mp3", "opus", "vorbis"}
SUBS_TEXT_CODECS = {"subrip", "ass", "ssa", "mov_text", "webvtt", "srt"}


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
    name = re.sub(r'_(?:PC|TV|VF|EN)(?=_|$|\.)', '', name, flags=re.IGNORECASE)
    year_match = re.search(_YEAR_PATTERN, name)
    if year_match:
        name = name[:year_match.end()]
    name = re.sub(_RELEASE_TAGS, '', name, flags=re.IGNORECASE)
    name = re.sub(_EDITION_TAGS, '', name, flags=re.IGNORECASE)
    name = re.sub(r'[-_][a-zA-Z0-9]+$', '', name)
    name = re.sub(r'\[.*?\]|\(.*?\)|\{.*?\}', '', name)
    name = re.sub(r'[\(\[\{].*$', '', name)
    name = re.sub(r'[._]+', ' ', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name.title()


def extract_year(filename: str) -> str | None:
    match = re.search(_YEAR_PATTERN, filename)
    return match.group(1) if match else None


def extract_tags(filename: str) -> dict:
    stem = Path(filename).stem
    lang = "vf" if re.search(r'_VF(?=_|$|\.)', stem, re.IGNORECASE) else "en"
    device = "tv" if re.search(r'_TV(?=_|$|\.)', stem, re.IGNORECASE) else "pc"
    is_series = bool(re.search(r'[Ss]\d{1,2}[Ee]\d{1,2}', stem))
    return {"lang": lang, "device": device, "kind": "tv" if is_series else "movie"}


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
    name = re.sub(r'_(?:PC|TV|VF|EN)(?=_|$|\.)', '', name, flags=re.IGNORECASE)
    name = re.sub(r'[._\-]+', ' ', name)
    name = re.sub(r'\s+', ' ', name).strip()
    name = re.sub(r'\s*-\s*$', '', name).strip()
    return name.title()


# ══════════════════════════════════════════════════════════════════════════════
# ANALYSE FFPROBE
# ══════════════════════════════════════════════════════════════════════════════

def probe_codecs(filepath: str) -> dict:
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
                        print(f"📝 Nouveau sous-titre détecté : {sub_file.name}")
                        break
                if srt_changed:
                    break

            if not srt_changed:
                try:
                    return json.loads(metadata_file.read_text(encoding="utf-8"))
                except Exception:
                    pass

        cache_dir.mkdir(parents=True, exist_ok=True)
        print(f"🔍 Extraction des pistes : {movie['filename']}")

        try:
            result = subprocess.run([
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_streams", str(filepath)
            ], capture_output=True, text=True, timeout=30)
            data = json.loads(result.stdout)
        except Exception as e:
            print(f"⚠  ffprobe échec : {e}")
            return {"audio_tracks": [], "subtitle_tracks": []}

        streams = data.get("streams", [])
        audio_tracks = []
        subtitle_tracks = []
        audio_idx = 0
        subs_idx = 0

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
                if codec in SUBS_TEXT_CODECS:
                    vtt_path = cache_dir / f"subs_{subs_idx}.vtt"
                    try:
                        subprocess.run([
                            "ffmpeg", "-y", "-i", str(filepath),
                            "-map", f"0:s:{subs_idx}",
                            "-c:s", "webvtt",
                            str(vtt_path)
                        ], capture_output=True, timeout=60)
                        if vtt_path.exists() and vtt_path.stat().st_size > 0:
                            subtitle_tracks.append({
                                "index":    subs_idx,
                                "language": lang,
                                "label":    title or _lang_label(lang),
                                "url":      f"/track/subs/{movie_id}/{subs_idx}",
                            })
                            print(f"   ✓ Sous-titres {lang} : {title or codec}")
                    except Exception as e:
                        print(f"   ⚠ Échec sous-titres {subs_idx} : {e}")
                else:
                    print(f"   ⏭  Sous-titres image ({codec}) ignorés")
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
                try:
                    if ext == '.vtt':
                        import shutil
                        shutil.copy(sub_file, vtt_path)
                    else:
                        subprocess.run([
                            "ffmpeg", "-y", "-i", str(sub_file),
                            "-c:s", "webvtt", str(vtt_path)
                        ], capture_output=True, timeout=30)
                    if vtt_path.exists() and vtt_path.stat().st_size > 0:
                        subtitle_tracks.append({
                            "index":    external_idx,
                            "language": lang,
                            "label":    f"{_lang_label(lang)} (externe)",
                            "url":      f"/track/subs/{movie_id}/{external_idx}",
                        })
                        print(f"   ✓ Sous-titres externes : {sub_file.name} ({lang})")
                        subs_idx += 1
                except Exception as e:
                    print(f"   ⚠ Échec sous-titres externes {sub_file.name} : {e}")

            simple_sub = movie_dir / f"{movie_stem}{ext}"
            if simple_sub.exists():
                external_idx = 1000 + subs_idx
                vtt_path = cache_dir / f"subs_{external_idx}.vtt"
                try:
                    if ext == '.vtt':
                        import shutil
                        shutil.copy(simple_sub, vtt_path)
                    else:
                        subprocess.run([
                            "ffmpeg", "-y", "-i", str(simple_sub),
                            "-c:s", "webvtt", str(vtt_path)
                        ], capture_output=True, timeout=30)
                    if vtt_path.exists() and vtt_path.stat().st_size > 0:
                        subtitle_tracks.append({
                            "index":    external_idx,
                            "language": "und",
                            "label":    "Sous-titres (externe)",
                            "url":      f"/track/subs/{movie_id}/{external_idx}",
                        })
                        print(f"   ✓ Sous-titres externes : {simple_sub.name}")
                        subs_idx += 1
                except Exception as e:
                    print(f"   ⚠ Échec sous-titres externes {simple_sub.name} : {e}")

        metadata = {
            "audio_tracks":    audio_tracks,
            "subtitle_tracks": subtitle_tracks,
        }
        metadata_file.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        print(f"   ✅ {len(audio_tracks)} piste(s) audio, {len(subtitle_tracks)} sous-titre(s)")
        return metadata


def clear_tracks_cache(movie_id: str):
    cache_dir = TRACKS_CACHE_DIR / movie_id
    if cache_dir.exists():
        import shutil
        shutil.rmtree(cache_dir)


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
            print(f"✅ TMDB : '{query}' → '{results[0].get('title')}'")
        else:
            result = None
            print(f"❌ TMDB : '{query}' ({year}) → aucun résultat")
        _tmdb_cache[key] = result
        return result
    except Exception as e:
        print(f"⚠  TMDB échec pour '{query}' : {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# SCAN
# ══════════════════════════════════════════════════════════════════════════════

def scan_movies() -> list:
    items = []
    series_groups = {}

    if not MOVIES_DIR.exists():
        print(f"⚠  Dossier introuvable : {MOVIES_DIR}")
        return items

    for filepath in sorted(MOVIES_DIR.rglob("*")):
        if filepath.suffix.lower() not in SUPPORTED_EXTS:
            continue
        if filepath.name.startswith("."):
            continue

        movie_id = str(hash(str(filepath)) & 0xFFFFFFFF)
        tags = extract_tags(filepath.name)
        ep = parse_episode(filepath.name)

        try:
            rel = filepath.relative_to(MOVIES_DIR)
            category = rel.parts[0] if len(rel.parts) > 1 else "Films"
        except ValueError:
            category = "Films"

        common = {
            "id":         movie_id,
            "filename":   filepath.name,
            "category":   category,
            "size_mb":    round(filepath.stat().st_size / 1024 / 1024),
            "ext":        filepath.suffix.lower(),
            "path":       str(filepath),
            "stream_url": f"/stream/{movie_id}",
            "cast_url":   f"/cast/{movie_id}",
            "lang":       tags["lang"],
            "device":     tags["device"],
        }

        if ep:
            stitle = series_title(filepath.name)
            group_key = f"{stitle}|{tags['device']}|{tags['lang']}"
            episode_data = {
                **common,
                "season":  ep["season"],
                "episode": ep["episode"],
                "title":   f"S{ep['season']:02d}E{ep['episode']:02d}",
            }
            if group_key not in series_groups:
                series_groups[group_key] = {
                    "stitle": stitle, "category": category,
                    "tags": tags, "episodes": [],
                }
            series_groups[group_key]["episodes"].append(episode_data)
        else:
            items.append({
                **common,
                "title":    clean_title(filepath.name),
                "year":     extract_year(filepath.name),
                "kind":     "movie",
                "poster":   None, "backdrop": None, "overview": "",
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
            "lang":        group["tags"]["lang"],
            "device":      group["tags"]["device"],
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
        print(f"🎞  Recherche TMDB pour {to_fetch_count} entrée(s)...")

    with ThreadPoolExecutor(max_workers=10) as ex:
        results = list(ex.map(fetch_for_item, items))

    for it, tmdb in zip(items, results):
        if tmdb:
            it["poster"]   = tmdb["poster"]
            it["backdrop"] = tmdb["backdrop"]
            it["overview"] = tmdb["overview"]

    _save_tmdb_cache()
    return items


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
    for item in get_movies():
        if item["id"] == movie_id:
            return item
        if item.get("kind") == "series":
            for ep in item.get("episodes", []):
                if ep["id"] == movie_id:
                    return ep
    return None


# ══════════════════════════════════════════════════════════════════════════════
# STREAMING
# ══════════════════════════════════════════════════════════════════════════════

MIME_MAP = {
    ".mp4":  "video/mp4",
    ".mkv":  "video/x-matroska",
    ".webm": "video/webm",
}


def _stream_file_ranged(filepath: str, mimetype: str):
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
                chunk = f.read(min(67108864, remaining))
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

        a_args = (
            ["-c:a", "copy"] if track_codec in AUDIO_OK
            else ["-c:a", "aac", "-b:a", "192k", "-ac", "2"]
        )

        print(f"🔧 Remux piste audio {audio_idx} ({track_codec or '?'}) : {movie['filename']}")
        cmd = [
            "ffmpeg", "-y", "-i", str(filepath),
            "-map", "0:v:0", "-c:v", "copy",
            "-map", f"0:a:{audio_idx}", *a_args,
            "-movflags", "+faststart",
            str(tmp_path)
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        def _watch():
            proc.wait()
            with _audio_remux_lock:
                if tmp_path.exists() and tmp_path.stat().st_size > 0 and proc.returncode == 0:
                    try:
                        tmp_path.rename(out_path)
                        _audio_remux_state[key] = {"status": "ready", "progress": 1.0}
                        print(f"   ✅ Remux prêt : audio_{audio_idx}.mp4")
                        return
                    except Exception as e:
                        print(f"   ⚠ Renommage échoué : {e}")
                if tmp_path.exists():
                    try:
                        tmp_path.unlink()
                    except Exception:
                        pass
                _audio_remux_state[key] = {"status": "error", "progress": 0.0}
                print(f"   ⚠ Échec remux piste audio {audio_idx} (code {proc.returncode})")

        threading.Thread(target=_watch, daemon=True).start()
        _audio_remux_state[key] = {"status": "preparing", "progress": 0.0, "process": proc}
        return {"status": "preparing", "progress": 0.0}


def _ensure_audio_ready(movie: dict, audio_idx: int, timeout: float = 1800.0) -> Path | None:
    """Bloque jusqu'à ce que le MP4 remuxé soit prêt. Retourne le chemin ou None."""
    import time
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
    return jsonify({"status": "ok", "count": len(get_movies())})


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
    ?audio=N : sélectionne une piste audio spécifique (transcodage).
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
        print(f"🖥️  PC piste {audio_idx} (remux cache) : {movie['filename']}")
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

    video_ok = codecs["video"] in VIDEO_OK

    # Piste audio alternative + vidéo h264 → utilise le remux cache (seekable)
    if audio_idx is not None and video_ok:
        out_path = _ensure_audio_ready(movie, audio_idx)
        if out_path is None:
            return Response("Échec préparation de la piste audio", status=500)
        print(f"📺 Cast piste audio {audio_idx} (remux cache) : {movie['filename']}")
        return _stream_file_ranged(str(out_path), "video/mp4")

    # Argument vidéo
    v_arg = ["-map", "0:v:0", "-c:v", "copy"] if video_ok else [
        "-map", "0:v:0", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23"
    ]

    # Argument audio : piste spécifique ou piste par défaut
    if audio_idx is not None:
        a_arg = ["-map", f"0:a:{audio_idx}", "-c:a", "aac", "-b:a", "192k", "-ac", "2"]
        print(f"📺 Cast piste audio {audio_idx} (transcodage vidéo) : {movie['filename']}")
    else:
        audio_ok = codecs["audio"] in AUDIO_OK
        if video_ok and audio_ok:
            print(f"📺 Cast direct : {movie['filename']}")
            return redirect(f"/stream/{movie_id}", code=302)
        a_arg = ["-map", "0:a:0", "-c:a", "copy"] if audio_ok else [
            "-map", "0:a:0", "-c:a", "aac", "-b:a", "192k", "-ac", "2"
        ]
        print(f"📺 Cast transcodage : {movie['filename']}")
        print(f"   Vidéo {codecs['video']} → {'copy' if video_ok else 'H.264'}")
        print(f"   Audio {codecs['audio']} → {'copy' if audio_ok else 'AAC'}")

    return _transcode_stream(filepath, v_arg, a_arg)


# ─── LECTURE LOCALE HDMI (MPV) ────────────────────────────────────────────────

_current_player = None


@app.route("/play/<movie_id>", methods=["POST"])
def play_local(movie_id):
    global _current_player
    movie = get_movie_by_id(movie_id)
    if not movie:
        return jsonify({"status": "error", "message": "Film introuvable"}), 404

    if _current_player and _current_player.poll() is None:
        _current_player.terminate()

    _current_player = subprocess.Popen([
        "mpv", "--fullscreen", "--hwdec=auto", "--no-osc",
        "--no-input-default-bindings",
        movie["path"]
    ], env={**os.environ, "DISPLAY": ":0"})

    print(f"🎬 Lecture locale : {movie['filename']}")
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
    print("🎬  CineLocal — Serveur de films local")
    print(f"📁  Dossier films : {MOVIES_DIR}")
    print(f"🌐  Interface     : http://localhost:{PORT}")
    print(f"📺  Pour TV/Cast  : http://<ton-ip>:{PORT}")
    print("━" * 60)

    get_movies()
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
