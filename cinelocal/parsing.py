"""
Analyse des noms de fichiers (titre, année, qualité, épisode) et
identifiants stables.

IMPORTANT : on n'utilise PAS hash() de Python pour générer les identifiants.
hash() sur une chaîne est randomisé à chaque démarrage du processus
(PYTHONHASHSEED), donc après un « systemctl restart » tous les movie_id
changeaient → le cache .tracks_cache/<id> devenait orphelin et les
sous-titres devaient être ré-extraits à chaque redémarrage.

On dérive maintenant l'identifiant d'un MD5 du chemin : stable tant que le
fichier ne bouge pas, donc le cache (sous-titres, remux audio) survit aux
redémarrages du service.
"""

import hashlib
import re
from pathlib import Path

YEAR_PATTERN = r'\b(19[5-9]\d|20[0-3]\d)\b'

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


# ─── Identifiants stables ────────────────────────────────────────────────────

def stable_file_id(filepath) -> str:
    """ID stable et déterministe d'un fichier (survit aux redémarrages)."""
    return hashlib.md5(str(filepath).encode("utf-8")).hexdigest()[:16]


def stable_group_id(key: str) -> str:
    """ID stable pour un groupe (série)."""
    return "s_" + hashlib.md5(key.encode("utf-8")).hexdigest()[:16]


# ─── Titre / année / qualité / épisode ───────────────────────────────────────

def clean_title(filename: str) -> str:
    name = Path(filename).stem
    year_match = re.search(YEAR_PATTERN, name)
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
    match = re.search(YEAR_PATTERN, filename)
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


# ─── Langues ─────────────────────────────────────────────────────────────────

def lang_label(code: str) -> str:
    """Libellé lisible d'un code de langue de piste ('fre' → 'Français')."""
    return {
        "fre": "Français", "fra": "Français", "fr": "Français",
        "eng": "English",  "en": "English",
        "spa": "Español",  "es": "Español",
        "ger": "Deutsch",  "deu": "Deutsch", "de": "Deutsch",
        "ita": "Italiano", "it": "Italiano",
        "jpn": "日本語",    "ja": "日本語",
        "und": "Inconnue",
    }.get((code or "und").lower(), (code or "und").upper())


def norm_lang(code: str) -> str:
    """Normalise un code de langue (fre/fra/french → fr, eng/english → en)."""
    c = (code or "").lower()
    if c in ("fr", "fre", "fra", "french", "francais", "français"):
        return "fr"
    if c in ("en", "eng", "english"):
        return "en"
    return c


def norm_title(s: str) -> str:
    """Titre normalisé pour comparaison (minuscules, sans ponctuation)."""
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()
