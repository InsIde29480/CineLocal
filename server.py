#!/usr/bin/env python3
"""
CineLocal - Serveur de films local style Netflix
Lance avec: python server.py

Point d'entrée : configure les logs, assemble l'application Flask (voir le
package `cinelocal/`) puis démarre le serveur waitress. Toute la logique
métier vit dans les modules du package :

    cinelocal/config.py         valeurs par défaut (chemins, codecs, timeouts)
    cinelocal/settings.py       paramètres persistés (onglet Paramètres du site)
    cinelocal/state.py          état partagé rebindable (chemins configurés)
    cinelocal/parsing.py        analyse des noms de fichiers + ids stables
    cinelocal/library.py        scan de la bibliothèque (films/séries/qualités)
    cinelocal/scanner.py        extraction en masse + analyse auto périodique
    cinelocal/media/            ffprobe, sous-titres, streaming/transcodage
    cinelocal/providers/        clients TMDB et OpenSubtitles
    cinelocal/routes/           routes HTTP (blueprints Flask)
"""

import logging
import threading

from waitress import serve

from cinelocal import config, create_app, state
from cinelocal.library import get_movies
from cinelocal.scanner import auto_scan_loop, start_subtitle_scan

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s : %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

app = create_app()

if __name__ == "__main__":
    print("━" * 60)
    print("CineLocal — Serveur de films local")
    print(f"Dossiers films : {', '.join(str(d) for d in state.MOVIES_DIRS)}")
    print(f"Interface     : http://localhost:{config.PORT}")
    print(f"Pour TV/Cast  : http://<ton-ip>:{config.PORT}")
    print("━" * 60)

    get_movies()

    # Extraction anticipée des sous-titres de tout le dossier, en tâche de fond.
    # Grâce aux id MD5 stables, les caches existants sont réutilisés après un
    # redémarrage : seuls les fichiers nouveaux ou modifiés sont ré-extraits.
    start_subtitle_scan()

    # Analyse automatique périodique (nouveaux films + extraction + téléchargement FR).
    threading.Thread(target=auto_scan_loop, daemon=True).start()

    serve(app, host=config.HOST, port=config.PORT, threads=8)
