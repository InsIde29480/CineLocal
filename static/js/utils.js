// ═══════════════════════════════════════════════════════════════════
// UTILS — petites fonctions partagées par tous les modules
// (échappement HTML, formats, préférence de sous-titres, épisodes vus)
// Chargé en PREMIER. Aucune dépendance.
// ═══════════════════════════════════════════════════════════════════

function escHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
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

// Mélange de Fisher-Yates (copie, ne modifie pas l'original)
function _shuffle(arr) {
  var a = arr.slice();
  for (var i = a.length - 1; i > 0; i--) {
    var j = Math.floor(Math.random() * (i + 1));
    var t = a[i]; a[i] = a[j]; a[j] = t;
  }
  return a;
}

// ─── Préférence GLOBALE de sous-titres (mémorisée, appliquée partout en PC) ──
function getSubPref() {
  try { return JSON.parse(localStorage.getItem('cinelocal-sub-pref') || 'null'); }
  catch (e) { return null; }
}

function setSubPref(track) {
  localStorage.setItem('cinelocal-sub-pref',
    JSON.stringify(track ? { language: track.language, label: track.label } : null));
}

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

// Un épisode peut avoir plusieurs versions (4K/HD) : « vu » vaut pour n'importe
// laquelle. On considère donc tous les ids de ses variantes.
function _episodeIds(ep) {
  var ids = [ep.id];
  (ep.qualities || []).forEach(function (q) { if (ids.indexOf(q.id) < 0) ids.push(q.id); });
  return ids;
}

function isEpisodeWatched(ep) {
  var map = getWatchedMap();
  return _episodeIds(ep).some(function (id) { return !!map[id]; });
}

function toggleEpisodeWatched(episodeId) {
  if (!currentSeries) return;
  var ep = currentSeries.episodes.find(function (e) { return e.id === episodeId; });
  if (!ep) return;
  var map = getWatchedMap();
  if (isEpisodeWatched(ep)) _episodeIds(ep).forEach(function (id) { delete map[id]; });
  else                      map[ep.id] = true;
  localStorage.setItem('cinelocal-watched', JSON.stringify(map));
  renderEpisodes();
  updateSeriesMeta();
}
