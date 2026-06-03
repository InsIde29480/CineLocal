<#
.SYNOPSIS
    Scanne RECURSIVEMENT le dossier courant et tous ses sous-dossiers, et verifie
    pour chaque fichier video s'il est au bon format cible :
        - Video : H.264
        - Audio : AAC, 2 canaux (stereo) -- toutes les pistes audio
        - Resolution : <= 1080p

    Code couleur :
        VERT   -> tout est bon (H.264 + AAC stereo + <= 1080p)
        ORANGE -> video et audio OK mais resolution > 1080p
        ROUGE  -> mauvais codec video ou audio (ou fichier illisible)

.PREREQUIS
    ffprobe accessible dans le PATH.
#>

# ====================================================================
# CONFIGURATION
# ====================================================================
$VideoExtensions  = @('.mkv', '.mp4', '.avi', '.mov', '.m4v', '.wmv', '.flv', '.webm', '.ts', '.m2ts')
$VideoOK          = @('h264')
$AudioOK          = @('aac')
$RequiredChannels = 2
$MaxHeight        = 1080


# ====================================================================
# VERIFICATION DE FFPROBE
# ====================================================================
try {
    $null = & ffprobe -version 2>&1
} catch {
    Write-Host "[ERREUR] ffprobe introuvable dans le PATH." -ForegroundColor Red
    exit 1
}


# ====================================================================
# ANALYSE D'UN FICHIER
# ====================================================================
function Get-MediaInfo {
    param([string]$Path)

    $json = & ffprobe -v quiet -print_format json -show_streams "$Path" 2>$null | ConvertFrom-Json

    $video         = $null
    $height        = 0
    $audioCodecs   = @()
    $audioChannels = @()

    if ($json) {
        foreach ($stream in $json.streams) {
            if ($stream.codec_type -eq 'video' -and -not $video) {
                $video = $stream.codec_name
                if ($stream.height) { $height = [int]$stream.height }
            }
            if ($stream.codec_type -eq 'audio') {
                $audioCodecs   += $stream.codec_name
                $audioChannels += [int]$stream.channels
            }
        }
    }

    return [PSCustomObject]@{
        Video         = $video
        Height        = $height
        AudioCodecs   = $audioCodecs
        AudioChannels = $audioChannels
    }
}


# ====================================================================
# SCAN RECURSIF
# ====================================================================
$root = Get-Location
Write-Host ""
Write-Host "Scan recursif de : $root" -ForegroundColor Cyan
Write-Host ("-" * 78)

$files = Get-ChildItem -Recurse -File |
         Where-Object { $VideoExtensions -contains $_.Extension.ToLower() } |
         Sort-Object FullName

if ($files.Count -eq 0) {
    Write-Host "Aucun fichier video trouve." -ForegroundColor Yellow
    exit 0
}

$nbGreen  = 0
$nbOrange = 0
$nbRed    = 0

foreach ($file in $files) {
    $info = Get-MediaInfo -Path $file.FullName

    # Chemin relatif pour un affichage lisible
    $rel = $file.FullName.Substring($root.Path.Length).TrimStart('\', '/')

    # --- Evaluation ---
    $videoOk = $VideoOK -contains $info.Video

    if ($info.AudioCodecs.Count -eq 0) {
        $audioOk = $false
    } else {
        $audioOk = $true
        for ($i = 0; $i -lt $info.AudioCodecs.Count; $i++) {
            if (($AudioOK -notcontains $info.AudioCodecs[$i]) -or
                ($info.AudioChannels[$i] -ne $RequiredChannels)) {
                $audioOk = $false
            }
        }
    }

    $resTooHigh = ($info.Height -gt $MaxHeight)

    # --- Detail audio pour l'affichage (premiere piste) ---
    if ($info.AudioCodecs.Count -gt 0) {
        $aDesc = "$($info.AudioCodecs[0]) $($info.AudioChannels[0])ch"
        if ($info.AudioCodecs.Count -gt 1) { $aDesc += " (+$($info.AudioCodecs.Count - 1))" }
    } else {
        $aDesc = "sans audio"
    }

    $vDesc = if ($info.Video) { $info.Video } else { "illisible" }
    $rDesc = if ($info.Height -gt 0) { "$($info.Height)p" } else { "?" }
    $detail = "[{0} / {1} / {2}]" -f $vDesc, $aDesc, $rDesc

    # --- Classification + couleur ---
    if (-not $videoOk -or -not $audioOk) {
        $color = "Red"
        $tag   = "MAUVAIS"
        $nbRed++
    } elseif ($resTooHigh) {
        $color = "Yellow"
        $tag   = "RES >1080"
        $nbOrange++
    } else {
        $color = "Green"
        $tag   = "OK"
        $nbGreen++
    }

    Write-Host ("[{0,-9}] {1}" -f $tag, $rel) -ForegroundColor $color
    Write-Host ("            {0}" -f $detail) -ForegroundColor DarkGray
}


# ====================================================================
# RESUME
# ====================================================================
Write-Host ("-" * 78)
Write-Host ""
Write-Host "Resume :" -ForegroundColor Cyan
Write-Host ("  Bon format (vert)        : {0}" -f $nbGreen)  -ForegroundColor Green
Write-Host ("  Trop haute res. (orange) : {0}" -f $nbOrange) -ForegroundColor Yellow
Write-Host ("  Mauvais format (rouge)   : {0}" -f $nbRed)    -ForegroundColor Red
Write-Host ("  Total                    : {0}" -f $files.Count)
Write-Host ""
