"""
État partagé et REBINDABLE entre les modules.

Les chemins configurables (dossiers de films, cache des sous-titres) peuvent
changer en cours d'exécution quand l'utilisateur modifie les Paramètres :
`settings.apply_paths_from_settings()` les rebinde ici. Les autres modules
doivent donc TOUJOURS y accéder via `state.MOVIES_DIRS` / `state.TRACKS_CACHE_DIR`
(jamais via `from ... import MOVIES_DIRS`, qui figerait l'ancienne valeur).
"""

from pathlib import Path

from . import config

# Dossiers de films scannés (liste — plusieurs disques possibles).
MOVIES_DIRS: list = [config.MOVIES_DIR]

# Dossier de cache des sous-titres VTT + métadonnées (.tracks_cache).
TRACKS_CACHE_DIR: Path = config.TRACKS_CACHE_DIR

# Jeton d'authentification OpenSubtitles (login). Réinitialisé à None quand
# les identifiants changent dans les Paramètres → nouvelle connexion au
# prochain téléchargement.
os_token = None
