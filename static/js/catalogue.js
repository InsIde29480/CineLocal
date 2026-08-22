// ═══════════════════════════════════════════════════════════════════
// CATALOGUE — chargement des films, modes, recherche, rendu des cards
// et fond animé (diaporama).
// Dépend de : utils.js (escHtml, _shuffle, isWatched…).
// ═══════════════════════════════════════════════════════════════════

var allMovies = [];

// Mode mémorisé ('pc' ou 'tv'). L'ancien mode 'local' (TV directe, supprimé)
// peut encore traîner dans le navigateur : on retombe alors sur 'pc'.
var currentMode = localStorage.getItem('cinelocal-mode') || 'pc';
if (currentMode !== 'pc' && currentMode !== 'tv') currentMode = 'pc';

// ─── MODES & FILTRES ───────────────────────────────────────────────
function setMode(mode) {
  currentMode = mode;
  localStorage.setItem('cinelocal-mode', mode);
  document.querySelectorAll('.mode-btn[data-mode]').forEach(function (btn) {
    btn.classList.toggle('active', btn.dataset.mode === mode);
  });
  document.getElementById('castBtn').style.display = (mode === 'tv') ? 'inline-block' : 'none';
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

// ─── CHARGEMENT & RENDU ────────────────────────────────────────────
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
