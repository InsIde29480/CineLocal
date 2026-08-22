// ═══════════════════════════════════════════════════════════════════
// SUBTITLES — modale d'extraction en masse des sous-titres (fenêtre de
// progression) et modale de resynchronisation (décalage des timecodes).
// Dépend de : utils.js (escHtml) ; settings.js (closeSettingsModal).
// ═══════════════════════════════════════════════════════════════════

// ─── EXTRACTION EN MASSE (fenêtre de progression) ──────────────────
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
//   'new'      → vérifie tout, saute ce qui est déjà complet
//   'retry'    → ne reprend que les échecs et les films sans sous-titre
//   'download' → télécharge le français manquant (OpenSubtitles)
//   'force'    → purge tous les caches puis tout ré-extrait
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
// RESYNCHRONISATION DES SOUS-TITRES (décalage des timecodes)
// ═══════════════════════════════════════════════════════════════════
var _syncCatalog  = [];
var _syncSelected = null;
var _syncDir      = 1;   // 1 = retarder (+), -1 = avancer (−)

function openSubSyncModal() {
  closeSettingsModal();
  document.getElementById('subsync-modal').classList.add('open');
  document.body.style.overflow = 'hidden';
  _syncSelected = null;
  document.getElementById('syncSearch').value = '';
  document.getElementById('syncControls').style.display = 'none';
  document.getElementById('syncStatus').textContent = '';
  document.getElementById('syncApplyBtn').disabled = true;
  document.getElementById('syncSelected').textContent = 'Chargement…';
  document.getElementById('syncList').innerHTML = '';
  setSyncDir(1);
  fetch('/api/subtitles/catalog')
    .then(function (r) { return r.json(); })
    .then(function (list) {
      _syncCatalog = list || [];
      document.getElementById('syncSelected').textContent = _syncCatalog.length
        ? 'Aucun sous-titre sélectionné.'
        : 'Aucun sous-titre en cache — lance d’abord une extraction (💬).';
      renderSyncList();
    })
    .catch(function () {
      document.getElementById('syncSelected').textContent = 'Serveur injoignable.';
    });
}

function closeSubSyncModal() {
  document.getElementById('subsync-modal').classList.remove('open');
  document.body.style.overflow = '';
}

function renderSyncList() {
  var q = (document.getElementById('syncSearch').value || '').toLowerCase().trim();
  var el = document.getElementById('syncList');
  var list = _syncCatalog;
  if (q) list = list.filter(function (e) { return (e.title + ' ' + e.sub).toLowerCase().indexOf(q) >= 0; });
  var shown = list.slice(0, 300);
  if (!shown.length) { el.innerHTML = '<div class="subs-empty-msg">Aucun résultat.</div>'; return; }
  el.innerHTML = shown.map(function (e) {
    var id  = e.movie_id + ':' + e.idx;
    var sel = (_syncSelected && (_syncSelected.movie_id + ':' + _syncSelected.idx) === id) ? ' active' : '';
    return '<button class="sync-row' + sel + '" onclick="selectSyncSub(\'' + e.movie_id + '\',' + e.idx + ')">'
      + '<span class="sync-row-title">' + escHtml(e.title) + '</span>'
      + '<span class="sync-row-sub">' + escHtml(e.sub) + '</span></button>';
  }).join('') + (list.length > shown.length
    ? '<div class="subs-empty-msg">… ' + (list.length - shown.length) + ' de plus, affine la recherche.</div>'
    : '');
}

function selectSyncSub(movieId, idx) {
  _syncSelected = _syncCatalog.find(function (e) { return e.movie_id === movieId && e.idx === idx; }) || null;
  renderSyncList();
  if (!_syncSelected) return;
  document.getElementById('syncSelected').innerHTML =
    '▶ ' + escHtml(_syncSelected.title) + ' — <b>' + escHtml(_syncSelected.sub) + '</b>';
  document.getElementById('syncControls').style.display = 'block';
  document.getElementById('syncApplyBtn').disabled = false;
  document.getElementById('syncStatus').textContent = '';
  updateSyncPreview();
}

function setSyncDir(d) {
  _syncDir = d;
  document.getElementById('syncDirLater').classList.toggle('active', d === 1);
  document.getElementById('syncDirEarlier').classList.toggle('active', d === -1);
  updateSyncPreview();
}

function _syncOffset() {
  var v = parseFloat(document.getElementById('syncSeconds').value);
  if (isNaN(v) || v < 0) v = 0;
  return _syncDir * v;
}

function updateSyncPreview() {
  var off = _syncOffset();
  var el = document.getElementById('syncPreview');
  if (!off) { el.textContent = 'Entre un nombre de secondes (> 0).'; return; }
  var s = (off > 0 ? '+' : '') + off;
  el.textContent = 'Décalage : ' + s + ' s → sous-titres ' + (off > 0 ? 'plus tard' : 'plus tôt');
}

function applySubShift() {
  if (!_syncSelected) return;
  var off = _syncOffset();
  if (!off) { _setSyncStatus('Entre un nombre de secondes différent de 0.', 'err'); return; }
  var btn = document.getElementById('syncApplyBtn');
  btn.disabled = true;
  _setSyncStatus('Application…', '');
  fetch('/api/subtitles/shift', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ movie_id: _syncSelected.movie_id, idx: _syncSelected.idx, offset: off }),
  })
    .then(function (r) { return r.json(); })
    .then(function (res) {
      btn.disabled = false;
      if (res.status === 'ok') {
        var msg = '✓ ' + (off > 0 ? '+' : '') + off + ' s appliqué à ' + (res.shifted || 0) + ' timecode(s)';
        if (res.source_shifted) msg += ' (+ fichier source ' + escHtml(res.source || '') + ')';
        msg += '. Relance la lecture pour vérifier ; ré-applique pour affiner.';
        _setSyncStatus(msg, 'ok');
      } else {
        _setSyncStatus('✗ ' + (res.message || 'échec'), 'err');
      }
    })
    .catch(function () {
      btn.disabled = false;
      _setSyncStatus('✗ Serveur injoignable.', 'err');
    });
}

function _setSyncStatus(msg, cls) {
  var el = document.getElementById('syncStatus');
  el.className = 'set-status' + (cls ? ' ' + cls : '');
  el.textContent = msg;
}
