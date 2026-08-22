"""Analyse des fichiers vidéo via ffprobe (codecs, durée)."""

import json
import logging
import re
import subprocess

log = logging.getLogger(__name__)

# Cache mémoire des codecs déjà sondés (clé = chemin du fichier).
_codec_cache = {}


def probe_codecs(filepath: str) -> dict:
    key = str(filepath)
    if key in _codec_cache:
        return _codec_cache[key]
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", str(filepath)],
            capture_output=True, text=True, timeout=10
        )
        data = json.loads(result.stdout)
        vstream = next((s for s in data.get("streams", [])
                        if s.get("codec_type") == "video"), {})
        vcodec = vstream.get("codec_name")
        vpix   = vstream.get("pix_fmt", "") or ""
        acodec = next((s["codec_name"] for s in data.get("streams", [])
                       if s.get("codec_type") == "audio"), None)
        # 10/12-bit (ex. yuv420p10le) : le Chromecast ne sait pas le décoder
        # et produit des artefacts colorés (teintes mauves).
        high_bit = bool(re.search(r'(10|12|16)(le|be)', vpix))
        codecs = {"video": vcodec, "audio": acodec,
                  "pix_fmt": vpix, "high_bit": high_bit}
    except Exception as e:
        log.warning("ffprobe échec : %s", e)
        codecs = {"video": None, "audio": None, "pix_fmt": "", "high_bit": False}
    _codec_cache[key] = codecs
    return codecs


def get_duration(filepath: str):
    """Durée du film en secondes (via ffprobe), ou None."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", str(filepath)],
            capture_output=True, text=True, timeout=15,
        )
        return float(json.loads(r.stdout).get("format", {}).get("duration"))
    except Exception:
        return None
