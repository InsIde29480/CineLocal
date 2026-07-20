// ═══════════════════════════════════════════════════════════════════
// CineLocal — logique front-end
// ═══════════════════════════════════════════════════════════════════

// ─── ÉTAT GLOBAL ───────────────────────────────────────────────────
var allMovies     = [];
var currentMovie  = null;
var castSession   = null;
var castAvailable = false;
var castSdkLoaded = false;
var currentMode   = localStorage.getItem('cinelocal-mode') || 'pc';
var currentSeries = null;
var currentSeason = null;

// État Cast / options de lecture
var optionsTarget     = 'cast';   // 'pc' = navigateur, 'cast' = Chromecast, 'local' = MPV sur la TV du Pi
var castMoviePending  = null;
var castTracks        = null;
var castSelectedAudio = 0;
var castSelectedSub   = null;
var optQualities      = [];       // variantes de qualité du titre courant
var optSelectedQuality = 0;       // index dans optQualities
var optLaunchToken    = 0;        // incrémenté à chaque ouverture (anti-course)
var pendingCastMovie  = null;
var pendingAudioIdx   = null;
var pendingSubTrack   = null;

// ═══════════════════════════════════════════════════════════════════
// MODES & FILTRES
// ═══════════════════════════════════════════════════════════════════
function setMode(mode) {
  currentMode = mode;
  localStorage.setItem('cinelocal-mode', mode);
  document.querySelectorAll('.mode-btn[data-mode]').forEach(function (btn) {
    btn.classList.toggle('active', btn.dataset.mode === mode);
  });
  document.getElementById('castBtn').style.display = (mode === 'tv') ? '' : 'none';
  if (mode === 'tv' && !castSdkLoaded) loadCastSdk();
  if (mode !== 'pc') closePlayer();
  if (allMovies.length) applyFilters();
}

function applyFilters() {
  var q = (document.getElementById('search').value || '').toLowerCase().trim();
  var filtered = allMovies;
  if (q) {
    filtered = filtered.filter(function (m) {
      return m.title.toLowerCase().includes(q) ||
             (m.year || '').includes(q) ||
             m.category.toLowerCase().includes(q);
    });
  }
  renderMovies(filtered);
}

// ═══════════════════════════════════════════════════════════════════
// CHROMECAST SDK
// ═══════════════════════════════════════════════════════════════════
window['__onGCastApiAvailable'] = function (isAvailable) {
  castAvailable = isAvailable;
  console.log('[Cast] API disponible :', isAvailable);
  if (!isAvailable) return;

  var ctx = cast.framework.CastContext.getInstance();
  ctx.setOptions({
    receiverApplicationId: chrome.cast.media.DEFAULT_MEDIA_RECEIVER_APP_ID,
    autoJoinPolicy: chrome.cast.AutoJoinPolicy.ORIGIN_SCOPED,
  });

  ctx.addEventListener(
    cast.framework.CastContextEventType.SESSION_STATE_CHANGED,
    function (e) {
      var S = cast.framework.SessionState;
      if (e.sessionState === S.SESSION_STARTED || e.sessionState === S.SESSION_RESUMED) {
        castSession = ctx.getCurrentSession();
        document.getElementById('castStatus').classList.add('visible');
        // Lance le film en attente si on vient d'ouvrir une session.
        // Petit délai : juste après la connexion, le récepteur n'est pas
        // toujours prêt et le tout premier loadMedia échoue sinon.
        if (pendingCastMovie && pendingAudioIdx !== null) {
          var pMovie = pendingCastMovie, pAudio = pendingAudioIdx, pSub = pendingSubTrack;
          pendingCastMovie = null;
          pendingAudioIdx  = null;
          pendingSubTrack  = null;
          closeCastOptions();
          setTimeout(function () { sendToCast(pMovie, pAudio, pSub); }, 700);
        }
      } else if (e.sessionState === S.SESSION_ENDED) {
        castSession = null;
        document.getElementById('castStatus').classList.remove('visible');
      }
    }
  );
};

function loadCastSdk() {
  if (castSdkLoaded) return;
  castSdkLoaded = true;
  var s = document.createElement('script');
  s.src = 'https://www.gstatic.com/cv/js/sender/v1/cast_sender.js?loadCastFramework=1';
  document.head.appendChild(s);
}

function sendToCast(movie, audioIdx, subTrack, attempt) {
  if (!castSession) return;
  attempt = attempt || 1;

  // Le serveur décide du mode : direct (fichier brut), remux (MP4 cache) ou
  // HLS (transcodage). Les trois sont « reconnectables » : le Chromecast peut
  // re-demander une plage d'octets ou un segment sans perdre la session —
  // contrairement à l'ancien pipe MP4 qui plantait après ~1 h.
  var infoUrl = '/api/cast_info/' + movie.id + (audioIdx > 0 ? '?audio=' + audioIdx : '');
  fetch(infoUrl)
    .then(function (r) { return r.json(); })
    .then(function (info) {
      if (!info || info.error) {
        alert('Erreur Cast : ' + (info && info.error ? info.error : 'réponse invalide'));
        return;
      }
      launchCastMedia(movie, audioIdx, subTrack, attempt, info);
    })
    .catch(function (e) {
      console.error('[Cast] cast_info échoué :', e);
      if (attempt < 3) {
        setTimeout(function () { sendToCast(movie, audioIdx, subTrack, attempt + 1); }, 1500);
      } else {
        alert('Erreur Cast : impossible de préparer le flux');
      }
    });
}

function launchCastMedia(movie, audioIdx, subTrack, attempt, info) {
  if (!castSession) return;

  var castUrl = window.location.origin + info.url;
  var mediaInfo = new chrome.cast.media.MediaInfo(castUrl, info.mime);

  if (info.mode === 'hls') {
    mediaInfo.streamType = chrome.cast.media.StreamType.BUFFERED;
    if (chrome.cast.media.HlsSegmentFormat) {
      mediaInfo.hlsSegmentFormat = chrome.cast.media.HlsSegmentFormat.TS;
    }
  }

  mediaInfo.metadata = new chrome.cast.media.MovieMediaMetadata();
  mediaInfo.metadata.title = movie.title;
  if (movie.year)   mediaInfo.metadata.releaseDate = movie.year;
  if (movie.poster) mediaInfo.metadata.images = [new chrome.cast.Image(movie.poster)];

  var activeTrackIds = [];
  if (subTrack) {
    var track = new chrome.cast.media.Track(1, chrome.cast.media.TrackType.TEXT);
    track.trackContentId  = window.location.origin + subTrack.url;
    track.trackContentType = 'text/vtt';
    track.subtype  = chrome.cast.media.TextTrackType.SUBTITLES;
    track.name     = subTrack.label;
    track.language = subTrack.language;
    mediaInfo.tracks = [track];
    activeTrackIds.push(1);
  }

  var req = new chrome.cast.media.LoadRequest(mediaInfo);
  req.activeTrackIds = activeTrackIds;

  castSession.loadMedia(req)
    .then(function () {
      var statusText = '▶ ' + movie.title;
      if (subTrack) statusText += ' — ' + subTrack.label;
      document.getElementById('castStatusText').textContent = statusText;
      markWatched(movie.id);
      console.log('[Cast] Lecture :', movie.title);
    })
    .catch(function (e) {
      console.error('[Cast] Erreur (tentative ' + attempt + ') :', e);
      // Juste après l'ouverture de la session, le récepteur peut refuser le
      // tout premier chargement : on retente automatiquement avant d'abandonner.
      if (attempt < 3) {
        setTimeout(function () { sendToCast(movie, audioIdx, subTrack, attempt + 1); }, 1200);
        return;
      }
      alert('Erreur Cast : ' + (e.description || JSON.stringify(e)));
    });
}

// ═══════════════════════════════════════════════════════════════════
// MODAL OPTIONS (PC navigateur / Chromecast / TV directe)
// ═══════════════════════════════════════════════════════════════════
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
    // local: ['📽️ Options TV directe',   '📽️ Lire sur la TV'],
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

// Wrappers conservés pour compat (appelés ailleurs)
function startCast(movie)  { openOptions(movie, 'cast'); }
function startLocal(movie) { openOptions(movie, 'local'); }

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
  // Cast / TV : les pistes audio/sous-titres peuvent différer selon la
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
  if (optionsTarget === 'pc')         launchPc();
  else if (optionsTarget === 'local') launchLocal();
  else                                launchCast();
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

function doCast(movie, audioIdx, subTrack) {
  if (castSession) {
    sendToCast(movie, audioIdx, subTrack);
  } else {
    pendingCastMovie = movie;
    pendingAudioIdx  = audioIdx;
    pendingSubTrack  = subTrack;
    document.getElementById('castBtn').click();
  }
}

function prepareAudioThenCast(movie, audioIdx, subTrack) {
  var btn   = document.getElementById('btnCastLaunch');
  var label = document.getElementById('btnLaunchLabel');
  var myToken = optLaunchToken;
  btn.disabled = true;
  label.textContent = '⏳ Préparation de la piste…';

  // Pas encore de session : on ouvre le sélecteur de Chromecast TOUT DE
  // SUITE, tant que le clic de l'utilisateur est encore « frais ».
  if (!castSession) {
    document.getElementById('castBtn').click();
  }

  function restore() {
    label.textContent = '📺 Lancer le Cast';
    btn.disabled = false;
  }

  function fail(message) {
    restore();
    alert('Impossible de préparer la piste audio : ' + message);
  }

  function poll() {
    if (optLaunchToken !== myToken) { restore(); return; }   // modale fermée / changée

    fetch('/api/audio_status/' + movie.id + '/' + audioIdx)
      .then(function (r) { return r.json(); })
      .then(function (state) {
        if (optLaunchToken !== myToken) { restore(); return; }
        if (state.status === 'ready') {
          if (castSession) {
            restore();
            closeCastOptions();
            sendToCast(movie, audioIdx, subTrack);
          } else {
            pendingCastMovie = movie;
            pendingAudioIdx  = audioIdx;
            pendingSubTrack  = subTrack;
            btn.disabled = false;
            label.textContent = '📺 Connecter le Chromecast';
            document.getElementById('castOptionsFilm').textContent =
              movie.title + ' — piste audio prête ✓';
          }
        } else if (state.status === 'preparing') {
          var pct = Math.round((state.progress || 0) * 100);
          label.textContent = '⏳ Préparation ' + pct + '%';
          setTimeout(poll, 800);
        } else {
          fail(state.message || 'échec du remux côté serveur');
        }
      })
      .catch(function () { fail('serveur injoignable'); });
  }

  poll();
}

// ─── LANCEMENT TV DIRECTE (MPV) ───────────────────────────────────────────
function launchLocal() {
  if (!castMoviePending) return;

  var play     = _playItem();
  var audioIdx = _chosenAudioIdx();
  var subTrack = _chosenSubTrack();
  var subIdx   = subTrack ? subTrack.index : null;

  closeCastOptions();

  var url = '/play/' + play.id + '?audio=' + audioIdx;
  if (subIdx !== null) url += '&sub=' + subIdx;

  fetch(url, { method: 'POST' })
    .then(function (r) { return r.json(); })
    .then(function (data) {
      if (data.status === 'ok') {
        markWatched(play.id);
        alert('▶ Lecture sur la TV : ' + data.playing);
      } else {
        alert('Erreur : ' + (data.message || 'lecture impossible'));
      }
    })
    .catch(function (e) { alert('Erreur : ' + e); });
}

// ═══════════════════════════════════════════════════════════════════
// LECTURE PC
// ═══════════════════════════════════════════════════════════════════
function setLoadingState(text, substep, videoDone, tracksDone) {
  document.getElementById('loadingText').textContent    = text;
  document.getElementById('loadingSubstep').textContent = substep;
  document.getElementById('stepVideo').className  = 'loading-step' + (videoDone  ? ' done' : ' active');
  document.getElementById('stepTracks').className = 'loading-step' + (tracksDone ? ' done' : (videoDone ? ' active' : ''));
}

// Le choix de la piste audio en mode PC passe désormais par le menu natif du
// lecteur Chrome : le fichier est servi brut avec toutes ses pistes, donc le
// navigateur bascule instantanément, sans remuxage ni rechargement.
// (Le remuxage /stream?audio=N reste en place côté serveur : le Cast en a besoin.)

function attachSubtitleTracks(video, subtitleTracks) {
  video.querySelectorAll('track').forEach(function (t) { t.remove(); });
  subtitleTracks.forEach(function (sub) {
    var track = document.createElement('track');
    track.kind    = 'subtitles';
    track.src     = sub.url;
    track.srclang = sub.language;
    track.label   = sub.label;
    video.appendChild(track);
  });
  setTimeout(function () {
    for (var i = 0; i < video.textTracks.length; i++) {
      video.textTracks[i].mode = 'disabled';
    }
  }, 100);
}

// Trouve l'index d'une piste à partir de l'objet exact ou d'une préférence
// { language, label } mémorisée (correspondance entre variantes de qualité).
function matchSubIndex(subtitleTracks, wanted) {
  if (!wanted || !subtitleTracks || !subtitleTracks.length) return -1;
  var idx = subtitleTracks.indexOf(wanted);
  if (idx < 0 && wanted.label) {
    idx = subtitleTracks.findIndex(function (s) { return s.label === wanted.label; });
  }
  if (idx < 0 && wanted.language && wanted.language !== 'und') {
    idx = subtitleTracks.findIndex(function (s) { return s.language === wanted.language; });
  }
  return idx;
}

// Active la piste idx (ou rien si idx < 0)
function activateSubtitle(video, idx) {
  if (idx < 0) return;
  setTimeout(function () {
    for (var i = 0; i < video.textTracks.length; i++) {
      video.textTracks[i].mode = (i === idx) ? 'showing' : 'disabled';
    }
  }, 160);   // après le 'disabled' global d'attachSubtitleTracks (100 ms)
}

// ─── BOUTON CC DU LECTEUR ────────────────────────────────────────────
// Bouton « CC » toujours affiché dès qu'au moins un sous-titre existe,
// indépendamment du menu natif du navigateur. Ouvre un panneau de choix.
var _playerSubTracks = [];

function setupSubSelector(video, subtitleTracks, activeIdx) {
  var sel   = document.getElementById('subSelector');
  var panel = document.getElementById('subPanel');
  sel.classList.remove('open');
  _playerSubTracks = subtitleTracks || [];

  if (!_playerSubTracks.length) {
    sel.classList.remove('visible');
    panel.innerHTML = '';
    return;
  }

  sel.classList.add('visible');
  var html = '<div class="sub-panel-label">💬 Sous-titres</div>';
  html += '<button class="sub-option" data-sub="-1" onclick="selectPlayerSub(-1)">Aucun</button>';
  _playerSubTracks.forEach(function (s, i) {
    html += '<button class="sub-option" data-sub="' + i + '" onclick="selectPlayerSub(' + i + ')">'
      + escHtml(s.label) + '</button>';
  });
  panel.innerHTML = html;
  refreshSubUI(activeIdx);

  // Reste synchronisé si l'utilisateur change via le menu natif du lecteur
  video.textTracks.onchange = function () {
    var showing = -1;
    for (var i = 0; i < video.textTracks.length; i++) {
      if (video.textTracks[i].mode === 'showing') showing = i;
    }
    refreshSubUI(showing);
  };
}

function refreshSubUI(activeIdx) {
  document.querySelectorAll('#subPanel .sub-option').forEach(function (btn) {
    btn.classList.toggle('active', parseInt(btn.dataset.sub, 10) === activeIdx);
  });
  document.getElementById('subToggle').classList.toggle('active', activeIdx >= 0);
}

function toggleSubPanel() {
  document.getElementById('subSelector').classList.toggle('open');
}

function selectPlayerSub(i) {
  var video = document.getElementById('player-video');
  for (var k = 0; k < video.textTracks.length; k++) {
    video.textTracks[k].mode = (k === i) ? 'showing' : 'disabled';
  }
  // Devient la préférence globale mémorisée
  setSubPref(i >= 0 && _playerSubTracks[i] ? _playerSubTracks[i] : null);
  refreshSubUI(i);
  document.getElementById('subSelector').classList.remove('open');
}

// Lecture navigateur. `movie.id` est l'id de la variante de qualité choisie.
// audioIdx 0 = piste par défaut (fichier brut, instantané) ; sinon remux cache.
// subTrack = sous-titre à activer au démarrage (ou null → préférence globale).
function playPc(movie, audioIdx, subTrack) {
  currentMovie = movie;
  markWatched(movie.id);
  // Pas de sous-titre explicite : on applique la préférence globale mémorisée
  // (choisie dans la fiche film). null / « Aucun » = rien d'activé.
  if (!subTrack) subTrack = getSubPref();
  var video   = document.getElementById('player-video');
  var modal   = document.getElementById('player-modal');
  var loading = document.getElementById('playerLoading');
  var movieId = movie.id;

  document.getElementById('player-title').textContent = movie.title;
  loading.classList.remove('hidden');
  setLoadingState('Préparation du film…', 'Chargement de la vidéo et des pistes', false, false);
  video.querySelectorAll('track').forEach(function (t) { t.remove(); });
  modal.classList.add('open');
  document.body.style.overflow = 'hidden';

  function start(streamUrl) {
    var videoReady  = false;
    var tracksReady = false;

    var videoPromise = new Promise(function (resolve, reject) {
      video.addEventListener('loadedmetadata', function () {
        videoReady = true;
        setLoadingState(
          tracksReady ? 'Prêt' : 'Préparation des sous-titres…',
          tracksReady ? 'Démarrage de la lecture' : 'Extraction en cours…',
          true, tracksReady
        );
        resolve();
      }, { once: true });
      video.addEventListener('error', function () { reject(new Error('Erreur vidéo')); }, { once: true });
      video.src     = streamUrl;
      video.preload = 'auto';
      video.load();
    });

    var tracksPromise = fetch('/api/tracks/' + movieId)
      .then(function (r) { return r.json(); })
      .then(function (data) {
        tracksReady = true;
        setLoadingState(
          videoReady ? 'Prêt' : 'Chargement de la vidéo…',
          videoReady ? 'Démarrage de la lecture' : 'Vidéo en cours de chargement',
          videoReady, true
        );
        return data;
      })
      .catch(function () { return { audio_tracks: [], subtitle_tracks: [] }; });

    Promise.all([videoPromise, tracksPromise])
      .then(function (results) {
        var tracks = results[1];
        if (!currentMovie || currentMovie.id !== movieId) return;
        attachSubtitleTracks(video, tracks.subtitle_tracks);
        var activeIdx = matchSubIndex(tracks.subtitle_tracks, subTrack);
        activateSubtitle(video, activeIdx);
        setupSubSelector(video, tracks.subtitle_tracks, activeIdx);
        setLoadingState('Prêt !', 'Démarrage…', true, true);
        setTimeout(function () {
          loading.classList.add('hidden');
          video.play().catch(function () {});
        }, 200);
      })
      .catch(function (e) {
        console.error('Erreur chargement :', e);
        setLoadingState('Erreur', e.message || 'Impossible de charger le film', true, true);
      });
  }

  // Piste par défaut : fichier brut, lecture immédiate.
  if (!audioIdx) {
    start('/stream/' + movieId);
    return;
  }

  // Piste audio alternative : on attend le remux (avec progression) puis on lit.
  setLoadingState('Préparation de la piste audio…', 'Démarrage…', false, false);
  (function poll() {
    if (!currentMovie || currentMovie.id !== movieId) return;   // lecteur fermé/changé
    fetch('/api/audio_status/' + movieId + '/' + audioIdx)
      .then(function (r) { return r.json(); })
      .then(function (state) {
        if (!currentMovie || currentMovie.id !== movieId) return;
        if (state.status === 'ready') {
          start('/stream/' + movieId + '?audio=' + audioIdx);
        } else if (state.status === 'preparing') {
          var pct = Math.round((state.progress || 0) * 100);
          setLoadingState('Préparation de la piste audio…', 'Remuxage ' + pct + '%', false, false);
          setTimeout(poll, 800);
        } else {
          setLoadingState('Erreur', state.message || 'Échec de la préparation', true, true);
        }
      })
      .catch(function () {
        setLoadingState('Erreur', 'Connexion impossible', true, true);
      });
  })();
}

function closePlayer() {
  var video = document.getElementById('player-video');
  video.pause();
  video.src = '';
  video.querySelectorAll('track').forEach(function (t) { t.remove(); });
  video.textTracks.onchange = null;
  currentMovie = null;   // stoppe un éventuel polling de préparation audio
  _playerSubTracks = [];
  var sel = document.getElementById('subSelector');
  sel.classList.remove('visible');
  sel.classList.remove('open');
  document.getElementById('subPanel').innerHTML = '';
  document.getElementById('player-modal').classList.remove('open');
  document.getElementById('playerLoading').classList.remove('hidden');
  document.body.style.overflow = '';
}

// ═══════════════════════════════════════════════════════════════════
// ACTIONS
// ═══════════════════════════════════════════════════════════════════
// ═══════════════════════════════════════════════════════════════════
// SUIVI DES ÉPISODES VUS (stocké dans le navigateur, ids stables)
// ═══════════════════════════════════════════════════════════════════
function getWatchedMap() {
  try { return JSON.parse(localStorage.getItem('cinelocal-watched') || '{}'); }
  catch (e) { return {}; }
}

function isWatched(id) {
  return !!getWatchedMap()[id];
}

function markWatched(id) {
  var map = getWatchedMap();
  if (map[id]) return;
  map[id] = true;
  localStorage.setItem('cinelocal-watched', JSON.stringify(map));
}

function toggleWatched(id) {
  var map = getWatchedMap();
  if (map[id]) delete map[id];
  else         map[id] = true;
  localStorage.setItem('cinelocal-watched', JSON.stringify(map));
  renderEpisodes();
  updateSeriesMeta();
}

// ═══════════════════════════════════════════════════════════════════
// FICHE FILM (style Netflix, données TMDB)
// ═══════════════════════════════════════════════════════════════════
var currentFiche     = null;
var ficheSubTracks   = [];      // pistes de sous-titres du film affiché
var ficheSelectedSub = null;    // index dans ficheSubTracks, ou null = aucun
var ficheToken       = 0;       // anti-course sur le chargement des pistes
var ficheQualities       = [];  // versions/qualités du film affiché
var ficheSelectedQuality = 0;   // index dans ficheQualities
var ficheBaseMeta        = '';  // méta de la fiche sans la durée

// ─── Préférence GLOBALE de sous-titres (mémorisée, appliquée partout en PC) ──
function getSubPref() {
  try { return JSON.parse(localStorage.getItem('cinelocal-sub-pref') || 'null'); }
  catch (e) { return null; }
}

function setSubPref(track) {
  localStorage.setItem('cinelocal-sub-pref',
    JSON.stringify(track ? { language: track.language, label: track.label } : null));
}

function fmtSize(mb) {
  if (!mb) return '';
  return mb >= 1024 ? (Math.round(mb / 102.4) / 10 + ' Go') : (mb + ' Mo');
}

// Durée en secondes → « 1h52 » ou « 47min »
function fmtDuration(sec) {
  sec = Math.round(sec || 0);
  if (sec <= 0) return '';
  var h = Math.floor(sec / 3600);
  var m = Math.round((sec % 3600) / 60);
  if (m === 60) { return (h + 1) + 'h00'; }
  return h > 0 ? (h + 'h' + (m < 10 ? '0' : '') + m) : (m + 'min');
}

function openMovieDetails(id) {
  var m = allMovies.find(function (x) { return x.id === id && x.kind === 'movie'; });
  if (!m) return;
  currentFiche = m;
  ficheToken++;

  document.getElementById('ficheTitle').textContent = m.title;

  var best = (m.qualities && m.qualities[0]) || null;
  var has4k = !!(m.qualities || []).some(function (q) { return (q.height || 0) >= 2160; });
  var metaParts = [
    m.year,
    m.category !== 'Films' ? m.category : '',
    fmtSize(best ? best.size_mb : m.size_mb),
    has4k ? '4K' : (best && best.label) || '',
  ].filter(Boolean);
  ficheBaseMeta = metaParts.join(' · ');   // la durée s'ajoute au chargement des pistes
  document.getElementById('ficheMeta').textContent = ficheBaseMeta;

  var ov = document.getElementById('ficheOverview');
  ov.textContent = m.overview || '';
  ov.style.display = m.overview ? '' : 'none';

  // Image de fond : backdrop TMDB, sinon grande initiale en filigrane
  var container = document.getElementById('ficheContainer');
  var backdrop  = document.getElementById('ficheBackdrop');
  var letter    = document.getElementById('ficheLetter');
  var bg = m.backdrop || m.poster;
  if (bg) {
    container.classList.remove('no-image');
    backdrop.style.backgroundImage = 'url(' + bg + ')';
    letter.textContent = '';
  } else {
    container.classList.add('no-image');
    backdrop.style.backgroundImage = '';
    letter.textContent = (m.title || '?').charAt(0).toUpperCase();
  }

  // Versions/qualités : sélecteur (4K / 1080p / …) avec résolution, taille et
  // format pour distinguer chaque version, même quand les libellés sont vides.
  ficheQualities = (m.qualities && m.qualities.length)
    ? m.qualities
    : [{ id: m.id, label: '', ext: m.ext, size_mb: m.size_mb }];
  ficheSelectedQuality = 0;
  renderFicheVersions();

  // Boutons : Lire (la version choisie) + Chromecast (avec la version choisie)
  var btns = '<button class="btn-fiche primary" onclick="fichePlay()">▶ Lire</button>';
  btns += '<button class="btn-fiche ghost" onclick="ficheCast()">📺 Chromecast</button>';
  // btns += '<button class="btn-fiche ghost" onclick="ficheLocal()">📽️ TV directe</button>';
  document.getElementById('ficheButtons').innerHTML = btns;

  // Sous-titres de la version sélectionnée (rechargés si on change de version).
  loadFicheSubs(_ficheSelectedQ().id);

  document.getElementById('movie-modal').classList.add('open');
  document.body.style.overflow = 'hidden';
}

function renderFicheSubs() {
  var el = document.getElementById('ficheSubs');
  if (!ficheSubTracks.length) {
    el.style.display = 'none';
    el.innerHTML = '';
    return;
  }
  el.style.display = '';
  var html = '<span class="fiche-subs-label">💬 Sous-titres</span>';
  html += '<button class="sub-chip' + (ficheSelectedSub === null ? ' active' : '')
    + '" onclick="selectFicheSub(null)">Aucun</button>';
  ficheSubTracks.forEach(function (s, i) {
    html += '<button class="sub-chip' + (ficheSelectedSub === i ? ' active' : '')
      + '" onclick="selectFicheSub(' + i + ')">' + escHtml(s.label) + '</button>';
  });
  el.innerHTML = html;
}

function selectFicheSub(i) {
  ficheSelectedSub = i;
  // Devient la préférence globale : appliquée aussi aux prochains films/épisodes
  setSubPref(i === null ? null : ficheSubTracks[i]);
  renderFicheSubs();
}

function closeMovieModal() {
  ficheToken++;   // stoppe un éventuel chargement de pistes en cours
  document.getElementById('movie-modal').classList.remove('open');
  document.body.style.overflow = '';
}

// Libellé court d'une version : « 4K », « 1080p », « HD »… (sans taille ni format)
function _qualityLabel(q) {
  if (q.label) return q.label;
  if (q.height) return q.height + 'p';
  return 'HD';
}

function renderFicheVersions() {
  var el = document.getElementById('ficheVersions');
  if (!ficheQualities || ficheQualities.length <= 1) {
    el.style.display = 'none';
    el.innerHTML = '';
    return;
  }
  el.style.display = '';
  var html = '<span class="fiche-subs-label">🎞️ Version</span>';
  ficheQualities.forEach(function (q, i) {
    html += '<button class="sub-chip' + (ficheSelectedQuality === i ? ' active' : '')
      + '" onclick="selectFicheVersion(' + i + ')">' + escHtml(_qualityLabel(q)) + '</button>';
  });
  el.innerHTML = html;
}

function selectFicheVersion(i) {
  if (i === ficheSelectedQuality) return;
  ficheSelectedQuality = i;
  renderFicheVersions();
  // Les sous-titres peuvent différer d'une version à l'autre : on les recharge.
  loadFicheSubs(_ficheSelectedQ().id);
}

function _ficheSelectedQ() {
  return ficheQualities[ficheSelectedQuality] || ficheQualities[0] || currentFiche;
}

// Charge les sous-titres d'une version (id de fichier) et remplit la fiche.
function loadFicheSubs(qid) {
  ficheToken++;                       // invalide un chargement précédent
  var myToken = ficheToken;
  ficheSubTracks   = [];
  ficheSelectedSub = null;
  var subsEl = document.getElementById('ficheSubs');
  subsEl.style.display = '';
  subsEl.innerHTML = '<span class="fiche-subs-label">💬 Sous-titres</span>'
    + '<span class="fiche-subs-loading">chargement…</span>';

  fetch('/api/tracks/' + qid)
    .then(function (r) { return r.json(); })
    .then(function (data) {
      if (ficheToken !== myToken) return;   // version/fiche changée entre-temps
      if (data.duration) {
        document.getElementById('ficheMeta').textContent =
          ficheBaseMeta + (ficheBaseMeta ? ' · ' : '') + fmtDuration(data.duration);
      }
      ficheSubTracks = data.subtitle_tracks || [];
      // Pré-sélectionne la préférence globale mémorisée si elle correspond
      var pref = getSubPref();
      if (pref) {
        var i = ficheSubTracks.findIndex(function (s) { return s.label === pref.label; });
        if (i < 0 && pref.language && pref.language !== 'und') {
          i = ficheSubTracks.findIndex(function (s) { return s.language === pref.language; });
        }
        ficheSelectedSub = (i >= 0) ? i : null;
      }
      renderFicheSubs();
    })
    .catch(function () {
      if (ficheToken !== myToken) return;
      ficheSubTracks = [];
      renderFicheSubs();
    });
}

// Lecture navigateur de la version choisie. Le sous-titre sélectionné dans la
// fiche est activé au démarrage ; l'audio reste géré par le lecteur natif.
function fichePlay() {
  if (!currentFiche) return;
  var m = currentFiche;
  var q = _ficheSelectedQ();
  var play = Object.assign({}, m, { id: q.id, ext: q.ext || m.ext });
  var subTrack = (ficheSelectedSub !== null) ? ficheSubTracks[ficheSelectedSub] : null;
  closeMovieModal();
  playPc(play, 0, subTrack);
}

function ficheCast() {
  if (!currentFiche) return;
  var m = currentFiche;
  var qi = ficheSelectedQuality;
  closeMovieModal();
  openOptions(m, 'cast', qi);
}

function ficheLocal() {
  if (!currentFiche) return;
  var m = currentFiche;
  closeMovieModal();
  openOptions(m, 'local');
}

// ═══════════════════════════════════════════════════════════════════
// SÉRIES
// ═══════════════════════════════════════════════════════════════════
function openSeries(seriesId) {
  var series = allMovies.find(function (x) { return x.id === seriesId && x.kind === 'series'; });
  if (!series) return;
  currentSeries = series;
  document.getElementById('seriesModalTitle').textContent = series.title;
  updateSeriesMeta();
  document.getElementById('seriesModalOverview').textContent = series.overview || '';

  var headerBg = series.backdrop || series.poster;
  document.getElementById('seriesHeader').style.backgroundImage =
    headerBg ? ('url(' + headerBg + ')') : 'linear-gradient(135deg, var(--bg3), var(--surface))';

  var seasons = [...new Set(series.episodes.map(function (e) { return e.season; }))].sort(function (a, b) { return a - b; });
  currentSeason = seasons[0];
  document.getElementById('seasonTabs').innerHTML = seasons.map(function (s) {
    return '<button class="season-tab ' + (s === currentSeason ? 'active' : '') +
           '" onclick="selectSeason(' + s + ')">Saison ' + String(s).padStart(2, '0') + '</button>';
  }).join('');
  renderEpisodes();
  document.getElementById('series-modal').classList.add('open');
  document.body.style.overflow = 'hidden';
}

function selectSeason(season) {
  currentSeason = season;
  document.querySelectorAll('.season-tab').forEach(function (t) {
    t.classList.toggle('active', t.textContent.includes(String(season).padStart(2, '0')));
  });
  renderEpisodes();
}

function updateSeriesMeta() {
  if (!currentSeries) return;
  var watchedCount = currentSeries.episodes.filter(function (e) { return isWatched(e.id); }).length;
  document.getElementById('seriesModalMeta').textContent =
    currentSeries.season_count + ' saison' + (currentSeries.season_count > 1 ? 's' : '') +
    ' · ' + currentSeries.episode_count + ' épisodes' +
    ' · ' + watchedCount + '/' + currentSeries.episode_count + ' vus';
}

function renderEpisodes() {
  if (!currentSeries) return;
  var eps      = currentSeries.episodes.filter(function (e) { return e.season === currentSeason; });
  var btnLabel = currentMode === 'tv' ? '📺 Caster' : currentMode === 'local' ? '📽️ TV' : '▶ Lire';

  // Prochain épisode à voir : premier non vu de toute la série
  // (les épisodes sont déjà triés saison/épisode côté serveur).
  var nextEp = currentSeries.episodes.find(function (e) { return !isWatched(e.id); });
  var nextId = nextEp ? nextEp.id : null;

  document.getElementById('episodesList').innerHTML = eps.map(function (ep) {
    var watched = isWatched(ep.id);
    var nextTag = (ep.id === nextId) ? '<span class="ep-next-tag">À suivre</span>' : '';
    return '<div class="episode-row' + (watched ? ' watched' : '') + '" onclick="playEpisode(\'' + ep.id + '\')">'
      + '<button class="ep-check' + (watched ? ' watched' : '') + '" title="' + (watched ? 'Marquer non vu' : 'Marquer vu') + '"'
      + ' onclick="event.stopPropagation();toggleWatched(\'' + ep.id + '\')">✓</button>'
      + '<div class="episode-num">E' + String(ep.episode).padStart(2, '0') + '</div>'
      + '<div class="episode-info">'
      + '<div class="episode-title">Épisode ' + ep.episode + nextTag + '</div>'
      + '<div class="episode-meta">' + ep.size_mb + ' Mo · ' + ep.ext.replace('.', '').toUpperCase()
      + (watched ? ' · <span class="ep-seen">✓ vu</span>' : '') + '</div>'
      + '</div>'
      + '<button class="episode-play" onclick="event.stopPropagation();playEpisode(\'' + ep.id + '\')">' + btnLabel + '</button>'
      + '</div>';
  }).join('');
}

function playEpisode(episodeId) {
  if (!currentSeries) return;
  var ep = currentSeries.episodes.find(function (e) { return e.id === episodeId; });
  if (!ep) return;
  var fakeMovie = Object.assign({}, ep, {
    title: currentSeries.title + ' - S' + String(ep.season).padStart(2, '0') + 'E' + String(ep.episode).padStart(2, '0'),
    year: null,
    poster: currentSeries.poster,
  });
  closeSeriesModal();
  if (currentMode === 'tv')         openOptions(fakeMovie, 'cast');
  else if (currentMode === 'local') openOptions(fakeMovie, 'local');
  else                              playPc(fakeMovie, 0, null);
}

function closeSeriesModal() {
  document.getElementById('series-modal').classList.remove('open');
  document.body.style.overflow = '';
}

// ═══════════════════════════════════════════════════════════════════
// CHARGEMENT & RENDU
// ═══════════════════════════════════════════════════════════════════
function loadMovies() {
  fetch('/api/movies')
    .then(function (r) { return r.json(); })
    .then(function (data) {
      allMovies = data;
      applyFilters();
      startHeroSlideshow();
    })
    .catch(function () {
      document.getElementById('loading').innerHTML =
        '<div style="color:var(--red)">Impossible de joindre le serveur Flask.</div>';
    });
}

function refreshMovies() {
  document.getElementById('loading').style.display = 'block';
  document.getElementById('movies-container').style.display = 'none';
  fetch('/api/movies/refresh')
    .then(function () { return loadMovies(); });
}

function renderMovies(movies) {
  document.getElementById('loading').style.display = 'none';
  var container = document.getElementById('movies-container');
  container.style.display = '';
  if (movies.length === 0) {
    document.getElementById('empty').style.display = 'block';
    container.innerHTML = '';
    return;
  }
  document.getElementById('empty').style.display = 'none';

  var byCategory = {};
  movies.forEach(function (m) {
    if (!byCategory[m.category]) byCategory[m.category] = [];
    byCategory[m.category].push(m);
  });

  // Tri alphabétique par titre dans chaque catégorie (indépendamment de la
  // qualité ou du disque d'origine).
  var collator = new Intl.Collator('fr', { sensitivity: 'base', numeric: true });
  Object.keys(byCategory).forEach(function (cat) {
    byCategory[cat].sort(function (a, b) { return collator.compare(a.title, b.title); });
  });

  container.innerHTML = Object.entries(byCategory).sort(function (a, b) {
    return collator.compare(a[0], b[0]);
  }).map(function (entry) {
    var cat = entry[0], films = entry[1];
    return '<section class="category-section">'
      + '<div class="category-title">' + cat
      + '<span class="category-count">' + films.length + ' titres</span></div>'
      + '<div class="movies-row">' + films.map(movieCard).join('') + '</div>'
      + '</section>';
  }).join('');
}

function movieCard(m) {
  var isSeries = m.kind === 'series';
  var meta     = isSeries
    ? (m.season_count + ' saison' + (m.season_count > 1 ? 's' : '') + ' · ' + m.episode_count + ' ép.')
    : ((m.year || '') + ' · ' + m.size_mb + ' Mo');
  var action   = isSeries
    ? ("event.stopPropagation();openSeries('" + m.id + "')")
    : ("event.stopPropagation();openMovieDetails('" + m.id + "')");
  var cardClick   = isSeries ? ("openSeries('" + m.id + "')") : ("openMovieDetails('" + m.id + "')");
  var actionLabel = isSeries ? '📂 Voir épisodes' : 'ℹ Détails';
  var poster = m.poster
    ? ('<img class="card-thumb" src="' + m.poster + '" alt="' + escHtml(m.title) + '" loading="lazy"'
       + ' onerror="this.style.display=\'none\';this.nextElementSibling.style.display=\'flex\'" />')
    : '';
  // Badge qualité : affiché si une variante 4K/2160p existe pour ce film
  var qualityBadge = '';
  if (!isSeries && m.qualities && m.qualities.length) {
    var has4k = m.qualities.some(function (q) { return (q.height || 0) >= 2160; });
    if (has4k) qualityBadge = '<div class="quality-badge">4K</div>';
  }
  return '<div class="movie-card" onclick="' + cardClick + '">'
    + poster
    + '<div class="card-placeholder" style="display:' + (m.poster ? 'none' : 'flex') + '">'
    + (isSeries ? '📺' : '🎬') + '<span>' + escHtml(m.title.substring(0, 30)) + '</span></div>'
    + qualityBadge
    + (isSeries ? '<div class="series-badge">SÉRIE</div>' : '')
    + '<div class="card-info">'
    + '<div class="card-title">' + escHtml(m.title) + '</div>'
    + '<div class="card-meta">' + meta + '</div>'
    + '<div class="card-actions">'
    + '<button class="card-btn" onclick="' + action + '">' + actionLabel + '</button>'
    + '</div></div></div>';
}

// ═══════════════════════════════════════════════════════════════════
// FOND ANIMÉ (diaporama aléatoire des films)
// ═══════════════════════════════════════════════════════════════════
var heroSlideTimer = null;
var heroShuffled   = [];
var heroPos        = 0;
var heroActiveLayer = 0;   // 0 ou 1 : couche actuellement visible

function _shuffle(arr) {
  var a = arr.slice();
  for (var i = a.length - 1; i > 0; i--) {
    var j = Math.floor(Math.random() * (i + 1));
    var t = a[i]; a[i] = a[j]; a[j] = t;
  }
  return a;
}

function startHeroSlideshow() {
  if (heroSlideTimer) { clearInterval(heroSlideTimer); heroSlideTimer = null; }

  // On ne garde que les films avec une image utilisable
  var withImg = allMovies.filter(function (m) { return m.backdrop || m.poster; });
  if (withImg.length === 0) return;

  heroShuffled = _shuffle(withImg);
  heroPos = 0;

  nextHeroSlide();                       // première image tout de suite
  if (heroShuffled.length > 1) {
    heroSlideTimer = setInterval(nextHeroSlide, 8000);   // puis toutes les 8 s
  }
}

function nextHeroSlide() {
  if (heroShuffled.length === 0) return;
  var m  = heroShuffled[heroPos % heroShuffled.length];
  heroPos++;
  // Quand on a fait le tour, on re-mélange pour un nouvel ordre
  if (heroPos % heroShuffled.length === 0) {
    heroShuffled = _shuffle(heroShuffled);
  }

  var bg = m.backdrop || m.poster;
  if (!bg) return;

  // Précharge l'image avant de l'afficher (évite un flash blanc)
  var img = new Image();
  img.onload = function () {
    var layers = document.querySelectorAll('.hero-layer');
    var current = layers[heroActiveLayer];
    var nextIdx = heroActiveLayer === 0 ? 1 : 0;
    var next    = layers[nextIdx];
    next.style.backgroundImage = 'url(' + bg + ')';
    next.classList.add('visible');
    current.classList.remove('visible');
    heroActiveLayer = nextIdx;
  };
  img.src = bg;
}

// ═══════════════════════════════════════════════════════════════════
// EXTRACTION EN MASSE DES SOUS-TITRES (fenêtre de progression)
// ═══════════════════════════════════════════════════════════════════
var _subsPollTimer = null;
var _lastSubsState = null;
// État ouvert/fermé des menus déroulants (conservé entre deux rafraîchissements)
var _subsDropOpen  = { nosubs: false, fails: true };

function openSubsModal() {
  document.getElementById('subs-modal').classList.add('open');
  document.body.style.overflow = 'hidden';
  // Affiche l'état courant tout de suite, puis rafraîchit en continu.
  fetchSubsStatus();
  startSubsPolling();
}

function closeSubsModal() {
  stopSubsPolling();
  document.getElementById('subs-modal').classList.remove('open');
  document.body.style.overflow = '';
}

function startSubsPolling() {
  if (_subsPollTimer) return;
  _subsPollTimer = setInterval(fetchSubsStatus, 1000);
}

function stopSubsPolling() {
  if (_subsPollTimer) { clearInterval(_subsPollTimer); _subsPollTimer = null; }
}

function fetchSubsStatus() {
  fetch('/api/subtitles/status')
    .then(function (r) { return r.json(); })
    .then(function (state) {
      renderSubsState(state);
      // Plus rien à surveiller une fois l'extraction terminée : on arrête le
      // rafraîchissement mais on garde le dernier état affiché.
      if (!state.running) stopSubsPolling();
    })
    .catch(function () {
      document.getElementById('subsPhase').textContent = 'Serveur injoignable';
    });
}

// Lance (ou relance) l'extraction côté serveur.
//   'new'   → vérifie tout, saute ce qui est déjà complet
//   'retry' → ne reprend que les échecs et les films sans sous-titre
//   'force' → purge tous les caches puis tout ré-extrait
function startSubsExtraction(mode) {
  mode = mode || 'new';
  if (mode === 'force' && !confirm('Purger TOUS les caches de sous-titres et tout ré-extraire ?\nCela peut être long sur un disque dur.')) return;
  _setSubsButtonsDisabled(true);
  fetch('/api/subtitles/scan?mode=' + mode, { method: 'POST' })
    .then(function (r) { return r.json(); })
    .then(function (state) {
      renderSubsState(state);
      startSubsPolling();
    })
    .catch(function () {
      document.getElementById('subsPhase').textContent = 'Impossible de lancer l’extraction';
      _setSubsButtonsDisabled(false);
    });
}

function _setSubsButtonsDisabled(disabled) {
  ['subsStartBtn', 'subsRetryBtn', 'subsForceBtn', 'subsDlBtn'].forEach(function (id) {
    var el = document.getElementById(id);
    if (el) el.disabled = disabled;
  });
}

function renderSubsState(s) {
  _lastSubsState = s;
  var total   = s.total || 0;
  var done    = s.done || 0;
  var pct     = total ? Math.round((done / total) * 100) : (s.running ? 0 : 0);

  document.getElementById('subsBar').style.width  = pct + '%';
  document.getElementById('subsPct').textContent  = pct + '%';
  document.getElementById('subsStatDone').textContent  = done;
  document.getElementById('subsStatTotal').textContent = total;
  document.getElementById('subsStatWith').textContent  = s.with_subs || 0;
  document.getElementById('subsStatNone').textContent  = s.no_subs || 0;
  document.getElementById('subsStatFail').textContent  = s.failed || 0;
  document.getElementById('subsStatDl').textContent    = s.downloaded || 0;

  // Phase / message d'état
  var phase = document.getElementById('subsPhase');
  if (s.running) {
    phase.textContent = 'Extraction en cours… (' + done + '/' + total + ')';
  } else if (s.finished_at) {
    var msg = 'Terminé — ' + (s.with_subs || 0) + ' avec sous-titres, '
      + (s.failed || 0) + ' échec' + ((s.failed || 0) > 1 ? 's' : '');
    if (s.downloaded) msg += ', ' + s.downloaded + ' téléchargé' + (s.downloaded > 1 ? 's' : '');
    phase.textContent = msg;
  } else {
    phase.textContent = 'Aucune extraction lancée pour l’instant';
  }

  // Fichier en cours
  document.getElementById('subsCurrent').textContent = s.running && s.current ? s.current : '';

  // Boutons
  _setSubsButtonsDisabled(!!s.running);

  // Menus déroulants : films sans sous-titre + échecs (avec la raison)
  renderSubsDropdowns(s);
}

// Bascule un menu déroulant ouvert/fermé (état conservé au rafraîchissement)
function toggleSubsDrop(which) {
  _subsDropOpen[which] = !_subsDropOpen[which];
  renderSubsDropdowns(_lastSubsState);
}

function renderSubsDropdowns(s) {
  var el = document.getElementById('subsFailures');
  if (!s) { el.innerHTML = ''; return; }

  var noSubs = s.no_subs_files || [];
  var fails  = s.failures || [];

  var noSubsLabel = (s.mode === 'download')
    ? '💬 Films sans sous-titre français'
    : '💬 Films sans sous-titre';

  var html = '';
  html += _subsDropSection('nosubs', noSubsLabel, noSubs, true);
  html += _subsDropSection('fails',  '⚠ Échecs',   fails,  true);

  // Rien à signaler une fois l'extraction terminée
  if (!noSubs.length && !fails.length) {
    html = (!s.running && s.finished_at)
      ? '<div class="subs-empty-msg">✓ ' + (s.mode === 'download'
          ? 'Tous les films analysés ont un sous-titre français.'
          : 'Tous les films analysés ont au moins un sous-titre.') + '</div>'
      : '';
  }
  el.innerHTML = html;
}

// Construit une section repliable. `withReasons` = afficher la raison d'échec.
function _subsDropSection(key, label, items, withReasons) {
  if (!items.length) return '';
  var open   = !!_subsDropOpen[key];
  var caret  = open ? '▾' : '▸';
  var rows   = items.map(function (it) {
    var reasons = withReasons
      ? (it.reasons || []).map(function (r) { return escHtml(r); }).join('<br>')
      : '';
    return '<div class="subs-fail-row">'
      + '<div class="subs-fail-name">' + escHtml(it.filename || it.title || '?') + '</div>'
      + (reasons ? '<div class="subs-fail-reason">' + reasons + '</div>' : '')
      + '</div>';
  }).join('');

  return '<div class="subs-drop ' + (withReasons ? 'err' : '') + '">'
    + '<button class="subs-drop-head" onclick="toggleSubsDrop(\'' + key + '\')">'
    +   '<span class="subs-drop-caret">' + caret + '</span>'
    +   '<span class="subs-drop-label">' + label + '</span>'
    +   '<span class="subs-drop-count">' + items.length + '</span>'
    + '</button>'
    + '<div class="subs-drop-body" style="display:' + (open ? 'flex' : 'none') + '">'
    +   rows
    + '</div>'
    + '</div>';
}

// ═══════════════════════════════════════════════════════════════════
// PARAMÈTRES (clés API TMDB / OpenSubtitles)
// ═══════════════════════════════════════════════════════════════════
function openSettingsModal() {
  document.getElementById('settings-modal').classList.add('open');
  document.body.style.overflow = 'hidden';
  document.getElementById('setStatus').textContent = '';
  fetch('/api/settings')
    .then(function (r) { return r.json(); })
    .then(function (s) {
      document.getElementById('setMoviesDirs').value = (s.movies_dirs || []).join('\n');
      document.getElementById('setTracksDir').value = s.tracks_cache_dir || '';
      document.getElementById('setTmdbKey').value = s.tmdb_api_key || '';
      document.getElementById('setOsKey').value   = s.opensubtitles_api_key || '';
      document.getElementById('setOsUser').value  = s.opensubtitles_username || '';
      document.getElementById('setOsLangs').value = s.opensubtitles_langs || 'fr, en';
      document.getElementById('setAutoEnabled').checked = s.auto_scan_enabled !== false;
      document.getElementById('setAutoInterval').value  = s.auto_scan_interval_minutes || 60;
      var pass = document.getElementById('setOsPass');
      pass.value = '';
      pass.placeholder = s.opensubtitles_password_set
        ? '•••• (enregistré — laisser vide pour garder)'
        : 'mot de passe';
      var missing = (s.movies_dirs_status || []).filter(function (d) { return !d.exists; });
      if (missing.length) {
        var st = document.getElementById('setStatus');
        st.className = 'set-status err';
        st.textContent = '⚠ Dossier(s) introuvable(s) : ' + missing.map(function (d) { return d.path; }).join(' · ');
      }
    })
    .catch(function () {
      document.getElementById('setStatus').textContent = 'Impossible de charger les paramètres.';
    });
}

function closeSettingsModal() {
  document.getElementById('settings-modal').classList.remove('open');
  document.body.style.overflow = '';
}

function saveSettings() {
  var btn = document.getElementById('setSaveBtn');
  var status = document.getElementById('setStatus');
  btn.disabled = true;
  status.className = 'set-status';
  status.textContent = 'Enregistrement…';

  var payload = {
    movies_dirs:            document.getElementById('setMoviesDirs').value,
    tracks_cache_dir:       document.getElementById('setTracksDir').value,
    tmdb_api_key:           document.getElementById('setTmdbKey').value,
    opensubtitles_api_key:  document.getElementById('setOsKey').value,
    opensubtitles_username: document.getElementById('setOsUser').value,
    opensubtitles_langs:    document.getElementById('setOsLangs').value,
    opensubtitles_password: document.getElementById('setOsPass').value,
    auto_scan_enabled:          document.getElementById('setAutoEnabled').checked,
    auto_scan_interval_minutes: document.getElementById('setAutoInterval').value,
  };

  fetch('/api/settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
    .then(function (r) { return r.json(); })
    .then(function (res) {
      btn.disabled = false;
      if (res.status === 'ok') {
        document.getElementById('setOsPass').value = '';
        var missing = (res.movies_dirs_status || []).filter(function (d) { return !d.exists; });
        if (res.paths_changed && missing.length) {
          status.className = 'set-status err';
          status.textContent = '✓ Enregistré, mais introuvable : ' + missing.map(function (d) { return d.path; }).join(' · ');
        } else {
          status.className = 'set-status ok';
          status.textContent = res.paths_changed
            ? '✓ Enregistré — bibliothèque en cours de rechargement…'
            : '✓ Paramètres enregistrés.';
        }
        // Un changement de chemin relance le scan : on recharge le catalogue.
        if (res.paths_changed) loadMovies();
      } else {
        status.className = 'set-status err';
        status.textContent = '✗ Échec de l’enregistrement (voir les logs serveur).';
      }
    })
    .catch(function () {
      btn.disabled = false;
      status.className = 'set-status err';
      status.textContent = '✗ Serveur injoignable.';
    });
}

// ═══════════════════════════════════════════════════════════════════
// UTILITAIRE
// ═══════════════════════════════════════════════════════════════════
function escHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// ═══════════════════════════════════════════════════════════════════
// ÉVÉNEMENTS & INIT
// ═══════════════════════════════════════════════════════════════════
document.getElementById('search').addEventListener('input', applyFilters);

document.getElementById('player-modal').addEventListener('click', function (e) {
  if (e.target === document.getElementById('player-modal')) closePlayer();
});
document.getElementById('series-modal').addEventListener('click', function (e) {
  if (e.target === document.getElementById('series-modal')) closeSeriesModal();
});
document.getElementById('cast-options-modal').addEventListener('click', function (e) {
  if (e.target === document.getElementById('cast-options-modal')) closeCastOptions();
});
document.getElementById('subs-modal').addEventListener('click', function (e) {
  if (e.target === document.getElementById('subs-modal')) closeSubsModal();
});
document.getElementById('settings-modal').addEventListener('click', function (e) {
  if (e.target === document.getElementById('settings-modal')) closeSettingsModal();
});
document.addEventListener('keydown', function (e) {
  if (e.key === 'Escape') { closePlayer(); closeSeriesModal(); closeCastOptions(); closeMovieModal(); closeSubsModal(); closeSettingsModal(); }
});

setMode(currentMode);
loadMovies();
