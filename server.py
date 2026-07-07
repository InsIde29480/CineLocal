#!/usr/bin/env python3
"""
CineLocal - Serveur de films local style Netflix
Lance avec: python server.py
"""

import os
import re
import json
import struct
import hashlib
import subprocess
import threading
import shutil
import time
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

TMDB_API_KEY     = ""

# ─── OpenSubtitles (téléchargement des sous-titres manquants) ─────────────────
# Beaucoup de rips BluRay n'ont que des sous-titres PGS (image) : impossible à
# convertir en texte sans OCR. On récupère alors de vrais .srt texte en ligne.
#
# À REMPLIR (compte gratuit sur https://www.opensubtitles.com puis
# https://www.opensubtitles.com/consumers pour la clé API) :
#   - OPENSUBTITLES_API_KEY  : clé « consumer » (obligatoire)
#   - OPENSUBTITLES_USERNAME / OPENSUBTITLES_PASSWORD : identifiants du compte
#     (obligatoires pour TÉLÉCHARGER ; la recherche seule n'en a pas besoin)
# Sans ces valeurs, le bouton de téléchargement reste inactif et l'explique.
OPENSUBTITLES_API_KEY    = ""
OPENSUBTITLES_USERNAME   = ""
OPENSUBTITLES_PASSWORD   = ""
OPENSUBTITLES_LANGS      = ["fr", "en"]   # ordre de préférence : fr d'abord, en en secours
OPENSUBTITLES_USER_AGENT = "CineLocal v1.0"
OPENSUBTITLES_BASE       = "https://api.opensubtitles.com/api/v1"

# ─── Paramètres modifiables depuis l'interface (persistés) ───────────────────
# Les valeurs ci-dessus servent de valeurs PAR DÉFAUT. Elles peuvent être
# surchargées via l'onglet « Paramètres » du site ; la config est alors
# sauvegardée dans SETTINGS_FILE et rechargée à chaque démarrage.
SETTINGS_FILE  = Path.home() / "storage/6TO/.cinelocal_settings.json"
_settings_lock = threading.Lock()

# Valeurs par défaut des chemins (capturées avant toute surcharge par settings).
_DEFAULT_MOVIES_DIR       = MOVIES_DIR
_DEFAULT_TRACKS_CACHE_DIR = TRACKS_CACHE_DIR


def _default_settings() -> dict:
    return {
        "tmdb_api_key":           TMDB_API_KEY,
        "opensubtitles_api_key":  OPENSUBTITLES_API_KEY,
        "opensubtitles_username": OPENSUBTITLES_USERNAME,
        "opensubtitles_password": OPENSUBTITLES_PASSWORD,
        "opensubtitles_langs":    list(OPENSUBTITLES_LANGS),
        "movies_dir":             str(_DEFAULT_MOVIES_DIR),
        "tracks_cache_dir":       str(_DEFAULT_TRACKS_CACHE_DIR),
    }


def _load_settings() -> dict:
    s = _default_settings()
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            for k in s:
                if k in data and data[k] not in (None, ""):
                    s[k] = data[k]
        except Exception as e:
            print(f"Lecture des paramètres échouée : {e}")
    return s


_settings = _load_settings()


def _apply_paths_from_settings():
    """Rebinde les chemins (films / cache sous-titres) depuis les paramètres."""
    global MOVIES_DIR, TRACKS_CACHE_DIR
    MOVIES_DIR = Path(_settings["movies_dir"]).expanduser()
    TRACKS_CACHE_DIR = Path(_settings["tracks_cache_dir"]).expanduser()
    try:
        TRACKS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        print(f"Création du dossier des sous-titres échouée ({TRACKS_CACHE_DIR}) : {e}")


# Applique les chemins configurés dès le chargement (avant tout scan / mkdir).
_apply_paths_from_settings()


def save_settings(new: dict) -> bool:
    """Fusionne et persiste les paramètres. Réinitialise le jeton OpenSubtitles."""
    global _os_token
    with _settings_lock:
        for k in _default_settings():
            if k in new and new[k] is not None:
                _settings[k] = new[k]
        try:
            SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            SETTINGS_FILE.write_text(
                json.dumps(_settings, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            print(f"Écriture des paramètres échouée : {e}")
            return False
    _os_token = None   # identifiants potentiellement changés → nouvelle connexion
    return True

VIDEO_OK         = {"h264"}
AUDIO_OK         = {"aac", "mp3"}
BROWSER_AUDIO_OK = {"aac", "mp3", "opus", "vorbis"}
SUBS_TEXT_CODECS = {"subrip", "ass", "ssa", "mov_text", "webvtt", "srt"}
SUBS_LANG_OK     = {"fre", "fra", "fr", "francais", "français", "eng", "en", "english", "und", "vo"}

# Par défaut on extrait les sous-titres TEXTE de toutes les langues : mieux vaut
# tout proposer et laisser l'utilisateur choisir dans l'interface que de rater
# un sous-titre parce que sa langue n'était pas dans la liste ci-dessus.
# Passe à False pour te limiter strictement à SUBS_LANG_OK.
SUBS_ACCEPT_ALL_LANGS = True

# Timeouts ffmpeg pour l'extraction des sous-titres.
# ATTENTION : ce ne sont PAS des plafonds de temps total, mais des délais
# d'INACTIVITÉ. ffmpeg n'est tué que si aucune progression n'est détectée
# pendant ce laps de temps. Une extraction longue (film avec des milliers de
# lignes de sous-titres sur un HDD) peut donc durer bien plus longtemps tant
# qu'elle avance — elle ne s'arrête que si elle est réellement bloquée.
SUBS_TIMEOUT_EMBEDDED = 300   # 5 min sans aucune avancée : extraction depuis un MKV
SUBS_TIMEOUT_EXTERNAL = 180   # 3 min sans aucune avancée : conversion .srt -> .vtt


# ══════════════════════════════════════════════════════════════════════════════
# IDENTIFIANTS STABLES
# ══════════════════════════════════════════════════════════════════════════════
# IMPORTANT : on n'utilise PAS hash() de Python pour générer les identifiants.
# hash() sur une chaîne est randomisé à chaque démarrage du processus
# (PYTHONHASHSEED), donc après un « systemctl restart » tous les movie_id
# changeaient → le cache .tracks_cache/<id> devenait orphelin et les
# sous-titres devaient être ré-extraits à chaque redémarrage.
#
# On dérive maintenant l'identifiant d'un MD5 du chemin : stable tant que le
# fichier ne bouge pas, donc le cache (sous-titres, remux audio) survit aux
# redémarrages du service.

def stable_file_id(filepath) -> str:
    """ID stable et déterministe d'un fichier (survit aux redémarrages)."""
    return hashlib.md5(str(filepath).encode("utf-8")).hexdigest()[:16]


def stable_group_id(key: str) -> str:
    """ID stable pour un groupe (série)."""
    return "s_" + hashlib.md5(key.encode("utf-8")).hexdigest()[:16]


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


def _run_ffmpeg_subs(cmd: list, vtt_path: Path, idle_timeout: int, label: str):
    """
    Lance ffmpeg pour produire un VTT. Renvoie (ok, reason) :
      - (True, None) si le fichier est réellement utilisable ;
      - (False, "raison") en cas d'échec (le fichier partiel est supprimé).

    IMPORTANT : `idle_timeout` est un délai d'INACTIVITÉ, pas un plafond de temps
    total. ffmpeg n'est tué que si AUCUNE progression n'est détectée pendant
    `idle_timeout` secondes. Un film avec des milliers de lignes de sous-titres
    sur un HDD peut donc prendre bien plus de temps que ça, tant qu'il avance.

    La progression est détectée de deux façons redondantes :
      1. la sortie `-progress pipe:1` de ffmpeg (mise à jour ~2×/s) ;
      2. la taille du fichier VTT de sortie qui augmente.
    """
    # `-progress pipe:1` doit être placé AVANT le fichier de sortie (dernier arg),
    # sinon ffmpeg l'interpréterait comme une seconde sortie.
    full_cmd = cmd[:-1] + ["-progress", "pipe:1", cmd[-1]]

    last_activity = [time.time()]      # liste = mutable partagé avec les threads
    stderr_tail = []

    try:
        proc = subprocess.Popen(
            full_cmd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
    except Exception as e:
        _safe_remove(vtt_path)
        print(f"Échec {label} : {e}")
        return False, str(e)

    def _drain_stdout():
        # Toute ligne de -progress (out_time=…, progress=continue/end…) = signe de vie.
        try:
            for _line in proc.stdout:
                last_activity[0] = time.time()
        except Exception:
            pass

    def _drain_stderr():
        # On garde les dernières lignes pour expliquer un éventuel échec.
        try:
            for line in proc.stderr:
                line = line.rstrip()
                if line:
                    stderr_tail.append(line)
                    if len(stderr_tail) > 10:
                        del stderr_tail[0]
        except Exception:
            pass

    t_out = threading.Thread(target=_drain_stdout, daemon=True)
    t_err = threading.Thread(target=_drain_stderr, daemon=True)
    t_out.start()
    t_err.start()

    killed_idle = False
    last_size = -1
    while True:
        try:
            proc.wait(timeout=2)
            break                       # ffmpeg a terminé (ok ou erreur)
        except subprocess.TimeoutExpired:
            pass

        # Secours : le VTT grossit-il ? (utile si -progress reste muet un instant)
        try:
            if vtt_path.exists():
                sz = vtt_path.stat().st_size
                if sz != last_size:
                    last_size = sz
                    last_activity[0] = time.time()
        except Exception:
            pass

        # Tue uniquement en cas d'inactivité prolongée — jamais sur le temps total.
        if time.time() - last_activity[0] > idle_timeout:
            killed_idle = True
            try:
                proc.kill()
                proc.wait(timeout=10)
            except Exception:
                pass
            break

    t_out.join(timeout=1)
    t_err.join(timeout=1)

    if killed_idle:
        _safe_remove(vtt_path)
        reason = f"Bloqué : aucune progression pendant {idle_timeout}s (HDD trop lent ou fichier abîmé ?)"
        print(f"Extraction {label} tuée pour inactivité ({idle_timeout}s)")
        return False, reason

    if proc.returncode != 0 or not vtt_path.exists() or vtt_path.stat().st_size == 0:
        _safe_remove(vtt_path)
        tail = stderr_tail[-3:] if stderr_tail else []
        reason = ' | '.join(tail) if tail else f"ffmpeg code {proc.returncode}"
        print(f"ffmpeg échec {label} (rc={proc.returncode}): {reason}")
        return False, reason
    return True, None


# ─── ENCODAGE DES SOUS-TITRES ────────────────────────────────────────────────
# Beaucoup de .srt (surtout français) sont en Windows-1252 / Latin-1 : lus comme
# de l'UTF-8, les accents deviennent illisibles (« Ã© », « � »). On détecte donc
# l'encodage réel et on normalise tout en UTF-8 avant de produire le VTT.

def _decode_subtitle_bytes(raw: bytes) -> str:
    """Décode des octets de sous-titre en texte, en devinant l'encodage."""
    # 1) UTF-8 (avec ou sans BOM) en priorité
    for enc in ("utf-8-sig", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    # 2) Détection automatique (charset_normalizer est fourni avec requests)
    try:
        from charset_normalizer import from_bytes
        best = from_bytes(raw).best()
        if best is not None:
            return str(best)
    except Exception:
        pass
    try:
        import chardet
        enc = (chardet.detect(raw) or {}).get("encoding")
        if enc:
            return raw.decode(enc, errors="replace")
    except Exception:
        pass
    # 3) Replis classiques : Windows-1252 puis Latin-1 (ce dernier ne lève jamais)
    for enc in ("cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", errors="replace")


def _external_sub_to_vtt(src: Path, vtt_path: Path):
    """
    Convertit un sous-titre externe (.srt/.vtt) en VTT UTF-8. Renvoie (ok, reason).
    Le fichier source est d'abord normalisé en UTF-8 pour éviter tout mojibake.
    """
    ext = src.suffix.lower()
    try:
        text = _decode_subtitle_bytes(src.read_bytes())
    except Exception as e:
        return False, f"lecture impossible : {e}"

    if ext == ".vtt":
        # Déjà du VTT : on le réécrit simplement en UTF-8 propre.
        try:
            vtt_path.write_text(text, encoding="utf-8")
        except Exception as e:
            _safe_remove(vtt_path)
            return False, f"écriture impossible : {e}"
        if vtt_path.exists() and vtt_path.stat().st_size > 0:
            return True, None
        _safe_remove(vtt_path)
        return False, "fichier VTT vide/illisible"

    # .srt (ou autre format texte) : on écrit une copie UTF-8 puis ffmpeg → webvtt.
    tmp = vtt_path.parent / (vtt_path.stem + ".src.utf8.srt")
    try:
        tmp.write_text(text, encoding="utf-8")
    except Exception as e:
        _safe_remove(tmp)
        return False, f"normalisation impossible : {e}"
    ok, reason = _run_ffmpeg_subs(
        ["ffmpeg", "-y", "-sub_charenc", "UTF-8", "-i", str(tmp),
         "-c:s", "webvtt", str(vtt_path)],
        vtt_path, SUBS_TIMEOUT_EXTERNAL,
        f"sous-titres externes {src.name}",
    )
    _safe_remove(tmp)
    return ok, reason


def extract_tracks(movie: dict, force: bool = False) -> dict:
    """
    Extrait les sous-titres en VTT + liste les pistes audio (métadonnées).
    Cache dans .tracks_cache/<movie_id>/tracks.json

    force=True : ignore COMPLÈTEMENT tout cache existant, supprime le dossier du
    film et ré-extrait de zéro (utilisé par la reprise « échecs / sans S-T » et
    le « tout ré-extraire »). C'est ce qui garantit qu'on relance vraiment
    ffmpeg au lieu de se contenter de constater qu'un dossier existe déjà.
    """
    movie_id = movie["id"]
    filepath = movie["path"]
    cache_dir = TRACKS_CACHE_DIR / movie_id
    metadata_file = cache_dir / "tracks.json"

    if movie_id not in _extraction_locks:
        _extraction_locks[movie_id] = threading.Lock()

    with _extraction_locks[movie_id]:
        if force:
            # Reprise forcée : on repart d'un dossier vierge, sans consulter le
            # moindre cache. Suppression puis ré-extraction intégrale.
            if cache_dir.exists():
                print(f"Reprise : suppression du cache existant → {movie['filename']}")
                shutil.rmtree(cache_dir, ignore_errors=True)

        # Vérifie les SRT externes modifiés (sauté en mode forcé)
        if not force and metadata_file.exists():
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
            return {"audio_tracks": [], "subtitle_tracks": []}

        streams = data.get("streams", [])
        audio_tracks = []
        subtitle_tracks = []
        failures = []               # raisons d'échec d'extraction
        skipped  = []               # pistes écartées SANS erreur (image, langue)
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
                codec = stream.get("codec_name", "") or ""
                if not SUBS_ACCEPT_ALL_LANGS and lang not in SUBS_LANG_OK:
                    msg = f"Piste {subs_idx} « {_lang_label(lang)} » ignorée (langue non retenue)"
                    print(msg)
                    skipped.append(msg)
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
                        SUBS_TIMEOUT_EMBEDDED,
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
                        failures.append(f"Piste {_lang_label(lang)} ({codec}) : {reason}")
                else:
                    msg = (f"Piste {subs_idx} « {_lang_label(lang)} » image "
                           f"({codec or 'inconnu'}) ignorée — non convertible en texte (OCR requis)")
                    print(msg)
                    skipped.append(msg)
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
                    ok, reason = _external_sub_to_vtt(sub_file, vtt_path)
                except Exception as e:
                    _safe_remove(vtt_path)
                    ok, reason = False, str(e)
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
                    failures.append(f"Externe {sub_file.name} : {reason}")

            simple_sub = movie_dir / f"{movie_stem}{ext}"
            if simple_sub.exists():
                external_idx = 1000 + subs_idx
                vtt_path = cache_dir / f"subs_{external_idx}.vtt"
                try:
                    ok, reason = _external_sub_to_vtt(simple_sub, vtt_path)
                except Exception as e:
                    _safe_remove(vtt_path)
                    ok, reason = False, str(e)
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
                    failures.append(f"Externe {simple_sub.name} : {reason}")

        metadata = {
            "audio_tracks":        audio_tracks,
            "subtitle_tracks":     subtitle_tracks,
            "extraction_complete": extraction_complete,
            "failures":            failures,
            "skipped":             skipped,
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


# ══════════════════════════════════════════════════════════════════════════════
# OPENSUBTITLES — téléchargement des sous-titres manquants
# ══════════════════════════════════════════════════════════════════════════════
# Pour les films dont les seuls sous-titres sont des images (PGS), on récupère
# un vrai .srt texte en ligne et on l'enregistre à côté du film sous la forme
# « NomDuFilm.fr.srt » : le pipeline d'extraction existant le détecte alors
# automatiquement et le convertit en VTT.

_os_token = None            # jeton d'authentification OpenSubtitles (login)
_os_token_lock = threading.Lock()


def opensubtitles_hash(path: str) -> str | None:
    """
    Empreinte OpenSubtitles d'un fichier vidéo : taille + somme des 64 Kio de
    début et de fin (algorithme officiel). Permet un appariement fiable.
    """
    try:
        fmt = "<q"                       # entier 64 bits little-endian
        bsize = struct.calcsize(fmt)
        filesize = os.path.getsize(path)
        if filesize < 65536 * 2:
            return None                  # fichier trop petit
        h = filesize
        with open(path, "rb") as f:
            for _ in range(65536 // bsize):
                (val,) = struct.unpack(fmt, f.read(bsize))
                h = (h + val) & 0xFFFFFFFFFFFFFFFF
            f.seek(max(0, filesize - 65536), 0)
            for _ in range(65536 // bsize):
                (val,) = struct.unpack(fmt, f.read(bsize))
                h = (h + val) & 0xFFFFFFFFFFFFFFFF
        return "%016x" % h
    except Exception as e:
        print(f"Hash OpenSubtitles échec ({path}) : {e}")
        return None


def _os_headers(with_auth: bool = False) -> dict:
    h = {
        "Api-Key":      _settings["opensubtitles_api_key"],
        "Content-Type": "application/json",
        "User-Agent":   OPENSUBTITLES_USER_AGENT,
    }
    if with_auth and _os_token:
        h["Authorization"] = f"Bearer {_os_token}"
    return h


def _os_login() -> tuple:
    """Ouvre une session OpenSubtitles (nécessaire pour télécharger). (ok, reason)."""
    global _os_token
    if _os_token:
        return True, None
    username = _settings["opensubtitles_username"]
    password = _settings["opensubtitles_password"]
    if not (username and password):
        return False, "identifiants OpenSubtitles non configurés (username/password)"
    with _os_token_lock:
        if _os_token:
            return True, None
        try:
            r = requests.post(
                f"{OPENSUBTITLES_BASE}/login",
                headers=_os_headers(),
                json={"username": username, "password": password},
                timeout=15,
            )
            if r.status_code != 200:
                return False, f"login refusé (HTTP {r.status_code})"
            _os_token = r.json().get("token")
            return (True, None) if _os_token else (False, "login sans jeton")
        except Exception as e:
            return False, f"login injoignable : {e}"


def _os_search(movie: dict, lang: str):
    """Cherche un sous-titre pour un film dans une langue. Renvoie le meilleur file_id ou None."""
    params_base = {"languages": lang}
    mh = movie.get("_os_hash")
    attempts = []
    if mh:
        attempts.append({**params_base, "moviehash": mh})
    # Repli par titre (+ année) si le hash ne donne rien.
    query = movie.get("title") or Path(movie["path"]).stem
    q = {**params_base, "query": query}
    if movie.get("year"):
        q["year"] = movie["year"]
    attempts.append(q)

    for params in attempts:
        try:
            r = requests.get(
                f"{OPENSUBTITLES_BASE}/subtitles",
                headers=_os_headers(), params=params, timeout=15,
            )
            if r.status_code != 200:
                continue
            data = r.json().get("data", [])
            if not data:
                continue
            # Priorité : correspondance par hash, puis nombre de téléchargements.
            def score(item):
                attr = item.get("attributes", {})
                return (1 if attr.get("moviehash_match") else 0,
                        attr.get("download_count", 0) or 0)
            data.sort(key=score, reverse=True)
            for item in data:
                files = item.get("attributes", {}).get("files", [])
                if files and files[0].get("file_id"):
                    return files[0]["file_id"]
        except Exception as e:
            print(f"Recherche OpenSubtitles échec ({lang}) : {e}")
    return None


def _os_download_content(file_id) -> tuple:
    """Récupère le contenu SRT d'un file_id. (texte|None, reason)."""
    ok, reason = _os_login()
    if not ok:
        return None, reason
    try:
        r = requests.post(
            f"{OPENSUBTITLES_BASE}/download",
            headers=_os_headers(with_auth=True),
            json={"file_id": file_id}, timeout=20,
        )
        if r.status_code == 406:
            return None, "quota de téléchargements OpenSubtitles atteint (réessaie demain)"
        if r.status_code == 401:
            # jeton expiré : on force une nouvelle connexion au prochain appel
            globals()["_os_token"] = None
            return None, "session expirée (réessaie)"
        if r.status_code != 200:
            return None, f"téléchargement refusé (HTTP {r.status_code})"
        link = r.json().get("link")
        if not link:
            return None, "lien de téléchargement absent"
        dl = requests.get(link, timeout=30)
        if dl.status_code != 200 or not dl.content:
            return None, f"contenu injoignable (HTTP {dl.status_code})"
        # Détecte l'encodage réel (souvent Windows-1252 pour le français) et
        # normalise en UTF-8, sinon les accents ressortent en mojibake.
        return _decode_subtitle_bytes(dl.content), None
    except Exception as e:
        return None, f"téléchargement injoignable : {e}"


def download_subtitle_for(movie: dict) -> tuple:
    """
    Télécharge un sous-titre pour un film qui n'a pas de français.
    Priorité au français ; l'anglais n'est tenté en secours QUE si le film n'a
    encore aucun sous-titre (inutile de retélécharger une langue déjà présente).
    Enregistre à côté du film sous « stem.<lang>.srt ».
    Renvoie (ok, info) — info = langue téléchargée ou raison de l'échec.
    """
    if not _settings["opensubtitles_api_key"]:
        return False, "clé API OpenSubtitles non configurée (onglet Paramètres)"

    path = movie["path"]
    movie["_os_hash"] = opensubtitles_hash(path)

    have = _cached_sub_langs(movie["id"])   # langues déjà disponibles
    last_reason = "aucun sous-titre français trouvé en ligne"
    for lang in (_settings["opensubtitles_langs"] or ["fr", "en"]):
        if lang in have:
            continue    # déjà présent (ex. l'anglais) → on ne le retélécharge pas
        file_id = _os_search(movie, lang)
        if not file_id:
            continue
        content, reason = _os_download_content(file_id)
        if not content:
            last_reason = reason or last_reason
            # Quota atteint : inutile d'insister sur les autres langues.
            if reason and "quota" in reason:
                return False, reason
            continue
        stem = Path(path).stem
        srt_path = Path(path).parent / f"{stem}.{lang}.srt"
        try:
            srt_path.write_text(content, encoding="utf-8")
        except Exception as e:
            return False, f"écriture impossible ({srt_path.name}) : {e} — dossier en lecture seule ?"
        print(f"Sous-titre téléchargé ({lang}) : {srt_path.name}")
        return True, lang

    return False, last_reason


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
            params={"api_key": _settings["tmdb_api_key"], "query": clean, "language": "fr-FR"},
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
        params = {"api_key": _settings["tmdb_api_key"], "query": query, "language": "fr-FR"}
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

        movie_id = stable_file_id(filepath)
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
        series_id = stable_group_id(group_key)
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
# EXTRACTION EN MASSE DES SOUS-TITRES (balayage profond du dossier Films)
# ══════════════════════════════════════════════════════════════════════════════
# Extrait à l'avance les sous-titres de TOUS les fichiers (films + épisodes),
# sans attendre qu'on clique sur un film. Le résultat est mis en cache dans
# .tracks_cache/<id> avec un id MD5 stable, donc il survit à un redémarrage du
# service : au prochain lancement, seuls les fichiers nouveaux/modifiés sont
# ré-extraits.

_subs_scan_lock  = threading.Lock()
_subs_scan_thread = None
_subs_scan_state = {
    "running":     False,
    "mode":        None,  # 'new' | 'retry' | 'force' | 'download'
    "started_at":  None,
    "finished_at": None,
    "total":       0,     # nombre total de fichiers à traiter
    "done":        0,     # fichiers traités
    "current":     None,  # fichier en cours d'extraction
    "with_subs":     0,   # fichiers pour lesquels au moins un sous-titre a été produit
    "no_subs":       0,   # fichiers sans aucun sous-titre exploitable
    "failed":        0,   # fichiers avec au moins un échec d'extraction
    "downloaded":    0,   # sous-titres récupérés en ligne (mode 'download')
    "failures":      [],  # [{filename, title, reasons: [...]}]
    "no_subs_files": [],  # [{filename, title, reasons: [...]}] — pour vérification manuelle
}


def _public_scan_state() -> dict:
    with _subs_scan_lock:
        st = dict(_subs_scan_state)
        st["failures"]      = list(_subs_scan_state["failures"])
        st["no_subs_files"] = list(_subs_scan_state["no_subs_files"])
    return st


def _cached_subs_state(movie_id: str) -> str:
    """
    État en cache d'un fichier, sans rien ré-extraire :
      'has_subs'  → au moins un sous-titre exploitable déjà en cache ;
      'no_subs'   → extraction complète mais aucun sous-titre trouvé ;
      'failed'    → une extraction précédente a échoué (incomplète) ;
      'none'      → jamais scanné.
    """
    meta_file = TRACKS_CACHE_DIR / movie_id / "tracks.json"
    if not meta_file.exists():
        return "none"
    try:
        data = json.loads(meta_file.read_text(encoding="utf-8"))
    except Exception:
        return "none"
    if not data.get("extraction_complete", False):
        return "failed"
    if len(data.get("subtitle_tracks", [])) == 0:
        return "no_subs"
    return "has_subs"


def _needs_retry(movie_id: str) -> bool:
    """Vrai pour les fichiers à reprendre en mode 'retry' (échecs, sans S-T, jamais scannés)."""
    return _cached_subs_state(movie_id) != "has_subs"


def _norm_lang(code: str) -> str:
    """Normalise un code de langue (fre/fra/french → fr, eng/english → en)."""
    c = (code or "").lower()
    if c in ("fr", "fre", "fra", "french", "francais", "français"):
        return "fr"
    if c in ("en", "eng", "english"):
        return "en"
    return c


def _cached_sub_langs(movie_id: str) -> set:
    """Ensemble des langues de sous-titres déjà en cache pour un film (normalisées)."""
    meta_file = TRACKS_CACHE_DIR / movie_id / "tracks.json"
    langs = set()
    if meta_file.exists():
        try:
            data = json.loads(meta_file.read_text(encoding="utf-8"))
            for t in data.get("subtitle_tracks", []):
                langs.add(_norm_lang(t.get("language")))
        except Exception:
            pass
    return langs


def _needs_download(movie_id: str) -> bool:
    """
    Vrai si le film n'a PAS de sous-titre français : aucun sous-titre du tout,
    seulement de l'anglais, ou seulement des externes non-français. C'est ce qui
    déclenche le téléchargement d'un .srt français en ligne.
    """
    return "fr" not in _cached_sub_langs(movie_id)


def _run_subtitle_scan(mode: str = "new"):
    """
    Balaie les fichiers jouables et extrait leurs sous-titres.
    Exécuté dans un thread de fond. Séquentiel volontairement : sur un HDD,
    lancer plusieurs ffmpeg en parallèle ralentit tout le monde (seeks concurrents).

    mode :
      'new'   → tout vérifier ; les fichiers déjà complets en cache sont sautés
                instantanément (comportement par défaut / démarrage).
      'retry' → ne reprend QUE les échecs et les fichiers sans sous-titre
                (leur cache est purgé puis ré-extrait). Ceux qui ont déjà des
                sous-titres ne sont pas touchés.
      'force' → purge TOUS les caches puis tout ré-extrait.
      'download' → pour les fichiers SANS sous-titre texte, télécharge un .srt
                en ligne (OpenSubtitles, fr puis en) et le convertit en VTT.
    """
    get_movies()   # garantit le scan + le remplissage de _playable_index
    items = list(_playable_index.values())

    # 'retry' cible les échecs et fichiers sans sous-titre ; 'download' cible les
    # films sans sous-titre FRANÇAIS (rien, ou seulement anglais/externe).
    if mode == "retry":
        items = [it for it in items if _needs_retry(it["id"])]
    elif mode == "download":
        items = [it for it in items if _needs_download(it["id"])]

    with _subs_scan_lock:
        _subs_scan_state.update({
            "running":     True,
            "mode":        mode,
            "started_at":  time.time(),
            "finished_at": None,
            "total":       len(items),
            "done":        0,
            "current":     None,
            "with_subs":     0,
            "no_subs":       0,
            "failed":        0,
            "downloaded":    0,
            "failures":      [],
            "no_subs_files": [],
        })

    labels = {
        "new": "vérification", "retry": "reprise des échecs/sans S-T",
        "force": "forcée", "download": "téléchargement en ligne",
    }
    print(f"Extraction des sous-titres ({labels.get(mode, mode)}) : {len(items)} fichier(s)")

    for item in items:
        filename = item.get("filename", "?")
        title    = item.get("title") or filename
        with _subs_scan_lock:
            _subs_scan_state["current"] = filename

        try:
            if mode == "download":
                # Télécharge un .srt français à côté du film puis (re)extrait pour
                # le convertir en VTT via le pipeline « sous-titres externes ».
                dl_ok, dl_info = download_subtitle_for(item)
                meta = extract_tracks(item, force=True)
                reasons = meta.get("failures", [])
                present = _cached_sub_langs(item["id"])   # langues après extraction
                has_fr  = "fr" in present
                with _subs_scan_lock:
                    if reasons:
                        _subs_scan_state["failed"] += 1
                        _subs_scan_state["failures"].append({
                            "filename": filename, "title": title, "reasons": reasons,
                        })
                    if has_fr:
                        _subs_scan_state["with_subs"] += 1
                        if dl_ok:
                            _subs_scan_state["downloaded"] += 1
                    else:
                        # Toujours pas de français : on liste pourquoi (raison du
                        # téléchargement + langues déjà présentes le cas échéant).
                        _subs_scan_state["no_subs"] += 1
                        why = []
                        if dl_info:
                            why.append(f"Téléchargement : {dl_info}")
                        others = sorted(l for l in present if l and l != "und")
                        if others:
                            why.append("Sous-titres présents (pas de FR) : " + ", ".join(others))
                        why += meta.get("skipped", [])
                        if not why:
                            why = ["Aucun sous-titre français disponible"]
                        _subs_scan_state["no_subs_files"].append({
                            "filename": filename, "title": title, "reasons": why,
                        })
            else:
                # 'force' et 'retry' relancent une extraction complète : on passe
                # force=True à extract_tracks, qui supprime le dossier du film et
                # ré-extrait de zéro (au lieu de réutiliser le cache existant).
                hard = mode in ("force", "retry")
                meta = extract_tracks(item, force=hard)
                n_subs   = len(meta.get("subtitle_tracks", []))
                reasons  = meta.get("failures", [])
                with _subs_scan_lock:
                    if reasons:
                        _subs_scan_state["failed"] += 1
                        _subs_scan_state["failures"].append({
                            "filename": filename,
                            "title":    title,
                            "reasons":  reasons,
                        })
                    if n_subs > 0:
                        _subs_scan_state["with_subs"] += 1
                    else:
                        _subs_scan_state["no_subs"] += 1
                        # Pourquoi ce film n'a-t-il aucun sous-titre ? (image,
                        # langue, ou aucune piste) — pour vérification manuelle.
                        why = list(meta.get("skipped", []))
                        if not why:
                            why = ["Aucune piste de sous-titre dans le fichier"]
                        _subs_scan_state["no_subs_files"].append({
                            "filename": filename,
                            "title":    title,
                            "reasons":  why,
                        })
        except Exception as e:
            with _subs_scan_lock:
                _subs_scan_state["failed"] += 1
                _subs_scan_state["failures"].append({
                    "filename": filename,
                    "title":    title,
                    "reasons":  [f"Erreur inattendue : {e}"],
                })
        finally:
            with _subs_scan_lock:
                _subs_scan_state["done"] += 1

    with _subs_scan_lock:
        _subs_scan_state["running"]     = False
        _subs_scan_state["current"]     = None
        _subs_scan_state["finished_at"] = time.time()

    print(f"Extraction terminée : {_subs_scan_state['with_subs']} avec sous-titres, "
          f"{_subs_scan_state['no_subs']} sans, {_subs_scan_state['failed']} en échec"
          + (f", {_subs_scan_state['downloaded']} téléchargé(s)" if mode == "download" else ""))


def start_subtitle_scan(mode: str = "new") -> bool:
    """Démarre l'extraction en masse si elle n'est pas déjà en cours."""
    global _subs_scan_thread
    if mode not in ("new", "retry", "force", "download"):
        mode = "new"
    with _subs_scan_lock:
        if _subs_scan_state["running"]:
            return False
        _subs_scan_state["running"] = True   # verrou immédiat (évite un double départ)
    _subs_scan_thread = threading.Thread(
        target=_run_subtitle_scan, kwargs={"mode": mode}, daemon=True
    )
    _subs_scan_thread.start()
    return True


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


@app.route("/api/subtitles/scan", methods=["POST"])
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
    started = start_subtitle_scan(mode=mode)
    return jsonify({
        "status": "started" if started else "already_running",
        **_public_scan_state(),
    })


@app.route("/api/subtitles/status")
def api_subtitles_status():
    """État courant de l'extraction en masse (pour la fenêtre de progression)."""
    return jsonify(_public_scan_state())


@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    """
    Renvoie les paramètres pour préremplir le formulaire.
    Le mot de passe n'est jamais renvoyé : on indique seulement s'il est défini.
    """
    with _settings_lock:
        return jsonify({
            "tmdb_api_key":            _settings["tmdb_api_key"],
            "opensubtitles_api_key":   _settings["opensubtitles_api_key"],
            "opensubtitles_username":  _settings["opensubtitles_username"],
            "opensubtitles_langs":     ", ".join(_settings["opensubtitles_langs"] or []),
            "opensubtitles_password_set": bool(_settings["opensubtitles_password"]),
            "movies_dir":              _settings["movies_dir"],
            "tracks_cache_dir":        _settings["tracks_cache_dir"],
            "movies_dir_exists":       Path(_settings["movies_dir"]).expanduser().exists(),
        })


@app.route("/api/settings", methods=["POST"])
def api_save_settings():
    """
    Enregistre les paramètres (persistés dans SETTINGS_FILE).
    Le mot de passe n'est mis à jour que si un nouveau est fourni (non vide).
    Les langues sont acceptées en chaîne « fr, en » ou en liste.
    """
    global _movies_cache
    body = request.get_json(silent=True) or {}
    new = {}

    for key in ("tmdb_api_key", "opensubtitles_api_key", "opensubtitles_username"):
        if key in body:
            new[key] = (body[key] or "").strip()

    # Chemins : dossier des films et dossier de stockage des sous-titres.
    for key in ("movies_dir", "tracks_cache_dir"):
        if key in body and (body[key] or "").strip():
            new[key] = body[key].strip()

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

    ok = save_settings(new)

    # Si un chemin a changé : on rebinde les globals et on force un rescan de la
    # bibliothèque au prochain /api/movies (nouveau dossier de films).
    paths_changed = ok and any(k in new for k in ("movies_dir", "tracks_cache_dir"))
    if paths_changed:
        _apply_paths_from_settings()
        with _movies_lock:
            _movies_cache = None

    return jsonify({
        "status":            "ok" if ok else "error",
        "paths_changed":     paths_changed,
        "movies_dir_exists": MOVIES_DIR.exists(),
    }), (200 if ok else 500)


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

    get_movies()

    # Extraction anticipée des sous-titres de tout le dossier, en tâche de fond.
    # Grâce aux id MD5 stables, les caches existants sont réutilisés après un
    # redémarrage : seuls les fichiers nouveaux ou modifiés sont ré-extraits.
    start_subtitle_scan()

    serve(app, host=HOST, port=PORT, threads=8)
