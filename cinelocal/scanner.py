"""
Extraction en masse des sous-titres (balayage profond des dossiers de films)
et analyse automatique périodique.

Extrait à l'avance les sous-titres de TOUS les fichiers (films + épisodes),
sans attendre qu'on clique sur un film. Le résultat est mis en cache dans
<tracks_cache>/<id> avec un id MD5 stable, donc il survit à un redémarrage du
service : au prochain lancement, seuls les fichiers nouveaux/modifiés sont
ré-extraits.
"""

import logging
import threading
import time

from . import library
from .media.subtitles import cached_subs_state, extract_tracks, group_sub_langs
from .providers.opensubtitles import download_subtitle_for
from .settings import SETTINGS

log = logging.getLogger(__name__)

_subs_scan_lock  = threading.Lock()
_subs_scan_thread = None
_subs_scan_state = {
    "running":     False,
    "mode":        None,  # 'new' | 'retry' | 'force' | 'download'
    "started_at":  None,
    "finished_at": None,
    "total":       0,     # nombre total de fichiers à traiter
    "done":        0,     # fichiers traités
    "current":     None,  # fichier en cours d'extraction
    "with_subs":     0,   # fichiers pour lesquels au moins un sous-titre a été produit
    "no_subs":       0,   # fichiers sans aucun sous-titre exploitable
    "failed":        0,   # fichiers avec au moins un échec d'extraction
    "downloaded":    0,   # sous-titres récupérés en ligne (mode 'download')
    "failures":      [],  # [{filename, title, reasons: [...]}]
    "no_subs_files": [],  # [{filename, title, reasons: [...]}] — pour vérification manuelle
}


def public_scan_state() -> dict:
    with _subs_scan_lock:
        st = dict(_subs_scan_state)
        st["failures"]      = list(_subs_scan_state["failures"])
        st["no_subs_files"] = list(_subs_scan_state["no_subs_files"])
    return st


def _needs_retry(movie_id: str) -> bool:
    """Vrai pour les fichiers à reprendre en mode 'retry' (échecs, sans S-T, jamais scannés)."""
    return cached_subs_state(movie_id) != "has_subs"


def _needs_download(movie: dict) -> bool:
    """
    Vrai si AUCUNE version du film n'a de sous-titre français (rien, ou seulement
    de l'anglais / un externe). Comme les versions partagent leurs sous-titres,
    un seul téléchargement couvre HD et 4K.
    """
    return "fr" not in group_sub_langs(movie)


def _run_subtitle_scan(mode: str = "new"):
    """
    Balaie les fichiers jouables et extrait leurs sous-titres.
    Exécuté dans un thread de fond. Séquentiel volontairement : sur un HDD,
    lancer plusieurs ffmpeg en parallèle ralentit tout le monde (seeks concurrents).

    mode :
      'new'   → tout vérifier ; les fichiers déjà complets en cache sont sautés
                instantanément (comportement par défaut / démarrage).
      'retry' → ne reprend QUE les échecs et les fichiers sans sous-titre
                (leur cache est purgé puis ré-extrait). Ceux qui ont déjà des
                sous-titres ne sont pas touchés.
      'force' → purge TOUS les caches puis tout ré-extrait.
      'download' → pour les fichiers SANS sous-titre texte, télécharge un .srt
                en ligne (OpenSubtitles, fr puis en) et le convertit en VTT.
    """
    items = library.playable_items()   # garantit le scan + l'index jouable

    # 'retry' cible les échecs et fichiers sans sous-titre ; 'download' cible les
    # films sans sous-titre FRANÇAIS (rien, ou seulement anglais/externe).
    if mode == "retry":
        items = [it for it in items if _needs_retry(it["id"])]
    elif mode == "download":
        # Un seul téléchargement par film (les versions HD/4K partagent les S-T) :
        # on garde une seule variante par groupe.
        filtered, seen_groups = [], set()
        for it in items:
            if not _needs_download(it):
                continue
            gid = tuple(sorted(it.get("sibling_ids") or [it["id"]]))
            if gid in seen_groups:
                continue
            seen_groups.add(gid)
            filtered.append(it)
        items = filtered

    with _subs_scan_lock:
        _subs_scan_state.update({
            "running":     True,
            "mode":        mode,
            "started_at":  time.time(),
            "finished_at": None,
            "total":       len(items),
            "done":        0,
            "current":     None,
            "with_subs":     0,
            "no_subs":       0,
            "failed":        0,
            "downloaded":    0,
            "failures":      [],
            "no_subs_files": [],
        })

    labels = {
        "new": "vérification", "retry": "reprise des échecs/sans S-T",
        "force": "forcée", "download": "téléchargement en ligne",
    }
    log.info("Extraction des sous-titres (%s) : %d fichier(s)",
             labels.get(mode, mode), len(items))

    for item in items:
        filename = item.get("filename", "?")
        title    = item.get("title") or filename
        with _subs_scan_lock:
            _subs_scan_state["current"] = filename

        try:
            if mode == "download":
                # Télécharge un .srt français à côté du film. On ne ré-extrait
                # (coûteux : force=True supprime et refait tout le cache) QUE si
                # un sous-titre a réellement été téléchargé. Sinon on lit le cache
                # existant — sans quoi chaque cycle re-traiterait tous les
                # épisodes sans français trouvable en ligne.
                dl_ok, dl_info = download_subtitle_for(item)
                meta = extract_tracks(item, force=dl_ok)
                reasons = meta.get("failures", [])
                present = group_sub_langs(item)   # langues de toutes les versions
                has_fr  = "fr" in present
                with _subs_scan_lock:
                    if reasons:
                        _subs_scan_state["failed"] += 1
                        _subs_scan_state["failures"].append({
                            "filename": filename, "title": title, "reasons": reasons,
                        })
                    if has_fr:
                        _subs_scan_state["with_subs"] += 1
                        if dl_ok:
                            _subs_scan_state["downloaded"] += 1
                    else:
                        # Toujours pas de français : on liste pourquoi (raison du
                        # téléchargement + langues déjà présentes le cas échéant).
                        _subs_scan_state["no_subs"] += 1
                        why = []
                        if dl_info:
                            why.append(f"Téléchargement : {dl_info}")
                        others = sorted(l for l in present if l and l != "und")
                        if others:
                            why.append("Sous-titres présents (pas de FR) : " + ", ".join(others))
                        why += meta.get("skipped", [])
                        if not why:
                            why = ["Aucun sous-titre français disponible"]
                        _subs_scan_state["no_subs_files"].append({
                            "filename": filename, "title": title, "reasons": why,
                        })
            else:
                # 'force' et 'retry' relancent une extraction complète : on passe
                # force=True à extract_tracks, qui supprime le dossier du film et
                # ré-extrait de zéro (au lieu de réutiliser le cache existant).
                hard = mode in ("force", "retry")
                meta = extract_tracks(item, force=hard)
                n_subs   = len(meta.get("subtitle_tracks", []))
                reasons  = meta.get("failures", [])
                with _subs_scan_lock:
                    if reasons:
                        _subs_scan_state["failed"] += 1
                        _subs_scan_state["failures"].append({
                            "filename": filename,
                            "title":    title,
                            "reasons":  reasons,
                        })
                    if n_subs > 0:
                        _subs_scan_state["with_subs"] += 1
                    else:
                        _subs_scan_state["no_subs"] += 1
                        # Pourquoi ce film n'a-t-il aucun sous-titre ? (image,
                        # langue, ou aucune piste) — pour vérification manuelle.
                        why = list(meta.get("skipped", []))
                        if not why:
                            why = ["Aucune piste de sous-titre dans le fichier"]
                        _subs_scan_state["no_subs_files"].append({
                            "filename": filename,
                            "title":    title,
                            "reasons":  why,
                        })
        except Exception as e:
            with _subs_scan_lock:
                _subs_scan_state["failed"] += 1
                _subs_scan_state["failures"].append({
                    "filename": filename,
                    "title":    title,
                    "reasons":  [f"Erreur inattendue : {e}"],
                })
        finally:
            with _subs_scan_lock:
                _subs_scan_state["done"] += 1

    with _subs_scan_lock:
        _subs_scan_state["running"]     = False
        _subs_scan_state["current"]     = None
        _subs_scan_state["finished_at"] = time.time()

    log.info("Extraction terminée : %d avec sous-titres, %d sans, %d en échec%s",
             _subs_scan_state['with_subs'], _subs_scan_state['no_subs'],
             _subs_scan_state['failed'],
             f", {_subs_scan_state['downloaded']} téléchargé(s)" if mode == "download" else "")


def start_subtitle_scan(mode: str = "new") -> bool:
    """Démarre l'extraction en masse si elle n'est pas déjà en cours."""
    global _subs_scan_thread
    if mode not in ("new", "retry", "force", "download"):
        mode = "new"
    with _subs_scan_lock:
        if _subs_scan_state["running"]:
            return False
        _subs_scan_state["running"] = True   # verrou immédiat (évite un double départ)
    _subs_scan_thread = threading.Thread(
        target=_run_subtitle_scan, kwargs={"mode": mode}, daemon=True
    )
    _subs_scan_thread.start()
    return True


# ══════════════════════════════════════════════════════════════════════════════
# ANALYSE AUTOMATIQUE PÉRIODIQUE
# ══════════════════════════════════════════════════════════════════════════════
# Toutes les N minutes (réglable dans les Paramètres) :
#   1. redétecte les nouveaux films ajoutés sur le disque ;
#   2. extrait leurs sous-titres ;
#   3. télécharge le sous-titre français manquant (si clé OpenSubtitles définie).

def _wait_scan_done(timeout: float = 6 * 3600):
    """Bloque tant qu'une analyse est en cours (avec garde-fou de temps)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _subs_scan_lock:
            if not _subs_scan_state["running"]:
                return
        time.sleep(2)


def _run_auto_cycle():
    """Un passage complet : rescan disque → extraction → téléchargement FR."""
    _wait_scan_done()                       # laisse finir un scan démarrage/manuel
    library.invalidate_cache()              # force la redétection des nouveaux fichiers
    if start_subtitle_scan(mode="new"):
        _wait_scan_done()
    # Téléchargement seulement si OpenSubtitles est configuré et l'auto encore actif.
    if SETTINGS.get("opensubtitles_api_key") and SETTINGS.get("auto_scan_enabled", True):
        if start_subtitle_scan(mode="download"):
            _wait_scan_done()


def auto_scan_loop():
    """Boucle de fond de l'analyse automatique (à lancer dans un thread daemon)."""
    time.sleep(120)                          # laisse le démarrage se terminer
    while True:
        try:
            if SETTINGS.get("auto_scan_enabled", True):
                log.info("Analyse automatique : démarrage d'un cycle…")
                _run_auto_cycle()
                log.info("Analyse automatique : cycle terminé.")
        except Exception as e:
            log.error("Analyse automatique : erreur — %s", e)
        try:
            interval = max(5, int(SETTINGS.get("auto_scan_interval_minutes", 60)))
        except Exception:
            interval = 60
        # Sommeil découpé en tranches d'1 min pour réagir vite à un changement
        # d'intervalle ou de l'activation via les Paramètres.
        for _ in range(interval):
            time.sleep(60)
