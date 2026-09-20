/**
 * Noisygram — dashboard.
 *
 * Quatre formes, chacune choisie pour le travail que la donnée doit faire :
 *   • timeline    — scatter, un point par événement, cliquable. Une ligne
 *                   impliquerait une continuité entre événements discrets.
 *   • histogramme — colonnes 0–23, catégorie ordonnée.
 *   • tendance    — ligne, une seule série.
 *   • heatmap     — table HTML, accessible par construction.
 *
 * AUCUNE COULEUR N'EST CODÉE EN DUR ICI. Tout passe par cssVar(), sinon le
 * thème sombre serait à moitié appliqué — le pire des deux mondes : des axes
 * lisibles et des marques qui disparaissent.
 *
 * AUCUN DOUBLE AXE nulle part. Score (0–1) et comptage (0–N) ne partagent
 * jamais un graphe : l'alignement de deux échelles est arbitraire, donc le
 * graphe inventerait une corrélation absente des données.
 */

'use strict';

const $ = (id) => document.getElementById(id);

const cssVar = (nom) =>
  getComputedStyle(document.documentElement).getPropertyValue(nom).trim();

function palette() {
  return {
    surface: cssVar('--surface'),
    grid: cssVar('--grid'),
    axis: cssVar('--axis'),
    muted: cssVar('--muted'),
    ink: cssVar('--ink'),
    ink2: cssVar('--ink-2'),
    series: cssVar('--series'),
    seq: [1, 2, 3, 4, 5, 6].map((n) => cssVar('--seq-' + n)),
  };
}

/** Ajoute un canal alpha à une couleur CSS (hex ou rgb/rgba). */
function alpha(couleur, a) {
  const c = couleur.trim();
  if (c.startsWith('#')) {
    const h = c.length === 4
      ? c.slice(1).split('').map((x) => x + x).join('')
      : c.slice(1);
    const n = parseInt(h, 16);
    return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
  }
  const m = c.match(/rgba?\(([^)]+)\)/);
  if (m) {
    const [r, g, b] = m[1].split(',').map((x) => parseFloat(x));
    return `rgba(${r}, ${g}, ${b}, ${a})`;
  }
  return c;
}

function luminance(couleur) {
  const c = couleur.trim();
  let r = 0, g = 0, b = 0;
  if (c.startsWith('#')) {
    const h = c.length === 4 ? c.slice(1).split('').map((x) => x + x).join('') : c.slice(1);
    const n = parseInt(h, 16);
    r = (n >> 16) & 255; g = (n >> 8) & 255; b = n & 255;
  } else {
    const m = c.match(/rgba?\(([^)]+)\)/);
    if (m) [r, g, b] = m[1].split(',').map((x) => parseFloat(x));
  }
  const lin = (v) => {
    v /= 255;
    return v <= 0.04045 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
  };
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
}

// ---------------------------------------------------------------- état

const state = {
  tz: 'Europe/Paris',
  threshold: 0.35,
  range: { from: null, to: null },
  points: [],
  histogram: [],
  peakHour: null,
  charts: {},
  chargement: false,
  // Dernier point cliqué : c'est lui que le bouton « toute la rafale » rejoue.
  dernierPoint: null,
  // URL d'objet du WAV assemblé, à révoquer avant d'en créer une autre.
  blobUrl: null,
};

const iso = (d) => d.toISOString();

function setRangeHeures(heures) {
  const to = new Date();
  const from = new Date(to.getTime() - heures * 3600 * 1000);
  state.range = { from, to };
  $('from').value = iso(from).slice(0, 10);
  $('to').value = iso(to).slice(0, 10);
}

// ---------------------------------------------------------------- formats

// Les formateurs portent le fuseau RENVOYÉ PAR LE SERVEUR, pas celui du
// navigateur. Les instants arrivent en UTC ; les regrouper à l'écran dans un
// autre fuseau que celui des agrégats produirait un histogramme et une
// timeline qui ne parlent pas du même moment de la journée.
const _formateurs = {};
function fmt() {
  if (!_formateurs[state.tz]) {
    const tz = { timeZone: state.tz };
    _formateurs[state.tz] = {
      heure: new Intl.DateTimeFormat('fr-FR', Object.assign({ hour: '2-digit', minute: '2-digit' }, tz)),
      heureMinute: new Intl.DateTimeFormat('fr-FR', Object.assign(
        { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }, tz)),
      jour: new Intl.DateTimeFormat('fr-FR', Object.assign({ day: '2-digit', month: '2-digit' }, tz)),
      jourLong: new Intl.DateTimeFormat('fr-FR', Object.assign(
        { weekday: 'short', day: '2-digit', month: '2-digit' }, tz)),
    };
  }
  return _formateurs[state.tz];
}

/** Un « AAAA-MM-JJ » de l'API → Date, ancré à midi UTC pour ne pas glisser
 *  d'un jour au passage en heure locale. */
const jourDe = (s) => new Date(s + 'T12:00:00Z');

const nombre = (v, d) => (v === null || v === undefined ? '—' : Number(v).toFixed(d === undefined ? 2 : d));
const decimal = (v) => (v === null || v === undefined ? '—' : String(v).replace('.', ','));

// ---------------------------------------------------------------- plugins

/**
 * Ligne de seuil : hairline PLEINE, jamais pointillée — le pointillé se lit
 * comme « projection », pas comme « seuil ». Avec un label direct, pour
 * qu'on n'ait pas à deviner de quelle valeur il s'agit.
 */
const pluginSeuil = {
  id: 'seuil',
  afterDatasetsDraw(chart) {
    const { ctx, chartArea, scales } = chart;
    if (!scales.y) return;
    const y = scales.y.getPixelForValue(state.threshold);
    if (y < chartArea.top - 1 || y > chartArea.bottom + 1) return;
    const p = palette();

    ctx.save();
    ctx.strokeStyle = p.axis;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(chartArea.left, y);
    ctx.lineTo(chartArea.right, y);
    ctx.stroke();

    ctx.fillStyle = p.ink2;
    ctx.font = '11px system-ui, -apple-system, "Segoe UI", sans-serif';
    ctx.textAlign = 'right';
    ctx.textBaseline = 'bottom';
    ctx.fillText('seuil ' + decimal(state.threshold.toFixed(2)), chartArea.right - 2, y - 3);
    ctx.restore();
  },
};

/**
 * Label direct sur le sommet de la colonne de pointe.
 *
 * L'emphase de l'histogramme est portée par l'OPACITÉ et par ce label, pas par
 * une seconde couleur : en thème sombre, la bande de luminosité exploitable est
 * trop étroite pour que deux paliers d'une même teinte restent distinguables
 * (vérifié au validateur — aucun couple ne passe). Une seule teinte, deux
 * intensités, et le chiffre écrit.
 */
const pluginPic = {
  id: 'pic',
  afterDatasetsDraw(chart) {
    const pic = state.peakHour;
    if (pic === null) return;
    const meta = chart.getDatasetMeta(0);
    const bar = meta.data[pic];
    const valeur = chart.data.datasets[0].data[pic];
    if (!bar || !valeur) return;
    const p = palette();
    const { ctx } = chart;
    ctx.save();
    ctx.fillStyle = p.ink;
    ctx.font = '600 12px system-ui, -apple-system, "Segoe UI", sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'bottom';
    ctx.fillText(String(valeur), bar.x, bar.y - 5);
    ctx.restore();
  },
};

// ---------------------------------------------------------------- graphiques

function optionsBase(p) {
  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: window.matchMedia('(prefers-reduced-motion: reduce)').matches
      ? false
      : { duration: 180 },
    // Le survol enrichit, il ne conditionne rien : toutes les valeurs sont
    // aussi lisibles dans la vue tableau de chaque carte.
    plugins: {
      legend: { display: false }, // mono-série : le titre de la carte nomme la série
      tooltip: {
        backgroundColor: p.ink,
        titleColor: p.surface,
        bodyColor: p.surface,
        padding: 8,
        displayColors: false,
        cornerRadius: 6,
      },
    },
    scales: {
      x: {
        grid: { color: p.grid, drawTicks: false },
        border: { color: p.axis },
        ticks: { color: p.muted, font: { size: 11 }, maxRotation: 0, autoSkipPadding: 16 },
      },
      y: {
        grid: { color: p.grid, drawTicks: false },
        border: { color: p.axis },
        ticks: { color: p.muted, font: { size: 11 } },
      },
    },
  };
}

function dessineTimeline(points) {
  const p = palette();
  const canvas = $('timeline');
  const vide = points.length === 0;
  $('empty-timeline').hidden = !vide;
  $('wrap-timeline').style.display = vide ? 'none' : '';

  if (state.charts.timeline) { state.charts.timeline.destroy(); state.charts.timeline = null; }
  if (vide) return;

  const span = state.range.to - state.range.from;
  const f = fmt();
  const axeX = span <= 36 * 3600 * 1000 ? f.heure : span <= 10 * 86400 * 1000 ? f.jour : f.jourLong;

  const options = optionsBase(p);
  // Interaction stricte : uniquement quand la souris touche précisément le point bleu
  options.interaction = {
    mode: 'point',
    intersect: true,
  };
  options.onHover = (evt, elements) => {
    if (evt && evt.chart && evt.chart.canvas) {
      evt.chart.canvas.style.cursor = elements.length ? 'pointer' : 'default';
    }
  };
  options.onClick = (evt, elements, chart) => {
    // Clic strict : uniquement si on clique pile sur le point bleu
    const hits = chart.getElementsAtEventForMode(evt, 'point', { intersect: true }, true);
    if (!hits.length) return;
    const pt = points[hits[0].index];
    if (pt) joue(pt);
  };
  canvas.oncontextmenu = (evt) => {
    evt.preventDefault();
    if (!state.charts.timeline) return;
    const hits = state.charts.timeline.getElementsAtEventForMode(evt, 'point', { intersect: true }, true);
    if (!hits.length) return;
    const pt = points[hits[0].index];
    if (!pt) return;
    if (confirm('Marquer la capture n°' + pt.id + ' comme fausse ? Elle sera retirée des graphiques.')) {
      fetch('/api/events/' + pt.id + '/disapprove', { method: 'POST' }).then(() => {
        rafraichis();
      });
    }
  };

  options.scales.x = Object.assign(options.scales.x, {
    type: 'linear',
    // Pas d'adaptateur de date : un axe linéaire + callback Intl produit un
    // rendu identique pour un scatter, et reste honnête sur un filtre de
    // plage, ce qu'un axe temps à saut automatique n'est pas.
    ticks: {
      color: p.muted,
      font: { size: 11 },
      maxRotation: 0,
      autoSkipPadding: 16,
      callback: (v) => axeX.format(new Date(v)),
    },
  });
  // y ÉPINGLÉ à [0,1] : le score du modèle est borné, et une hauteur de point
  // doit vouloir dire le même score quel que soit le filtre. Un axe
  // auto-adapté ferait paraître un jour calme comme un jour bruyant.
  options.scales.y = Object.assign(options.scales.y, {
    min: 0, max: 1,
    ticks: { color: p.muted, font: { size: 11 }, callback: (v) => decimal(v.toFixed(2)) },
  });
  options.plugins.tooltip = Object.assign(options.plugins.tooltip || {}, {
    mode: 'point',
    intersect: true,
    callbacks: {
      title: (items) => {
        const pt = points[items[0].dataIndex];
        const num = pt && pt.id != null ? 'Capture n°' + pt.id + ' · ' : '';
        return num + f.heureMinute.format(new Date(items[0].parsed.x));
      },
      label: (item) => 'score ' + decimal(item.parsed.y.toFixed(3)),
    },
  });

  state.charts.timeline = new Chart(canvas, {
    type: 'scatter',
    data: {
      datasets: [{
        label: 'Événements',
        data: points.map((pt) => ({ x: pt.t, y: pt.score })),
        backgroundColor: p.series,
        pointRadius: 6,
        pointHoverRadius: 8,
        // L'anneau de 2 px couleur surface garde les événements superposés
        // lisibles — sans lui, deux événements au même score fusionnent.
        pointBorderColor: p.surface,
        pointBorderWidth: 2,
        // Cible stricte : pas de tolérance artificielle, il faut toucher le bleu
        pointHitRadius: 0,
      }],
    },
    options,
    plugins: [pluginSeuil],
  });
}

function dessineHistogramme(buckets, peakHour, total) {
  const p = palette();
  const vide = total === 0;
  $('empty-histogram').hidden = !vide;
  $('histogram').parentElement.style.display = vide ? 'none' : '';

  if (state.charts.histogram) { state.charts.histogram.destroy(); state.charts.histogram = null; }
  if (vide) return;

  const options = optionsBase(p);
  options.scales.x.ticks.callback = (v) => String(v).padStart(2, '0') + ' h';
  options.scales.y.beginAtZero = true;
  options.scales.y.ticks.precision = 0;
  options.plugins.tooltip.callbacks = {
    title: (items) => items[0].label,
    label: (item) => item.parsed.y + (item.parsed.y > 1 ? ' événements' : ' événement'),
  };

  state.charts.histogram = new Chart($('histogram'), {
    type: 'bar',
    data: {
      labels: buckets.map((b) => b.hour),
      datasets: [{
        label: 'Événements par heure',
        data: buckets.map((b) => b.count),
        // Deux intensités d'UNE SEULE teinte : la pointe à pleine saturation,
        // le reste en retrait. Rien n'est encodé deux fois — la hauteur dit le
        // comptage, l'opacité dit seulement « c'est celui-là ».
        backgroundColor: buckets.map((b) => alpha(p.series, b.hour === peakHour ? 1 : 0.42)),
        borderRadius: { topLeft: 4, topRight: 4, bottomLeft: 0, bottomRight: 0 },
        borderSkipped: 'bottom',
        maxBarThickness: 24, // colonnes fines : des blocs épais lisent « criard »
        categoryPercentage: 0.9,
        barPercentage: 0.9, // l'écart de 2 px entre colonnes voisines
      }],
    },
    options,
    plugins: [pluginPic],
  });
}

function dessineTendance(points) {
  const p = palette();
  const vide = points.length === 0;
  $('empty-daily').hidden = !vide;
  $('daily').parentElement.style.display = vide ? 'none' : '';

  if (state.charts.daily) { state.charts.daily.destroy(); state.charts.daily = null; }
  if (vide) return;

  const f = fmt();
  const options = optionsBase(p);
  options.scales.x.ticks.callback = (v, i) => f.jour.format(jourDe(points[i].date));
  options.scales.y.beginAtZero = true;
  options.scales.y.ticks.precision = 0;
  options.plugins.tooltip.callbacks = {
    title: (items) => f.jourLong.format(jourDe(points[items[0].dataIndex].date)),
    label: (item) => item.parsed.y + (item.parsed.y > 1 ? ' événements' : ' événement'),
  };

  state.charts.daily = new Chart($('daily'), {
    type: 'line',
    data: {
      labels: points.map((pt) => pt.date),
      datasets: [{
        label: 'Événements par jour',
        data: points.map((pt) => pt.count),
        borderColor: p.series,
        borderWidth: 2,
        tension: 0.25,
        // Un point seulement au dernier : un marqueur à chaque jour d'un mois
        // transformerait la courbe en chapelet.
        pointRadius: points.map((_, i) => (i === points.length - 1 ? 4 : 0)),
        pointBackgroundColor: p.series,
        pointBorderColor: p.surface,
        pointBorderWidth: 2,
        fill: true,
        backgroundColor: alpha(p.series, 0.1), // un lavis, jamais un aplat
      }],
    },
    options,
  });
}

function dessineHeatmap(cells, maxCount, total) {
  const p = palette();
  const table = $('heatmap');
  const parCle = new Map(cells.map((c) => [c.dow + ':' + c.hour, c.count]));
  const jours = ['Lundi', 'Mardi', 'Mercredi', 'Jeudi', 'Vendredi', 'Samedi', 'Dimanche'];

  $('sub-heatmap').textContent =
    total === 0
      ? 'Aucun événement sur cette période.'
      : total + ' événements · maximum ' + maxCount + ' sur une case';

  let html = '<caption class="sr-only">Nombre d\'événements par jour de la semaine et par heure</caption><thead><tr><th scope="col">Jour</th>';
  for (let h = 0; h < 24; h++) html += '<th scope="col">' + String(h).padStart(2, '0') + '</th>';
  html += '<th scope="col">Total</th></tr></thead><tbody>';

  for (let dow = 1; dow <= 7; dow++) {
    html += '<tr><th scope="row">' + jours[dow - 1] + '</th>';
    let totalLigne = 0;
    for (let h = 0; h < 24; h++) {
      const n = parCle.get(dow + ':' + h) || 0;
      totalLigne += n;
      // Paliers séquentiels : une seule teinte, du plus clair (proche du fond)
      // au plus foncé. Jamais d'arc-en-ciel — il n'encode aucune magnitude.
      const palier = n === 0 ? -1 : Math.min(5, Math.floor((n / Math.max(1, maxCount)) * 6));
      const fond = palier < 0 ? '' : 'background:' + p.seq[palier] + ';';
      // Couleur du texte choisie sur la LUMINANCE du fond : un texte sombre
      // sur une case foncée serait illisible.
      const encre = palier < 0 ? p.muted : (luminance(p.seq[palier]) > 0.42 ? p.ink : '#ffffff');
      html +=
        '<td style="' + fond + 'color:' + encre + '"' +
        (n ? ' title="' + jours[dow - 1] + ' ' + h + ' h — ' + n + '"' : '') +
        '>' + (n || '·') + '</td>';
    }
    html += '<td class="total">' + totalLigne + '</td></tr>';
  }
  html += '</tbody>';
  table.innerHTML = html;
}

// ---------------------------------------------------------------- tableaux

function tableau(el, entetes, lignes) {
  let html = '<caption class="sr-only">Vue tableau</caption><thead><tr>';
  for (const e of entetes) html += '<th scope="col">' + e + '</th>';
  html += '</tr></thead><tbody>';
  for (const l of lignes) {
    html += '<tr>';
    for (const c of l) html += '<td>' + c + '</td>';
    html += '</tr>';
  }
  el.innerHTML = html + '</tbody>';
}

// ------------------------------------------------------------- export CSV

// Alimenté à chaque rafraîchissement, lu au clic. Le clic ne relit jamais le
// DOM des tableaux dépliables : la timeline y est tronquée à 200 lignes et sa
// colonne « Écouter » contient un <button>, deux choses qu'on ne veut pas dans
// un CSV.
const exportables = {};

/**
 * Deux conventions qu'impose Excel FR, et non le CSV lui-même : le
 * point-virgule comme séparateur — avec la virgule, Excel empile tout dans une
 * seule colonne — et le BOM UTF-8 en tête, sans lequel « Répartition »
 * s'affiche « RÃ©partition ». Les décimales suivent en virgule, comme partout
 * ailleurs dans la page.
 */
function csvTexte(entetes, lignes) {
  const cellule = (v) => {
    const s = v === null || v === undefined ? '' : String(v);
    return /[";\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  };
  return [entetes].concat(lignes).map((l) => l.map(cellule).join(';')).join('\r\n');
}

function telechargeCsv(entetes, lignes, nom) {
  const blob = new Blob(['\ufeff' + csvTexte(entetes, lignes)], {
    type: 'text/csv;charset=utf-8',
  });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = nom;
  document.body.appendChild(a);
  a.click();
  a.remove();
  // Révoquer dans la foulée annulerait le téléchargement sur certains
  // navigateurs : l'URL doit survivre au clic.
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** « 20260916-20260917 » — la période dans le nom du fichier, pour que deux
 *  exports ne s'écrasent pas et qu'on sache ce qu'on ouvre sans l'ouvrir. */
function periodeFichier() {
  const j = (d) => new Date(d).toISOString().slice(0, 10).replace(/-/g, '');
  return j(state.range.from) + '-' + j(state.range.to);
}

// ---------------------------------------------------------------- données

// -------------------------------------------------------- projet consulté
//
// ⚠️ VOIR n'est pas SURVEILLER. Le projet consulté voyage en paramètre de
// requête : le serveur filtre dessus, et rien ne change à ce que la capture
// surveille. L'activer, lui, demande un redémarrage.
let projetVue = (() => {
  try {
    const v = window.localStorage.getItem('noisygram.projetVue');
    return v ? Number(v) : null;
  } catch (e) { return null; }
})();

function memoriseProjetVue(id) {
  projetVue = id;
  try {
    if (id === null) window.localStorage.removeItem('noisygram.projetVue');
    else window.localStorage.setItem('noisygram.projetVue', String(id));
  } catch (e) { /* mode privé : on continue sans persister */ }
}

function avecProjet(chemin) {
  if (projetVue === null) return chemin;
  return chemin + (chemin.indexOf('?') === -1 ? '?' : '&') + 'projet=' + encodeURIComponent(projetVue);
}

async function api(chemin, options) {
  const r = await fetch(avecProjet(chemin),
    Object.assign({ headers: { Accept: 'application/json' } }, options || {}));
  if (!r.ok) throw new Error(chemin + ' → HTTP ' + r.status);
  return r.json();
}

async function chargeTout() {
  const q = 'from=' + encodeURIComponent(iso(state.range.from)) +
            '&to=' + encodeURIComponent(iso(state.range.to)) +
            '&tz=' + encodeURIComponent(state.tz);

  const [resume, histo, quotidien, chaleur, timeline] = await Promise.all([
    api('/api/stats/summary?tz=' + encodeURIComponent(state.tz)),
    api('/api/stats/histogram?' + q),
    api('/api/stats/daily?' + q),
    api('/api/stats/heatmap?' + q),
    api('/api/stats/timeline?' + q + '&max_points=2000'),
  ]);
  return { resume, histo, quotidien, chaleur, timeline };
}

async function rafraichis() {
  if (state.chargement) return;
  state.chargement = true;
  // On garde le rendu précédent à opacité réduite : pas de squelette qui
  // clignote, pas de saut de mise en page au rechargement.
  document.body.dataset.loading = 'oui';
  try {
    const d = await chargeTout();
    state.tz = d.resume.tz;
    state.points = d.timeline.points;
    state.histogram = d.histo.buckets;
    state.peakHour = d.histo.peak_hour;

    rendResume(d.resume, d.timeline);
    dessineTimeline(d.timeline.points);
    dessineHistogramme(d.histo.buckets, d.histo.peak_hour, d.histo.total);
    dessineTendance(d.quotidien.points);
    dessineHeatmap(d.chaleur.cells, d.chaleur.max_count, d.chaleur.total);

    const pDashboard = $('player');
    const svgPlay = '<svg viewBox="0 0 24 24" width="11" height="11" fill="currentColor" aria-hidden="true"><polygon points="6 3 20 12 6 21 6 3"/></svg>';
    const svgPause = '<svg viewBox="0 0 24 24" width="11" height="11" fill="currentColor" aria-hidden="true"><rect x="5" y="4" width="4" height="16" rx="1"/><rect x="15" y="4" width="4" height="16" rx="1"/></svg>';

    tableau($('table-timeline'), ['', 'N°', 'Date', 'Score'],
      d.timeline.points.slice().reverse().slice(0, 200).map((pt) => {
        const estEnLecture = state.dernierPoint && state.dernierPoint.id === pt.id && pDashboard && !pDashboard.paused;
        const icon = estEnLecture ? svgPause : svgPlay;
        const btnCls = 'btn-play-row' + (estEnLecture ? ' en-lecture' : '');
        return [
          '<button type="button" class="' + btnCls + '" data-id="' + pt.id + '" data-t="' + pt.t + '" data-score="' + pt.score + '" title="Écouter">' + icon + '</button>',
          'n° ' + pt.id,
          fmt().heureMinute.format(new Date(pt.t)),
          decimal(pt.score.toFixed(3)),
        ];
      }));
    tableau($('table-histogram'), ['Heure', 'Événements'],
      d.histo.buckets.map((b) => [String(b.hour).padStart(2, '0') + ' h', b.count]));
    tableau($('table-daily'), ['Jour', 'Événements'],
      d.quotidien.points.map((pt) => [pt.date, pt.count]));

    // Les données d'export sont construites ici, à partir des mêmes réponses
    // que les graphiques — jamais relues depuis le DOM.
    const periode = periodeFichier();
    exportables.timeline = {
      nom: 'noisygram-timeline-' + periode + '.csv',
      entetes: ['id', 'horodatage_utc', 'score'],
      // TOUS les points chargés, pas les 200 du tableau dépliable : un export
      // sert à analyser, pas à relire ce qu'on a déjà sous les yeux.
      lignes: d.timeline.points.map((pt) => [
         pt.id, new Date(pt.t).toISOString(), decimal(pt.score.toFixed(3)),
      ]),
    };
    exportables.histogram = {
      nom: 'noisygram-repartition-horaire-' + periode + '.csv',
      entetes: ['heure', 'evenements'],
      lignes: d.histo.buckets.map((b) => [String(b.hour).padStart(2, '0'), b.count]),
    };
    exportables.daily = {
      nom: 'noisygram-tendance-quotidienne-' + periode + '.csv',
      entetes: ['jour', 'evenements'],
      lignes: d.quotidien.points.map((pt) => [pt.date, pt.count]),
    };
    // La heatmap est une matrice : on exporte exactement la grille affichée,
    // colonne Total comprise, pour que la page et le CSV se lisent pareil.
    const parCle = new Map(d.chaleur.cells.map((c) => [c.dow + ':' + c.hour, c.count]));
    const joursExport = ['Lundi', 'Mardi', 'Mercredi', 'Jeudi', 'Vendredi', 'Samedi', 'Dimanche'];
    exportables.heatmap = {
      nom: 'noisygram-jour-heure-' + periode + '.csv',
      entetes: ['jour'].concat(
        Array.from({ length: 24 }, (_, h) => String(h).padStart(2, '0')), ['total']),
      lignes: joursExport.map((nomJour, i) => {
        const heures = Array.from(
          { length: 24 }, (_, h) => parCle.get((i + 1) + ':' + h) || 0);
        return [nomJour].concat(heures, [heures.reduce((a, b) => a + b, 0)]);
      }),
    };

    $('status').hidden = true;
    const tronque = d.timeline.truncated
      ? ' · timeline : ' + d.timeline.points.length + ' points sur ' + d.timeline.total +
        ' (les plus forts)'
      : '';
    $('footer-info').textContent =
      'Fuseau ' + state.tz + ' · seuil ' + decimal(state.threshold.toFixed(2)) + tronque;
  } catch (err) {
    $('status').hidden = false;
    $('status').className = 'status status--error';
    $('status').textContent = 'Chargement impossible : ' + err.message;
  } finally {
    document.body.dataset.loading = 'non';
    state.chargement = false;
  }
}

function rendResume(resume, timeline) {
  $('hero').textContent = resume.count;
  // Dénominateur = heures ÉCOULÉES, pas 24. Diviser par 24 à 9 h fait paraître
  // chaque matin calme et chaque soir alarmant. Et il est écrit, pour qu'on
  // sache sur quoi le chiffre est calculé.
  $('hero-sub').textContent =
    decimal(resume.per_hour) + ' par heure, sur ' +
    decimal(resume.hours_elapsed.toFixed(1)) + ' h écoulées';
  $('t-max').textContent = nombre(resume.max_noisy_score, 3);
  $('t-mean').textContent = nombre(resume.mean_noisy_score, 3);
  $('t-prev').textContent = resume.count_prev_day;
  $('t-week').textContent = resume.count_prev_week_same_day;
  $('header-sub').textContent =
    'Qualification YAMNet · ' + timeline.total + ' événements sur la période';
}

function majLigneTimelineEnLecture() {
  const p = $('player');
  const enLecture = p && !p.paused;
  const table = $('table-timeline');
  if (!table) return;

  const svgPlay = '<svg viewBox="0 0 24 24" width="11" height="11" fill="currentColor" aria-hidden="true"><polygon points="6 3 20 12 6 21 6 3"/></svg>';
  const svgPause = '<svg viewBox="0 0 24 24" width="11" height="11" fill="currentColor" aria-hidden="true"><rect x="5" y="4" width="4" height="16" rx="1"/><rect x="15" y="4" width="4" height="16" rx="1"/></svg>';

  table.querySelectorAll('tbody tr').forEach((tr) => {
    const btn = tr.querySelector('.btn-play-row');
    if (!btn) return;
    const ptId = Number(btn.dataset.id);
    const estCePoint = state.dernierPoint && state.dernierPoint.id === ptId;
    if (estCePoint && enLecture) {
      tr.classList.add('ligne-en-lecture');
      btn.classList.add('en-lecture');
      btn.innerHTML = svgPause;
      btn.title = 'Mettre en pause';
    } else {
      tr.classList.remove('ligne-en-lecture');
      btn.classList.remove('en-lecture');
      btn.innerHTML = svgPlay;
      btn.title = 'Écouter';
    }
  });
}

function joue(point) {
  const audio = $('player');

  // Si l'événement est déjà en cours de lecture et qu'on reclique : bascule en pause
  if (state.dernierPoint && state.dernierPoint.id === point.id && audio && !audio.paused) {
    audio.pause();
    majLigneTimelineEnLecture();
    return;
  }

  // Le bouton « toute la rafale » ne peut exister qu'après un clic : avant, on
  // ne sait pas de quel événement on parlerait.
  state.dernierPoint = point;
  $('rafale').hidden = false;
  $('rafale-etat').hidden = true;

  const btnFaux = $('marquer-faux');
  if (btnFaux) {
    btnFaux.hidden = false;
    btnFaux.onclick = async () => {
      if (confirm('Marquer la capture n°' + point.id + ' comme faux positif ? Elle sera retirée des graphiques.')) {
        try {
          const res = await fetch('/api/events/' + point.id + '/disapprove', { method: 'POST' });
          if (res.ok) {
            audio.pause();
            btnFaux.hidden = true;
            $('rafale').hidden = true;
            $('player-sub').textContent = 'Capture n°' + point.id + ' marquée comme fausse.';
            majLigneTimelineEnLecture();
            rafraichis();
          }
        } catch (e) {
          alert('Erreur lors de la désapprobation : ' + e);
        }
      }
    };
  }

  $('player-sub').textContent =
    'Capture n°' + point.id + ' · ' + fmt().heureMinute.format(new Date(point.t)) + ' · chargement…';

  // L'URL est demandée à l'API plutôt que devinée : le chemin de rangement
  // (date LOCALE, identifiant d'événement) appartient au serveur, et le
  // reconstruire ici le figerait côté client.
  fetch('/api/events/' + point.id)
    .then((r) => r.json())
    .then((e) => {
      libereBlob();
      audio.src = e.mp3_url;
      audio.play().then(() => {
        majLigneTimelineEnLecture();
      }).catch(() => {
        majLigneTimelineEnLecture();
      });
      $('player-sub').textContent =
        'Capture n°' + point.id + ' · ' + fmt().heureMinute.format(new Date(point.t)) + ' · score ' + decimal(point.score.toFixed(3));
      majLigneTimelineEnLecture();
    })
    .catch(() => {
      $('player-sub').textContent = 'Capture n°' + point.id + ' introuvable.';
      majLigneTimelineEnLecture();
    });
}

// ------------------------------------------------------------ rafale

// Fréquence de décodage. YAMNet ne voit rien au-dessus de 8 kHz et le MP3
// archivé est déjà du 16 kHz : décoder à la fréquence native du contexte
// (48 kHz) triplerait la mémoire sans rien apporter.
const SR_SEQUENCE = 16000;
const LOT_DECODAGE = 4; // requêtes simultanées : une box n'aime pas en voir 60

function libereBlob() {
  if (state.blobUrl) {
    URL.revokeObjectURL(state.blobUrl);
    state.blobUrl = null;
  }
}

/** Les MP3 ne passent pas par api() : son en-tête Accept: application/json et
 *  son r.json() ne savent pas lire de l'audio. */
async function fetchBin(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(url + ' → HTTP ' + r.status);
  return r.arrayBuffer();
}

/** En-tête RIFF de 44 octets. En LITTLE-ENDIAN : oublier le `true` du
 *  troisième argument de setUint32 donne un WAV illisible, sans erreur. */
function wavHeader(octetsDonnees, sampleRate, channels) {
  const v = new DataView(new ArrayBuffer(44));
  const texte = (off, s) => {
    for (let i = 0; i < s.length; i++) v.setUint8(off + i, s.charCodeAt(i));
  };
  texte(0, 'RIFF');
  v.setUint32(4, 36 + octetsDonnees, true);
  texte(8, 'WAVE');
  texte(12, 'fmt ');
  v.setUint32(16, 16, true); // taille du bloc fmt
  v.setUint16(20, 1, true); // PCM
  v.setUint16(22, channels, true);
  v.setUint32(24, sampleRate, true);
  v.setUint32(28, sampleRate * channels * 2, true); // octets par seconde
  v.setUint16(32, channels * 2, true); // alignement de bloc
  v.setUint16(34, 16, true); // bits par échantillon
  texte(36, 'data');
  v.setUint32(40, octetsDonnees, true);
  return new Uint8Array(v.buffer);
}

/**
 * Décode un MP3 avec le décodeur DU NAVIGATEUR — le serveur n'en embarque
 * aucun, c'est un principe assumé du projet, et c'est ce qui permet de
 * recoller sans lui faire décoder quoi que ce soit.
 *
 * Le contexte est jetable et à la fréquence voulue : `decodeAudioData` rend des
 * échantillons À LA FRÉQUENCE DU CONTEXTE, donc en créer un par défaut ferait
 * décoder en 48 kHz. Un ArrayBuffer NEUF par clip, aussi : decodeAudioData le
 * détache, et un tampon réutilisé serait vide — lecture silencieuse.
 */
async function decodeClip(url) {
  const octets = await fetchBin(url);
  const ctx = new OfflineAudioContext(1, 1, SR_SEQUENCE);
  try {
    return await ctx.decodeAudioData(octets);
  } finally {
    // Un contexte par clic jamais fermé finit par épuiser le budget
    // d'AudioContext de la page.
    if (ctx.close) ctx.close();
  }
}

/**
 * Recollé bout à bout en un seul WAV.
 *
 * Chaque clip est ramené EXACTEMENT à sa durée annoncée : le codec MP3 ajoute
 * un délai et un remplissage à chaque fichier, et sans cette remise à longueur
 * les coutures dérivent — au bout de quarante clips, l'offset de l'ancre ne
 * tombe plus sur le bon événement, et la durée totale est fausse.
 *
 * La fréquence de l'en-tête vient du décodage, JAMAIS d'un 16000 supposé : si
 * le navigateur a décodé autrement, l'en-tête doit le dire.
 */
function assembleWav(clips) {
  const rate = clips[0].buf.sampleRate;
  const longueurs = clips.map((c) => Math.round((c.durationMs / 1000) * rate));
  const total = longueurs.reduce((a, b) => a + b, 0);
  const pcm = new Int16Array(total);

  let ecrit = 0;
  clips.forEach((c, i) => {
    const src = c.buf.getChannelData(0);
    const n = Math.min(longueurs[i], src.length);
    for (let j = 0; j < n; j++) {
      const v = Math.max(-1, Math.min(1, src[j]));
      pcm[ecrit + j] = v < 0 ? v * 0x8000 : v * 0x7fff;
    }
    ecrit += longueurs[i]; // le reste (padding du codec) reste à zéro
  });

  const donnees = new Uint8Array(pcm.buffer);
  return new Blob([wavHeader(donnees.length, rate, 1), donnees], { type: 'audio/wav' });
}

/**
 * Joue la rafale entière d'un seul « play ».
 *
 * Les segments se touchent quand le son se répète sans discontinuer : les
 * recoller restitue l'enregistrement continu, ce qu'un enchaînement de
 * <audio> sur `ended` ne ferait pas — chaque bascule ajoute un blanc de
 * 100 à 300 ms, soit plusieurs secondes sur une rafale d'une minute.
 */
async function joueRafale() {
  const point = state.dernierPoint;
  if (!point) return;
  const audio = $('player');
  const bouton = $('rafale');
  const etat = $('rafale-etat');

  bouton.disabled = true;
  etat.hidden = false;
  etat.textContent = 'Lecture de la rafale…';

  try {
    const seq = await api('/api/events/sequence/' + point.id);

    const clips = [];
    let manquants = 0;
    for (let i = 0; i < seq.events.length; i += LOT_DECODAGE) {
      const lot = seq.events.slice(i, i + LOT_DECODAGE);
      const faits = await Promise.all(
        lot.map((e) =>
          decodeClip(e.mp3_url)
            .then((buf) => ({ id: e.id, durationMs: e.duration_ms, buf }))
            // Un clip effacé ne doit pas faire échouer toute la rafale : on le
            // saute et on le dit, plutôt que de renvoyer une erreur sèche sur
            // une séquence qu'on pouvait presque entièrement écouter.
            .catch(() => null)
        )
      );
      faits.forEach((f) => (f ? clips.push(f) : manquants++));
      etat.textContent =
        'Assemblage ' + Math.min(i + LOT_DECODAGE, seq.events.length) +
        '/' + seq.events.length + '…';
    }
    if (!clips.length) throw new Error('aucun clip lisible');

    // L'offset de l'ancre est recalculé sur les clips RÉELLEMENT assemblés :
    // ceux qui manquaient décaleraient le repère du serveur.
    let offsetAncre = 0;
    for (const c of clips) {
      if (c.id === seq.anchor_id) break;
      offsetAncre += c.durationMs;
    }

    libereBlob();
    state.blobUrl = URL.createObjectURL(assembleWav(clips));
    audio.src = state.blobUrl;
    audio.addEventListener(
      'loadedmetadata',
      () => { audio.currentTime = offsetAncre / 1000; },
      { once: true }
    );
    audio.play().catch(() => {});

    const secondes = (clips.reduce((a, c) => a + c.durationMs, 0) / 1000).toFixed(0);
    etat.textContent =
      'Rafale : ' + clips.length + ' clip(s) · ' + secondes + ' s' +
      (seq.total > clips.length ? ' · ' + seq.total + ' au total' : '') +
      (manquants ? ' · ' + manquants + ' illisible(s)' : '');
  } catch (err) {
    etat.textContent = 'Rafale indisponible : ' + err.message;
  } finally {
    bouton.disabled = false;
  }
}

// ---------------------------------------------------------------- thème

function estSombre() {
  const actuel = document.documentElement.dataset.theme;
  return actuel
    ? actuel === 'dark'
    : window.matchMedia('(prefers-color-scheme: dark)').matches;
}

function majBoutonTheme() {
  const btn = $('theme');
  if (!btn) return;
  const sombre = estSombre();
  const isFr = (navigator.language || 'fr').toLowerCase().startsWith('fr');
  if (sombre) {
    btn.innerHTML = `<svg class="icon-theme icon-moon" viewBox="0 0 24 24" width="18" height="18" fill="currentColor">
      <path d="M12.3 2a10 10 0 0 0-.19 14 10 10 0 0 0 11.63 2.18A10 10 0 1 1 12.3 2z"/>
      <polygon points="19,2 19.7,3.8 21.5,4.5 19.7,5.2 19,7 18.3,5.2 16.5,4.5 18.3,3.8"/>
    </svg>`;
    const texte = isFr ? 'Passer au mode clair' : 'Switch to light mode';
    btn.title = texte;
    btn.setAttribute('aria-label', texte);
  } else {
    btn.innerHTML = `<svg class="icon-theme icon-sun" viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="#e5a50a" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
      <circle cx="12" cy="12" r="4.5" fill="#e5a50a" stroke="none"/>
      <line x1="12" y1="2" x2="12" y2="4.5"/>
      <line x1="12" y1="19.5" x2="12" y2="22"/>
      <line x1="4.22" y1="4.22" x2="5.99" y2="5.99"/>
      <line x1="18.01" y1="18.01" x2="19.78" y2="19.78"/>
      <line x1="2" y1="12" x2="4.5" y2="12"/>
      <line x1="19.5" y1="12" x2="22" y2="12"/>
      <line x1="4.22" y1="19.78" x2="5.99" y2="18.01"/>
      <line x1="18.01" y1="5.99" x2="19.78" y2="4.22"/>
    </svg>`;
    const texte = isFr ? 'Passer au mode sombre' : 'Switch to dark mode';
    btn.title = texte;
    btn.setAttribute('aria-label', texte);
  }
}

function appliqueTheme(theme) {
  if (theme) document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
  try {
    localStorage.setItem('noisygram.theme', theme || '');
    localStorage.setItem('aboigramme.theme', theme || '');
  } catch (e) {}
  const lienEcouter = document.querySelector('a[href*="/listen/"]');
  if (lienEcouter) {
    lienEcouter.href = 'https://noisy.virtuaworld.org/listen/' + (theme ? '?theme=' + encodeURIComponent(theme) : '');
  }
  majBoutonTheme();
  // Les couleurs sont relues depuis les variables CSS : sans ce redessin, le
  // canvas garderait les teintes de l'ancien thème.
  rafraichis();
}

// ------------------------------------------------------- actualisation

/**
 * Le dashboard ne reçoit AUCUNE poussée du serveur, et ce n'est pas un oubli.
 *
 * La capture et l'admin sont deux PROCESSUS distincts qui ne partagent que
 * PostgreSQL : un événement écrit par l'un n'existe pour l'autre que dans la
 * base. Et les deux sites passent par un reverse proxy. Une connexion tenue
 * ouverte (SSE, WebSocket) demanderait donc de toucher à ce proxy, et
 * tomberait au premier hoquet réseau sans reprise propre.
 *
 * On interroge donc « qu'est-ce qui est arrivé depuis l'id N » à cadence
 * courte : latence de l'ordre de la seconde, rien à configurer nulle part, et
 * une coupure se rattrape toute seule au tour suivant.
 */
const DIRECT = {
  intervalleMs: 2000,     // cadence nominale
  intervalleMaxMs: 30000, // plafond du ralentissement en cas d'échec
  debounceMs: 1000,       // regroupement avant redessin
};

const direct = {
  dernierId: null, // null = pas encore synchronisé
  minuteur: null,
  rafraichissement: null,
  echecs: 0,
  enAttente: 0,
};

function voyant(etat, texte) {
  $('direct').dataset.etat = etat;
  $('direct-texte').textContent = texte;
}

/** Le délai DOUBLE à chaque échec jusqu'au plafond : un serveur en difficulté
 *  n'est pas martelé, et on ne renonce jamais — le direct reprend seul. */
function programmeInterrogation(delai) {
  clearTimeout(direct.minuteur);
  direct.minuteur = setTimeout(interroge, delai === undefined ? DIRECT.intervalleMs : delai);
}

/** Dix événements en trois secondes ne doivent pas déclencher dix
 *  rechargements complets : on annonce tout de suite, on redessine après. */
function annonceNouveautes(n) {
  direct.enAttente += n;
  voyant('nouveau', direct.enAttente + (direct.enAttente > 1 ? ' nouveaux' : ' nouveau'));
  clearTimeout(direct.rafraichissement);
  direct.rafraichissement = setTimeout(() => {
    rafraichis().finally(() => {
      if (!direct.enAttente) voyant('direct', 'en direct');
    });
  }, DIRECT.debounceMs);
}

async function interroge() {
  // Onglet caché : on ne consomme rien. Le retour au premier plan interroge
  // immédiatement, donc rien n'est manqué — juste économisé.
  if (document.hidden) {
    voyant('pause', 'en pause');
    return;
  }
  try {
    const url = direct.dernierId === null
      ? '/api/events/since' // synchronisation : ne rapatrie rien
      : '/api/events/since?after_id=' + direct.dernierId;
    const r = await fetch(url, { headers: { Accept: 'application/json' } });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();

    const premiere = direct.dernierId === null;
    direct.dernierId = d.last_id;
    direct.echecs = 0;
    if (premiere || (d.count === 0 && !direct.enAttente)) voyant('direct', 'en direct');
    if (!premiere && d.count > 0) annonceNouveautes(d.count);
    programmeInterrogation();
  } catch (err) {
    direct.echecs++;
    const delai = Math.min(
      DIRECT.intervalleMs * Math.pow(2, direct.echecs),
      DIRECT.intervalleMaxMs
    );
    voyant('erreur', 'hors ligne — nouvel essai dans ' + Math.round(delai / 1000) + ' s');
    programmeInterrogation(delai);
  }
}

// ---------------------------------------------------------------- amorçage

(function init() {
  const urlTheme = new URLSearchParams(window.location.search).get('theme');
  const enregistre = urlTheme || (() => {
    try { return localStorage.getItem('noisygram.theme') || localStorage.getItem('aboigramme.theme'); } catch (e) { return null; }
  })();
  if (enregistre) {
    document.documentElement.dataset.theme = enregistre;
    try {
      localStorage.setItem('noisygram.theme', enregistre);
      localStorage.setItem('aboigramme.theme', enregistre);
    } catch (e) {}
  }
  const lienEcouter = document.querySelector('a[href*="/listen/"]');
  if (lienEcouter && enregistre) {
    lienEcouter.href = 'https://noisy.virtuaworld.org/listen/?theme=' + encodeURIComponent(enregistre);
  }
  majBoutonTheme();

  setRangeHeures(24);

  document.querySelectorAll('.filters [data-range]').forEach((b) => {
    b.addEventListener('click', () => {
      document.querySelectorAll('.filters [data-range]').forEach((x) => x.classList.remove('active'));
      b.classList.add('active');
      setRangeHeures(Number(b.dataset.range));
      rafraichis();
    });
  });

  $('apply').addEventListener('click', () => {
    const f = $('from').value, t = $('to').value;
    if (!f || !t) return;
    state.range = { from: new Date(f + 'T00:00:00'), to: new Date(t + 'T23:59:59') };
    document.querySelectorAll('.filters [data-range]').forEach((x) => x.classList.remove('active'));
    rafraichis();
  });

  $('theme').addEventListener('click', () => {
    appliqueTheme(estSombre() ? 'light' : 'dark');
  });

  window.addEventListener('storage', (e) => {
    if (e.key === 'noisygram.theme' || e.key === 'aboigramme.theme') {
      appliqueTheme(e.newValue || null);
    }
  });

  // Événements du lecteur audio du dashboard
  const pAudio = $('player');
  if (pAudio) {
    pAudio.addEventListener('pause', () => {
      setTimeout(majLigneTimelineEnLecture, 50);
    });
    pAudio.addEventListener('ended', () => {
      majLigneTimelineEnLecture();
    });
    pAudio.addEventListener('play', () => {
      majLigneTimelineEnLecture();
    });
  }

  // Les boutons « écouter » et clics de ligne dans la vue tableau timeline.
  document.addEventListener('click', (e) => {
    const table = $('table-timeline');
    if (table && table.contains(e.target)) {
      const tr = e.target.closest('tbody tr');
      if (!tr) return;
      const btn = tr.querySelector('.btn-play-row');
      if (!btn) return;
      joue({ id: Number(btn.dataset.id), t: Number(btn.dataset.t), score: Number(btn.dataset.score || 0) });
      return;
    }
    const b = e.target.closest('button[data-id]');
    if (!b) return;
    joue({ id: Number(b.dataset.id), t: Number(b.dataset.t), score: Number(b.dataset.score || 0) });
  });

  // Le bouton « écouter toute la rafale » rejoue le dernier point cliqué.
  $('rafale').addEventListener('click', joueRafale);

  // ------------------------------------------------------------- Quality Center
  function initQC() {
    const btnQC = $('btn-qc');
    const modalQC = $('modal-qc');
    const btnFermer = $('qc-fermer');
    if (!btnQC || !modalQC) return;

    btnQC.addEventListener('click', () => {
      modalQC.showModal();
      const p = $('player');
      if (p && !p.paused) p.pause();
      chargeQC();
    });

    if (btnFermer) {
      btnFermer.addEventListener('click', () => {
        modalQC.close();
      });
    }

    modalQC.addEventListener('click', (e) => {
      if (e.target === modalQC) modalQC.close();
    });

    modalQC.addEventListener('close', () => {
      const p = $('qc-player');
      if (p) p.pause();
    });

    const btnSauver = $('qc-sauver-seuils');
    if (btnSauver) {
      btnSauver.addEventListener('click', async () => {
        await sauveSeuilsQC();
      });
    }

    // Onglets Candidats / Best-of
    const tabCand = $('qc-tab-candidats');
    const tabBestof = $('qc-tab-bestof');
    const vueCand = $('qc-vue-candidats');
    const vueBestof = $('qc-vue-bestof');

    if (tabCand && tabBestof) {
      tabCand.addEventListener('click', () => {
        tabCand.classList.add('active');
        tabCand.setAttribute('aria-selected', 'true');
        tabBestof.classList.remove('active');
        tabBestof.setAttribute('aria-selected', 'false');
        if (vueCand) vueCand.hidden = false;
        if (vueBestof) vueBestof.hidden = true;
      });

      tabBestof.addEventListener('click', () => {
        tabBestof.classList.add('active');
        tabBestof.setAttribute('aria-selected', 'true');
        tabCand.classList.remove('active');
        tabCand.setAttribute('aria-selected', 'false');
        if (vueCand) vueCand.hidden = true;
        if (vueBestof) vueBestof.hidden = false;
      });
    }

    const btnRebuild = $('qc-rebuild-btn');
    if (btnRebuild) {
      btnRebuild.addEventListener('click', async () => {
        btnRebuild.disabled = true;
        btnRebuild.textContent = 'Calcul…';
        try {
          const res = await fetch('/api/qc/rebuild', { method: 'POST' });
          if (res.ok) {
            alert('Empreinte de référence master recalculée avec succès.');
          } else {
            alert('Erreur recalcul empreinte : HTTP ' + res.status);
          }
        } catch (err) {
          alert('Erreur recalcul empreinte : ' + err);
        } finally {
          btnRebuild.disabled = false;
          btnRebuild.textContent = 'Reconstruire';
          chargeQC();
        }
      });
    }
  }

  async function chargeQC() {
    const statutSeuils = $('qc-statut-seuils');
    if (statutSeuils) statutSeuils.textContent = '';
    try {
      const resConfig = await fetch('/api/qc/config');
      if (resConfig.ok) {
        const data = await resConfig.json();
        const badgeStatus = $('qc-service-status');
        if (badgeStatus) {
          const ok = data.health && data.health.status === 'ok';
          badgeStatus.textContent = ok ? 'QC Actif' : 'QC Hors-ligne';
          badgeStatus.style.background = ok ? 'color-mix(in srgb, #22c55e 18%, transparent)' : 'color-mix(in srgb, var(--danger) 18%, transparent)';
          badgeStatus.style.color = ok ? '#22c55e' : 'var(--danger)';
        }
        afficheSeuilsQC(data.grid || {});
      }

      const resCand = await fetch('/api/qc/candidates?limit=100');
      if (resCand.ok) {
        const candidats = await resCand.json();
        afficheCandidatsQC(candidats);
        const candCount = $('qc-candidats-count');
        if (candCount) candCount.textContent = String(candidats.length);
      }

      const resSnip = await fetch('/api/qc/snippets');
      if (resSnip.ok) {
        const snippets = await resSnip.json();
        afficheBestofQC(snippets);
        const bestCount = $('qc-bestof-count');
        if (bestCount) bestCount.textContent = snippets.length + ' extrait(s)';
        const refCount = $('qc-ref-count');
        if (refCount) refCount.textContent = snippets.length + ' active(s)';
      }
    } catch (err) {
      console.warn('Erreur chargement QC:', err);
    }
  }

  function afficheSeuilsQC(grid) {
    const corps = $('qc-seuils-corps');
    if (!corps) return;
    corps.innerHTML = '';
    const brackets = [
      { key: '<=0.5s', label: '≤ 0.5 s' },
      { key: '<=1s', label: '≤ 1.0 s' },
      { key: '<=2s', label: '≤ 2.0 s' },
      { key: '<=3s', label: '≤ 3.0 s' },
      { key: '<=4s', label: '≤ 4.0 s' },
      { key: '<=5s', label: '≤ 5.0 s' },
      { key: '<=6s', label: '≤ 6.0 s' },
      { key: '<=7s', label: '≤ 7.0 s' },
      { key: '<=10s', label: '≤ 10.0 s' },
      { key: '>10s', label: '> 10.0 s' },
    ];

    for (const b of brackets) {
      const tr = document.createElement('tr');
      const tdLabel = document.createElement('td');
      tdLabel.textContent = b.label;
      tdLabel.style.fontWeight = '500';

      const tdInput = document.createElement('td');
      tdInput.style.textAlign = 'right';
      const inp = document.createElement('input');
      inp.type = 'number';
      inp.min = '0';
      inp.max = '100';
      inp.step = '1';
      inp.className = 'qc-input-thresh';
      inp.dataset.bracket = b.key;
      inp.value = grid[b.key] != null ? grid[b.key] : 50;

      tdInput.appendChild(inp);
      tdInput.appendChild(document.createTextNode(' %'));
      tr.appendChild(tdLabel);
      tr.appendChild(tdInput);
      corps.appendChild(tr);
    }
  }

  async function sauveSeuilsQC() {
    const corps = $('qc-seuils-corps');
    const statut = $('qc-statut-seuils');
    if (!corps) return;
    const inputs = corps.querySelectorAll('input.qc-input-thresh');
    const grid = {};
    inputs.forEach((inp) => {
      grid[inp.dataset.bracket] = parseFloat(inp.value) || 0;
    });

    if (statut) statut.textContent = 'Enregistrement…';
    try {
      const r = await fetch('/api/qc/config', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ grid }),
      });
      if (r.ok) {
        if (statut) statut.textContent = '✓ Enregistré';
        setTimeout(() => { if (statut) statut.textContent = ''; }, 3000);
      } else {
        if (statut) statut.textContent = 'Erreur HTTP ' + r.status;
      }
    } catch (err) {
      if (statut) statut.textContent = 'Erreur réseau';
    }
  }

  function afficheCandidatsQC(candidats) {
    const corps = $('qc-candidats-corps');
    const player = $('qc-player');
    const playerSub = $('qc-player-sub');
    if (!corps) return;
    corps.innerHTML = '';

    const svgPlay = '<svg viewBox="0 0 24 24" width="11" height="11" fill="currentColor"><polygon points="6 3 20 12 6 21 6 3"/></svg>';
    const svgPause = '<svg viewBox="0 0 24 24" width="11" height="11" fill="currentColor"><rect x="5" y="4" width="4" height="16" rx="1"/><rect x="15" y="4" width="4" height="16" rx="1"/></svg>';

    for (const c of candidats) {
      const tr = document.createElement('tr');
      if (c.is_reference) tr.classList.add('row-ref');

      // Bouton Play
      const tdPlay = document.createElement('td');
      const btnPlay = document.createElement('button');
      btnPlay.type = 'button';
      btnPlay.className = 'btn-play-row';
      btnPlay.innerHTML = svgPlay;
      btnPlay.title = 'Écouter la capture';
      btnPlay.addEventListener('click', (ev) => {
        ev.stopPropagation();
        if (player.src === c.mp3_url && !player.paused) {
          player.pause();
          btnPlay.innerHTML = svgPlay;
        } else {
          player.src = c.mp3_url;
          player.play().catch(() => {});
          btnPlay.innerHTML = svgPause;
          if (playerSub) {
            const d = new Date(c.detected_at);
            playerSub.textContent = 'Capture n°' + c.id + ' · ' + d.toLocaleTimeString('fr-FR') + ' · durée ' + (c.duration_ms / 1000).toFixed(1) + 's';
          }
        }
      });
      tdPlay.appendChild(btnPlay);
      tr.appendChild(tdPlay);

      // N°
      const tdId = document.createElement('td');
      tdId.textContent = 'n° ' + c.id;
      tr.appendChild(tdId);

      // Date
      const tdDate = document.createElement('td');
      const d = new Date(c.detected_at);
      tdDate.textContent = d.toLocaleString('fr-FR', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
      tr.appendChild(tdDate);

      // Durée
      const tdDur = document.createElement('td');
      tdDur.textContent = (c.duration_ms / 1000).toFixed(1) + ' s';
      tr.appendChild(tdDur);

      // Score QC
      const tdQC = document.createElement('td');
      if (c.qc_score != null) {
        tdQC.textContent = (c.qc_score * 100).toFixed(1) + ' %';
      } else {
        tdQC.textContent = '—';
      }
      tr.appendChild(tdQC);

      // Extraits actifs
      const tdSnips = document.createElement('td');
      tdSnips.style.textAlign = 'center';
      if (c.snippets_count > 0) {
        const badge = document.createElement('span');
        badge.className = 'badge-snippet-count';
        badge.textContent = c.snippets_count + ' réf';
        tdSnips.appendChild(badge);
      } else {
        tdSnips.textContent = '—';
        tdSnips.style.opacity = '0.5';
      }
      tr.appendChild(tdSnips);

      // Action Exclure
      const tdAction = document.createElement('td');
      const isRefused = c.backend && c.backend.includes('refused');
      if (!isRefused) {
        const btnFaux = document.createElement('button');
        btnFaux.type = 'button';
        btnFaux.className = 'btn-danger';
        btnFaux.textContent = 'Exclure';
        btnFaux.title = 'Marquer comme Faux Négatif car Positif';
        btnFaux.addEventListener('click', async (ev) => {
          ev.stopPropagation();
          if (confirm('Marquer la capture n°' + c.id + ' comme Faux Négatif car Positif ?')) {
            try {
              await fetch('/api/events/' + c.id + '/disapprove', { method: 'POST' });
              await chargeQC();
              rafraichis();
            } catch (e) {
              alert('Erreur: ' + e);
            }
          }
        });
        tdAction.appendChild(btnFaux);
      } else {
        const spanExclu = document.createElement('span');
        spanExclu.className = 'card-sub';
        spanExclu.style.fontSize = '11px';
        spanExclu.style.opacity = '0.6';
        spanExclu.textContent = 'Exclu';
        tdAction.appendChild(spanExclu);
      }
      tr.appendChild(tdAction);

      corps.appendChild(tr);
    }
  }

  function afficheBestofQC(snippets) {
    const corps = $('qc-bestof-corps');
    const vide = $('qc-bestof-vide');
    const player = $('qc-player');
    const playerSub = $('qc-player-sub');
    if (!corps) return;
    corps.innerHTML = '';

    if (!snippets || snippets.length === 0) {
      if (vide) vide.hidden = false;
      return;
    }
    if (vide) vide.hidden = true;

    const svgPlay = '<svg viewBox="0 0 24 24" width="11" height="11" fill="currentColor"><polygon points="6 3 20 12 6 21 6 3"/></svg>';
    const svgPause = '<svg viewBox="0 0 24 24" width="11" height="11" fill="currentColor"><rect x="5" y="4" width="4" height="16" rx="1"/><rect x="15" y="4" width="4" height="16" rx="1"/></svg>';

    for (const s of snippets) {
      const tr = document.createElement('tr');

      // Play
      const tdPlay = document.createElement('td');
      const btnPlay = document.createElement('button');
      btnPlay.type = 'button';
      btnPlay.className = 'btn-play-row';
      btnPlay.innerHTML = svgPlay;
      btnPlay.title = 'Écouter cet extrait';
      btnPlay.addEventListener('click', (ev) => {
        ev.stopPropagation();
        if (player.src.endsWith(s.audio_url) && !player.paused) {
          player.pause();
          btnPlay.innerHTML = svgPlay;
        } else {
          player.src = s.audio_url;
          player.play().catch(() => {});
          btnPlay.innerHTML = svgPause;
          if (playerSub) {
            playerSub.textContent = 'Extrait Best-of · Capture n°' + (s.event_id || '—') + ' · ' + s.debut_s.toFixed(2) + 's à ' + s.fin_s.toFixed(2) + 's (' + s.duration_s.toFixed(1) + 's)';
          }
        }
      });
      tdPlay.appendChild(btnPlay);
      tr.appendChild(tdPlay);

      // Source
      const tdSrc = document.createElement('td');
      tdSrc.textContent = s.event_id ? 'Capture n°' + s.event_id : s.wav_name;
      tdSrc.title = s.wav_name;
      tr.appendChild(tdSrc);

      // Intervalle
      const tdInt = document.createElement('td');
      tdInt.textContent = s.debut_s.toFixed(2) + 's à ' + s.fin_s.toFixed(2) + 's';
      tr.appendChild(tdInt);

      // Durée
      const tdDur = document.createElement('td');
      tdDur.textContent = s.duration_s.toFixed(1) + ' s';
      tr.appendChild(tdDur);

      // Son / Score
      const tdScore = document.createElement('td');
      const label = s.sound ? s.sound.replace('⚠ événement détecté dans cette fenêtre', '').trim() : '—';
      const sc = s.score != null ? decimal(s.score.toFixed(3)) : '—';
      tdScore.textContent = label + ' (' + sc + ')';
      tr.appendChild(tdScore);

      // Action Supprimer
      const tdDel = document.createElement('td');
      tdDel.style.textAlign = 'center';
      const btnDel = document.createElement('button');
      btnDel.type = 'button';
      btnDel.className = 'btn-delete-snippet';
      btnDel.innerHTML = '🗑';
      btnDel.title = 'Retirer du catalogue Best-of';
      btnDel.addEventListener('click', async (ev) => {
        ev.stopPropagation();
        if (confirm('Retirer cet extrait du Best-of ?')) {
          try {
            const res = await fetch('/api/qc/snippets/' + s.id, { method: 'DELETE' });
            if (res.ok) {
              await chargeQC();
            } else {
              alert('Erreur lors de la suppression');
            }
          } catch (e) {
            alert('Erreur réseau : ' + e);
          }
        }
      });
      tdDel.appendChild(btnDel);
      tr.appendChild(tdDel);

      corps.appendChild(tr);
    }
  }

  // ------------------------------------------------------------- Projet
  //
  // Les champs du formulaire sont créés par ce code, donc absents du HTML : on
  // garde leurs RÉFÉRENCES plutôt que de les chercher par identifiant, ce que
  // le test de câblage interdit — et il a raison.
  const refsNouveau = { nom: null, terme: null, classes: null, creer: null };

  function noteProjet(texte, genre) {
    const n = $('projet-note');
    if (!n) return;
    n.textContent = texte || '';
    n.hidden = !texte;
    n.classList.toggle('avert', genre === 'avert');
    n.classList.toggle('ok', genre === 'ok');
  }

  function carteProjet(projet, actif) {
    const carte = document.createElement('div');
    carte.className = 'projet-carte' + (actif ? ' projet-carte--actif' : '');

    const tete = document.createElement('div');
    tete.className = 'projet-tete';
    const nom = document.createElement('strong');
    nom.textContent = (actif ? '● ' : '○ ') + (projet.nom || '(sans nom)');
    tete.appendChild(nom);
    if (actif) {
      const badge = document.createElement('span');
      badge.className = 'projet-badge';
      badge.textContent = 'surveillé par la capture';
      tete.appendChild(badge);
    }
    carte.appendChild(tete);

    const details = document.createElement('p');
    details.className = 'card-sub';
    details.textContent =
      (projet.terme ? 'terme « ' + projet.terme + ' » — ' : '') +
      'seuil ' + (projet.seuil == null ? 'non calibré' : projet.seuil.toFixed(2)) +
      ' — ' + projet.classes.length + ' classe(s) : ' + projet.classes.join(', ');
    carte.appendChild(details);

    const actions = document.createElement('div');
    actions.className = 'projet-actions';

    if (!actif) {
      const voir = document.createElement('button');
      voir.type = 'button';
      voir.textContent = 'Voir';
      voir.title = 'Afficher ce projet sans rien interrompre';
      voir.addEventListener('click', () => voirProjet(projet));
      actions.appendChild(voir);
      const surveiller = document.createElement('button');
      surveiller.type = 'button';
      surveiller.className = 'btn-primary';
      surveiller.textContent = 'Surveiller ce projet';
      surveiller.addEventListener('click', () => activeProjet(projet));
      actions.appendChild(surveiller);
    }

    const renommer = document.createElement('button');
    renommer.type = 'button';
    renommer.textContent = 'Renommer';
    renommer.title = 'Changer le nom — ni les classes ni le seuil ne bougent';
    renommer.addEventListener('click', () => renommeProjet(projet));
    actions.appendChild(renommer);

    // La poubelle dit ce qu'elle emporte dans son infobulle : un bouton
    // destructeur qui ne dit pas ce qu'il détruit est un piège.
    const poubelle = document.createElement('button');
    poubelle.type = 'button';
    poubelle.className = 'btn-poubelle';
    poubelle.textContent = '🗑';
    poubelle.setAttribute('aria-label', 'Supprimer le projet ' + projet.nom);
    poubelle.title = projet.n_evenements
      ? 'Supprimer « ' + projet.nom + ' » et ses ' + projet.n_evenements + ' événement(s)'
      : 'Supprimer « ' + projet.nom + ' »';
    poubelle.addEventListener('click', () => supprimeProjet(projet));
    actions.appendChild(poubelle);

    carte.appendChild(actions);
    return carte;
  }

  async function renommeProjet(projet) {
    const nom = window.prompt('Nouveau nom du projet :', projet.nom);
    if (nom === null) return;
    if (!nom.trim()) { noteProjet('Le nom ne peut pas être vide.', 'avert'); return; }
    try {
      await api('/api/projets/' + projet.id, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ nom: nom.trim() }),
      });
      noteProjet('Projet renommé.', 'ok');
      await rendProjet();
    } catch (err) {
      noteProjet('Renommage refusé : ' + err.message, 'avert');
    }
  }

  async function supprimeProjet(projet) {
    const quoi = projet.n_evenements
      ? '« ' + projet.nom + ' »\nET ses ' + projet.n_evenements + ' événement(s)'
      : '« ' + projet.nom + ' »';
    if (!window.confirm('Supprimer ' + quoi + ' ?\n\nCette action est IRRÉVERSIBLE.')) return;
    try {
      const r = await api('/api/projets/' + projet.id, { method: 'DELETE' });
      // Si on CONSULTAIT ce projet, la vue n'a plus de sens : on revient à
      // l'actif, sinon les graphiques resteraient vides sans qu'on sache pourquoi.
      if (projetVue === projet.id) memoriseProjetVue(null);
      noteProjet(r.message, 'ok');
      await rendProjet();
    } catch (err) {
      noteProjet('Suppression refusée : ' + err.message, 'avert');
    }
  }

  async function rendProjet() {
    const corps = $('projet-corps');
    if (!corps) return;
    corps.textContent = 'Chargement…';
    let data;
    try { data = await api('/api/projets'); }
    catch (err) { corps.textContent = 'Projets illisibles : ' + err.message; return; }
    corps.textContent = '';

    const enTete = document.createElement('p');
    enTete.className = 'card-sub';
    enTete.textContent = 'La capture ne surveille qu\'un projet à la fois. '
      + 'Consulter est instantané ; surveiller demande un redémarrage de la capture.';
    corps.appendChild(enTete);
    for (const p of data.projets) corps.appendChild(carteProjet(p, p.actif));

    // Le titre et la pastille de droite suivent : la modale vient peut-être de
    // créer, renommer, supprimer ou changer ce qui est surveillé.
    majNomProjet();
  }

  function rendNouveau() {
    const corps = $('projet-corps');
    corps.textContent = '';

    const aide = document.createElement('p');
    aide.className = 'card-sub';
    aide.textContent = 'Il est conseillé de nommer en anglais, et de fournir un '
      + 'court extrait du son cherché. Un projet sans nom ni référence fonctionne '
      + 'aussi : le best-of se remplira ensuite par « + Réf ».';
    corps.appendChild(aide);

    const mk = (label, ph, cle) => {
      const lab = document.createElement('label');
      lab.className = 'projet-champ';
      lab.textContent = label;
      const inp = document.createElement('input');
      inp.type = 'text';
      inp.placeholder = ph;
      lab.appendChild(inp);
      corps.appendChild(lab);
      refsNouveau[cle] = inp;
    };
    mk('Nom du projet ', 'aboiement, chainsaw, ronflements…', 'nom');
    mk('Ce qu\'on cherche à compter ', 'aboiement, tronçonneuse, miaou…', 'terme');

    const chercher = document.createElement('button');
    chercher.type = 'button';
    chercher.textContent = 'Proposer les classes';
    chercher.addEventListener('click', () => proposeClasses(refsNouveau.terme.value));
    corps.appendChild(chercher);

    // --- Deuxième voie : donner un extrait du son -------------------------
    const ou = document.createElement('p');
    ou.className = 'card-sub';
    ou.textContent = 'Ou donne un court extrait du son :';
    corps.appendChild(ou);

    const depot = document.createElement('div');
    depot.className = 'projet-depot';
    depot.textContent = 'Dépose un WAV ou un MP3 ici, ou clique pour choisir';
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = 'audio/wav,audio/mpeg,.wav,.mp3';
    input.hidden = true;
    depot.addEventListener('click', () => input.click());
    depot.addEventListener('dragover', (ev) => { ev.preventDefault(); depot.classList.add('survol'); });
    depot.addEventListener('dragleave', () => depot.classList.remove('survol'));
    depot.addEventListener('drop', (ev) => {
      ev.preventDefault();
      depot.classList.remove('survol');
      if (ev.dataTransfer.files.length) traiteExtrait(ev.dataTransfer.files[0]);
    });
    input.addEventListener('change', () => {
      if (input.files.length) traiteExtrait(input.files[0]);
    });
    corps.appendChild(depot);
    corps.appendChild(input);

    const zone = document.createElement('div');
    zone.className = 'projet-classes-zone';
    corps.appendChild(zone);
    refsNouveau.classes = zone;

    const creer = document.createElement('button');
    creer.type = 'button';
    creer.className = 'btn-primary';
    creer.textContent = 'Créer le projet';
    creer.addEventListener('click', () => creeProjet(refsNouveau.nom.value));
    corps.appendChild(creer);
    refsNouveau.creer = creer;

    $('projet-nouveau').hidden = true;
    $('projet-retour').hidden = false;
  }

  function encodeWav(float32, sr) {
    const n = float32.length;
    const buf = new ArrayBuffer(44 + n * 2);
    const v = new DataView(buf);
    const texte = (off, s) => { for (let i = 0; i < s.length; i++) v.setUint8(off + i, s.charCodeAt(i)); };
    texte(0, 'RIFF');
    v.setUint32(4, 36 + n * 2, true);
    texte(8, 'WAVE');
    texte(12, 'fmt ');
    v.setUint32(16, 16, true);
    v.setUint16(20, 1, true);          // PCM
    v.setUint16(22, 1, true);          // mono
    v.setUint32(24, sr, true);
    v.setUint32(28, sr * 2, true);
    v.setUint16(32, 2, true);
    v.setUint16(34, 16, true);
    texte(36, 'data');
    v.setUint32(40, n * 2, true);
    for (let i = 0; i < n; i++) {
      const s = Math.max(-1, Math.min(1, float32[i]));
      v.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    }
    return buf;
  }

  async function versWav(file) {
    const buf = await file.arrayBuffer();
    if (/\.wav$/i.test(file.name) || /wav/i.test(file.type || '')) return buf;

    // ⚠️ Sinon on décode ICI, dans le navigateur : le serveur n'embarque aucun
    // décodeur MP3, par choix délibéré (350 Mo de ffmpeg évités). Le navigateur,
    // lui, décode nativement.
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) throw new Error('ce navigateur ne sait pas décoder ce format');
    const ctx = new Ctx();
    try {
      const audio = await ctx.decodeAudioData(buf);
      const n = audio.length;
      const mono = new Float32Array(n);
      for (let c = 0; c < audio.numberOfChannels; c++) {
        const d = audio.getChannelData(c);
        for (let i = 0; i < n; i++) mono[i] += d[i] / audio.numberOfChannels;
      }
      return encodeWav(mono, audio.sampleRate);
    } finally {
      if (ctx.close) ctx.close();
    }
  }

  async function traiteExtrait(fichier) {
    const zone = refsNouveau.classes;
    if (!zone) return;
    noteProjet('');
    zone.textContent = 'Lecture du fichier…';
    let wav;
    try { wav = await versWav(fichier); }
    catch (err) { zone.textContent = ''; noteProjet('Fichier illisible : ' + err.message, 'avert'); return; }

    zone.textContent = 'Analyse par le modèle…';
    try {
      const r = await fetch(avecProjet('/api/projets/extrait'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/octet-stream' },
        body: wav,
      });
      if (!r.ok) {
        let detail = 'HTTP ' + r.status;
        try { const c = await r.json(); if (c && c.detail) detail = c.detail; } catch (e) { /* non-JSON */ }
        throw new Error(detail);
      }
      const data = await r.json();
      afficheClasses(data.classes);
      noteProjet('Extrait de ' + data.duree_s + ' s analysé. Décoche les classes '
        + 'qui ne conviennent pas.', 'ok');
    } catch (err) {
      zone.textContent = '';
      noteProjet('Analyse impossible : ' + err.message, 'avert');
    }
  }

  function afficheClasses(items) {
    const zone = refsNouveau.classes;
    if (!zone) return;
    zone.textContent = '';
    if (!items || !items.length) return;
    const p = document.createElement('p');
    p.className = 'card-sub';
    p.textContent = 'Décoche les classes qui ne conviennent pas :';
    zone.appendChild(p);
    const liste = document.createElement('div');
    liste.className = 'projet-classes';
    for (const c of items) {
      const lab = document.createElement('label');
      lab.className = 'projet-classe';
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      // La VALEUR est le nom du modèle : c'est lui qui part en base. Le
      // français n'est qu'un affichage.
      cb.value = c.nom;
      cb.checked = true;
      cb.dataset.classe = '1';
      const s = document.createElement('span');
      s.textContent = c.nom;
      lab.appendChild(cb);
      lab.appendChild(s);
      if (c.fr && c.fr !== c.nom) {
        const trad = document.createElement('em');
        trad.className = 'projet-classe-fr';
        trad.textContent = '— ' + c.fr;
        lab.appendChild(trad);
      }
      liste.appendChild(lab);
    }
    zone.appendChild(liste);
  }

  async function proposeClasses(terme) {
    const zone = refsNouveau.classes;
    if (!zone) return;
    if (!terme.trim()) { zone.textContent = ''; return; }
    zone.textContent = 'Recherche…';
    let data;
    try { data = await api('/api/projets/proposer?terme=' + encodeURIComponent(terme)); }
    catch (err) { zone.textContent = 'Recherche impossible : ' + err.message; return; }
    zone.textContent = '';

    if (!data.propositions || !data.propositions.length) {
      const p = document.createElement('p');
      p.className = 'card-sub avert';
      p.textContent = 'Aucune classe connue pour ce terme. Tu peux créer le projet '
        + 'sans, ou essayer un autre mot (le vocabulaire du modèle est en anglais).';
      zone.appendChild(p);
      return;
    }
    // Les deux voies aboutissent ICI : nommer et uploader produisent toutes les
    // deux une liste de classes, et c'est la même liste à cocher qui les rend.
    afficheClasses(data.classes);
  }

  async function creeProjet(nom) {
    const zone = refsNouveau.classes;
    const cases = zone ? [...zone.querySelectorAll('input[data-classe]')] : [];
    const classes = cases.filter((c) => c.checked).map((c) => c.value);
    if (!classes.length) {
      noteProjet('Un projet sans aucune classe ne surveillerait rien : propose '
        + 'd\'abord des classes.', 'avert');
      return;
    }
    const terme = refsNouveau.terme ? refsNouveau.terme.value.trim() : '';
    if (refsNouveau.creer) refsNouveau.creer.disabled = true;
    try {
      const r = await api('/api/projets', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ nom: nom.trim() || null, terme: terme || null, classes }),
      });
      noteProjet('Projet « ' + r.projet.nom + ' » créé, INACTIF. ' + r.conseil, 'ok');
      $('projet-nouveau').hidden = false;
      $('projet-retour').hidden = true;
      await rendProjet();
    } catch (err) {
      noteProjet('Création refusée : ' + err.message, 'avert');
    } finally {
      if (refsNouveau.creer) refsNouveau.creer.disabled = false;
    }
  }

  function voirProjet(projet) {
    memoriseProjetVue(projet.id);
    $('projet').close();
    // ⚠️ Le TITRE doit suivre. Sans ça, il annonce encore l'ancien projet après
    // un « Voir » — donc l'en-tête ment sur ce que les graphiques montrent.
    majNomProjet();
    rafraichis();
  }

  async function activeProjet(projet) {
    if (!confirm('Faire surveiller « ' + projet.nom + ' » par la capture ?\n\n'
      + 'La capture devra être REDÉMARRÉE pour en tenir compte '
      + '(docker compose restart capture).')) return;
    try {
      const r = await api('/api/projets/' + projet.id + '/activer', { method: 'POST' });
      noteProjet(r.message, 'ok');
      await rendProjet();
    } catch (err) {
      noteProjet('Activation refusée : ' + err.message, 'avert');
    }
  }

  // Le titre montre le projet CONSULTÉ, la pastille de droite celui que la
  // capture SURVEILLE. Les deux sont distincts : on peut relire un ancien relevé
  // sans interrompre la campagne en cours, et confondre les deux ferait conclure
  // que « la nuit a été calme » alors qu'on lit une autre campagne.
  async function majNomProjet() {
    const consulteEl = $('projet-consulte');
    const surveilleEl = $('projet-surveille');
    try {
      const d = await api('/api/projets');
      const consulte = projetVue == null
        ? d.actif
        : (d.projets || []).find((p) => p.id === projetVue) || d.actif;

      if (consulteEl) {
        consulteEl.textContent = (consulte && consulte.nom) || 'aucun projet';
        const autre = !!(d.actif && consulte && consulte.id !== d.actif.id);
        // Le mot « (consultation) » est ajouté par le CSS : l'état est écrit, pas
        // seulement coloré.
        consulteEl.classList.toggle('consulte', autre);
      }
      if (surveilleEl) {
        surveilleEl.textContent = d.actif ? d.actif.nom : 'aucun projet surveillé';
        surveilleEl.title = d.actif
          ? 'La capture surveille « ' + d.actif.nom + ' »'
          : 'Aucun projet actif : la capture ne surveille rien';
      }
    } catch (err) {
      if (consulteEl) consulteEl.textContent = 'projets illisibles';
    }
  }

  function initProjet() {
    const d = $('projet');
    const btn = $('btn-projet');
    if (!d || !btn) return;
    majNomProjet();
    btn.addEventListener('click', async () => {
      noteProjet('');
      $('projet-nouveau').hidden = false;
      $('projet-retour').hidden = true;
      await rendProjet();
      d.showModal();
    });
    $('projet-fermer').addEventListener('click', () => d.close());
    $('projet-retour').addEventListener('click', () => {
      $('projet-nouveau').hidden = false;
      $('projet-retour').hidden = true;
      noteProjet('');
      rendProjet();
    });
    $('projet-nouveau').addEventListener('click', () => { noteProjet(''); rendNouveau(); });
    d.addEventListener('click', (e) => { if (e.target === d) d.close(); });
  }

  initProjet();
  initQC();

  // Les exports CSV, un par graphique. Le registre est relu à chaque clic :
  // c'est celui du dernier rafraîchissement, donc de la période affichée.
  document.querySelectorAll('button[data-export]').forEach((b) => {
    b.addEventListener('click', () => {
      const e = exportables[b.dataset.export];
      if (!e) return; // avant le premier chargement, il n'y a rien à exporter
      telechargeCsv(e.entetes, e.lignes, e.nom);
    });
  });

  api('/api/health')
    .then((h) => { state.threshold = h.threshold; })
    .catch(() => {})
    .finally(() => {
      rafraichis();
      // Le direct démarre APRÈS le premier rendu : le point de synchronisation
      // part de ce qui est déjà affiché, donc la page n'annonce pas comme
      // « nouveau » ce qu'elle vient de montrer.
      interroge();
    });

  // Revenir sur l'onglet doit rattraper tout de suite, pas attendre le tour
  // suivant — c'est le moment où l'on regarde l'écran.
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
      clearTimeout(direct.minuteur);
      interroge();
    }
  });

  // Le thème système peut changer pendant que la page est ouverte.
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    if (!document.documentElement.dataset.theme) rafraichis();
  });
})();
