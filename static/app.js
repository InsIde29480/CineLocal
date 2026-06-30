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

  var castUrl = (audioIdx > 0)
    ? (window.location.origin + '/cast/' + movie.id + '?audio=' + audioIdx)
    : (window.location.origin + '/cast/' + movie.id);

  var mimeMap = { '.mp4': 'video/mp4', '.mkv': 'video/x-matroska', '.webm': 'video/webm' };
  // Avec une piste audio alternative, le serveur renvoie toujours un MP4
  // remuxé : on annonce donc video/mp4 au Chromecast, pas le conteneur
  // d'origine (sinon un .mkv source ferait échouer le chargement).
  var mime = (audioIdx > 0) ? 'video/mp4' : (mimeMap[movie.ext] || 'video/mp4');

  var mediaInfo = new chrome.cast.media.MediaInfo(castUrl, mime);
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
function openOptions(movie, target) {
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
  optSelectedQuality = 0;

  var titles = {
    pc:    ['🎞️ Choix de la qualité', '▶ Lire'],
    cast:  ['📺 Options Cast',         '📺 Lancer le Cast'],
    local: ['📽️ Options TV directe',   '📽️ Lire sur la TV'],
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

// Active le sous-titre choisi dans la modale (sinon le menu natif reste dispo).
function activateSubtitle(video, subtitleTracks, subTrack) {
  if (!subTrack) return;
  var idx = subtitleTracks.indexOf(subTrack);
  if (idx < 0) return;
  setTimeout(function () {
    for (var i = 0; i < video.textTracks.length; i++) {
      video.textTracks[i].mode = (i === idx) ? 'showing' : 'disabled';
    }
  }, 160);   // après le 'disabled' global d'attachSubtitleTracks (100 ms)
}

// Lecture navigateur. `movie.id` est l'id de la variante de qualité choisie.
// audioIdx 0 = piste par défaut (fichier brut, instantané) ; sinon remux cache.
// subTrack = sous-titre à activer au démarrage (ou null).
function playPc(movie, audioIdx, subTrack) {
  currentMovie = movie;
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
        activateSubtitle(video, tracks.subtitle_tracks, subTrack);
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
  currentMovie = null;   // stoppe un éventuel polling de préparation audio
  document.getElementById('player-modal').classList.remove('open');
  document.getElementById('playerLoading').classList.remove('hidden');
  document.body.style.overflow = '';
}

// ═══════════════════════════════════════════════════════════════════
// ACTIONS
// ═══════════════════════════════════════════════════════════════════
function playMovie(id) {
  var m = allMovies.find(function (x) { return x.id === id; });
  if (!m) return;
  if (currentMode === 'local')   openOptions(m, 'local');
  else if (currentMode === 'tv') openOptions(m, 'cast');
  else {
    // PC : popup uniquement s'il existe plusieurs qualités (ex. 4K + HD),
    // juste pour choisir la qualité. Sinon lecture directe (audio et
    // sous-titres gérés par le lecteur du navigateur).
    if (m.qualities && m.qualities.length > 1) openOptions(m, 'pc');
    else                                       playPc(m, 0, null);
  }
}

function openMovie(id) {
  playMovie(id);
}

// ═══════════════════════════════════════════════════════════════════
// SÉRIES
// ═══════════════════════════════════════════════════════════════════
function openSeries(seriesId) {
  var series = allMovies.find(function (x) { return x.id === seriesId && x.kind === 'series'; });
  if (!series) return;
  currentSeries = series;
  document.getElementById('seriesModalTitle').textContent = series.title;
  document.getElementById('seriesModalMeta').textContent  =
    series.season_count + ' saison' + (series.season_count > 1 ? 's' : '') +
    ' · ' + series.episode_count + ' épisodes';
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

function renderEpisodes() {
  if (!currentSeries) return;
  var eps      = currentSeries.episodes.filter(function (e) { return e.season === currentSeason; });
  var btnLabel = currentMode === 'tv' ? '📺 Caster' : currentMode === 'local' ? '📽️ TV' : '▶ Lire';
  document.getElementById('episodesList').innerHTML = eps.map(function (ep) {
    return '<div class="episode-row" onclick="playEpisode(\'' + ep.id + '\')">'
      + '<div class="episode-num">E' + String(ep.episode).padStart(2, '0') + '</div>'
      + '<div class="episode-info">'
      + '<div class="episode-title">Épisode ' + ep.episode + '</div>'
      + '<div class="episode-meta">' + ep.size_mb + ' Mo · ' + ep.ext.replace('.', '').toUpperCase() + '</div>'
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

  container.innerHTML = Object.entries(byCategory).map(function (entry) {
    var cat = entry[0], films = entry[1];
    return '<section class="category-section">'
      + '<div class="category-title">' + cat
      + '<span class="category-count">' + films.length + ' titres</span></div>'
      + '<div class="movies-row">' + films.map(movieCard).join('') + '</div>'
      + '</section>';
  }).join('');
}

function movieCard(m) {
  var ext      = m.ext.replace('.', '').toUpperCase();
  var isSeries = m.kind === 'series';
  var btnLabel = currentMode === 'tv' ? '📺 Caster' : currentMode === 'local' ? '📽️ TV' : '▶ Lire';
  var meta     = isSeries
    ? (m.season_count + ' saison' + (m.season_count > 1 ? 's' : '') + ' · ' + m.episode_count + ' ép.')
    : ((m.year || '') + ' · ' + m.size_mb + ' Mo');
  var action   = isSeries
    ? ("event.stopPropagation();openSeries('" + m.id + "')")
    : ("event.stopPropagation();playMovie('" + m.id + "')");
  var cardClick   = isSeries ? ("openSeries('" + m.id + "')") : ("openMovie('" + m.id + "')");
  var actionLabel = isSeries ? '📂 Voir épisodes' : btnLabel;
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
    + '<div class="card-badge">' + ext + '</div>'
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
// UTILITAIRE
// ═══════════════════════════════════════════════════════════════════
function escHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// ═══════════════════════════════════════════════════════════════════
// PRÉ-EXTRACTION DES SOUS-TITRES (modal de suivi)
// ═══════════════════════════════════════════════════════════════════
var extractionPollTimer = null;
var extractionBackgroundTimer = null;

function fmtElapsed(s) {
  if (!s || s < 0) return '0s';
  if (s < 60) return s + 's';
  var m = Math.floor(s / 60), r = s % 60;
  if (m < 60) return m + 'm ' + r + 's';
  var h = Math.floor(m / 60); m = m % 60;
  return h + 'h ' + m + 'm';
}

function applyExtractionStatus(st, modalOpen) {
  var btn = document.getElementById('btnExtraction');
  if (btn) {
    btn.classList.toggle('active', !!st.in_progress);
    btn.classList.toggle('has-failures', !st.in_progress && (st.failed || 0) > 0);
  }
  if (!modalOpen) return;

  var pct = Math.round((st.progress || 0) * 100);
  document.getElementById('extractionFill').style.width = pct + '%';
  document.getElementById('extractionProgressText').textContent = pct + ' %';
  document.getElementById('extractionDone').textContent     = st.done || 0;
  document.getElementById('extractionPending').textContent  = st.pending || 0;
  document.getElementById('extractionFailed').textContent   = st.failed || 0;
  document.getElementById('extractionTotal').textContent    = st.total || 0;
  document.getElementById('extractionCurrent').textContent  = st.current || '—';
  document.getElementById('extractionElapsed').textContent  =
    st.elapsed_s ? ('Temps écoulé : ' + fmtElapsed(st.elapsed_s)) : '';

  var stateEl = document.getElementById('extractionState');
  stateEl.classList.remove('active', 'done', 'error');
  if (st.in_progress) {
    stateEl.textContent = 'En cours';
    stateEl.classList.add('active');
  } else if (st.total === 0) {
    stateEl.textContent = 'Aucun film à traiter';
  } else if ((st.failed || 0) > 0) {
    stateEl.textContent = 'Terminé avec ' + st.failed + ' échec(s)';
    stateEl.classList.add('error');
  } else {
    stateEl.textContent = 'Terminé';
    stateEl.classList.add('done');
  }
}

function pollExtractionStatus(modalOpen) {
  return fetch('/api/extraction/status')
    .then(function (r) { return r.json(); })
    .then(function (st) { applyExtractionStatus(st, modalOpen); return st; })
    .catch(function () { /* serveur inaccessible : on ignore */ });
}

function openExtractionModal() {
  document.getElementById('extraction-modal').classList.add('open');
  pollExtractionStatus(true);
  if (extractionPollTimer) clearInterval(extractionPollTimer);
  extractionPollTimer = setInterval(function () { pollExtractionStatus(true); }, 2000);
}

function closeExtractionModal() {
  document.getElementById('extraction-modal').classList.remove('open');
  if (extractionPollTimer) { clearInterval(extractionPollTimer); extractionPollTimer = null; }
}

function startBackgroundExtractionPoll() {
  // Rafraîchit juste l'indicateur du bouton tant qu'il y a du travail en cours.
  pollExtractionStatus(false).then(function (st) {
    if (extractionBackgroundTimer) clearInterval(extractionBackgroundTimer);
    extractionBackgroundTimer = setInterval(function () {
      pollExtractionStatus(false).then(function (s) {
        if (s && !s.in_progress && (!s.failed || s.failed === 0)) {
          clearInterval(extractionBackgroundTimer);
          extractionBackgroundTimer = null;
        }
      });
    }, 5000);
  });
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
document.getElementById('extraction-modal').addEventListener('click', function (e) {
  if (e.target === document.getElementById('extraction-modal')) closeExtractionModal();
});
document.addEventListener('keydown', function (e) {
  if (e.key === 'Escape') { closePlayer(); closeSeriesModal(); closeCastOptions(); closeExtractionModal(); }
});

setMode(currentMode);
loadMovies();
startBackgroundExtractionPoll();
