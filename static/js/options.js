// ═══════════════════════════════════════════════════════════════════
// OPTIONS — modale de choix avant lecture (qualité / piste audio /
// sous-titres) pour les cibles PC (navigateur) et Chromecast.
// Dépend de : utils.js (escHtml) ; cast.js (doCast, prepareAudioThenCast) ;
// player.js (playPc).
// ═══════════════════════════════════════════════════════════════════

var optionsTarget     = 'cast';   // 'pc' = navigateur, 'cast' = Chromecast
var castMoviePending  = null;
var castTracks        = null;
var castSelectedAudio = 0;
var castSelectedSub   = null;
var optQualities      = [];       // variantes de qualité du titre courant
var optSelectedQuality = 0;       // index dans optQualities
var optLaunchToken    = 0;        // incrémenté à chaque ouverture (anti-course)

function openOptions(movie, target, initialQuality) {
  optionsTarget    = target;
  currentMovie     = movie;
  castMoviePending = movie;
  castSelectedAudio = 0;
  castSelectedSub   = null;
  castTracks        = null;
  optLaunchToken++;

  // Variantes de qualité (4K / 1080p…). Les épisodes et les films à un seul
  // fichier n'en ont pas : on en synthétise une seule (pas de choix affiché).
  optQualities = (movie.qualities && movie.qualities.length)
    ? movie.qualities.slice()
    : [{ id: movie.id, label: '', height: 0, size_mb: movie.size_mb, ext: movie.ext }];
  // Pré-sélectionne la version choisie dans la fiche (bornée à la liste).
  optSelectedQuality = Math.min(Math.max(initialQuality || 0, 0), optQualities.length - 1);

  var titles = {
    pc:    ['🎞️ Choix de la qualité', '▶ Lire'],
    cast:  ['📺 Options Cast',         '📺 Lancer le Cast'],
  };
  document.getElementById('optionsTitle').textContent   = titles[target][0];
  document.getElementById('btnLaunchLabel').textContent = titles[target][1];

  document.getElementById('castOptionsFilm').textContent = movie.title;
  document.getElementById('cast-options-modal').classList.add('open');

  if (target === 'pc') {
    // PC : audio et sous-titres sont gérés nativement par le lecteur du
    // navigateur. On ne propose donc QUE la qualité, sans charger les pistes.
    castTracks = { audio_tracks: [], subtitle_tracks: [] };
    renderCastOptions();
    document.getElementById('btnCastLaunch').disabled = false;
    return;
  }

  loadTracksForQuality();
}

// Wrapper conservé pour compat (appelé ailleurs)
function startCast(movie)  { openOptions(movie, 'cast'); }

function _selectedQuality() {
  return optQualities[optSelectedQuality] || optQualities[0];
}

function loadTracksForQuality() {
  var q = _selectedQuality();
  var myToken = optLaunchToken;
  castTracks = null;
  document.getElementById('castOptionsBody').innerHTML =
    '<div class="cast-loading-msg">⏳ Chargement des pistes…</div>';
  document.getElementById('btnCastLaunch').disabled = true;

  fetch('/api/tracks/' + q.id)
    .then(function (r) { return r.json(); })
    .then(function (data) {
      if (optLaunchToken !== myToken) return;   // qualité ou film changé entre-temps
      castTracks = data;
      renderCastOptions();
      document.getElementById('btnCastLaunch').disabled = false;
    })
    .catch(function () {
      if (optLaunchToken !== myToken) return;
      castTracks = { audio_tracks: [], subtitle_tracks: [] };
      renderCastOptions();
      document.getElementById('btnCastLaunch').disabled = false;
    });
}

function selectQuality(idx) {
  if (idx === optSelectedQuality) return;
  optSelectedQuality = idx;
  castSelectedAudio = 0;
  castSelectedSub   = null;
  // PC : audio/sous-titres natifs, aucune piste à recharger.
  if (optionsTarget === 'pc') { renderCastOptions(); return; }
  // Cast : les pistes audio/sous-titres peuvent différer selon la
  // qualité ; on recharge celles du fichier choisi.
  optLaunchToken++;            // invalide une éventuelle requête de pistes en cours
  loadTracksForQuality();
}

function renderCastOptions() {
  if (!castTracks) return;
  var audio = castTracks.audio_tracks || [];
  var subs  = castTracks.subtitle_tracks || [];
  var pcQualityOnly = (optionsTarget === 'pc');
  var html  = '';

  // Section qualité (seulement s'il y a plusieurs variantes du même film)
  if (optQualities.length > 1) {
    html += '<div class="cast-section"><div class="cast-section-label">🎞️ Qualité</div>';
    optQualities.forEach(function (q, i) {
      var sel  = (i === optSelectedQuality) ? 'selected' : '';
      var ext  = q.ext ? q.ext.replace('.', '').toUpperCase() : '';
      var size = q.size_mb
        ? (q.size_mb >= 1024 ? (Math.round(q.size_mb / 102.4) / 10 + ' Go') : (q.size_mb + ' Mo'))
        : '';
      var meta = [ext, size].filter(Boolean).join(' · ');
      html += '<div class="cast-option-row ' + sel + '" onclick="selectQuality(' + i + ')">'
        + '<div class="cast-option-radio"><div class="cast-option-radio-dot"></div></div>'
        + '<div class="cast-option-text">'
        + '<div class="cast-option-name">' + escHtml(q.label || 'Vidéo') + '</div>'
        + (meta ? '<div class="cast-option-meta">' + meta + '</div>' : '')
        + '</div></div>';
    });
    html += '</div>';
  }

  // En mode PC, on s'arrête à la qualité : le navigateur gère lui-même
  // l'audio et les sous-titres via son lecteur natif.
  if (pcQualityOnly) {
    document.getElementById('castOptionsBody').innerHTML = html;
    return;
  }

  // Section audio
  if (audio.length > 0) {
    html += '<div class="cast-section"><div class="cast-section-label">🎵 Piste audio</div>';
    audio.forEach(function (track, i) {
      var sel      = (i === castSelectedAudio) ? 'selected' : '';
      var channels = track.channels ? (track.channels + ' canaux') : '';
      var codec    = track.codec ? track.codec.toUpperCase() : '';
      var meta     = [codec, channels].filter(Boolean).join(' · ');
      html += '<div class="cast-option-row ' + sel + '" onclick="selectCastAudio(' + i + ')">'
        + '<div class="cast-option-radio"><div class="cast-option-radio-dot"></div></div>'
        + '<div class="cast-option-text">'
        + '<div class="cast-option-name">' + escHtml(track.label) + '</div>'
        + (meta ? '<div class="cast-option-meta">' + meta + '</div>' : '')
        + '</div></div>';
    });
    html += '</div>';
  }

  // Section sous-titres
  html += '<div class="cast-section"><div class="cast-section-label">💬 Sous-titres</div>';
  var noSubSel = (castSelectedSub === null) ? 'selected' : '';
  html += '<div class="cast-option-row ' + noSubSel + '" onclick="selectCastSub(null)">'
    + '<div class="cast-option-radio"><div class="cast-option-radio-dot"></div></div>'
    + '<div class="cast-option-text"><div class="cast-option-name">Aucun sous-titre</div></div>'
    + '</div>';

  if (subs.length === 0) {
    html += '<div class="cast-empty-msg">Aucun sous-titre disponible (SRT non trouvé, PGS non supporté)</div>';
  } else {
    subs.forEach(function (sub, i) {
      var sel = (castSelectedSub === i) ? 'selected' : '';
      html += '<div class="cast-option-row ' + sel + '" onclick="selectCastSub(' + i + ')">'
        + '<div class="cast-option-radio"><div class="cast-option-radio-dot"></div></div>'
        + '<div class="cast-option-text"><div class="cast-option-name">' + escHtml(sub.label) + '</div></div>'
        + '</div>';
    });
  }
  html += '</div>';

  document.getElementById('castOptionsBody').innerHTML = html;
}

function selectCastAudio(idx) {
  castSelectedAudio = idx;
  renderCastOptions();
}

function selectCastSub(idx) {
  castSelectedSub = idx;
  renderCastOptions();
}

function closeCastOptions() {
  optLaunchToken++;   // invalide tout chargement de pistes / préparation en cours
  document.getElementById('cast-options-modal').classList.remove('open');
  castMoviePending = null;
}

function launchOptions() {
  if (optionsTarget === 'pc') launchPc();
  else                        launchCast();
}

// Construit l'objet à lire : le film courant mais avec l'id et l'extension de
// la variante de qualité sélectionnée (c'est cet id de fichier que la lecture,
// le cast et le remux utilisent).
function _playItem() {
  var q = _selectedQuality();
  return Object.assign({}, castMoviePending, { id: q.id, ext: q.ext || castMoviePending.ext });
}

// Index réel (0-based) de la piste audio choisie ; 0 = piste par défaut.
function _chosenAudioIdx() {
  var t = castTracks && castTracks.audio_tracks[castSelectedAudio];
  return t ? t.index : 0;
}

function _chosenSubTrack() {
  return (castSelectedSub !== null && castTracks)
    ? castTracks.subtitle_tracks[castSelectedSub]
    : null;
}

// ─── LANCEMENT PC (navigateur) ────────────────────────────────────────────
function launchPc() {
  if (!castMoviePending) return;
  var play     = _playItem();
  var audioIdx = _chosenAudioIdx();
  var subTrack = _chosenSubTrack();
  closeCastOptions();
  playPc(play, audioIdx, subTrack);
}

// ─── LANCEMENT CHROMECAST ─────────────────────────────────────────────────
function launchCast() {
  if (!castMoviePending) return;

  if (!castAvailable) {
    alert("Cast non disponible.\nAssure-toi d'utiliser Google Chrome avec le Chromecast sur le même réseau WiFi.");
    return;
  }

  var play     = _playItem();
  var audioIdx = _chosenAudioIdx();
  var subTrack = _chosenSubTrack();

  // Piste audio principale : le serveur sert le fichier directement
  // (ou transcode à la volée), on peut lancer tout de suite.
  if (audioIdx === 0) {
    closeCastOptions();
    doCast(play, audioIdx, subTrack);
    return;
  }

  // Piste audio alternative : le serveur doit d'abord terminer le remux
  // en MP4 (cache), sinon le Chromecast abandonnerait (timeout).
  prepareAudioThenCast(play, audioIdx, subTrack);
}
