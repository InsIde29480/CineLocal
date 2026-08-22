"""
CineLocal — serveur de films local style Netflix.

`create_app()` assemble l'application Flask : configuration du cache statique,
en-têtes CORS et enregistrement des blueprints de routes.
"""

from flask import Flask, request

from . import config, state


def create_app() -> Flask:
    """Usine de l'application Flask."""
    app = Flask(__name__, static_folder=str(config.STATIC_DIR))
    # Ne pas laisser le navigateur garder app.js / style.css / index.html en cache
    # (par défaut Flask les met en cache 12 h → l'interface reste bloquée sur une
    # ancienne version après une mise à jour). 0 = revalidation à chaque chargement.
    app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0
    state.TRACKS_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    @app.after_request
    def add_cors_headers(response):
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Headers'] = 'Range'
        response.headers['Access-Control-Expose-Headers'] = (
            'Accept-Ranges, Content-Range, Content-Length, Content-Type'
        )
        # Interface (HTML/JS/CSS) : jamais de cache long, pour que les mises à jour
        # soient prises en compte immédiatement. Les vidéos ne sont pas concernées.
        p = request.path
        if p == '/' or p.startswith('/static/') or p.startswith('/track/'):
            # /track/ inclus : après une resynchro, le VTT modifié doit être rechargé.
            response.headers['Cache-Control'] = 'no-cache, must-revalidate'
        return response

    # Enregistrement des routes (import ici pour éviter les imports circulaires
    # au chargement du package).
    from .routes import ALL_BLUEPRINTS
    for bp in ALL_BLUEPRINTS:
        app.register_blueprint(bp)

    return app
