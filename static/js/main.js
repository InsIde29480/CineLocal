// ═══════════════════════════════════════════════════════════════════
// MAIN — écouteurs globaux et initialisation.
// Chargé en DERNIER : toutes les fonctions des autres fichiers doivent
// être disponibles.
// ═══════════════════════════════════════════════════════════════════

document.getElementById('search').addEventListener('input', applyFilters);

// Clic sur le fond assombri d'une modale = fermeture
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
document.getElementById('subsync-modal').addEventListener('click', function (e) {
  if (e.target === document.getElementById('subsync-modal')) closeSubSyncModal();
});
document.addEventListener('keydown', function (e) {
  if (e.key === 'Escape') { closePlayer(); closeSeriesModal(); closeCastOptions(); closeMovieModal(); closeSubsModal(); closeSettingsModal(); closeSubSyncModal(); }
});

// Démarrage : applique le mode mémorisé puis charge le catalogue.
setMode(currentMode);
loadMovies();
