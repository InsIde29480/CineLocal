#!/usr/bin/env bash
#
# Scanne RECURSIVEMENT le dossier courant et tous ses sous-dossiers, et verifie
# pour chaque fichier video s'il est au bon format cible :
#     - Video : H.264
#     - Audio : AAC, 2 canaux (stereo) -- toutes les pistes audio
#     - Resolution : <= 1080p
#
# Code couleur :
#     VERT   -> tout est bon (H.264 + AAC stereo + <= 1080p)
#     ORANGE -> video et audio OK mais resolution > 1080p
#     ROUGE  -> mauvais codec video ou audio (ou fichier illisible)
#
# Prerequis : ffprobe accessible dans le PATH.
#
# Usage : ./check.sh [dossier]   (defaut : dossier courant)

set -u

# ====================================================================
# CONFIGURATION
# ====================================================================
VIDEO_EXTENSIONS=(mkv mp4 avi mov m4v wmv flv webm ts m2ts)
VIDEO_OK=(h264)
AUDIO_OK=(aac)
REQUIRED_CHANNELS=2
MAX_HEIGHT=1080

# Couleurs ANSI
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
GRAY='\033[0;90m'
CYAN='\033[0;36m'
NC='\033[0m'   # reset

# ====================================================================
# VERIFICATION DE FFPROBE
# ====================================================================
if ! command -v ffprobe >/dev/null 2>&1; then
    printf "%b[ERREUR] ffprobe introuvable dans le PATH.%b\n" "$RED" "$NC"
    exit 1
fi

# ====================================================================
# OUTILS
# ====================================================================
in_list() {
    # in_list <item> <element...>  -> 0 si trouve
    local item="$1"; shift
    local x
    for x in "$@"; do [ "$x" = "$item" ] && return 0; done
    return 1
}

ROOT="${1:-.}"
cd "$ROOT" || { printf "%bDossier introuvable : %s%b\n" "$RED" "$ROOT" "$NC"; exit 1; }

printf "\n%bScan recursif de : %s%b\n" "$CYAN" "$(pwd)" "$NC"
printf '%.0s-' {1..78}; printf '\n'

# Construit l'expression -iname pour find a partir de la liste d'extensions
find_args=()
for ext in "${VIDEO_EXTENSIONS[@]}"; do
    find_args+=(-iname "*.${ext}" -o)
done
unset 'find_args[${#find_args[@]}-1]'   # retire le dernier -o

nb_green=0
nb_orange=0
nb_red=0
total=0

# Boucle dans le shell principal (process substitution) pour garder les compteurs
while IFS= read -r -d '' f; do
    total=$((total + 1))
    rel="${f#./}"

    # --- Video : codec + hauteur ---
    video_info="$(ffprobe -v error -select_streams v:0 \
        -show_entries stream=codec_name,height -of csv=p=0 "$f" 2>/dev/null)"
    vcodec="${video_info%%,*}"
    vheight="${video_info##*,}"
    [[ "$vheight" =~ ^[0-9]+$ ]] || vheight=0

    # --- Audio : toutes les pistes (codec + canaux) ---
    audio_info="$(ffprobe -v error -select_streams a \
        -show_entries stream=codec_name,channels -of csv=p=0 "$f" 2>/dev/null)"

    audio_ok=true
    audio_count=0
    first_audio_desc=""
    while IFS=',' read -r acodec achan; do
        [ -z "$acodec" ] && continue
        audio_count=$((audio_count + 1))
        if [ "$audio_count" -eq 1 ]; then
            first_audio_desc="${acodec} ${achan}ch"
        fi
        if ! in_list "$acodec" "${AUDIO_OK[@]}" || [ "$achan" != "$REQUIRED_CHANNELS" ]; then
            audio_ok=false
        fi
    done <<< "$audio_info"

    if [ "$audio_count" -eq 0 ]; then
        audio_ok=false
        first_audio_desc="sans audio"
    elif [ "$audio_count" -gt 1 ]; then
        first_audio_desc="${first_audio_desc} (+$((audio_count - 1)))"
    fi

    # --- Video OK ? ---
    video_ok=false
    if [ -n "$vcodec" ] && in_list "$vcodec" "${VIDEO_OK[@]}"; then
        video_ok=true
    fi

    # --- Detail pour l'affichage ---
    vdesc="${vcodec:-illisible}"
    if [ "$vheight" -gt 0 ]; then rdesc="${vheight}p"; else rdesc="?"; fi
    detail="[${vdesc} / ${first_audio_desc} / ${rdesc}]"

    # --- Classification + couleur ---
    if [ "$video_ok" != true ] || [ "$audio_ok" != true ]; then
        color="$RED";    tag="MAUVAIS";   nb_red=$((nb_red + 1))
    elif [ "$vheight" -gt "$MAX_HEIGHT" ]; then
        color="$YELLOW"; tag="RES >1080"; nb_orange=$((nb_orange + 1))
    else
        color="$GREEN";  tag="OK";        nb_green=$((nb_green + 1))
    fi

    printf "%b[%-9s]%b %s\n" "$color" "$tag" "$NC" "$rel"
    printf "%b            %s%b\n" "$GRAY" "$detail" "$NC"

done < <(find . -type f \( "${find_args[@]}" \) -print0 | sort -z)

# ====================================================================
# RESUME
# ====================================================================
printf '%.0s-' {1..78}; printf '\n\n'

if [ "$total" -eq 0 ]; then
    printf "%bAucun fichier video trouve.%b\n" "$YELLOW" "$NC"
    exit 0
fi

printf "%bResume :%b\n" "$CYAN" "$NC"
printf "  %bBon format (vert)        : %d%b\n" "$GREEN"  "$nb_green"  "$NC"
printf "  %bTrop haute res. (orange) : %d%b\n" "$YELLOW" "$nb_orange" "$NC"
printf "  %bMauvais format (rouge)   : %d%b\n" "$RED"    "$nb_red"    "$NC"
printf "  Total                    : %d\n\n" "$total"
