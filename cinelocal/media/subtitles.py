"""
Extraction et gestion des sous-titres.

- Extraction des pistes internes (SRT/ASS…) en VTT via ffmpeg, TOUTES les
  pistes d'un fichier en UNE SEULE passe (crucial pour les gros remux 4K).
- Détection et conversion des sous-titres externes (.srt/.vtt) avec
  normalisation de l'encodage (les .srt français sont souvent en Windows-1252).
- Cache disque dans <tracks_cache>/<movie_id>/ (tracks.json + subs_N.vtt),
  avec des identifiants MD5 stables qui survivent aux redémarrages.
- Resynchronisation : décalage des timecodes d'un VTT/SRT.
"""

import json
import logging
import shutil
import subprocess
import threading
import time
import re
from pathlib import Path

from .. import config, state
from ..parsing import lang_label, norm_lang
from .ffprobe import get_duration

log = logging.getLogger(__name__)

# Un verrou par film : évite deux extractions concurrentes du même fichier.
_extraction_locks = {}


def _safe_remove(path: Path):
    try:
        if path.exists():
            path.unlink()
    except Exception:
        pass


# ─── SUPERVISEUR FFMPEG ──────────────────────────────────────────────────────

def _run_ffmpeg_supervised(cmd: list, out_paths: list, idle_timeout: int, label: str,
                           cleanup_on_fail: bool = False, require_outputs: bool = False):
    """
    Lance ffmpeg et le surveille. Renvoie (ok, reason) :
      - (True, None) en cas de succès ;
      - (False, "raison") en cas d'échec.

    IMPORTANT : `idle_timeout` est un délai d'INACTIVITÉ, pas un plafond de temps
    total. ffmpeg n'est tué que si AUCUNE progression n'est détectée pendant
    `idle_timeout` secondes. Un film avec des milliers de lignes de sous-titres
    sur un HDD peut donc prendre bien plus de temps que ça, tant qu'il avance.

    La progression est détectée de deux façons redondantes :
      1. la sortie `-progress pipe:1` de ffmpeg (mise à jour ~2×/s) — la
         commande doit donc contenir `-progress pipe:1` ;
      2. la taille cumulée des fichiers `out_paths` qui augmente.

    `cleanup_on_fail`   : supprime les fichiers de sortie en cas d'échec.
    `require_outputs`   : le succès exige que chaque sortie existe et soit non vide.
    """
    last_activity = [time.time()]      # liste = mutable partagé avec les threads
    stderr_tail = []

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
        )
    except Exception as e:
        if cleanup_on_fail:
            for p in out_paths:
                _safe_remove(p)
        log.warning("Échec %s : %s", label, e)
        return False, str(e)

    def _drain_stdout():
        # Toute ligne de -progress (out_time=…, progress=continue/end…) = signe de vie.
        try:
            for _line in proc.stdout:
                last_activity[0] = time.time()
        except Exception:
            pass

    def _drain_stderr():
        # On garde les dernières lignes pour expliquer un éventuel échec.
        try:
            for line in proc.stderr:
                line = line.rstrip()
                if line:
                    stderr_tail.append(line)
                    if len(stderr_tail) > 10:
                        del stderr_tail[0]
        except Exception:
            pass

    t_out = threading.Thread(target=_drain_stdout, daemon=True)
    t_err = threading.Thread(target=_drain_stderr, daemon=True)
    t_out.start()
    t_err.start()

    killed_idle = False
    last_size = -1
    while True:
        try:
            proc.wait(timeout=2)
            break                       # ffmpeg a terminé (ok ou erreur)
        except subprocess.TimeoutExpired:
            pass

        # Secours : les sorties grossissent-elles ? (utile si -progress reste muet)
        try:
            total = sum(p.stat().st_size for p in out_paths if p.exists())
            if total != last_size:
                last_size = total
                last_activity[0] = time.time()
        except Exception:
            pass

        # Tue uniquement en cas d'inactivité prolongée — jamais sur le temps total.
        if time.time() - last_activity[0] > idle_timeout:
            killed_idle = True
            try:
                proc.kill()
                proc.wait(timeout=10)
            except Exception:
                pass
            break

    t_out.join(timeout=1)
    t_err.join(timeout=1)

    if killed_idle:
        if cleanup_on_fail:
            for p in out_paths:
                _safe_remove(p)
        reason = (f"Bloqué : aucune progression pendant {idle_timeout}s "
                  f"(HDD trop lent ou fichier abîmé ?)")
        log.warning("Extraction %s tuée pour inactivité (%ss)", label, idle_timeout)
        return False, reason

    outputs_ok = (not require_outputs) or all(
        p.exists() and p.stat().st_size > 0 for p in out_paths
    )
    if proc.returncode != 0 or not outputs_ok:
        if cleanup_on_fail:
            for p in out_paths:
                _safe_remove(p)
        tail = stderr_tail[-3:] if stderr_tail else []
        reason = ' | '.join(tail) if tail else f"ffmpeg code {proc.returncode}"
        log.warning("ffmpeg échec %s (rc=%s): %s", label, proc.returncode, reason)
        return False, reason
    return True, None


def _run_ffmpeg_subs(cmd: list, vtt_path: Path, idle_timeout: int, label: str):
    """
    Lance ffmpeg pour produire UN VTT. Renvoie (ok, reason) — le fichier partiel
    est supprimé en cas d'échec.
    """
    # `-progress pipe:1` doit être placé AVANT le fichier de sortie (dernier arg),
    # sinon ffmpeg l'interpréterait comme une seconde sortie.
    full_cmd = cmd[:-1] + ["-progress", "pipe:1", cmd[-1]]
    return _run_ffmpeg_supervised(
        full_cmd, [vtt_path], idle_timeout, label,
        cleanup_on_fail=True, require_outputs=True,
    )


# ─── ENCODAGE DES SOUS-TITRES ────────────────────────────────────────────────
# Beaucoup de .srt (surtout français) sont en Windows-1252 / Latin-1 : lus comme
# de l'UTF-8, les accents deviennent illisibles (« Ã© », « � »). On détecte donc
# l'encodage réel et on normalise tout en UTF-8 avant de produire le VTT.

def decode_subtitle_bytes(raw: bytes) -> str:
    """Décode des octets de sous-titre en texte, en devinant l'encodage."""
    # 1) UTF-8 (avec ou sans BOM) en priorité
    for enc in ("utf-8-sig", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    # 2) Détection automatique (charset_normalizer est fourni avec requests)
    try:
        from charset_normalizer import from_bytes
        best = from_bytes(raw).best()
        if best is not None:
            return str(best)
    except Exception:
        pass
    try:
        import chardet
        enc = (chardet.detect(raw) or {}).get("encoding")
        if enc:
            return raw.decode(enc, errors="replace")
    except Exception:
        pass
    # 3) Replis classiques : Windows-1252 puis Latin-1 (ce dernier ne lève jamais)
    for enc in ("cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", errors="replace")


def _external_sub_to_vtt(src: Path, vtt_path: Path):
    """
    Convertit un sous-titre externe (.srt/.vtt) en VTT UTF-8. Renvoie (ok, reason).
    Le fichier source est d'abord normalisé en UTF-8 pour éviter tout mojibake.
    """
    ext = src.suffix.lower()
    try:
        text = decode_subtitle_bytes(src.read_bytes())
    except Exception as e:
        return False, f"lecture impossible : {e}"

    if ext == ".vtt":
        # Déjà du VTT : on le réécrit simplement en UTF-8 propre.
        try:
            vtt_path.write_text(text, encoding="utf-8")
        except Exception as e:
            _safe_remove(vtt_path)
            return False, f"écriture impossible : {e}"
        if vtt_path.exists() and vtt_path.stat().st_size > 0:
            return True, None
        _safe_remove(vtt_path)
        return False, "fichier VTT vide/illisible"

    # .srt (ou autre format texte) : on écrit une copie UTF-8 puis ffmpeg → webvtt.
    tmp = vtt_path.parent / (vtt_path.stem + ".src.utf8.srt")
    try:
        tmp.write_text(text, encoding="utf-8")
    except Exception as e:
        _safe_remove(tmp)
        return False, f"normalisation impossible : {e}"
    ok, reason = _run_ffmpeg_subs(
        ["ffmpeg", "-y", "-sub_charenc", "UTF-8", "-i", str(tmp),
         "-c:s", "webvtt", str(vtt_path)],
        vtt_path, config.SUBS_TIMEOUT_EXTERNAL,
        f"sous-titres externes {src.name}",
    )
    _safe_remove(tmp)
    return ok, reason


# ─── EXTRACTION DES PISTES D'UN FILM ─────────────────────────────────────────

def extract_tracks(movie: dict, force: bool = False) -> dict:
    """
    Extrait les sous-titres en VTT + liste les pistes audio (métadonnées).
    Cache dans <tracks_cache>/<movie_id>/tracks.json

    force=True : ignore COMPLÈTEMENT tout cache existant, supprime le dossier du
    film et ré-extrait de zéro (utilisé par la reprise « échecs / sans S-T » et
    le « tout ré-extraire »). C'est ce qui garantit qu'on relance vraiment
    ffmpeg au lieu de se contenter de constater qu'un dossier existe déjà.
    """
    movie_id = movie["id"]
    filepath = movie["path"]
    cache_dir = state.TRACKS_CACHE_DIR / movie_id
    metadata_file = cache_dir / "tracks.json"

    if movie_id not in _extraction_locks:
        _extraction_locks[movie_id] = threading.Lock()

    with _extraction_locks[movie_id]:
        if force:
            # Reprise forcée : on repart d'un dossier vierge, sans consulter le
            # moindre cache. Suppression puis ré-extraction intégrale.
            if cache_dir.exists():
                log.info("Reprise : suppression du cache existant → %s", movie['filename'])
                shutil.rmtree(cache_dir, ignore_errors=True)

        # Vérifie les SRT externes modifiés (sauté en mode forcé)
        if not force and metadata_file.exists():
            cache_mtime = metadata_file.stat().st_mtime
            movie_dir = Path(filepath).parent
            movie_stem = Path(filepath).stem

            srt_changed = False
            for ext in ('.srt', '.vtt'):
                for sub_file in movie_dir.glob(f"{movie_stem}*{ext}"):
                    if sub_file.stat().st_mtime > cache_mtime:
                        srt_changed = True
                        log.info("Nouveau sous-titre détecté : %s", sub_file.name)
                        break
                if srt_changed:
                    break

            if not srt_changed:
                try:
                    cached = json.loads(metadata_file.read_text(encoding="utf-8"))
                    # Auto-détection des caches incomplets (extractions interrompues
                    # par un timeout sur HDD). Si des .vtt orphelins traînent ou si
                    # une exécution précédente n'a pas marqué l'extraction complète,
                    # on relance entièrement.
                    cached_tracks = cached.get("subtitle_tracks", [])
                    vtt_files = list(cache_dir.glob("subs_*.vtt"))
                    extraction_ok = cached.get("extraction_complete", False)
                    if extraction_ok and len(vtt_files) == len(cached_tracks):
                        return cached
                    log.info(
                        "Cache incomplet pour %s (%d VTT sur disque, %d pistes en cache, "
                        "complete=%s) - relance",
                        movie['filename'], len(vtt_files), len(cached_tracks), extraction_ok,
                    )
                    # Purge les VTT orphelins avant relance pour repartir propre.
                    for f in vtt_files:
                        _safe_remove(f)
                except Exception:
                    pass

        cache_dir.mkdir(parents=True, exist_ok=True)
        log.info("Extraction des pistes : %s", movie['filename'])

        try:
            result = subprocess.run([
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_streams", "-show_format", str(filepath)
            ], capture_output=True, text=True, timeout=30)
            data = json.loads(result.stdout)
        except Exception as e:
            log.warning("ffprobe échec : %s", e)
            return {"audio_tracks": [], "subtitle_tracks": []}

        streams = data.get("streams", [])
        audio_tracks = []
        subtitle_tracks = []
        failures = []               # raisons d'échec d'extraction
        skipped  = []               # pistes écartées SANS erreur (image, langue)
        subs_to_extract = []        # pistes texte à extraire en une seule passe
        audio_idx = 0
        subs_idx = 0
        extraction_complete = True

        for stream in streams:
            codec_type = stream.get("codec_type")
            tags = stream.get("tags", {})
            lang = (tags.get("language", "und") or "und").lower()
            title = tags.get("title", "")

            if codec_type == "audio":
                audio_tracks.append({
                    "index":    audio_idx,
                    "language": lang,
                    "label":    title or lang_label(lang),
                    "codec":    stream.get("codec_name"),
                    "channels": stream.get("channels"),
                })
                audio_idx += 1

            elif codec_type == "subtitle":
                codec = stream.get("codec_name", "") or ""
                if not config.SUBS_ACCEPT_ALL_LANGS and lang not in config.SUBS_LANG_OK:
                    msg = f"Piste {subs_idx} « {lang_label(lang)} » ignorée (langue non retenue)"
                    log.info(msg)
                    skipped.append(msg)
                elif codec in config.SUBS_TEXT_CODECS:
                    # On ne lance PAS ffmpeg tout de suite : on collecte les pistes
                    # texte pour tout extraire en une seule passe (voir plus bas).
                    subs_to_extract.append({
                        "subs_idx": subs_idx,
                        "lang":     lang,
                        "title":    title,
                        "codec":    codec,
                        "vtt":      cache_dir / f"subs_{subs_idx}.vtt",
                    })
                else:
                    msg = (f"Piste {subs_idx} « {lang_label(lang)} » image "
                           f"({codec or 'inconnu'}) ignorée — non convertible en texte (OCR requis)")
                    log.info(msg)
                    skipped.append(msg)
                subs_idx += 1

        # ── Extraction de TOUTES les pistes texte en UNE SEULE passe ──────────
        # (une seule lecture du fichier, au lieu d'une par piste). Décisif sur
        # les gros remux 4K à nombreuses langues.
        if subs_to_extract:
            cmd = ["ffmpeg", "-y", "-hide_banner", "-progress", "pipe:1",
                   "-i", str(filepath)]
            for t in subs_to_extract:
                cmd += ["-map", f"0:s:{t['subs_idx']}", "-c:s", "webvtt", str(t["vtt"])]
            log.info("Extraction %d sous-titre(s) en une passe : %s",
                     len(subs_to_extract), movie['filename'])
            _ok_all, reason = _run_ffmpeg_supervised(
                cmd, [t["vtt"] for t in subs_to_extract],
                config.SUBS_TIMEOUT_EMBEDDED,
                f"{len(subs_to_extract)} sous-titre(s) {movie['filename']}",
            )
            for t in subs_to_extract:
                if t["vtt"].exists() and t["vtt"].stat().st_size > 0:
                    subtitle_tracks.append({
                        "index":    t["subs_idx"],
                        "language": t["lang"],
                        "label":    t["title"] or lang_label(t["lang"]),
                        "url":      f"/track/subs/{movie_id}/{t['subs_idx']}",
                    })
                else:
                    extraction_complete = False
                    _safe_remove(t["vtt"])
                    failures.append(f"Piste {lang_label(t['lang'])} ({t['codec']}) : {reason or 'sortie vide'}")

        # Sous-titres externes
        movie_dir = Path(filepath).parent
        movie_stem = Path(filepath).stem

        for ext in ('.srt', '.vtt'):
            for sub_file in movie_dir.glob(f"{movie_stem}.*{ext}"):
                parts = sub_file.stem.split('.')
                lang = parts[-1].lower() if len(parts) > 1 else "und"
                if len(lang) > 3 or not lang.isalpha():
                    lang = "und"
                external_idx = 1000 + subs_idx
                vtt_path = cache_dir / f"subs_{external_idx}.vtt"
                try:
                    ok, reason = _external_sub_to_vtt(sub_file, vtt_path)
                except Exception as e:
                    _safe_remove(vtt_path)
                    ok, reason = False, str(e)
                    log.warning("Échec sous-titres externes %s : %s", sub_file.name, e)
                if ok:
                    subtitle_tracks.append({
                        "index":    external_idx,
                        "language": lang,
                        "label":    f"{lang_label(lang)} (externe)",
                        "url":      f"/track/subs/{movie_id}/{external_idx}",
                        "source":   str(sub_file),
                    })
                    log.info("Sous-titres externes : %s (%s)", sub_file.name, lang)
                    subs_idx += 1
                else:
                    extraction_complete = False
                    failures.append(f"Externe {sub_file.name} : {reason}")

            simple_sub = movie_dir / f"{movie_stem}{ext}"
            if simple_sub.exists():
                external_idx = 1000 + subs_idx
                vtt_path = cache_dir / f"subs_{external_idx}.vtt"
                try:
                    ok, reason = _external_sub_to_vtt(simple_sub, vtt_path)
                except Exception as e:
                    _safe_remove(vtt_path)
                    ok, reason = False, str(e)
                    log.warning("Échec sous-titres externes %s : %s", simple_sub.name, e)
                if ok:
                    subtitle_tracks.append({
                        "index":    external_idx,
                        "language": "und",
                        "label":    "Sous-titres (externe)",
                        "url":      f"/track/subs/{movie_id}/{external_idx}",
                        "source":   str(simple_sub),
                    })
                    log.info("Sous-titres externes : %s", simple_sub.name)
                    subs_idx += 1
                else:
                    extraction_complete = False
                    failures.append(f"Externe {simple_sub.name} : {reason}")

        try:
            duration = float(data.get("format", {}).get("duration"))
        except (TypeError, ValueError):
            duration = None

        metadata = {
            "audio_tracks":        audio_tracks,
            "subtitle_tracks":     subtitle_tracks,
            "extraction_complete": extraction_complete,
            "failures":            failures,
            "skipped":             skipped,
            "duration":            duration,
        }
        metadata_file.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        log.info("    %d piste(s) audio, %d sous-titre(s)",
                 len(audio_tracks), len(subtitle_tracks))
        return metadata


# ─── LECTURE DU CACHE ────────────────────────────────────────────────────────

def clear_tracks_cache(movie_id: str):
    cache_dir = state.TRACKS_CACHE_DIR / movie_id
    if cache_dir.exists():
        shutil.rmtree(cache_dir)


def read_cached_meta(movie_id: str) -> dict | None:
    meta_file = state.TRACKS_CACHE_DIR / movie_id / "tracks.json"
    if meta_file.exists():
        try:
            return json.loads(meta_file.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def with_sibling_subs(movie: dict, data: dict) -> dict:
    """
    Ajoute aux sous-titres du film ceux des AUTRES versions (HD/4K) déjà en
    cache, sans les ré-extraire. Dédoublonnage par (langue, libellé).
    """
    siblings = [s for s in (movie.get("sibling_ids") or []) if s != movie["id"]]
    if not siblings:
        return data
    merged = list(data.get("subtitle_tracks", []))
    seen = {(t.get("language"), t.get("label")) for t in merged}
    for sid in siblings:
        meta = read_cached_meta(sid)
        if not meta:
            continue
        for t in meta.get("subtitle_tracks", []):
            key = (t.get("language"), t.get("label"))
            if key not in seen:
                seen.add(key)
                merged.append(t)
    return {**data, "subtitle_tracks": merged}


def with_duration(movie: dict, data: dict) -> dict:
    """Garantit la présence de la durée (la calcule et la persiste si absente)."""
    if data.get("duration"):
        return data
    dur = get_duration(movie["path"])
    if dur is None:
        return data
    data = {**data, "duration": dur}
    # Persiste dans le cache pour ne pas re-sonder à chaque ouverture.
    meta_file = state.TRACKS_CACHE_DIR / movie["id"] / "tracks.json"
    try:
        if meta_file.exists():
            m = json.loads(meta_file.read_text(encoding="utf-8"))
            m["duration"] = dur
            meta_file.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return data


def cached_subs_state(movie_id: str) -> str:
    """
    État en cache d'un fichier, sans rien ré-extraire :
      'has_subs'  → au moins un sous-titre exploitable déjà en cache ;
      'no_subs'   → extraction complète mais aucun sous-titre trouvé ;
      'failed'    → une extraction précédente a échoué (incomplète) ;
      'none'      → jamais scanné.
    """
    meta_file = state.TRACKS_CACHE_DIR / movie_id / "tracks.json"
    if not meta_file.exists():
        return "none"
    try:
        data = json.loads(meta_file.read_text(encoding="utf-8"))
    except Exception:
        return "none"
    if not data.get("extraction_complete", False):
        return "failed"
    if len(data.get("subtitle_tracks", [])) == 0:
        return "no_subs"
    return "has_subs"


def cached_sub_langs(movie_id: str) -> set:
    """Ensemble des langues de sous-titres déjà en cache pour un film (normalisées)."""
    meta_file = state.TRACKS_CACHE_DIR / movie_id / "tracks.json"
    langs = set()
    if meta_file.exists():
        try:
            data = json.loads(meta_file.read_text(encoding="utf-8"))
            for t in data.get("subtitle_tracks", []):
                langs.add(norm_lang(t.get("language")))
        except Exception:
            pass
    return langs


def group_sub_langs(movie: dict) -> set:
    """Langues de sous-titres présentes sur TOUTE version du film (HD + 4K…)."""
    langs = set()
    for sid in (movie.get("sibling_ids") or [movie["id"]]):
        langs |= cached_sub_langs(sid)
    return langs


def collect_subs_for_ids(ids: list) -> list:
    """Sous-titres (depuis le cache) pour un ensemble d'ids de fichiers, dédoublonnés."""
    out, seen = [], set()
    for mid in ids:
        meta = read_cached_meta(mid)
        if not meta:
            continue
        for t in meta.get("subtitle_tracks", []):
            key = (t.get("language"), t.get("label"))
            if key in seen:
                continue
            vtt = state.TRACKS_CACHE_DIR / mid / f"subs_{t.get('index')}.vtt"
            if not vtt.exists():
                continue
            seen.add(key)
            out.append({
                "movie_id": mid,
                "idx":      t.get("index"),
                "label":    t.get("label") or lang_label(t.get("language")),
                "language": t.get("language"),
            })
    return out


# ─── RESYNCHRONISATION (décalage des timecodes) ──────────────────────────────

# Timecode SRT/VTT. Les heures sont OPTIONNELLES : ffmpeg écrit souvent la
# forme courte WebVTT « MM:SS.mmm » pour les temps < 1 h (ex. 00:32.448).
# On accepte donc HH:MM:SS[.,]mmm ET MM:SS[.,]mmm.
_TS_RE = re.compile(r'(?:(\d+):)?([0-5]?\d):([0-5]\d)([.,])(\d{3})')


def _shift_ts(m, offset_sec: float) -> str:
    h  = int(m.group(1)) if m.group(1) else 0
    total = (h * 3600000 + int(m.group(2)) * 60000
             + int(m.group(3)) * 1000 + int(m.group(5))
             + int(round(offset_sec * 1000)))
    if total < 0:
        total = 0
    h, rem = divmod(total, 3600000)
    mm, rem = divmod(rem, 60000)
    ss, ms = divmod(rem, 1000)
    return f"{h:02d}:{mm:02d}:{ss:02d}{m.group(4)}{ms:03d}"


def shift_subtitle_file(path: Path, offset_sec: float) -> int:
    """
    Décale TOUS les timecodes d'un fichier VTT/SRT de `offset_sec` (± décimal).
    Renvoie le nombre de timecodes décalés, ou -1 en cas d'erreur.
    """
    try:
        text = decode_subtitle_bytes(path.read_bytes())
        shifted, n = _TS_RE.subn(lambda m: _shift_ts(m, offset_sec), text)
        if n > 0:
            path.write_text(shifted, encoding="utf-8")
        return n
    except Exception as e:
        log.warning("Décalage sous-titre échoué (%s) : %s", path.name, e)
        return -1


def find_external_source(video_path: str, language: str):
    """Retrouve le fichier de sous-titre externe (.srt/.vtt) à côté de la vidéo."""
    p = Path(video_path)
    stem, d = p.stem, p.parent
    cands = []
    if language and language != "und":
        cands += [d / f"{stem}.{language}.srt", d / f"{stem}.{language}.vtt"]
    cands += [d / f"{stem}.srt", d / f"{stem}.vtt"]
    for c in cands:
        if c.exists():
            return c
    return None
