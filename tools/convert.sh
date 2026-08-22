#!/usr/bin/env bash
# ==============================================================================
# SYNOPSIS
#   Scanne le dossier courant et détecte les fichiers à convertir :
#     - vidéo non H.264, ou audio non AAC/MP3
#     - OU vidéo en plus de 1080p (4K, 1440p...) -> downscale en 1080p
#       (même si déjà en H.264 + AAC : un H.264 4K est injouable sur le Pi)
#   Les fichiers "_out" sont aussi inspectés : s'ils dépassent 1080p, ils sont
#   proposés et remplacés en place (downscale du _out existant).
#   Permet d'en sélectionner un ou plusieurs et les convertit.
#   Sortie : même nom + "_out.mkv" (toutes pistes audio et sous-titres conservées).
#
# PRÉREQUIS
#   ffmpeg et ffprobe accessibles dans le PATH.
# ==============================================================================

set -euo pipefail

# ==============================================================================
# CONFIGURATION
# ==============================================================================
VIDEO_EXTENSIONS=( mkv mp4 avi mov m4v wmv flv webm ts m2ts )
VIDEO_OK=( h264 )
AUDIO_OK=( aac mp3 )

# Encodeur : "cpu" (libx264), "nvidia" (h264_nvenc), "intel" (h264_qsv), "amd" (h264_amf)
ENCODER="nvidia"
CRF=19

# Hauteur maximale tolérée sans downscale. Au-delà -> réduit à 1080p.
MAX_HEIGHT=1080
# Largeur maximale de la boite de sortie.
MAX_WIDTH=1920

# Couleurs ANSI
RED='\033[0;31m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
WHITE='\033[1;37m'
GRAY='\033[0;37m'
DARK_GRAY='\033[2;37m'
GREEN='\033[0;32m'
NC='\033[0m' # No Color


# ==============================================================================
# VÉRIFICATION DE FFMPEG
# ==============================================================================
check_ffmpeg() {
    if ! command -v ffmpeg &>/dev/null || ! command -v ffprobe &>/dev/null; then
        echo -e "${RED}[ERREUR] ffmpeg/ffprobe introuvable dans le PATH.${NC}"
        echo -e "${YELLOW}         Installe-le via ton gestionnaire de paquets (ex: apt install ffmpeg).${NC}"
        exit 1
    fi
}


# ==============================================================================
# ANALYSE DES CODECS + RÉSOLUTION
# ==============================================================================
# Remplit les variables globales : CODEC_VIDEO, CODEC_AUDIO, RES_WIDTH, RES_HEIGHT
get_codecs() {
    local path="$1"
    local json
    json=$(ffprobe -v quiet -print_format json -show_streams "$path" 2>/dev/null)

    CODEC_VIDEO=$(echo "$json" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for s in data.get('streams', []):
    if s.get('codec_type') == 'video':
        print(s.get('codec_name', ''))
        break
" 2>/dev/null || echo "")

    CODEC_AUDIO=$(echo "$json" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for s in data.get('streams', []):
    if s.get('codec_type') == 'audio':
        print(s.get('codec_name', ''))
        break
" 2>/dev/null || echo "")

    RES_WIDTH=$(echo "$json" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for s in data.get('streams', []):
    if s.get('codec_type') == 'video':
        print(s.get('width', 0))
        break
" 2>/dev/null || echo "0")

    RES_HEIGHT=$(echo "$json" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for s in data.get('streams', []):
    if s.get('codec_type') == 'video':
        print(s.get('height', 0))
        break
" 2>/dev/null || echo "0")

    RES_WIDTH=${RES_WIDTH:-0}
    RES_HEIGHT=${RES_HEIGHT:-0}
}


# ==============================================================================
# RÉCUPÉRATION DE LA DURÉE
# ==============================================================================
get_duration() {
    local path="$1"
    local d
    d=$(ffprobe -v error \
        -show_entries format=duration \
        -of default=noprint_wrappers=1:nokey=1 \
        "$path" 2>/dev/null || echo "0")
    echo "${d:-0}"
}


# ==============================================================================
# VÉRIFICATION D'APPARTENANCE À UNE LISTE
# ==============================================================================
in_list() {
    local value="$1"
    shift
    local item
    for item in "$@"; do
        [[ "$item" == "$value" ]] && return 0
    done
    return 1
}


# ==============================================================================
# ARGUMENTS VIDÉO SELON L'ENCODEUR
# ==============================================================================
# Affiche les arguments ffmpeg pour la vidéo sur stdout (un arg par ligne)
get_video_args() {
    local needs_conversion="$1"   # "true" / "false"
    local downscale="$2"          # "true" / "false"

    if [[ "$needs_conversion" == "false" && "$downscale" == "false" ]]; then
        echo "-c:v"
        echo "copy"
        return
    fi

    local scale_arg="scale=w=${MAX_WIDTH}:h=${MAX_HEIGHT}:force_original_aspect_ratio=decrease:force_divisible_by=2"

    case "$ENCODER" in
        nvidia)
            echo "-c:v";    echo "h264_nvenc"
            echo "-preset"; echo "p7"
            echo "-cq";     echo "$CRF"
            ;;
        intel)
            echo "-c:v";           echo "h264_qsv"
            echo "-global_quality"; echo "$CRF"
            ;;
        amd)
            echo "-c:v";    echo "h264_amf"
            echo "-quality"; echo "balanced"
            echo "-qp_i";   echo "$CRF"
            echo "-qp_p";   echo "$CRF"
            ;;
        *)  # cpu / libx264
            echo "-c:v";    echo "libx264"
            echo "-preset"; echo "slow"
            echo "-crf";    echo "$CRF"
            ;;
    esac

    echo "-vf";       echo "$scale_arg"
    echo "-pix_fmt";  echo "yuv420p"
    echo "-profile:v"; echo "high"
    echo "-level";    echo "4.1"
}


# ==============================================================================
# AFFICHAGE DE LA PROGRESSION
# ==============================================================================
show_progress() {
    local current_sec="$1"
    local total_sec="$2"
    local start_epoch="$3"
    local filename="$4"

    if (( $(echo "$total_sec > 0" | bc -l) )); then
        local percent
        percent=$(echo "scale=0; $current_sec * 100 / $total_sec" | bc)
        percent=$(( percent > 100 ? 100 : percent ))

        local now elapsed remaining eta_str=""
        now=$(date +%s%N | cut -b1-13)  # ms
        elapsed=$(echo "scale=1; ($now - $start_epoch) / 1000" | bc)

        if (( $(echo "$current_sec > 0" | bc -l) )); then
            remaining=$(echo "scale=1; $elapsed * ($total_sec / $current_sec) - $elapsed" | bc)
            if (( $(echo "$remaining > 0" | bc -l) )); then
                local rm_min rm_sec
                rm_min=$(echo "scale=0; $remaining / 60" | bc)
                rm_sec=$(printf "%.0f" "$(echo "$remaining - $rm_min * 60" | bc)")
                eta_str=$(printf " - ETA %02dm%02ds" "$rm_min" "$rm_sec")
            fi
        fi

        local bar_width=40
        local filled=$(( percent * bar_width / 100 ))
        local bar
        bar=$(printf '%0.s#' $(seq 1 $filled))
        bar+=$(printf '%0.s-' $(seq 1 $(( bar_width - filled ))))

        printf "\r  [%-40s] %3d%% (%ds/%ds)%s   " \
            "$bar" "$percent" "${current_sec%.*}" "${total_sec%.*}" "$eta_str"
    fi
}


# ==============================================================================
# SCAN DU DOSSIER COURANT
# ==============================================================================
check_ffmpeg

echo ""
echo -e "${CYAN}Scan du dossier : $(pwd)${NC}"
printf '%0.s-' {1..70}; echo ""

# Construire le pattern de find pour les extensions vidéo
find_args=()
first=true
for ext in "${VIDEO_EXTENSIONS[@]}"; do
    if $first; then
        find_args+=( -iname "*.${ext}" )
        first=false
    else
        find_args+=( -o -iname "*.${ext}" )
    fi
done

mapfile -t files < <(find . -maxdepth 1 -type f \( "${find_args[@]}" \) | sort)

if [[ ${#files[@]} -eq 0 ]]; then
    echo -e "${YELLOW}Aucun fichier vidéo trouvé dans ce dossier.${NC}"
    exit 0
fi

# Tableaux parallèles pour les fichiers à convertir
declare -a TC_NUM TC_PATH TC_BASENAME TC_VIDEO TC_AUDIO TC_VIDEO_OK TC_AUDIO_OK TC_DOWNSCALE TC_HEIGHT TC_IS_OUT
index=0

for filepath in "${files[@]}"; do
    filepath="${filepath#./}"  # Retire le ./ initial
    basename="${filepath%.*}"
    basename="${basename##*/}"

    # Ignore les fichiers temporaires
    [[ "$basename" == *.tmp ]] && continue

    is_out=false
    [[ "$basename" == *_out ]] && is_out=true

    get_codecs "$filepath"
    video_codec="$CODEC_VIDEO"
    audio_codec="$CODEC_AUDIO"
    height="$RES_HEIGHT"

    video_ok=false
    audio_ok=false
    in_list "$video_codec" "${VIDEO_OK[@]}" && video_ok=true
    in_list "$audio_codec" "${AUDIO_OK[@]}" && audio_ok=true

    needs_downscale=false
    (( height > MAX_HEIGHT )) && needs_downscale=true

    needs_processing=false
    if $is_out; then
        $needs_downscale && needs_processing=true
    else
        { ! $video_ok || ! $audio_ok || $needs_downscale; } && needs_processing=true
    fi

    if $needs_processing; then
        index=$(( index + 1 ))

        v_status=$( $video_ok && echo "OK" || echo "NON" )
        a_status=$( $audio_ok && echo "OK" || echo "NON" )
        if $needs_downscale; then
            res_status="${height}p -> ${MAX_HEIGHT}p"
        else
            res_status="${height}p"
        fi
        tag=""
        $is_out && tag=" (_out, sera remplacé)"

        TC_NUM+=("$index")
        TC_PATH+=("$filepath")
        TC_BASENAME+=("$basename")
        TC_VIDEO+=("$video_codec")
        TC_AUDIO+=("$audio_codec")
        TC_VIDEO_OK+=("$video_ok")
        TC_AUDIO_OK+=("$audio_ok")
        TC_DOWNSCALE+=("$needs_downscale")
        TC_HEIGHT+=("$height")
        TC_IS_OUT+=("$is_out")

        printf "${WHITE}[%2d] %s%s${NC}\n" "$index" "$filepath" "$tag"
        printf "${GRAY}     Video : %-6s [%s]   Audio : %-6s [%s]   Res : %s${NC}\n" \
            "$video_codec" "$v_status" "$audio_codec" "$a_status" "$res_status"
    fi
done

if [[ ${#TC_NUM[@]} -eq 0 ]]; then
    echo ""
    echo -e "${GREEN}Tous les fichiers sont déjà au bon format (H.264 + AAC, <= ${MAX_HEIGHT}p).${NC}"
    exit 0
fi

printf '%0.s-' {1..70}; echo ""
echo ""


# ==============================================================================
# SÉLECTION INTERACTIVE
# ==============================================================================
echo -e "${CYAN}Quels fichiers convertir ?${NC}"
echo "  - Numéros séparés par des virgules (ex: 1,3,5)"
echo "  - Plage (ex: 1-4)"
echo "  - 'all' pour tout convertir"
echo "  - 'q' pour quitter"
echo ""
read -rp "Sélection : " selection

if [[ "$selection" == "q" || -z "$selection" ]]; then
    echo -e "${YELLOW}Annulé.${NC}"
    exit 0
fi

declare -a selected_nums=()

if [[ "$selection" == "all" ]]; then
    selected_nums=( "${TC_NUM[@]}" )
else
    IFS=',' read -ra parts <<< "$selection"
    for part in "${parts[@]}"; do
        part="${part// /}"  # trim espaces
        if [[ "$part" =~ ^([0-9]+)-([0-9]+)$ ]]; then
            start="${BASH_REMATCH[1]}"
            end="${BASH_REMATCH[2]}"
            for (( n=start; n<=end; n++ )); do
                selected_nums+=("$n")
            done
        elif [[ "$part" =~ ^[0-9]+$ ]]; then
            selected_nums+=("$part")
        fi
    done
fi

# Filtrer les indices valides
declare -a valid_indices=()
for n in "${selected_nums[@]}"; do
    for i in "${!TC_NUM[@]}"; do
        if [[ "${TC_NUM[$i]}" == "$n" ]]; then
            valid_indices+=("$i")
            break
        fi
    done
done

if [[ ${#valid_indices[@]} -eq 0 ]]; then
    echo -e "${YELLOW}Aucun fichier valide sélectionné.${NC}"
    exit 0
fi


# ==============================================================================
# CONVERSION
# ==============================================================================
echo ""
printf "${CYAN}%0.s=${NC}" {1..70}; echo ""
echo -e "${CYAN}Conversion de ${#valid_indices[@]} fichier(s)${NC}"
printf "${CYAN}%0.s=${NC}" {1..70}; echo ""

count=0
total=${#valid_indices[@]}

for i in "${valid_indices[@]}"; do
    count=$(( count + 1 ))
    filepath="${TC_PATH[$i]}"
    basename="${TC_BASENAME[$i]}"
    is_out="${TC_IS_OUT[$i]}"
    video_ok="${TC_VIDEO_OK[$i]}"
    audio_ok="${TC_AUDIO_OK[$i]}"
    downscale="${TC_DOWNSCALE[$i]}"
    height="${TC_HEIGHT[$i]}"
    dir=$(dirname "$filepath")

    if [[ "$is_out" == "true" ]]; then
        output="${dir}/${basename}.tmp.mkv"
        final_path="$filepath"
    else
        output="${dir}/${basename}_out.mkv"
        final_path="$output"
    fi

    echo ""
    echo -e "${WHITE}[$count/$total] $filepath${NC}"
    if [[ "$is_out" == "true" ]]; then
        echo -e "${GRAY}  -> remplacé en place (downscale)${NC}"
    else
        echo -e "${GRAY}  -> ${basename}_out.mkv${NC}"
    fi

    # Construction des args vidéo
    needs_conv=false
    [[ "$video_ok" == "false" ]] && needs_conv=true
    mapfile -t v_args < <(get_video_args "$needs_conv" "$downscale")

    # Args audio : AAC-LC stéréo (compatible Chromecast)
    a_args=( -c:a aac -profile:a aac_low -b:a 192k -ac 2 )

    # Message vidéo
    if [[ "$downscale" == "true" && "$video_ok" == "true" ]]; then
        v_msg="H.264 ${height}p -> ${MAX_HEIGHT}p ($ENCODER)"
    elif [[ "$downscale" == "true" ]]; then
        v_msg="-> H.264 ${MAX_HEIGHT}p ($ENCODER)"
    elif [[ "$video_ok" == "false" ]]; then
        v_msg="-> H.264 ($ENCODER)"
    else
        v_msg="copy"
    fi
    echo -e "${DARK_GRAY}  Vidéo : $v_msg   Audio : -> AAC-LC stéréo${NC}"

    total_sec=$(get_duration "$filepath")
    start_epoch=$(date +%s%N | cut -b1-13)  # millisecondes
    log_file=$(mktemp /tmp/ffmpeg_XXXXXX.log)

    # Lancement de ffmpeg avec lecture de la progression via -progress pipe:1
    ffmpeg \
        -i "$filepath" \
        -map 0:v:0 \
        -map 0:a \
        -map "0:s?" \
        "${v_args[@]}" \
        "${a_args[@]}" \
        -c:s copy \
        -progress pipe:1 \
        -nostats \
        -y \
        "$output" \
        2>"$log_file" | while IFS='=' read -r key value; do
            if [[ "$key" == "out_time" ]]; then
                # Format HH:MM:SS.microseconds
                if [[ "$value" =~ ^([0-9]+):([0-9]+):([0-9.]+)$ ]]; then
                    h="${BASH_REMATCH[1]}"
                    m="${BASH_REMATCH[2]}"
                    s="${BASH_REMATCH[3]}"
                    current_sec=$(echo "$h * 3600 + $m * 60 + $s" | bc)
                    show_progress "$current_sec" "$total_sec" "$start_epoch" "$filepath"
                fi
            fi
        done

    # Récupérer le code de retour de ffmpeg (pas du pipe)
    exit_code=${PIPESTATUS[0]}
    printf "\r%80s\r" ""  # Effacer la ligne de progression

    end_epoch=$(date +%s)
    start_s=$(( start_epoch / 1000 ))
    duration_s=$(( end_epoch - start_s ))
    dur_min=$(( duration_s / 60 ))
    dur_sec=$(( duration_s % 60 ))

    out_size=0
    [[ -f "$output" ]] && out_size=$(stat -c%s "$output" 2>/dev/null || echo 0)

    if [[ "$exit_code" -eq 0 && "$out_size" -gt 0 ]]; then
        if [[ "$is_out" == "true" ]]; then
            if rm -f "$final_path" && mv "$output" "$final_path"; then
                :
            else
                echo -e "${YELLOW}  [ATTENTION] Remplacement impossible, fichier laissé en .tmp.mkv${NC}"
                final_path="$output"
            fi
        fi
        size_mb=$(( $(stat -c%s "$final_path" 2>/dev/null || echo 0) / 1048576 ))
        printf "${GREEN}  [OK] Terminé en %02dm%02ds - %d Mo${NC}\n" "$dur_min" "$dur_sec" "$size_mb"
    else
        echo -e "${RED}  [ÉCHEC] Conversion échouée (code ffmpeg : $exit_code)${NC}"
        if [[ -f "$log_file" ]]; then
            echo -e "${YELLOW}  --- Derniers messages ffmpeg ---${NC}"
            tail -15 "$log_file" | while IFS= read -r line; do
                echo -e "${DARK_GRAY}    $line${NC}"
            done
        fi
        # Supprimer le fichier vide laissé par ffmpeg
        if [[ -f "$output" && "$out_size" -eq 0 ]]; then
            rm -f "$output"
        fi
    fi

    [[ -f "$log_file" ]] && rm -f "$log_file"
done

echo ""
printf "${CYAN}%0.s=${NC}" {1..70}; echo ""
echo -e "${GREEN}Conversion terminée.${NC}"
printf "${CYAN}%0.s=${NC}" {1..70}; echo ""
