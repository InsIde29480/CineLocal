"""
Bibliothèque : scan des dossiers de films, regroupement (films / séries /
variantes de qualité) et enrichissement TMDB.

Deux index sont maintenus :
  - le catalogue (`get_movies`) : entrées « film » et « série » pour l'interface ;
  - l'index jouable (`get_movie_by_id`) : chaque FICHIER (variante de qualité,
    épisode) indexé à plat par son id — c'est ce que la lecture/cast utilisent.
"""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from . import config, state
from .parsing import (clean_title, detect_quality, extract_year, parse_episode,
                      series_title, stable_file_id, stable_group_id)
from .providers import tmdb

log = logging.getLogger(__name__)

_movies_cache = None
_movies_lock = threading.Lock()
_playable_index = {}        # id de fichier -> dict jouable (rempli par scan_movies)


def _set_playable(mapping: dict):
    global _playable_index
    _playable_index = mapping


def scan_movies() -> list:
    items = []
    series_groups = {}
    movie_groups = {}
    playable = {}        # id de fichier -> dict jouable (path, title, ext...)

    roots = [d for d in state.MOVIES_DIRS if d.exists()]
    if not roots:
        log.warning("Aucun dossier de films accessible : %s",
                    [str(d) for d in state.MOVIES_DIRS])
        _set_playable(playable)
        return items

    # On agrège les fichiers de TOUS les dossiers configurés (même structure
    # Films/<catégorie>/…). L'id étant un MD5 du chemin complet, il reste unique
    # même si deux disques ont la même arborescence.
    scanned = []
    for root in roots:
        for filepath in root.rglob("*"):
            if filepath.suffix.lower() not in config.SUPPORTED_EXTS:
                continue
            try:
                rel = filepath.relative_to(root)
            except ValueError:
                continue
            if any(part.startswith(".") for part in rel.parts):
                continue
            scanned.append((filepath, rel))

    for filepath, rel in sorted(scanned, key=lambda t: str(t[0])):
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
            qlabel, qheight = detect_quality(filepath.name)
            # Tous les épisodes d'une série sont regroupés ; à l'intérieur, les
            # fichiers d'un même épisode (S01E01) sont regroupés comme variantes
            # de qualité (4K / HD), exactement comme les films.
            group_key = stitle
            ep_variant = {
                **common,
                "season":         ep["season"],
                "episode":        ep["episode"],
                "quality":        qlabel,
                "quality_height": qheight,
                "title":          f"S{ep['season']:02d}E{ep['episode']:02d}",
            }
            playable[movie_id] = ep_variant
            if group_key not in series_groups:
                series_groups[group_key] = {
                    "stitle": stitle, "category": category,
                    "ep_variants": {},
                }
            ep_key = (ep["season"], ep["episode"])
            series_groups[group_key]["ep_variants"].setdefault(ep_key, []).append(ep_variant)
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
        # Toutes les versions d'un même film partagent leurs sous-titres : on
        # note sur chaque variante la liste des ids frères (HD, 4K…).
        variant_ids = [v["id"] for v in variants]
        for v in variants:
            v["sibling_ids"] = variant_ids
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
        episodes = []
        for ep_key, variants in group["ep_variants"].items():
            # Meilleure qualité en premier ; à hauteur égale, le plus gros fichier.
            variants = sorted(
                variants, key=lambda v: (v["quality_height"], v["size_mb"]), reverse=True
            )
            # Les versions d'un même épisode partagent leurs sous-titres.
            variant_ids = [v["id"] for v in variants]
            for v in variants:
                v["sibling_ids"] = variant_ids
            primary = variants[0]
            episodes.append({
                "id":       primary["id"],          # id jouable par défaut (meilleure qualité)
                "season":   primary["season"],
                "episode":  primary["episode"],
                "size_mb":  primary["size_mb"],
                "ext":      primary["ext"],
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
            })
        episodes.sort(key=lambda e: (e["season"], e["episode"]))
        items.append({
            "id":          series_id,
            "title":       group["stitle"],
            "year":        None,
            "category":    group["category"],
            "size_mb":     sum(e["size_mb"] for e in episodes),
            "ext":         episodes[0]["ext"] if episodes else ".mkv",
            "kind":        "series",
            "episodes":    episodes,
            "episode_count": len(episodes),
            "season_count": len({e["season"] for e in episodes}),
            "poster":      None, "backdrop": None, "overview": "",
        })

    def fetch_for_item(item):
        if item["kind"] == "series":
            return tmdb.fetch_tmdb_tv(item["title"])
        return tmdb.fetch_tmdb_movie(item["title"], item.get("year"))

    to_fetch_count = sum(1 for it in items
                         if (it["kind"] == "series" and not tmdb.is_tv_cached(it["title"]))
                         or (it["kind"] == "movie" and not tmdb.is_movie_cached(it["title"], it.get("year"))))
    if to_fetch_count:
        log.info("Recherche TMDB pour %d entrée(s)...", to_fetch_count)

    with ThreadPoolExecutor(max_workers=10) as ex:
        results = list(ex.map(fetch_for_item, items))

    for it, tmdb_data in zip(items, results):
        if tmdb_data:
            it["poster"]   = tmdb_data["poster"]
            it["backdrop"] = tmdb_data["backdrop"]
            it["overview"] = tmdb_data["overview"]
            # Propage l'id TMDB aux fichiers jouables : indispensable pour un
            # appariement fiable des sous-titres OpenSubtitles.
            tid = tmdb_data.get("tmdb_id")
            it["tmdb_id"] = tid
            if tid:
                if it["kind"] == "movie":
                    for q in it.get("qualities", []):
                        if q["id"] in playable:
                            playable[q["id"]]["tmdb_id"] = tid
                else:
                    for ep in it.get("episodes", []):
                        for q in ep.get("qualities", [{"id": ep["id"]}]):
                            if q["id"] in playable:
                                playable[q["id"]]["tmdb_id"] = tid

    _set_playable(playable)
    tmdb.save_tmdb_cache()
    return items


def get_movies() -> list:
    global _movies_cache
    with _movies_lock:
        if _movies_cache is None:
            log.info("Scan des films en cours...")
            _movies_cache = scan_movies()
            log.info(" %d film(s) trouvé(s)", len(_movies_cache))
        return _movies_cache


def get_movie_by_id(movie_id: str) -> dict | None:
    # Garantit que le scan a eu lieu (remplit aussi _playable_index).
    get_movies()
    # Toutes les variantes de qualité et tous les épisodes sont indexés à plat
    # par leur id de fichier — c'est ce que la lecture/cast/pistes utilisent.
    return _playable_index.get(movie_id)


def playable_items() -> list:
    """Tous les fichiers jouables (films + épisodes), après scan garanti."""
    get_movies()
    return list(_playable_index.values())


def invalidate_cache():
    """Force un re-scan complet au prochain get_movies() (nouveaux fichiers)."""
    global _movies_cache
    with _movies_lock:
        _movies_cache = None
