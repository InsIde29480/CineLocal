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
var optionsTarget     = 'cast';   // 'cast' = Chromecast, 'local' = MPV sur la TV du Pi
var castMoviePending  = null;
var castTracks        = null;
var castSelectedAudio = 0;
var castSelectedSub   = null;
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
  document.getElementById('heroPrimaryLabel').textContent =
    mode === 'tv' ? 'CASTER' : mode === 'local' ? 'TV DIRECTE' : 'LIRE';
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
// MODAL OPTIONS (Chromecast ou TV directe)
// ═══════════════════════════════════════════════════════════════════
function startCast(movie) {
  optionsTarget = 'cast';
  currentMovie = movie;
  openCastOptions(movie);
}

function startLocal(movie) {
  optionsTarget = 'local';
  currentMovie = movie;
  openCastOptions(movie);
}

function openCastOptions(movie) {
  castMoviePending  = movie;
  castSelectedAudio = 0;
  castSelectedSub   = null;
  castTracks        = null;

  var isCast = (optionsTarget === 'cast');
  document.getElementById('optionsTitle').textContent  = isCast ? '📺 Options Cast' : '📽️ Options TV directe';
  document.getElementById('btnLaunchLabel').textContent = isCast ? '📺 Lancer le Cast' : '📽️ Lire sur la TV';

  document.getElementById('castOptionsFilm').textContent = movie.title;
  document.getElementById('castOptionsBody').innerHTML =
    '<div class="cast-loading-msg">⏳ Chargement des pistes…</div>';
  document.getElementById('btnCastLaunch').disabled = true;
  document.getElementById('cast-options-modal').classList.add('open');

  fetch('/api/tracks/' + movie.id)
    .then(function (r) { return r.json(); })
    .then(function (data) {
      castTracks = data;
      renderCastOptions();
      document.getElementById('btnCastLaunch').disabled = false;
    })
    .catch(function () {
      castTracks = { audio_tracks: [], subtitle_tracks: [] };
      renderCastOptions();
      document.getElementById('btnCastLaunch').disabled = false;
    });
}

function renderCastOptions() {
  if (!castTracks) return;
  var audio = castTracks.audio_tracks || [];
  var subs  = castTracks.subtitle_tracks || [];
  var html  = '';

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
  document.getElementById('cast-options-modal').classList.remove('open');
  castMoviePending = null;
}

function launchOptions() {
  if (optionsTarget === 'local') launchLocal();
  else                           launchCast();
}

function launchCast() {
  if (!castMoviePending) return;

  if (!castAvailable) {
    alert("Cast non disponible.\nAssure-toi d'utiliser Google Chrome avec le Chromecast sur le même réseau WiFi.");
    return;
  }

  var movie    = castMoviePending;
  var audioIdx = castSelectedAudio;
  var subTrack = (castSelectedSub !== null && castTracks)
    ? castTracks.subtitle_tracks[castSelectedSub]
    : null;

  // Piste audio principale : le serveur sert le fichier directement
  // (ou transcode à la volée), on peut lancer tout de suite.
  if (audioIdx === 0) {
    closeCastOptions();
    doCast(movie, audioIdx, subTrack);
    return;
  }

  // Piste audio alternative : le serveur doit d'abord terminer le remux
  // en MP4 (cache). Si on lançait le Cast immédiatement, le Chromecast
  // attendrait la réponse pendant tout le remux et abandonnerait (timeout).
  // On attend donc que la piste soit prête, comme en mode PC.
  prepareAudioThenCast(movie, audioIdx, subTrack);
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
  btn.disabled = true;
  label.textContent = '⏳ Préparation de la piste…';

  // Pas encore de session : on ouvre le sélecteur de Chromecast TOUT DE
  // SUITE, tant que le clic de l'utilisateur est encore « frais ». Si on
  // attendait la fin du remux (plusieurs minutes), Chrome ignorerait le
  // .click() programmé et rien ne se passerait. L'utilisateur choisit donc
  // sa TV pendant que le serveur prépare la piste ; la lecture démarre
  // automatiquement dès que les deux sont prêts.
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
    // Modale fermée ou autre film ouvert entre-temps : on arrête le suivi
    // (le remux continue côté serveur et restera en cache pour plus tard).
    if (castMoviePending !== movie) { restore(); return; }

    fetch('/api/audio_status/' + movie.id + '/' + audioIdx)
      .then(function (r) { return r.json(); })
      .then(function (state) {
        if (castMoviePending !== movie) { restore(); return; }
        if (state.status === 'ready') {
          if (castSession) {
            restore();
            closeCastOptions();
            sendToCast(movie, audioIdx, subTrack);
          } else {
            // Piste prête mais Chromecast pas encore connecté : la lecture
            // partira automatiquement à l'ouverture de la session (via
            // pendingCastMovie). Si le sélecteur a été refermé, un nouveau
            // clic sur le bouton rouvre le choix de la TV.
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

function launchLocal() {
  if (!castMoviePending) return;

  var movie = castMoviePending;

  // index réel de la piste audio choisie (0-based)
  var audioTrack = (castTracks && castTracks.audio_tracks[castSelectedAudio]) || null;
  var audioIdx   = audioTrack ? audioTrack.index : 0;

  // index réel du sous-titre choisi (0-based interne ou >=1000 externe), ou null
  var subTrack = (castSelectedSub !== null && castTracks)
    ? castTracks.subtitle_tracks[castSelectedSub]
    : null;
  var subIdx = subTrack ? subTrack.index : null;

  closeCastOptions();

  var url = '/play/' + movie.id + '?audio=' + audioIdx;
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

function playLocal(movie) {
  currentMovie = movie;
  var video   = document.getElementById('player-video');
  var modal   = document.getElementById('player-modal');
  var loading = document.getElementById('playerLoading');

  document.getElementById('player-title').textContent = movie.title;
  loading.classList.remove('hidden');
  setLoadingState('Préparation du film…', 'Chargement de la vidéo et extraction des pistes', false, false);

  video.querySelectorAll('track').forEach(function (t) { t.remove(); });

  modal.classList.add('open');
  document.body.style.overflow = 'hidden';

  var movieId     = movie.id;
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
    video.src     = '/stream/' + movieId;
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

function closePlayer() {
  var video = document.getElementById('player-video');
  video.pause();
  video.src = '';
  video.querySelectorAll('track').forEach(function (t) { t.remove(); });
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
  if (currentMode === 'local')   startLocal(m);
  else if (currentMode === 'tv') startCast(m);
  else                           playLocal(m);
}

function openMovie(id) {
  var m = allMovies.find(function (x) { return x.id === id; });
  if (m) setHero(m);
}

function playHero() {
  if (currentMovie) playMovie(currentMovie.id);
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
  if (currentMode === 'tv')         startCast(fakeMovie);
  else if (currentMode === 'local') startLocal(fakeMovie);
  else                              playLocal(fakeMovie);
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
      if (allMovies.length > 0) {
        setHero(allMovies[Math.floor(Math.random() * Math.min(allMovies.length, 10))]);
      }
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
  return '<div class="movie-card" onclick="' + cardClick + '" onmouseenter="previewHero(\'' + m.id + '\')">'
    + poster
    + '<div class="card-placeholder" style="display:' + (m.poster ? 'none' : 'flex') + '">'
    + (isSeries ? '📺' : '🎬') + '<span>' + escHtml(m.title.substring(0, 30)) + '</span></div>'
    + '<div class="card-badge">' + ext + '</div>'
    + (isSeries ? '<div class="series-badge">SÉRIE</div>' : '')
    + '<div class="card-info">'
    + '<div class="card-title">' + escHtml(m.title) + '</div>'
    + '<div class="card-meta">' + meta + '</div>'
    + '<div class="card-actions">'
    + '<button class="card-btn" onclick="' + action + '">' + actionLabel + '</button>'
    + '</div></div></div>';
}

// ═══════════════════════════════════════════════════════════════════
// HERO
// ═══════════════════════════════════════════════════════════════════
var heroTimeout;
function previewHero(id) {
  clearTimeout(heroTimeout);
  heroTimeout = setTimeout(function () {
    var m = allMovies.find(function (x) { return x.id === id; });
    if (m) setHero(m);
  }, 400);
}

function setHero(m) {
  currentMovie = m;
  document.getElementById('hero-title').textContent = m.title;
  document.getElementById('hero-meta').textContent  =
    [m.year, m.category !== 'Films' ? m.category : '', m.size_mb + ' Mo']
      .filter(Boolean).join(' · ');
  document.getElementById('hero-tag').textContent = m.category;
  var bg = m.backdrop || m.poster;
  if (bg) {
    var img = new Image();
    img.onload = function () { document.getElementById('hero-bg').style.backgroundImage = 'url(' + bg + ')'; };
    img.src = bg;
  } else {
    document.getElementById('hero-bg').style.backgroundImage = '';
  }
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
document.addEventListener('keydown', function (e) {
  if (e.key === 'Escape') { closePlayer(); closeSeriesModal(); closeCastOptions(); }
});

setMode(currentMode);
loadMovies();
