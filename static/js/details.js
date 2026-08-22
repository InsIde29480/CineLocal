// ═══════════════════════════════════════════════════════════════════
// DETAILS — fiche film (style Netflix, données TMDB) et modale série
// (saisons, épisodes, suivi des épisodes vus).
// Dépend de : utils.js ; player.js (playPc) ; options.js (openOptions).
// ═══════════════════════════════════════════════════════════════════

// ─── FICHE FILM ────────────────────────────────────────────────────
var currentFiche     = null;
var ficheSubTracks   = [];      // pistes de sous-titres du film affiché
var ficheSelectedSub = null;    // index dans ficheSubTracks, ou null = aucun
var ficheToken       = 0;       // anti-course sur le chargement des pistes
var ficheQualities       = [];  // versions/qualités du film affiché
var ficheSelectedQuality = 0;   // index dans ficheQualities
var ficheBaseMeta        = '';  // méta de la fiche sans la durée

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

// ═══════════════════════════════════════════════════════════════════
// SÉRIES
// ═══════════════════════════════════════════════════════════════════
var currentSeries = null;
var currentSeason = null;

function openSeries(seriesId) {
  var series = allMovies.find(function (x) { return x.id === seriesId && x.kind === 'series'; });
  if (!series) return;
  currentSeries = series;
  document.getElementById('seriesModalTitle').textContent = series.title;
  updateSeriesMeta();
  var ov = document.getElementById('seriesModalOverview');
  ov.textContent = series.overview || '';
  ov.style.display = series.overview ? '' : 'none';

  // Image de fond : comme la fiche film (backdrop TMDB, sinon grande initiale)
  var container = document.getElementById('seriesContainer');
  var backdrop  = document.getElementById('seriesBackdrop');
  var letter    = document.getElementById('seriesLetter');
  var bg = series.backdrop || series.poster;
  if (bg) {
    container.classList.remove('no-image');
    backdrop.style.backgroundImage = 'url(' + bg + ')';
    letter.textContent = '';
  } else {
    container.classList.add('no-image');
    backdrop.style.backgroundImage = '';
    letter.textContent = (series.title || '?').charAt(0).toUpperCase();
  }

  var seasons = [...new Set(series.episodes.map(function (e) { return e.season; }))].sort(function (a, b) { return a - b; });
  currentSeason = seasons[0];
  renderSeasonTabs(seasons);
  renderEpisodes();
  document.getElementById('series-modal').classList.add('open');
  document.body.style.overflow = 'hidden';
}

// Puces de saisons (mêmes .sub-chip que les versions de la fiche film).
// Masquées s'il n'y a qu'une saison, comme le sélecteur de versions.
function renderSeasonTabs(seasons) {
  var el = document.getElementById('seasonTabs');
  if (!seasons || seasons.length <= 1) {
    el.style.display = 'none';
    el.innerHTML = '';
    return;
  }
  el.style.display = '';
  var html = '<span class="fiche-subs-label">📅 Saison</span>';
  seasons.forEach(function (s) {
    html += '<button class="sub-chip' + (s === currentSeason ? ' active' : '')
      + '" data-season="' + s + '" onclick="selectSeason(' + s + ')">S'
      + String(s).padStart(2, '0') + '</button>';
  });
  el.innerHTML = html;
}

function selectSeason(season) {
  currentSeason = season;
  document.querySelectorAll('#seasonTabs .sub-chip').forEach(function (c) {
    c.classList.toggle('active', parseInt(c.dataset.season, 10) === season);
  });
  renderEpisodes();
}

function updateSeriesMeta() {
  if (!currentSeries) return;
  var watchedCount = currentSeries.episodes.filter(function (e) { return isEpisodeWatched(e); }).length;
  document.getElementById('seriesModalMeta').textContent =
    currentSeries.season_count + ' saison' + (currentSeries.season_count > 1 ? 's' : '') +
    ' · ' + currentSeries.episode_count + ' épisodes' +
    ' · ' + watchedCount + '/' + currentSeries.episode_count + ' vus';
}

function renderEpisodes() {
  if (!currentSeries) return;
  var eps      = currentSeries.episodes.filter(function (e) { return e.season === currentSeason; });
  var btnLabel = currentMode === 'tv' ? '📺 Caster' : '▶ Lire';

  // Prochain épisode à voir : premier non vu de toute la série
  // (les épisodes sont déjà triés saison/épisode côté serveur).
  var nextEp = currentSeries.episodes.find(function (e) { return !isEpisodeWatched(e); });
  var nextId = nextEp ? nextEp.id : null;

  document.getElementById('episodesList').innerHTML = eps.map(function (ep) {
    var watched = isEpisodeWatched(ep);
    var nextTag = (ep.id === nextId) ? '<span class="ep-next-tag">À suivre</span>' : '';
    // Badge « versions » quand l'épisode existe en plusieurs qualités (4K/HD).
    var versTag = '';
    if (ep.qualities && ep.qualities.length > 1) {
      var has4k = ep.qualities.some(function (q) { return (q.height || 0) >= 2160; });
      versTag = '<span class="ep-vers-tag">' + (has4k ? '4K/HD' : ep.qualities.length + ' versions') + '</span>';
    }
    return '<div class="episode-row' + (watched ? ' watched' : '') + '" onclick="playEpisode(\'' + ep.id + '\')">'
      + '<button class="ep-check' + (watched ? ' watched' : '') + '" title="' + (watched ? 'Marquer non vu' : 'Marquer vu') + '"'
      + ' onclick="event.stopPropagation();toggleEpisodeWatched(\'' + ep.id + '\')">✓</button>'
      + '<div class="episode-num">E' + String(ep.episode).padStart(2, '0') + '</div>'
      + '<div class="episode-info">'
      + '<div class="episode-title">Épisode ' + ep.episode + nextTag + versTag + '</div>'
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
  var multiQuality = ep.qualities && ep.qualities.length > 1;
  closeSeriesModal();
  // Cast : la modale d'options propose déjà le choix de la qualité.
  if (currentMode === 'tv')  openOptions(fakeMovie, 'cast');
  // PC : s'il y a plusieurs versions (4K / HD), on ouvre le choix ; sinon lecture directe.
  else if (multiQuality)     openOptions(fakeMovie, 'pc');
  else                       playPc(fakeMovie, 0, null);
}

function closeSeriesModal() {
  document.getElementById('series-modal').classList.remove('open');
  document.body.style.overflow = '';
}
