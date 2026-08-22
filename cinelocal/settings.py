"""
Paramètres modifiables depuis l'interface (persistés).

Les valeurs de `cinelocal.config` servent de valeurs PAR DÉFAUT. Elles peuvent
être surchargées via l'onglet « Paramètres » du site ; la config est alors
sauvegardée dans SETTINGS_FILE et rechargée à chaque démarrage.

Le dictionnaire `SETTINGS` est créé UNE SEULE FOIS à l'import et n'est jamais
rebindé (save_settings le modifie en place) : les autres modules peuvent donc
l'importer directement.
"""

import json
import logging
import threading
from pathlib import Path

from . import config, state

log = logging.getLogger(__name__)

SETTINGS_LOCK = threading.Lock()

# Valeurs par défaut des chemins (capturées avant toute surcharge par settings).
_DEFAULT_MOVIES_DIR       = config.MOVIES_DIR
_DEFAULT_TRACKS_CACHE_DIR = config.TRACKS_CACHE_DIR


def default_settings() -> dict:
    return {
        "tmdb_api_key":           config.TMDB_API_KEY,
        "opensubtitles_api_key":  config.OPENSUBTITLES_API_KEY,
        "opensubtitles_username": config.OPENSUBTITLES_USERNAME,
        "opensubtitles_password": config.OPENSUBTITLES_PASSWORD,
        "opensubtitles_langs":    list(config.OPENSUBTITLES_LANGS),
        "movies_dirs":            [str(_DEFAULT_MOVIES_DIR)],
        "tracks_cache_dir":       str(_DEFAULT_TRACKS_CACHE_DIR),
        "auto_scan_enabled":          True,
        "auto_scan_interval_minutes": 60,
    }


def _load_settings() -> dict:
    s = default_settings()
    if config.SETTINGS_FILE.exists():
        try:
            data = json.loads(config.SETTINGS_FILE.read_text(encoding="utf-8"))
            for k in s:
                if k in data and data[k] not in (None, ""):
                    s[k] = data[k]
            # Migration : ancien champ unique « movies_dir » → liste « movies_dirs ».
            if "movies_dirs" not in data and data.get("movies_dir"):
                s["movies_dirs"] = [data["movies_dir"]]
        except Exception as e:
            log.warning("Lecture des paramètres échouée : %s", e)
    # Normalise movies_dirs en liste de chemins non vides.
    md = s.get("movies_dirs")
    if isinstance(md, str):
        md = [md]
    s["movies_dirs"] = [p for p in (md or []) if str(p).strip()] or [str(_DEFAULT_MOVIES_DIR)]
    return s


SETTINGS = _load_settings()


def apply_paths_from_settings():
    """Rebinde les chemins (films / cache sous-titres) depuis les paramètres."""
    state.MOVIES_DIRS = [Path(p).expanduser() for p in SETTINGS["movies_dirs"]]
    state.TRACKS_CACHE_DIR = Path(SETTINGS["tracks_cache_dir"]).expanduser()
    try:
        state.TRACKS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        log.warning("Création du dossier des sous-titres échouée (%s) : %s",
                    state.TRACKS_CACHE_DIR, e)


# Applique les chemins configurés dès le chargement (avant tout scan / mkdir).
apply_paths_from_settings()


def save_settings(new: dict) -> bool:
    """Fusionne et persiste les paramètres. Réinitialise le jeton OpenSubtitles."""
    with SETTINGS_LOCK:
        for k in default_settings():
            if k in new and new[k] is not None:
                SETTINGS[k] = new[k]
        try:
            config.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            config.SETTINGS_FILE.write_text(
                json.dumps(SETTINGS, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            log.error("Écriture des paramètres échouée : %s", e)
            return False
    state.os_token = None   # identifiants potentiellement changés → nouvelle connexion
    return True
