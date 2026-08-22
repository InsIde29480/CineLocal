// ═══════════════════════════════════════════════════════════════════
// PLAYER — lecture dans le navigateur (lecteur HTML5), gestion des
// sous-titres du lecteur et bouton CC.
// Dépend de : utils.js (escHtml, markWatched, getSubPref, setSubPref).
// ═══════════════════════════════════════════════════════════════════

// Film en cours de lecture / de préparation (null = lecteur fermé).
var currentMovie = null;

function setLoadingState(text, substep, videoDone, tracksDone) {
  document.getElementById('loadingText').textContent    = text;
  document.getElementById('loadingSubstep').textContent = substep;
  document.getElementById('stepVideo').className  = 'loading-step' + (videoDone  ? ' done' : ' active');
  document.getElementById('stepTracks').className = 'loading-step' + (tracksDone ? ' done' : (videoDone ? ' active' : ''));
}

// Le choix de la piste audio en mode PC passe par le menu natif du
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
