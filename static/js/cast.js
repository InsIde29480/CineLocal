// ═══════════════════════════════════════════════════════════════════
// CAST — SDK Google Cast : chargement, session et envoi d'un film
// au Chromecast (le serveur décide du mode : direct / remux / HLS).
// Dépend de : utils.js (markWatched) ; options.js (closeCastOptions).
// ═══════════════════════════════════════════════════════════════════

var castSession   = null;
var castAvailable = false;
var castSdkLoaded = false;

// Film en attente : sélectionné avant que la session Cast soit ouverte
var pendingCastMovie  = null;
var pendingAudioIdx   = null;
var pendingSubTrack   = null;

// Délais (ms) entre les tentatives d'envoi au Chromecast. À la PREMIÈRE
// connexion sur une TV, le récepteur met parfois 10-15 s à démarrer : les
// premiers loadMedia échouent alors normalement. On insiste donc longtemps
// (~25 s au total) avec des délais croissants au lieu d'abandonner au bout
// de 3 essais — c'est ce qui obligeait à recharger la page puis relancer.
var CAST_RETRY_DELAYS = [1000, 1500, 2500, 4000, 6000, 9000];

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
          _setCastStatus('⏳ Connexion à la TV…');
          setTimeout(function () { sendToCast(pMovie, pAudio, pSub); }, 1000);
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
  // Échec de chargement (hors Chrome, hors ligne…) : on pourra retenter
  // au prochain passage en mode 📺 au lieu de rester bloqué.
  s.onerror = function () {
    castSdkLoaded = false;
    console.warn('[Cast] SDK injoignable (navigateur non compatible ou hors ligne)');
  };
  document.head.appendChild(s);
}

// Charge le SDK dès l'arrivée sur la page (et pas seulement au passage en
// mode 📺) : le contexte Cast est ainsi initialisé bien avant le premier
// clic, ce qui fiabilise la toute première connexion à une TV.
loadCastSdk();

// Affiche un message dans la barre de statut Cast (en bas de l'écran).
function _setCastStatus(text) {
  var bar = document.getElementById('castStatus');
  document.getElementById('castStatusText').textContent = text;
  if (text) bar.classList.add('visible');
}

// Reprogramme un envoi après échec, avec un délai croissant. Renvoie false
// quand toutes les tentatives sont épuisées (au bout de ~25 s).
function _retryCast(movie, audioIdx, subTrack, attempt) {
  if (attempt >= CAST_RETRY_DELAYS.length) return false;
  var delay = CAST_RETRY_DELAYS[attempt - 1] || 2000;
  _setCastStatus('⏳ Connexion à la TV… (essai ' + (attempt + 1) + '/' + CAST_RETRY_DELAYS.length + ')');
  setTimeout(function () { sendToCast(movie, audioIdx, subTrack, attempt + 1); }, delay);
  return true;
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
        _setCastStatus('✗ Cast : ' + (info && info.error ? info.error : 'réponse invalide'));
        alert('Erreur Cast : ' + (info && info.error ? info.error : 'réponse invalide'));
        return;
      }
      launchCastMedia(movie, audioIdx, subTrack, attempt, info);
    })
    .catch(function (e) {
      console.error('[Cast] cast_info échoué :', e);
      if (!_retryCast(movie, audioIdx, subTrack, attempt)) {
        _setCastStatus('✗ Impossible de préparer le flux');
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
      // À la première connexion sur une TV, le récepteur met parfois plus de
      // 10 s à démarrer et refuse les premiers chargements : on insiste avec
      // des délais croissants (~25 s au total) avant d'abandonner.
      if (_retryCast(movie, audioIdx, subTrack, attempt)) return;
      _setCastStatus('✗ La TV n’a pas répondu — réessaie (📺 Lancer le Cast)');
      alert('Erreur Cast : ' + (e.description || JSON.stringify(e))
        + '\nLa TV n’a pas répondu à temps — clique à nouveau sur « Lancer le Cast »,'
        + ' la connexion est déjà établie.');
    });
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
