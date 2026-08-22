"""
Client TMDB : affiches, fonds et synopsis des films/séries.

Les résultats (y compris les « aucun résultat ») sont mis en cache dans
TMDB_CACHE_FILE pour ne pas re-interroger l'API à chaque scan.
"""

import json
import logging
import re
import threading

import requests

from .. import config
from ..parsing import YEAR_PATTERN
from ..settings import SETTINGS

log = logging.getLogger(__name__)

_tmdb_lock = threading.Lock()


def _load_tmdb_cache() -> dict:
    if config.TMDB_CACHE_FILE.exists():
        try:
            return json.loads(config.TMDB_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


_tmdb_cache = _load_tmdb_cache()


def save_tmdb_cache():
    with _tmdb_lock:
        config.TMDB_CACHE_FILE.write_text(
            json.dumps(_tmdb_cache, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )


def is_movie_cached(title: str, year) -> bool:
    """Vrai si la recherche film (titre, année) est déjà en cache."""
    query = re.sub(YEAR_PATTERN, '', title).strip()
    return f"{query}|{year or ''}" in _tmdb_cache


def is_tv_cached(title: str) -> bool:
    """Vrai si la recherche série (titre) est déjà en cache."""
    return f"TV|{title}" in _tmdb_cache


def _tmdb_format(m: dict, title_key: str = "title") -> dict:
    return {
        "poster":   f"https://image.tmdb.org/t/p/w342{m['poster_path']}"    if m.get("poster_path")   else None,
        "backdrop": f"https://image.tmdb.org/t/p/w1280{m['backdrop_path']}" if m.get("backdrop_path") else None,
        "overview": m.get("overview", ""),
        "tmdb_title": m.get(title_key, ""),
        "tmdb_id": m.get("id"),
    }


def fetch_tmdb_tv(title: str) -> dict | None:
    clean = re.sub(r'\s*[Ss]\d{1,2}[Ee]\d{1,2}.*$', '', title).strip()
    key = f"TV|{clean}"
    if key in _tmdb_cache:
        return _tmdb_cache[key]
    try:
        r = requests.get(
            "https://api.themoviedb.org/3/search/tv",
            params={"api_key": SETTINGS["tmdb_api_key"], "query": clean, "language": "fr-FR"},
            timeout=5
        )
        results = r.json().get("results", [])
        if results:
            result = _tmdb_format(results[0], title_key="name")
            log.info("TMDB (série) : '%s' → '%s'", clean, results[0].get('name'))
        else:
            result = None
            log.info("TMDB (série) : '%s' → aucun résultat", clean)
        _tmdb_cache[key] = result
        return result
    except Exception as e:
        log.warning("TMDB série échec : %s", e)
        return None


def fetch_tmdb_movie(title: str, year: str | None) -> dict | None:
    query = re.sub(YEAR_PATTERN, '', title).strip()
    key = f"{query}|{year or ''}"
    if key in _tmdb_cache:
        return _tmdb_cache[key]
    try:
        params = {"api_key": SETTINGS["tmdb_api_key"], "query": query, "language": "fr-FR"}
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
            log.info("TMDB : '%s' → '%s'", query, results[0].get('title'))
        else:
            result = None
            log.info("TMDB : '%s' (%s) → aucun résultat", query, year)
        _tmdb_cache[key] = result
        return result
    except Exception as e:
        log.warning("TMDB échec pour '%s' : %s", query, e)
        return None
