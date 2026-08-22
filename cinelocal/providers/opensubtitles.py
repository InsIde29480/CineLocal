"""
Client OpenSubtitles — téléchargement des sous-titres manquants.

Pour les films dont les seuls sous-titres sont des images (PGS), on récupère
un vrai .srt texte en ligne et on l'enregistre à côté du film sous la forme
« NomDuFilm.fr.srt » : le pipeline d'extraction existant le détecte alors
automatiquement et le convertit en VTT.
"""

import logging
import os
import struct
import threading
from pathlib import Path

import requests

from .. import config, state
from ..media.subtitles import cached_sub_langs, decode_subtitle_bytes
from ..parsing import norm_lang, norm_title
from ..settings import SETTINGS

log = logging.getLogger(__name__)

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
        log.warning("Hash OpenSubtitles échec (%s) : %s", path, e)
        return None


def _os_headers(with_auth: bool = False) -> dict:
    h = {
        "Api-Key":      SETTINGS["opensubtitles_api_key"],
        "Content-Type": "application/json",
        "User-Agent":   config.OPENSUBTITLES_USER_AGENT,
    }
    if with_auth and state.os_token:
        h["Authorization"] = f"Bearer {state.os_token}"
    return h


def _os_login() -> tuple:
    """Ouvre une session OpenSubtitles (nécessaire pour télécharger). (ok, reason)."""
    if state.os_token:
        return True, None
    username = SETTINGS["opensubtitles_username"]
    password = SETTINGS["opensubtitles_password"]
    if not (username and password):
        return False, "identifiants OpenSubtitles non configurés (username/password)"
    with _os_token_lock:
        if state.os_token:
            return True, None
        try:
            r = requests.post(
                f"{config.OPENSUBTITLES_BASE}/login",
                headers=_os_headers(),
                json={"username": username, "password": password},
                timeout=15,
            )
            if r.status_code != 200:
                return False, f"login refusé (HTTP {r.status_code})"
            state.os_token = r.json().get("token")
            return (True, None) if state.os_token else (False, "login sans jeton")
        except Exception as e:
            return False, f"login injoignable : {e}"


def _os_query(params: dict) -> list:
    """Un appel de recherche OpenSubtitles. Renvoie la liste des résultats."""
    try:
        r = requests.get(
            f"{config.OPENSUBTITLES_BASE}/subtitles",
            headers=_os_headers(), params=params, timeout=15,
        )
        if r.status_code != 200:
            return []
        return r.json().get("data", []) or []
    except Exception as e:
        log.warning("Recherche OpenSubtitles échec : %s", e)
        return []


def _os_pick_best(data: list, movie: dict, lang: str, verify_meta: bool):
    """
    Choisit le MEILLEUR sous-titre parmi des résultats, avec des garde-fous
    contre les mauvais appariements :
      - langue exacte ;
      - (si verify_meta) année ±1 et titre cohérent avec le film ;
      - on écarte les « foreign parts only » (quasi vides) ;
      - on privilégie hash match, sources de confiance, notes, téléchargements.
    Renvoie un file_id ou None.
    """
    try:
        want_year = int(movie["year"]) if movie.get("year") else None
    except (TypeError, ValueError):
        want_year = None
    want_title = norm_title(movie.get("title"))

    cands = []
    for item in data:
        attr = item.get("attributes", {}) or {}
        if norm_lang(attr.get("language")) != norm_lang(lang):
            continue
        files = attr.get("files") or []
        if not files or not files[0].get("file_id"):
            continue

        fd = attr.get("feature_details", {}) or {}
        if verify_meta:
            fy = fd.get("year")
            try:
                fy = int(fy) if fy else None
            except (TypeError, ValueError):
                fy = None
            if want_year and fy and abs(fy - want_year) > 1:
                continue   # mauvaise année → très probablement le mauvais film
            ft = norm_title(fd.get("title") or fd.get("movie_name"))
            if want_title and ft and want_title not in ft and ft not in want_title:
                aw, bw = set(want_title.split()), set(ft.split())
                if not aw or len(aw & bw) / len(aw) < 0.6:
                    continue   # titre trop différent

        cands.append((item, attr, files[0]["file_id"]))

    if not cands:
        return None

    def score(c):
        attr = c[1]
        return (
            0 if attr.get("foreign_parts_only") else 1,   # évite les sous-titres partiels
            1 if attr.get("moviehash_match") else 0,
            1 if attr.get("from_trusted") else 0,
            float(attr.get("ratings") or 0),
            int(attr.get("download_count") or 0),
        )
    cands.sort(key=score, reverse=True)
    return cands[0][2]


def _os_search(movie: dict, lang: str):
    """
    Cherche le meilleur sous-titre pour un film/épisode, du plus fiable au moins
    fiable : hash du fichier → identifiant TMDB (exact) → repli par titre/année
    (avec vérification stricte). Renvoie un file_id ou None.
    """
    is_episode = movie.get("season") is not None and movie.get("episode") is not None
    tmdb_id    = movie.get("tmdb_id")
    strategies = []   # (params, verify_meta)

    mh = movie.get("_os_hash")
    if mh:
        strategies.append(({"moviehash": mh, "languages": lang}, False))

    if tmdb_id and is_episode:
        strategies.append(({
            "parent_tmdb_id": tmdb_id,
            "season_number":  movie["season"],
            "episode_number": movie["episode"],
            "languages": lang, "type": "episode",
        }, False))
    elif tmdb_id:
        strategies.append(({"tmdb_id": tmdb_id, "languages": lang, "type": "movie"}, False))

    # Repli par titre : uniquement pour les films (pour un épisode sans TMDB, une
    # recherche « S01E01 » ne donnerait que du bruit → on s'abstient).
    if not is_episode:
        q = {"query": movie.get("title") or Path(movie["path"]).stem, "languages": lang}
        if movie.get("year"):
            q["year"] = movie["year"]
        strategies.append((q, True))

    for params, verify in strategies:
        best = _os_pick_best(_os_query(params), movie, lang, verify)
        if best:
            return best
    return None


def _os_download_content(file_id) -> tuple:
    """Récupère le contenu SRT d'un file_id. (texte|None, reason)."""
    ok, reason = _os_login()
    if not ok:
        return None, reason
    try:
        r = requests.post(
            f"{config.OPENSUBTITLES_BASE}/download",
            headers=_os_headers(with_auth=True),
            json={"file_id": file_id}, timeout=20,
        )
        if r.status_code == 406:
            return None, "quota de téléchargements OpenSubtitles atteint (réessaie demain)"
        if r.status_code == 401:
            # jeton expiré : on force une nouvelle connexion au prochain appel
            state.os_token = None
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
        return decode_subtitle_bytes(dl.content), None
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
    if not SETTINGS["opensubtitles_api_key"]:
        return False, "clé API OpenSubtitles non configurée (onglet Paramètres)"

    path = movie["path"]
    movie["_os_hash"] = opensubtitles_hash(path)

    have = cached_sub_langs(movie["id"])   # langues déjà disponibles
    last_reason = "aucun sous-titre français trouvé en ligne"
    for lang in (SETTINGS["opensubtitles_langs"] or ["fr", "en"]):
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
        log.info("Sous-titre téléchargé (%s) : %s", lang, srt_path.name)
        return True, lang

    return False, last_reason
