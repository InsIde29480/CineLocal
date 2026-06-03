<#
.SYNOPSIS
    Scanne le dossier courant et detecte les fichiers a convertir :
      - video non H.264, ou audio non AAC/MP3
      - OU video en plus de 1080p (4K, 1440p...) -> downscale en 1080p
        (meme si deja en H.264 + AAC : un H.264 4K est injouable sur le Pi)
    Les fichiers "_out" sont aussi inspectes : s'ils depassent 1080p, ils sont
    proposes et remplaces en place (downscale du _out existant).
    Permet d'en selectionner un ou plusieurs et les convertit.
    Sortie : meme nom + "_out.mkv" (toutes pistes audio et sous-titres conservees).

.PREREQUIS
    ffmpeg et ffprobe accessibles dans le PATH (teste : ffmpeg -version).
#>

# ====================================================================
# CONFIGURATION
# ====================================================================
$VideoExtensions = @('.mkv', '.mp4', '.avi', '.mov', '.m4v', '.wmv', '.flv', '.webm', '.ts', '.m2ts')
$VideoOK = @('h264')
$AudioOK = @('aac', 'mp3')

# Encodeur : "cpu" (libx264), "nvidia" (h264_nvenc), "intel" (h264_qsv), "amd" (h264_amf)
$Encoder = "nvidia"
$Crf     = 19

# Hauteur maximale toleree sans downscale. Au-dela -> reduit a 1080p.
$MaxHeight = 1080
# Largeur maximale de la boite de sortie. Indispensable pour les films
# ultra-larges (2.39:1, cinerama...) ou borner la hauteur seule produit une
# image trop large qui depasse le Level 4.1.
$MaxWidth  = 1920


# ====================================================================
# VERIFICATION DE FFMPEG
# ====================================================================
function Test-FFmpeg {
    try {
        $null = & ffmpeg -version 2>&1
        $null = & ffprobe -version 2>&1
        return $true
    } catch {
        return $false
    }
}

if (-not (Test-FFmpeg)) {
    Write-Host "[ERREUR] ffmpeg/ffprobe introuvable dans le PATH." -ForegroundColor Red
    Write-Host "         Telecharge-le sur https://www.gyan.dev/ffmpeg/builds/ et ajoute-le au PATH." -ForegroundColor Yellow
    exit 1
}


# ====================================================================
# ANALYSE DES CODECS + RESOLUTION
# ====================================================================
function Get-Codecs {
    param([string]$Path)

    $json = & ffprobe -v quiet -print_format json -show_streams "$Path" 2>$null | ConvertFrom-Json

    $video  = $null
    $audio  = $null
    $width  = 0
    $height = 0
    foreach ($stream in $json.streams) {
        if ($stream.codec_type -eq 'video' -and -not $video) {
            $video  = $stream.codec_name
            if ($stream.width)  { $width  = [int]$stream.width }
            if ($stream.height) { $height = [int]$stream.height }
        }
        if ($stream.codec_type -eq 'audio' -and -not $audio) { $audio = $stream.codec_name }
    }

    return [PSCustomObject]@{
        Video  = $video
        Audio  = $audio
        Width  = $width
        Height = $height
    }
}


# ====================================================================
# Recuperation de la duree
# ====================================================================
function Get-Duration {
    param([string]$Path)
    try {
        $d = & ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$Path" 2>$null
        return [double]$d
    } catch {
        return 0
    }
}


# ====================================================================
# ARGUMENTS VIDEO SELON L'ENCODEUR
#   -NeedsConversion : la video doit etre transcodee (codec != H.264)
#   -Downscale       : reduire a 1080p (force aussi le re-encodage)
# ====================================================================
function Get-VideoArgs {
    param(
        [bool]$NeedsConversion,
        [bool]$Downscale
    )

    # Ni transcodage ni downscale -> copie directe (instantane)
    if (-not $NeedsConversion -and -not $Downscale) {
        return @('-c:v', 'copy')
    }

    # Des qu'on re-encode, on borne la video dans une boite MaxWidth x MaxHeight
    # en gardant le ratio. force_original_aspect_ratio=decrease ne fait que
    # REDUIRE (jamais agrandir), et force_divisible_by=2 garantit des dimensions
    # paires exigees par H.264. Cela evite de depasser le Level 4.1 sur les
    # films ultra-larges ou borner la hauteur seule donne une image trop large.
    $scaleArg = @('-vf', "scale=w=${MaxWidth}:h=${MaxHeight}:force_original_aspect_ratio=decrease:force_divisible_by=2")

    switch ($Encoder) {
        "nvidia" { return @('-c:v', 'h264_nvenc', '-preset', 'p7', '-cq', "$Crf") + $scaleArg + @('-pix_fmt', 'yuv420p', '-profile:v', 'high', '-level', '4.1') }
        "intel"  { return @('-c:v', 'h264_qsv', '-global_quality', "$Crf") + $scaleArg + @('-pix_fmt', 'nv12', '-profile:v', 'high', '-level', '4.1') }
        "amd"    { return @('-c:v', 'h264_amf', '-quality', 'balanced', '-qp_i', "$Crf", '-qp_p', "$Crf") + $scaleArg + @('-pix_fmt', 'yuv420p', '-profile:v', 'high', '-level', '4.1') }
        default  { return @('-c:v', 'libx264', '-preset', 'slow', '-crf', "$Crf") + $scaleArg + @('-pix_fmt', 'yuv420p', '-profile:v', 'high', '-level', '4.1') }
    }
}


# ====================================================================
# SCAN DU DOSSIER COURANT
# ====================================================================
Write-Host ""
Write-Host "Scan du dossier : $(Get-Location)" -ForegroundColor Cyan
Write-Host ("-" * 70)

$files = Get-ChildItem -File | Where-Object { $VideoExtensions -contains $_.Extension.ToLower() }

if ($files.Count -eq 0) {
    Write-Host "Aucun fichier video trouve dans ce dossier." -ForegroundColor Yellow
    exit 0
}

$toConvert = @()
$index = 0

foreach ($file in $files) {
    # On ignore les fichiers temporaires de conversion
    if ($file.BaseName -like "*.tmp") { continue }

    $isOutFile = ($file.BaseName -like "*_out")

    $codecs  = Get-Codecs -Path $file.FullName
    $videoOk = $VideoOK -contains $codecs.Video
    $audioOk = $AudioOK -contains $codecs.Audio
    $needsDownscale = ($codecs.Height -gt $MaxHeight)

    if ($isOutFile) {
        # Un _out deja fini : on ne le repropose QUE s'il depasse 1080p
        $needsProcessing = $needsDownscale
    } else {
        # Fichier source : video a transcoder, audio a transcoder, OU > 1080p
        $needsProcessing = (-not $videoOk) -or (-not $audioOk) -or $needsDownscale
    }

    if ($needsProcessing) {
        $index++
        $vStatus = if ($videoOk) { "OK" } else { "NON" }
        $aStatus = if ($audioOk) { "OK" } else { "NON" }
        $resStatus = if ($needsDownscale) {
            "$($codecs.Height)p -> ${MaxHeight}p"
        } else {
            "$($codecs.Height)p"
        }
        $tag = if ($isOutFile) { " (_out, sera remplace)" } else { "" }

        $toConvert += [PSCustomObject]@{
            Num       = $index
            File      = $file
            Video     = $codecs.Video
            Audio     = $codecs.Audio
            VideoOk   = $videoOk
            AudioOk   = $audioOk
            Downscale = $needsDownscale
            Height    = $codecs.Height
            IsOut     = $isOutFile
        }

        Write-Host ("[{0,2}] {1}{2}" -f $index, $file.Name, $tag) -ForegroundColor White
        Write-Host ("     Video : {0,-6} [{1}]   Audio : {2,-6} [{3}]   Res : {4}" -f `
            $codecs.Video, $vStatus, $codecs.Audio, $aStatus, $resStatus) -ForegroundColor Gray
    }
}

if ($toConvert.Count -eq 0) {
    Write-Host ""
    Write-Host "Tous les fichiers sont deja au bon format (H.264 + AAC, <= ${MaxHeight}p)." -ForegroundColor Green
    exit 0
}

Write-Host ("-" * 70)
Write-Host ""


# ====================================================================
# SELECTION INTERACTIVE
# ====================================================================
Write-Host "Quels fichiers convertir ?" -ForegroundColor Cyan
Write-Host "  - Numeros separes par des virgules (ex: 1,3,5)"
Write-Host "  - Plage (ex: 1-4)"
Write-Host "  - 'all' pour tout convertir"
Write-Host "  - 'q' pour quitter"
Write-Host ""
$selection = Read-Host "Selection"

if ($selection -eq 'q' -or [string]::IsNullOrWhiteSpace($selection)) {
    Write-Host "Annule." -ForegroundColor Yellow
    exit 0
}

$selectedNums = @()
if ($selection -eq 'all') {
    $selectedNums = $toConvert.Num
} else {
    foreach ($part in $selection -split ',') {
        $part = $part.Trim()
        if ($part -match '^(\d+)-(\d+)$') {
            $start = [int]$Matches[1]
            $end   = [int]$Matches[2]
            $selectedNums += $start..$end
        } elseif ($part -match '^\d+$') {
            $selectedNums += [int]$part
        }
    }
}

$selectedFiles = $toConvert | Where-Object { $selectedNums -contains $_.Num }

if ($selectedFiles.Count -eq 0) {
    Write-Host "Aucun fichier valide selectionne." -ForegroundColor Yellow
    exit 0
}


# ====================================================================
# CONVERSION
# ====================================================================
Write-Host ""
Write-Host ("=" * 70) -ForegroundColor Cyan
Write-Host "Conversion de $($selectedFiles.Count) fichier(s)" -ForegroundColor Cyan
Write-Host ("=" * 70) -ForegroundColor Cyan

$count = 0
foreach ($item in $selectedFiles) {
    $count++
    $file = $item.File

    # Nom de sortie : un _out est remplace en place via un fichier temporaire,
    # sinon on cree <nom>_out.mkv
    if ($item.IsOut) {
        $output    = Join-Path $file.DirectoryName ($file.BaseName + ".tmp.mkv")
        $finalPath = $file.FullName
    } else {
        $output    = Join-Path $file.DirectoryName ($file.BaseName + "_out.mkv")
        $finalPath = $output
    }

    Write-Host ""
    Write-Host "[$count/$($selectedFiles.Count)] $($file.Name)" -ForegroundColor White
    if ($item.IsOut) {
        Write-Host "  -> remplace en place (downscale)" -ForegroundColor Gray
    } else {
        Write-Host "  -> $($file.BaseName)_out.mkv" -ForegroundColor Gray
    }

    $vArgs = Get-VideoArgs -NeedsConversion (-not $item.VideoOk) -Downscale $item.Downscale
    # Audio toujours en AAC-LC stereo (compatible Chromecast : pas de HE-AAC ni 5.1)
    $aArgs = @('-c:a', 'aac', '-profile:a', 'aac_low', '-b:a', '192k', '-ac', '2')

    # Message video selon le cas
    if ($item.Downscale -and $item.VideoOk) {
        $vMsg = "H.264 $($item.Height)p -> ${MaxHeight}p ($Encoder)"
    } elseif ($item.Downscale) {
        $vMsg = "-> H.264 ${MaxHeight}p ($Encoder)"
    } elseif (-not $item.VideoOk) {
        $vMsg = "-> H.264 ($Encoder)"
    } else {
        $vMsg = "copy"
    }
    Write-Host ("  Video : {0}   Audio : -> AAC-LC stereo" -f $vMsg) -ForegroundColor DarkGray

    # -map 0:s? : garde les sous-titres s'il y en a (SRT + PGS, MKV accepte tout)
    $ffmpegArgs = @(
        '-i', $file.FullName,
        '-map', '0:v:0',
        '-map', '0:a',
        '-map', '0:s?'
    ) + $vArgs + $aArgs + @(
        '-c:s', 'copy',
        '-progress', 'pipe:1',
        '-nostats',
        '-y',
        $output
    )

    $totalSeconds = Get-Duration -Path $file.FullName
    $startTime = Get-Date
    $logFile = Join-Path $env:TEMP ("ffmpeg_" + [guid]::NewGuid().ToString() + ".log")

    & ffmpeg @ffmpegArgs 2>$logFile | ForEach-Object {
        if ($_ -match 'out_time=(\d+):(\d+):([\d\.]+)') {
            $currentSec = [int]$Matches[1]*3600 + [int]$Matches[2]*60 + [double]$Matches[3]
            if ($totalSeconds -gt 0) {
                $percent = [math]::Min(100, [math]::Round(($currentSec / $totalSeconds) * 100))
                $elapsed = (Get-Date) - $startTime
                $etaStr = ""
                if ($currentSec -gt 0) {
                    $remaining = ($elapsed.TotalSeconds * ($totalSeconds / $currentSec)) - $elapsed.TotalSeconds
                    if ($remaining -gt 0) {
                        $etaStr = " - ETA {0:D2}m{1:D2}s" -f [int]([math]::Floor($remaining/60)), [int]($remaining%60)
                    }
                }
                Write-Progress -Activity ("Conversion : " + $file.Name) `
                    -Status ("{0}% ({1}s / {2}s){3}" -f $percent, [int]$currentSec, [int]$totalSeconds, $etaStr) `
                    -PercentComplete $percent
            }
        }
    }

    $exitCode = $LASTEXITCODE
    Write-Progress -Activity ("Conversion : " + $file.Name) -Completed
    $duration = (Get-Date) - $startTime

    $outSize = if (Test-Path $output) { (Get-Item $output).Length } else { 0 }

    if ($exitCode -eq 0 -and $outSize -gt 0) {
        # Si on remplace un _out : on supprime l'original 4K et on renomme le .tmp
        if ($item.IsOut) {
            try {
                Remove-Item $finalPath -Force
                Rename-Item $output $finalPath
            } catch {
                Write-Host "  [ATTENTION] Remplacement impossible, fichier laisse en .tmp.mkv" -ForegroundColor Yellow
                $finalPath = $output
            }
        }
        $sizeMB = [math]::Round((Get-Item $finalPath).Length / 1MB, 0)
        $durStr = "{0:D2}m{1:D2}s" -f [int]$duration.Minutes, [int]$duration.Seconds
        Write-Host ("  [OK] Termine en $durStr - $sizeMB Mo") -ForegroundColor Green
    } else {
        Write-Host "  [ECHEC] Conversion echouee (code ffmpeg : $exitCode)" -ForegroundColor Red
        if (Test-Path $logFile) {
            Write-Host "  --- Derniers messages ffmpeg ---" -ForegroundColor DarkYellow
            Get-Content $logFile -Tail 15 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
        }
        # On supprime le fichier vide laisse par ffmpeg
        if ((Test-Path $output) -and $outSize -eq 0) {
            Remove-Item $output -Force -ErrorAction SilentlyContinue
        }
    }

    if (Test-Path $logFile) { Remove-Item $logFile -Force -ErrorAction SilentlyContinue }
}

Write-Host ""
Write-Host ("=" * 70) -ForegroundColor Cyan
Write-Host "Conversion terminee." -ForegroundColor Green
Write-Host ("=" * 70) -ForegroundColor Cyan
