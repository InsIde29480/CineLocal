// ═══════════════════════════════════════════════════════════════════
// SETTINGS — modale des paramètres (chemins, clés API, analyse auto).
// Tout est enregistré côté serveur via /api/settings.
// Dépend de : catalogue.js (loadMovies).
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
