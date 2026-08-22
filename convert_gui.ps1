<#
.SYNOPSIS
    IHM (interface graphique) de conversion video H.264 + AAC pour CineLocal.
    Fusionne la logique de convert.ps1 et convert_only_format.ps1 :
      - detecte les fichiers non H.264 / non AAC stereo (2 canaux), ou au-dela de 1080p
      - permet de choisir l'encodeur (CPU / NVIDIA / Intel / AMD), la qualite (CRF/CQ)
        et le format video (garder l'original ou forcer 1080p max)
      - audio : toujours AAC stereo 2 canaux (Chromecast / PC) ; copie si deja conforme
      - convertit avec barre de progression + ETA, et bouton Annuler
    Sortie : <nom>_out.mkv (toutes pistes audio + sous-titres conservees).
    Un fichier _out deja > resolution max est propose et remplace en place.

.PREREQUIS
    Windows PowerShell 5.1+ (WinForms integre), ffmpeg/ffprobe dans le PATH.

.UTILISATION
    Clic droit > "Executer avec PowerShell", ou dans un terminal :
        powershell -ExecutionPolicy Bypass -File .\convert_gui.ps1
#>

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

# ====================================================================
# CONFIGURATION
# ====================================================================
$VideoExtensions = @('.mkv', '.mp4', '.avi', '.mov', '.m4v', '.wmv', '.flv', '.webm', '.ts', '.m2ts')
$VideoOK = @('h264')
# Audio conforme = AAC avec 2 canaux max (Chromecast + PC). Tout le reste est re-encode.
$AudioOKCodec   = 'aac'
$AudioMaxCanaux = 2
# Sous-titres texte a convertir en SRT (mov_text/text ne sont pas copiables en MKV)
$SubsToSrt = @('mov_text', 'text', 'webvtt')
# Sous-titres impossibles a garder proprement dans un MKV -> ignores
$SubsDrop  = @('dvb_teletext', 'eia_608', 'arib_caption')

# Etat global de la conversion
$script:Cancel     = $false
$script:Converting = $false

# ====================================================================
# HELPERS FFMPEG
# ====================================================================
function Test-FFmpeg {
    try { $null = & ffmpeg -version 2>&1; $null = & ffprobe -version 2>&1; return $true }
    catch { return $false }
}

function Get-Codecs {
    param([string]$Path)
    $video = $null; $audio = $null; $width = 0; $height = 0
    $channels = 0; $maxChannels = 0; $subs = @(); $audioCodecs = @()
    try {
        $json = & ffprobe -v quiet -print_format json -show_streams "$Path" 2>$null | ConvertFrom-Json
        foreach ($stream in $json.streams) {
            if ($stream.codec_type -eq 'video' -and -not $video) {
                # On ignore les pistes "attached_pic" (jaquettes) pour la detection
                if ($stream.disposition -and $stream.disposition.attached_pic -eq 1) { continue }
                $video = $stream.codec_name
                if ($stream.width)  { $width  = [int]$stream.width }
                if ($stream.height) { $height = [int]$stream.height }
            }
            if ($stream.codec_type -eq 'audio') {
                $ch = 0; if ($stream.channels) { $ch = [int]$stream.channels }
                if (-not $audio) { $audio = $stream.codec_name; $channels = $ch }
                if ($ch -gt $maxChannels) { $maxChannels = $ch }
                $audioCodecs += [string]$stream.codec_name
            }
            if ($stream.codec_type -eq 'subtitle') { $subs += [string]$stream.codec_name }
        }
    } catch {}
    return [PSCustomObject]@{
        Video = $video; Audio = $audio; Width = $width; Height = $height
        Channels = $channels; MaxChannels = $maxChannels
        AudioCodecs = $audioCodecs; Subs = $subs
    }
}

function Get-Duration {
    param([string]$Path)
    try {
        $d = & ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$Path" 2>$null
        return [double]$d
    } catch { return 0 }
}

# Arguments video selon l'encodeur / qualite / downscale
function Build-VideoArgs {
    param([bool]$NeedsConversion, [bool]$Downscale, [string]$Encoder, [int]$Crf, [int]$MaxW, [int]$MaxH)

    if (-not $NeedsConversion -and -not $Downscale) { return @('-c:v', 'copy') }

    # Base encodeur + qualite
    switch ($Encoder) {
        'nvidia' { $enc = @('-c:v', 'h264_nvenc', '-preset', 'p7', '-cq', "$Crf"); $pix = 'yuv420p' }
        'intel'  { $enc = @('-c:v', 'h264_qsv', '-global_quality', "$Crf");        $pix = 'nv12'    }
        'amd'    { $enc = @('-c:v', 'h264_amf', '-quality', 'balanced', '-qp_i', "$Crf", '-qp_p', "$Crf"); $pix = 'yuv420p' }
        default  { $enc = @('-c:v', 'libx264', '-preset', 'slow', '-crf', "$Crf"); $pix = 'yuv420p' }
    }

    $post = @('-pix_fmt', $pix)
    if ($Downscale) {
        # Downscale <= 1080p : on borne l'image ET on force profil High / Level 4.1
        # (valides pour <=1080p, compatibles Pi / Chromecast).
        $enc  += @('-vf', "scale=w=${MaxW}:h=${MaxH}:force_original_aspect_ratio=decrease:force_divisible_by=2")
        $post += @('-profile:v', 'high', '-level', '4.1')
    }
    # IMPORTANT : sans downscale (on garde la resolution d'origine, ex. 4K), on ne
    # force PAS -level 4.1 : ce niveau est plafonne a 1080p et ferait echouer
    # l'encodeur sur une source 4K (aucun paquet ecrit -> erreur -22).
    return $enc + $post
}

function Build-AudioArgs {
    param([bool]$AudioOk)
    # Toujours AAC stereo 2 canaux (Chromecast + PC). Copie directe si deja conforme.
    if ($AudioOk) { return @('-c:a', 'copy') }
    return @('-c:a', 'aac', '-profile:a', 'aac_low', '-b:a', '192k', '-ac', '2')
}

# Sous-titres : mapping piste par piste.
#  - mov_text / text / webvtt : non copiables (ou mal geres) en MKV -> conversion SRT
#  - dvb_teletext, eia_608...  : pas exploitables -> ignores
#  - le reste (subrip, ass, ssa, hdmv_pgs, dvd_subtitle...) : copie telle quelle
function Build-SubArgs {
    param([string[]]$Subs)
    if (-not $Subs -or $Subs.Count -eq 0) { return @() }
    $subArgs = @()
    $outIdx = 0
    for ($i = 0; $i -lt $Subs.Count; $i++) {
        $codec = $Subs[$i]
        if ($SubsDrop -contains $codec) { continue }   # piste non mappee -> ignoree
        $subArgs += @('-map', "0:s:$i")
        if ($SubsToSrt -contains $codec) { $subArgs += @("-c:s:$outIdx", 'srt') }
        else                             { $subArgs += @("-c:s:$outIdx", 'copy') }
        $outIdx++
    }
    return $subArgs
}

# Guillemets pour les arguments ffmpeg (chemins avec espaces) - Windows PowerShell 5.1
function Quote-Args {
    param([string[]]$Arr)
    ($Arr | ForEach-Object {
        if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ }
    }) -join ' '
}

# ====================================================================
# FENETRE
# ====================================================================
$form = New-Object System.Windows.Forms.Form
$form.Text          = "CineLocal - Conversion video (H.264 + AAC)"
$form.Size          = New-Object System.Drawing.Size(940, 660)
$form.MinimumSize   = New-Object System.Drawing.Size(820, 560)
$form.StartPosition = "CenterScreen"
$form.BackColor     = [System.Drawing.Color]::FromArgb(24, 26, 32)
$form.ForeColor     = [System.Drawing.Color]::Gainsboro
$form.Font          = New-Object System.Drawing.Font("Segoe UI", 9)

$gold = [System.Drawing.Color]::FromArgb(201, 168, 76)

# --- Ligne dossier ---
$lblFolder = New-Object System.Windows.Forms.Label
$lblFolder.Text = "Dossier :"
$lblFolder.Location = New-Object System.Drawing.Point(12, 15)
$lblFolder.AutoSize = $true
$form.Controls.Add($lblFolder)

$txtFolder = New-Object System.Windows.Forms.TextBox
$txtFolder.Location = New-Object System.Drawing.Point(75, 12)
$txtFolder.Size = New-Object System.Drawing.Size(600, 24)
$txtFolder.Anchor = 'Top,Left,Right'
$txtFolder.Text = (Get-Location).Path
$txtFolder.BackColor = [System.Drawing.Color]::FromArgb(36, 39, 47)
$txtFolder.ForeColor = [System.Drawing.Color]::Gainsboro
$form.Controls.Add($txtFolder)

$btnBrowse = New-Object System.Windows.Forms.Button
$btnBrowse.Text = "Parcourir..."
$btnBrowse.Location = New-Object System.Drawing.Point(685, 11)
$btnBrowse.Size = New-Object System.Drawing.Size(90, 26)
$btnBrowse.Anchor = 'Top,Right'
$form.Controls.Add($btnBrowse)

$btnScan = New-Object System.Windows.Forms.Button
$btnScan.Text = "Scanner"
$btnScan.Location = New-Object System.Drawing.Point(783, 11)
$btnScan.Size = New-Object System.Drawing.Size(130, 26)
$btnScan.Anchor = 'Top,Right'
$btnScan.BackColor = $gold
$btnScan.ForeColor = [System.Drawing.Color]::Black
$btnScan.FlatStyle = 'Flat'
$form.Controls.Add($btnScan)

# --- Panneau parametres ---
$grp = New-Object System.Windows.Forms.GroupBox
$grp.Text = "Parametres de conversion"
$grp.Location = New-Object System.Drawing.Point(12, 48)
$grp.Size = New-Object System.Drawing.Size(901, 78)
$grp.Anchor = 'Top,Left,Right'
$grp.ForeColor = $gold
$form.Controls.Add($grp)

# Encodeur
$lblEnc = New-Object System.Windows.Forms.Label
$lblEnc.Text = "Encodeur"; $lblEnc.Location = New-Object System.Drawing.Point(15, 22); $lblEnc.AutoSize = $true
$lblEnc.ForeColor = [System.Drawing.Color]::Gainsboro
$grp.Controls.Add($lblEnc)
$cmbEnc = New-Object System.Windows.Forms.ComboBox
$cmbEnc.Location = New-Object System.Drawing.Point(15, 42); $cmbEnc.Size = New-Object System.Drawing.Size(180, 24)
$cmbEnc.DropDownStyle = 'DropDownList'
[void]$cmbEnc.Items.Add("NVIDIA (GPU, h264_nvenc)")
[void]$cmbEnc.Items.Add("Intel (GPU, h264_qsv)")
[void]$cmbEnc.Items.Add("AMD (GPU, h264_amf)")
[void]$cmbEnc.Items.Add("CPU (libx264, lent)")
$cmbEnc.SelectedIndex = 0
$grp.Controls.Add($cmbEnc)

# Qualite
$lblQ = New-Object System.Windows.Forms.Label
$lblQ.Text = "Qualite (CRF/CQ, + bas = mieux)"; $lblQ.Location = New-Object System.Drawing.Point(215, 22); $lblQ.AutoSize = $true
$lblQ.ForeColor = [System.Drawing.Color]::Gainsboro
$grp.Controls.Add($lblQ)
$numCrf = New-Object System.Windows.Forms.NumericUpDown
$numCrf.Location = New-Object System.Drawing.Point(215, 42); $numCrf.Size = New-Object System.Drawing.Size(70, 24)
$numCrf.Minimum = 0; $numCrf.Maximum = 51; $numCrf.Value = 18
$numCrf.BackColor = [System.Drawing.Color]::FromArgb(36, 39, 47); $numCrf.ForeColor = [System.Drawing.Color]::Gainsboro
$grp.Controls.Add($numCrf)

# Resolution max
$lblRes = New-Object System.Windows.Forms.Label
$lblRes.Text = "Format video"; $lblRes.Location = New-Object System.Drawing.Point(310, 22); $lblRes.AutoSize = $true
$lblRes.ForeColor = [System.Drawing.Color]::Gainsboro
$grp.Controls.Add($lblRes)
$cmbRes = New-Object System.Windows.Forms.ComboBox
$cmbRes.Location = New-Object System.Drawing.Point(310, 42); $cmbRes.Size = New-Object System.Drawing.Size(190, 24)
$cmbRes.DropDownStyle = 'DropDownList'
[void]$cmbRes.Items.Add("Forcer 1080p max")
[void]$cmbRes.Items.Add("Garder le format d'origine")
$cmbRes.SelectedIndex = 0
$grp.Controls.Add($cmbRes)

# Audio (fixe : AAC stereo 2 canaux, copie si deja conforme)
$lblAud = New-Object System.Windows.Forms.Label
$lblAud.Text = "Audio"; $lblAud.Location = New-Object System.Drawing.Point(515, 22); $lblAud.AutoSize = $true
$lblAud.ForeColor = [System.Drawing.Color]::Gainsboro
$grp.Controls.Add($lblAud)
$lblAudFixed = New-Object System.Windows.Forms.Label
$lblAudFixed.Text = "AAC stereo 2 canaux (Chromecast / PC)"
$lblAudFixed.Location = New-Object System.Drawing.Point(515, 46); $lblAudFixed.AutoSize = $true
$lblAudFixed.ForeColor = [System.Drawing.Color]::Silver
$grp.Controls.Add($lblAudFixed)

# --- Liste des fichiers ---
$grid = New-Object System.Windows.Forms.DataGridView
$grid.Location = New-Object System.Drawing.Point(12, 132)
$grid.Size = New-Object System.Drawing.Size(901, 330)
$grid.Anchor = 'Top,Bottom,Left,Right'
$grid.AllowUserToAddRows = $false
$grid.AllowUserToDeleteRows = $false
$grid.RowHeadersVisible = $false
$grid.SelectionMode = 'FullRowSelect'
$grid.AutoSizeColumnsMode = 'Fill'
$grid.BackgroundColor = [System.Drawing.Color]::FromArgb(30, 33, 40)
$grid.GridColor = [System.Drawing.Color]::FromArgb(60, 63, 70)
$grid.EnableHeadersVisualStyles = $false
$grid.ColumnHeadersDefaultCellStyle.BackColor = [System.Drawing.Color]::FromArgb(36, 39, 47)
$grid.ColumnHeadersDefaultCellStyle.ForeColor = $gold
$grid.DefaultCellStyle.BackColor = [System.Drawing.Color]::FromArgb(30, 33, 40)
$grid.DefaultCellStyle.ForeColor = [System.Drawing.Color]::Gainsboro
$grid.DefaultCellStyle.SelectionBackColor = [System.Drawing.Color]::FromArgb(55, 50, 30)

$colChk = New-Object System.Windows.Forms.DataGridViewCheckBoxColumn
$colChk.HeaderText = "OK"; $colChk.Name = "Sel"; $colChk.FillWeight = 6
$grid.Columns.Add($colChk) | Out-Null
$colName = New-Object System.Windows.Forms.DataGridViewTextBoxColumn
$colName.HeaderText = "Fichier"; $colName.Name = "Nom"; $colName.FillWeight = 46; $colName.ReadOnly = $true
$grid.Columns.Add($colName) | Out-Null
$colV = New-Object System.Windows.Forms.DataGridViewTextBoxColumn
$colV.HeaderText = "Video"; $colV.Name = "Video"; $colV.FillWeight = 12; $colV.ReadOnly = $true
$grid.Columns.Add($colV) | Out-Null
$colA = New-Object System.Windows.Forms.DataGridViewTextBoxColumn
$colA.HeaderText = "Audio"; $colA.Name = "Audio"; $colA.FillWeight = 10; $colA.ReadOnly = $true
$grid.Columns.Add($colA) | Out-Null
$colR = New-Object System.Windows.Forms.DataGridViewTextBoxColumn
$colR.HeaderText = "Resolution"; $colR.Name = "Res"; $colR.FillWeight = 12; $colR.ReadOnly = $true
$grid.Columns.Add($colR) | Out-Null
$colS = New-Object System.Windows.Forms.DataGridViewTextBoxColumn
$colS.HeaderText = "Statut"; $colS.Name = "Statut"; $colS.FillWeight = 14; $colS.ReadOnly = $true
$grid.Columns.Add($colS) | Out-Null
$form.Controls.Add($grid)

# --- Barre du bas : selection + progression ---
$btnAll = New-Object System.Windows.Forms.Button
$btnAll.Text = "Tout cocher"; $btnAll.Location = New-Object System.Drawing.Point(12, 470); $btnAll.Size = New-Object System.Drawing.Size(100, 26)
$btnAll.Anchor = 'Bottom,Left'
$form.Controls.Add($btnAll)
$btnNone = New-Object System.Windows.Forms.Button
$btnNone.Text = "Tout decocher"; $btnNone.Location = New-Object System.Drawing.Point(118, 470); $btnNone.Size = New-Object System.Drawing.Size(110, 26)
$btnNone.Anchor = 'Bottom,Left'
$form.Controls.Add($btnNone)

$progress = New-Object System.Windows.Forms.ProgressBar
$progress.Location = New-Object System.Drawing.Point(12, 505); $progress.Size = New-Object System.Drawing.Size(770, 22)
$progress.Anchor = 'Bottom,Left,Right'
$form.Controls.Add($progress)

$btnConvert = New-Object System.Windows.Forms.Button
$btnConvert.Text = "> Convertir"; $btnConvert.Location = New-Object System.Drawing.Point(790, 468); $btnConvert.Size = New-Object System.Drawing.Size(123, 30)
$btnConvert.Anchor = 'Bottom,Right'
$btnConvert.BackColor = $gold; $btnConvert.ForeColor = [System.Drawing.Color]::Black; $btnConvert.FlatStyle = 'Flat'
$form.Controls.Add($btnConvert)
$btnCancel = New-Object System.Windows.Forms.Button
$btnCancel.Text = "# Annuler"; $btnCancel.Location = New-Object System.Drawing.Point(790, 503); $btnCancel.Size = New-Object System.Drawing.Size(123, 26)
$btnCancel.Anchor = 'Bottom,Right'; $btnCancel.Enabled = $false
$form.Controls.Add($btnCancel)

$lblStatus = New-Object System.Windows.Forms.Label
$lblStatus.Text = "Pret."; $lblStatus.Location = New-Object System.Drawing.Point(12, 532); $lblStatus.Size = New-Object System.Drawing.Size(901, 20)
$lblStatus.Anchor = 'Bottom,Left,Right'; $lblStatus.ForeColor = $gold
$form.Controls.Add($lblStatus)

$txtLog = New-Object System.Windows.Forms.TextBox
$txtLog.Location = New-Object System.Drawing.Point(12, 556); $txtLog.Size = New-Object System.Drawing.Size(901, 62)
$txtLog.Anchor = 'Bottom,Left,Right'; $txtLog.Multiline = $true; $txtLog.ScrollBars = 'Vertical'; $txtLog.ReadOnly = $true
$txtLog.BackColor = [System.Drawing.Color]::FromArgb(18, 20, 25); $txtLog.ForeColor = [System.Drawing.Color]::Silver
$txtLog.Font = New-Object System.Drawing.Font("Consolas", 8)
$form.Controls.Add($txtLog)

function Write-Log { param([string]$Msg)
    $txtLog.AppendText(("[{0}] {1}`r`n" -f (Get-Date -Format "HH:mm:ss"), $Msg))
}

# ====================================================================
# PARAMETRES DE RESOLUTION (depuis la combo)
# ====================================================================
function Get-ResLimit {
    switch ($cmbRes.SelectedIndex) {
        0 { return @{ H = 1080; W = 1920 } }              # Forcer 1080p max
        default { return @{ H = 100000; W = 100000 } }    # Garder le format d'origine
    }
}
function Get-EncoderKey {
    switch ($cmbEnc.SelectedIndex) { 0 {'nvidia'} 1 {'intel'} 2 {'amd'} default {'cpu'} }
}

# ====================================================================
# SCAN
# ====================================================================
function Do-Scan {
    $folder = $txtFolder.Text
    if (-not (Test-Path $folder)) { Write-Log "Dossier introuvable : $folder"; return }
    $grid.Rows.Clear()
    $lblStatus.Text = "Scan en cours..."
    [System.Windows.Forms.Application]::DoEvents()

    $files = Get-ChildItem -LiteralPath $folder -File | Where-Object { $VideoExtensions -contains $_.Extension.ToLower() }
    $n = 0
    foreach ($file in $files) {
        if ($file.BaseName -like "*.tmp") { continue }
        $isOut = ($file.BaseName -like "*_out")
        $c = Get-Codecs -Path $file.FullName
        $videoOk = $VideoOK -contains $c.Video
        # Audio conforme = TOUTES les pistes en AAC ET aucune piste au-dela de 2 canaux
        $nonAac = @($c.AudioCodecs | Where-Object { $_ -ne $AudioOKCodec })
        $audioOk = ($c.AudioCodecs.Count -gt 0) -and ($nonAac.Count -eq 0) `
                   -and ($c.MaxChannels -ge 1) -and ($c.MaxChannels -le $AudioMaxCanaux)

        $audTxt = if ($c.Audio) { if ($c.Channels -gt 0) { "$($c.Audio) $($c.Channels)ch" } else { $c.Audio } } else { "-" }
        $resTxt = if ($c.Height) { "$($c.Width)x$($c.Height)" } else { "?" }

        $ri = $grid.Rows.Add(@($false, $file.Name, $c.Video, $audTxt, $resTxt, ""))
        $row = $grid.Rows[$ri]
        $row.Tag = [PSCustomObject]@{
            File = $file; VideoOk = $videoOk; AudioOk = $audioOk
            Downscale = $false; Height = $c.Height; IsOut = $isOut; Subs = $c.Subs
        }
        $n++
    }
    Update-Statuses   # applique le format video choisi sans re-sonder les fichiers
    $lblStatus.Text = "$n fichier(s) trouve(s). Coche ceux a convertir puis 'Convertir'."
    Write-Log "Scan termine : $n fichier(s)."
}

# Recalcule statuts et coches d'apres le cache (Tag) - AUCUN nouvel appel ffprobe.
# Appele apres le scan et quand on change l'option de format video.
function Update-Statuses {
    if ($script:Converting) { return }
    $res = Get-ResLimit
    foreach ($row in $grid.Rows) {
        $meta = $row.Tag
        if (-not $meta) { continue }
        if ($row.Cells["Statut"].Value -eq "OK termine") { continue }  # deja traite

        $needsDownscale = ($meta.Height -gt $res.H)
        $meta.Downscale = $needsDownscale

        if ($meta.IsOut) { $needsProc = $needsDownscale }
        else             { $needsProc = (-not $meta.VideoOk) -or (-not $meta.AudioOk) -or $needsDownscale }

        $statut = if ($needsProc) {
            if ($meta.IsOut) { "a reduire (_out)" } else { "a convertir" }
        } else { "OK" }

        $row.Cells["Sel"].Value = $needsProc
        $row.Cells["Statut"].Value = $statut
        if ($needsProc) { $row.Cells["Statut"].Style.ForeColor = $gold }
        else            { $row.Cells["Statut"].Style.ForeColor = [System.Drawing.Color]::MediumSeaGreen }
    }
}

# ====================================================================
# CONVERSION
# ====================================================================
function Do-Convert {
    if ($script:Converting) { return }
    if (-not (Test-FFmpeg)) {
        [System.Windows.Forms.MessageBox]::Show("ffmpeg/ffprobe introuvable dans le PATH.`nTelechargez-le sur gyan.dev/ffmpeg/builds et ajoutez-le au PATH.", "Erreur", 'OK', 'Error') | Out-Null
        return
    }

    # Collecte des lignes cochees
    $queue = @()
    foreach ($row in $grid.Rows) {
        if ($row.Cells["Sel"].Value -eq $true -and $row.Tag) { $queue += $row }
    }
    if ($queue.Count -eq 0) { $lblStatus.Text = "Aucun fichier coche."; return }

    $encoder = Get-EncoderKey
    $crf     = [int]$numCrf.Value
    $res     = Get-ResLimit

    $script:Converting = $true; $script:Cancel = $false
    $btnConvert.Enabled = $false; $btnScan.Enabled = $false; $btnCancel.Enabled = $true
    Write-Log ("Conversion de {0} fichier(s) - encodeur={1}, qualite={2}, {3}." -f $queue.Count, $encoder, $crf, $cmbRes.Text)

    $i = 0
    foreach ($row in $queue) {
        if ($script:Cancel) { break }
        $i++
        $meta = $row.Tag
        $file = $meta.File

        if ($meta.IsOut) {
            $output = Join-Path $file.DirectoryName ($file.BaseName + ".tmp.mkv")
            $finalPath = $file.FullName
        } else {
            $output = Join-Path $file.DirectoryName ($file.BaseName + "_out.mkv")
            $finalPath = $output
        }

        $vArgs = Build-VideoArgs -NeedsConversion (-not $meta.VideoOk) -Downscale $meta.Downscale -Encoder $encoder -Crf $crf -MaxW $res.W -MaxH $res.H
        $aArgs = Build-AudioArgs -AudioOk $meta.AudioOk
        $sArgs = Build-SubArgs -Subs $meta.Subs

        # -map 0:a? : ne plante pas si le fichier n'a pas d'audio
        # -map 0:t? + -c:t copy : conserve les polices embarquees (sous-titres ASS)
        $ffArgs = @('-i', $file.FullName, '-map', '0:v:0', '-map', '0:a?') `
            + $sArgs + @('-map', '0:t?', '-c:t', 'copy') `
            + $vArgs + $aArgs + @('-progress', 'pipe:1', '-nostats', '-loglevel', 'error', '-y', $output)

        $total = Get-Duration -Path $file.FullName
        $start = Get-Date
        $row.Cells["Statut"].Value = "conversion..."
        $lblStatus.Text = "[$i/$($queue.Count)] $($file.Name) - demarrage..."
        [System.Windows.Forms.Application]::DoEvents()

        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = "ffmpeg"
        $psi.Arguments = Quote-Args $ffArgs
        $psi.UseShellExecute = $false
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.CreateNoWindow = $true

        $proc = $null
        try { $proc = [System.Diagnostics.Process]::Start($psi) } catch {
            Write-Log "ECHEC lancement ffmpeg : $_"; $row.Cells["Statut"].Value = "echec"; continue
        }

        while (-not $proc.HasExited) {
            $line = $proc.StandardOutput.ReadLine()
            if ($null -ne $line -and $line -match 'out_time=(\d+):(\d+):([\d\.]+)') {
                $cur = [int]$Matches[1]*3600 + [int]$Matches[2]*60 + [double]$Matches[3]
                if ($total -gt 0) {
                    $pct = [math]::Min(100, [int](($cur / $total) * 100))
                    $progress.Value = $pct
                    $elapsed = ((Get-Date) - $start).TotalSeconds
                    $eta = ""
                    if ($cur -gt 1) {
                        $rem = ($elapsed * ($total / $cur)) - $elapsed
                        if ($rem -gt 0) { $eta = " - ETA {0:D2}m{1:D2}s" -f [int][math]::Floor($rem/60), [int]($rem%60) }
                    }
                    $lblStatus.Text = "[$i/$($queue.Count)] $($file.Name) - $pct%$eta"
                }
            }
            [System.Windows.Forms.Application]::DoEvents()
            if ($script:Cancel) { try { $proc.Kill() } catch {}; break }
        }
        try { $proc.WaitForExit() } catch {}
        $exit = $proc.ExitCode
        $stderr = ""
        try { $stderr = $proc.StandardError.ReadToEnd() } catch {}

        $outSize = if (Test-Path $output) { (Get-Item $output).Length } else { 0 }

        if ($script:Cancel) {
            if ((Test-Path $output) -and $outSize -eq 0) { Remove-Item $output -Force -ErrorAction SilentlyContinue }
            $row.Cells["Statut"].Value = "annule"
            Write-Log "Annule : $($file.Name)"
            break
        }

        if ($exit -eq 0 -and $outSize -gt 0) {
            if ($meta.IsOut) {
                try { Remove-Item $finalPath -Force; Rename-Item $output $finalPath }
                catch { Write-Log "Remplacement impossible, laisse en .tmp.mkv"; $finalPath = $output }
            }
            $sizeMB = [math]::Round((Get-Item $finalPath).Length / 1MB, 0)
            $dur = (Get-Date) - $start
            $row.Cells["Sel"].Value = $false
            $row.Cells["Statut"].Value = "OK termine"
            $row.Cells["Statut"].Style.ForeColor = [System.Drawing.Color]::MediumSeaGreen
            Write-Log ("OK {0} - {1:D2}m{2:D2}s, {3} Mo" -f $file.Name, [int]$dur.Minutes, [int]$dur.Seconds, $sizeMB)
        } else {
            if ((Test-Path $output) -and $outSize -eq 0) { Remove-Item $output -Force -ErrorAction SilentlyContinue }
            $row.Cells["Statut"].Value = "echec"
            $row.Cells["Statut"].Style.ForeColor = [System.Drawing.Color]::IndianRed
            $tail = ($stderr -split "`n" | Select-Object -Last 2) -join " | "
            Write-Log ("ECHEC {0} (code {1}) {2}" -f $file.Name, $exit, $tail)
        }
        $progress.Value = 0
    }

    $script:Converting = $false
    $btnConvert.Enabled = $true; $btnScan.Enabled = $true; $btnCancel.Enabled = $false
    $progress.Value = 0
    $lblStatus.Text = if ($script:Cancel) { "Conversion annulee." } else { "Conversion terminee." }
    Write-Log $lblStatus.Text
}

# ====================================================================
# EVENEMENTS
# ====================================================================
$btnBrowse.Add_Click({
    $dlg = New-Object System.Windows.Forms.FolderBrowserDialog
    $dlg.SelectedPath = $txtFolder.Text
    if ($dlg.ShowDialog() -eq 'OK') { $txtFolder.Text = $dlg.SelectedPath; Do-Scan }
})
$btnScan.Add_Click({ Do-Scan })
$btnConvert.Add_Click({ Do-Convert })
$btnCancel.Add_Click({ $script:Cancel = $true; $lblStatus.Text = "Annulation..." })
$btnAll.Add_Click({ foreach ($r in $grid.Rows) { if ($r.Tag) { $r.Cells["Sel"].Value = $true } } })
$btnNone.Add_Click({ foreach ($r in $grid.Rows) { $r.Cells["Sel"].Value = $false } })
$cmbRes.Add_SelectedIndexChanged({ Update-Statuses })   # pas de re-scan : recalcul depuis le cache
$form.Add_FormClosing({ if ($script:Converting) { $script:Cancel = $true } })

# Scan initial du dossier courant
if (Test-FFmpeg) { $form.Add_Shown({ Do-Scan }) }
else { $form.Add_Shown({ Write-Log "ffmpeg introuvable dans le PATH - installe-le avant de convertir." }) }

[void]$form.ShowDialog()
