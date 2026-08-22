"""
Configuration de CineLocal : constantes et valeurs PAR DÉFAUT.

Ces valeurs peuvent être surchargées via l'onglet « Paramètres » du site
(voir `cinelocal.settings`) ; la config est alors sauvegardée dans
SETTINGS_FILE et rechargée à chaque démarrage.
"""

import os
from pathlib import Path

# ─── Chemins par défaut ──────────────────────────────────────────────────────
MOVIES_DIR       = Path.home() / "storage/6TO/Films"
STATIC_DIR       = Path(__file__).parent.parent / "static"
TMDB_CACHE_FILE  = Path.home() / "storage/6TO/.tmdb_cache.json"
TRACKS_CACHE_DIR = Path.home() / "storage/6TO/.tracks_cache"
SETTINGS_FILE    = Path.home() / "storage/6TO/.cinelocal_settings.json"

SUPPORTED_EXTS   = {".mp4", ".mkv"}
HOST             = "0.0.0.0"
PORT             = 8765

# ─── TMDB (affiches / synopsis) ──────────────────────────────────────────────
# La clé n'est PAS écrite en dur dans le code : elle se règle depuis l'onglet
# « Paramètres » du site (recommandé) ou via la variable d'environnement
# TMDB_API_KEY. Compte gratuit : https://www.themoviedb.org/settings/api
TMDB_API_KEY     = os.environ.get("TMDB_API_KEY", "")

# ─── OpenSubtitles (téléchargement des sous-titres manquants) ─────────────────
# Beaucoup de rips BluRay n'ont que des sous-titres PGS (image) : impossible à
# convertir en texte sans OCR. On récupère alors de vrais .srt texte en ligne.
#
# À REMPLIR depuis l'onglet « Paramètres » du site (compte gratuit sur
# https://www.opensubtitles.com puis https://www.opensubtitles.com/consumers
# pour la clé API) :
#   - clé API « consumer » (obligatoire)
#   - identifiant / mot de passe du compte (obligatoires pour TÉLÉCHARGER ;
#     la recherche seule n'en a pas besoin)
# Sans ces valeurs, le bouton de téléchargement reste inactif et l'explique.
OPENSUBTITLES_API_KEY    = ""
OPENSUBTITLES_USERNAME   = ""
OPENSUBTITLES_PASSWORD   = ""
OPENSUBTITLES_LANGS      = ["fr", "en"]   # ordre de préférence : fr d'abord, en en secours
OPENSUBTITLES_USER_AGENT = "CineLocal v1.0"
OPENSUBTITLES_BASE       = "https://api.opensubtitles.com/api/v1"

# ─── Codecs ──────────────────────────────────────────────────────────────────
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

# ─── Types MIME servis en streaming ──────────────────────────────────────────
MIME_MAP = {
    ".mp4":  "video/mp4",
    ".mkv":  "video/x-matroska",
    ".webm": "video/webm",
}
