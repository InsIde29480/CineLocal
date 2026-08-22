"""Routes HTTP de CineLocal, regroupées en blueprints Flask par domaine."""

from .movies import bp as movies_bp
from .pages import bp as pages_bp
from .settings import bp as settings_bp
from .streaming import bp as streaming_bp
from .subtitles import bp as subtitles_bp

ALL_BLUEPRINTS = [pages_bp, movies_bp, subtitles_bp, streaming_bp, settings_bp]
