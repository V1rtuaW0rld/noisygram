/**
 * Écoute directe du poste extérieur.
 *
 * Deux moitiés indépendantes dans cette page :
 *
 *   1. le DIRECT — un WebSocket qui pousse du PCM s16le 16 kHz, joué au fil de
 *      l'eau par Web Audio ;
 *   2. les SAMPLES — la liste des fichiers déjà enregistrés dans `export/`,
 *      qu'on réécoute et qu'on soumet à YAMNet.
 *
 * La seconde survit à la première : si le direct échoue, on peut toujours
 * réécouter et analyser. C'est délibéré, parce que c'est exactement le cas où
 * on en a le plus besoin.
 *
 * CETTE PAGE N'APPELLE JAMAIS getUserMedia, donc elle n'a pas besoin d'origine
 * sécurisée et ne fait aucun préflight. Recopier ici celui de la page de
 * capture serait une erreur de raisonnement : elle ne fait que JOUER.
 */

'use strict';

const $ = (id) => document.getElementById(id);

// Avance de gigue : le temps qu'on garde d'avance sur la lecture. C'est le
// curseur du compromis de cette page — plus grand, plus robuste aux hoquets du
// réseau mais plus on entend tard ; plus petit, plus « direct » mais le moindre
// retard fait un trou. 120 ms couvre un aller-retour LAN et reste inaudible.
const AVANCE_S = 0.12;

const SR = 16000;

const etat = {
  ws: null,
  tentatives: 0,
  minuterie: null,
  // Contexte audio et graphe. Créés au PREMIER clic : un AudioContext créé sans
  // geste utilisateur démarre « suspended », et il faudrait le réveiller après
  // coup — au moment précis où l'audio arrive.
  ctx: null,
  gain: null,
  prochain: 0,
  muet: false,
  volume: 0.5,
  direct: false,
  listenId: null,
  recu_ms: 0,
};

// ------------------------------------------------------------------ direct

function assureContexte() {
  if (etat.ctx) return true;
  let ctx;
  try {
    // 16 kHz : le taux du flux, donc AUCUN rééchantillonnage à la lecture.
    ctx = new AudioContext({ sampleRate: SR });
  } catch (err) {
    // Certains navigateurs refusent un taux imposé. On prend le défaut : le
    // navigateur rééchantillonnera les tampons, ce qui reste juste — juste un
    // peu plus cher.
    ctx = new AudioContext();
    journal('contexte à ' + ctx.sampleRate + ' Hz — ' + SR + ' ignoré', 'avert');
  }
  etat.ctx = ctx;
  etat.gain = ctx.createGain();
  etat.gain.gain.value = etat.muet ? 0 : etat.volume;
  etat.gain.connect(ctx.destination);
  etat.prochain = 0;
  return true;
}

/**
 * Joue un morceau, PLANIFIÉ à sa place exacte dans la timeline audio.
 *
 * `AudioBufferSourceNode` démarré à un instant calculé, et non un `<audio>` sur
 * `ended` : ce dernier ajoute 100 à 300 ms de blanc entre chaque morceau, tous
 * les 200 ms. La lecture deviendrait un hachis.
 */
function joue(i16) {
  if (!assureContexte()) return;

  const n = i16.length;
  if (!n) return;

  const f32 = new Float32Array(n);
  let somme = 0;
  for (let i = 0; i < n; i++) {
    // /32768 et non /32767 : c'est le diviseur qui garantit de rester dans
    // [-1, 1], comme `pcm16_to_float32` côté serveur.
    const v = i16[i] / 32768;
    f32[i] = v;
    somme += v * v;
  }
  vuMetre(Math.sqrt(somme / n));

  const buf = etat.ctx.createBuffer(1, n, SR);
  buf.copyToChannel(f32, 0);
  const src = etat.ctx.createBufferSource();
  src.buffer = buf;
  src.connect(etat.gain);

  const t = etat.ctx.currentTime;
  // Sous-alimentation : on s'est laissé distancer (hoquet réseau, onglet en
  // arrière-plan). On se RECALE au direct plutôt que d'accumuler du retard —
  // écouter ce qui s'est passé il y a huit secondes n'est plus écouter.
  if (etat.prochain < t + AVANCE_S) etat.prochain = t + AVANCE_S;
  src.start(etat.prochain);
  etat.prochain += buf.duration;
}

function vuMetre(rms) {
  // Échelle perceptuelle : le RMS brut est écrasé vers le bas et une barre
  // linéaire paraîtrait morte sur un son normal.
  const pct = Math.min(100, Math.round(Math.sqrt(rms) * 240));
  $('niveau').style.width = pct + '%';
  $('niveau').dataset.actif = rms > 0.002 ? 'oui' : 'non';
}

function connecte() {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(proto + '//' + window.location.host + '/ws/listen');
  // Le défaut est 'blob', et l'oublier rend tout `instanceof ArrayBuffer`
  // silencieusement faux.
  ws.binaryType = 'arraybuffer';
  etat.ws = ws;
  voyant('attente', 'connexion…');

  ws.onopen = () => {
    etat.tentatives = 0;
    voyant('ok', 'connecté');
  };

  ws.onmessage = (event) => {
    // ⚠️ On teste le TYPE, on ne tente PAS un JSON.parse sous try/catch comme
    // le fait la page de capture. Ici, une trame binaire est du PCM légitime :
    // ce motif l'avalerait en silence, et le symptôme serait « ça ne joue pas,
    // aucune erreur ».
    if (typeof event.data === 'string') {
      let msg;
      try {
        msg = JSON.parse(event.data);
      } catch (err) {
        return;
      }
      traite(msg);
      return;
    }
    joue(new Int16Array(event.data));
  };

  ws.onclose = () => {
    etat.direct = false;
    majBoutons();
    voyant('erreur', 'déconnecté — reconnexion');
    const delai = Math.min(1000 * Math.pow(2, etat.tentatives++), 30000);
    clearTimeout(etat.minuterie);
    etat.minuterie = setTimeout(connecte, delai);
  };

  ws.onerror = () => {
    /* onclose suit toujours : inutile de journaliser deux fois */
  };
}

function envoie(obj) {
  if (etat.ws && etat.ws.readyState === WebSocket.OPEN) {
    etat.ws.send(JSON.stringify(obj));
  }
}

// --------------------------------------------------------- messages serveur

function traite(msg) {
  if (msg.type === 'listen_started') {
    etat.direct = true;
    etat.listenId = msg.listen_id;
    etat.recu_ms = 0;
    etat.prochain = 0; // on repart au direct, sans reste de l'écoute précédente
    if (etat.ctx && etat.ctx.state === 'suspended') etat.ctx.resume().catch(() => {});
    $('etat').textContent =
      (msg.joined ? 'Branché sur une écoute déjà en cours — ' : 'Écoute en cours — ') +
      Math.round(msg.remaining_ms / 1000) + ' s restantes.';
    majBoutons();
    return;
  }

  if (msg.type === 'listen_progress') {
    etat.recu_ms = msg.received_ms;
    $('r-recu').textContent = (msg.received_ms / 1000).toFixed(1) + ' s';
    $('r-fenetres').textContent = String(msg.windows);
    $('r-score').textContent = msg.max_noisy_score == null ? '—' : decimal(msg.max_noisy_score.toFixed(3));
    // Un morceau perdu se lirait sinon comme du silence dans la pièce : c'est
    // la conclusion exactement inverse de la vérité.
    $('r-perdus').textContent = msg.dropped_chunks
      ? String(msg.dropped_chunks) + ' ⚠'
      : '0';
    if (etat.direct) {
      $('etat').textContent = 'Écoute en cours — ' + (msg.received_ms / 1000).toFixed(0) + ' s reçues.';
    }
    return;
  }

  if (msg.type === 'listen_ended') {
    etat.direct = false;
    etat.listenId = null;
    majBoutons();
    const a = msg.analysis || {};
    let texte = 'Écoute terminée (' + msg.reason + ')';
    if (msg.wav_name) {
      texte += ' — ' + msg.wav_name;
      if (a.noisy_score != null) {
        texte += ', score max ' + decimal(a.noisy_score.toFixed(3));
      }
      if (a.windows) {
        texte += ', ' + (a.windows_retenues || 0) + '/' + a.windows + ' fenêtres retenues';
      }
      if (msg.event_id) texte += ' — épisode archivé n°' + msg.event_id;
    } else {
      // Aucun échantillon reçu : le dire franchement plutôt que d'annoncer un
      // fichier qui n'existe pas.
      texte += ' — aucun son reçu, aucun fichier écrit';
    }
    // Ce que TU as raté à l'écoute, par opposition à ce que le serveur a raté à
    // l'analyse. Sans cette ligne, un hoquet réseau se lit comme une accalmie
    // dehors — exactement la conclusion inverse.
    if (msg.dropped_chunks) {
      texte += ' — ⚠ votre connexion a sauté ' + msg.dropped_chunks +
        ' morceau(x) : le WAV est complet, mais ce que vous avez entendu était troué';
      journal(msg.dropped_chunks + ' morceau(x) non reçus (connexion)', 'avert');
    }
    $('etat').textContent = texte;
    if (msg.partial) {
      journal('écoute partielle : ' + msg.dropped_windows + ' fenêtre(s) non classée(s)', 'avert');
    }
    chargeSamples();
    return;
  }

  if (msg.type === 'error') {
    journal('erreur ' + msg.code + ' : ' + msg.message, 'erreur');
    $('etat').textContent = msg.message;
    etat.direct = false;
    majBoutons();
    if (msg.fatal) voyant('erreur', 'incompatible');
    return;
  }
}

// ------------------------------------------------------------- interactions

function ecoute() {
  // Le contexte est créé SUR LE CLIC : sans geste utilisateur il démarrerait
  // « suspended », et il faudrait le réveiller au moment où l'audio arrive.
  assureContexte();
  if (etat.ctx.state === 'suspended') etat.ctx.resume().catch(() => {});
  envoie({ type: 'listen_begin', duration_ms: 60000 });
  $('etat').textContent = 'Commande envoyée au poste…';
}

function arret() {
  envoie({ type: 'listen_cancel' });
  $('etat').textContent = 'Arrêt demandé…';
}

function majBoutons() {
  $('ecouter').hidden = etat.direct;
  $('arreter').hidden = !etat.direct;
  $('ecouter').disabled = etat.direct;
}

function voyant(etatVoyant, texte) {
  $('lien').dataset.etat = etatVoyant;
  $('lien-texte').textContent = texte;
}

function journal(message, niveau) {
  // Pas de liste de journal sur cette page : la console suffit pour une page
  // d'outil, et l'état visible est porté par les libellés ci-dessus.
  if (niveau === 'erreur') console.error(message);
  else console.warn(message);
}

// -------------------------------------------------------------- les samples

async function api(chemin, options) {
  const r = await fetch(chemin, Object.assign({ headers: { Accept: 'application/json' } }, options || {}));
  if (!r.ok) {
    let detail = r.status + '';
    try {
      const corps = await r.json();
      if (corps && corps.detail) detail = corps.detail;
    } catch (err) {
      /* réponse non-JSON : le code suffit */
    }
    throw new Error(detail);
  }
  return r.json();
}

// Icônes SVG partagées avec timeline.js
const SVG_PLAY = '<svg viewBox="0 0 24 24" width="11" height="11" fill="currentColor" aria-hidden="true"><polygon points="6 3 20 12 6 21 6 3"/></svg>';
const SVG_PAUSE = '<svg viewBox="0 0 24 24" width="11" height="11" fill="currentColor" aria-hidden="true"><rect x="5" y="4" width="4" height="16" rx="1"/><rect x="15" y="4" width="4" height="16" rx="1"/></svg>';

// Contrôleur de lecture : Les Directs (samples)
let sampleEnLectureNom = null;
let sampleBtnEnLecture = null;
let sampleLigneEnLecture = null;

function arreteLectureSample() {
  if (sampleBtnEnLecture) {
    sampleBtnEnLecture.innerHTML = SVG_PLAY;
    sampleBtnEnLecture.classList.remove('en-lecture');
    sampleBtnEnLecture.title = 'Écouter ce sample';
  }
  if (sampleLigneEnLecture) {
    sampleLigneEnLecture.classList.remove('ligne-en-lecture');
  }
  sampleEnLectureNom = null;
  sampleBtnEnLecture = null;
  sampleLigneEnLecture = null;
}

function basculeLectureSample(s, btn, tr) {
  const p = $('player');
  if (!p) return;

  if (sampleEnLectureNom === s.name && !p.paused) {
    p.pause();
    arreteLectureSample();
    return;
  }

  arreteLectureCapture();
  arreteLectureSample();

  sampleEnLectureNom = s.name;
  sampleBtnEnLecture = btn;
  sampleLigneEnLecture = tr;

  if (btn) {
    btn.innerHTML = SVG_PAUSE;
    btn.classList.add('en-lecture');
    btn.title = 'Mettre en pause';
  }
  if (tr) tr.classList.add('ligne-en-lecture');

  const url = '/ondemand/' + encodeURIComponent(s.name);
  if (!p.src.endsWith(encodeURIComponent(s.name))) {
    p.src = url;
  }
  p.play().catch(() => {
    arreteLectureSample();
  });
  $('player-sub').textContent = s.name;
}

// Contrôleur de lecture : Les Capturés
let captureEnLectureId = null;
let captureBtnEnLecture = null;
let captureLigneEnLecture = null;

function arreteLectureCapture() {
  if (captureBtnEnLecture) {
    captureBtnEnLecture.innerHTML = SVG_PLAY;
    captureBtnEnLecture.classList.remove('en-lecture');
    captureBtnEnLecture.title = 'Écouter la capture';
  }
  if (captureLigneEnLecture) {
    captureLigneEnLecture.classList.remove('ligne-en-lecture');
  }
  captureEnLectureId = null;
  captureBtnEnLecture = null;
  captureLigneEnLecture = null;
}

function basculeLectureCapture(e, btn, tr) {
  const p = $('captures-player');
  if (!p) return;

  const isRefused = e.backend && e.backend.includes('refused');

  if (captureEnLectureId === e.id && !p.paused) {
    p.pause();
    arreteLectureCapture();
    return;
  }

  arreteLectureSample();
  arreteLectureCapture();

  captureEnLectureId = e.id;
  captureBtnEnLecture = btn;
  captureLigneEnLecture = tr;

  if (btn) {
    btn.innerHTML = SVG_PAUSE;
    btn.classList.add('en-lecture');
    btn.title = 'Mettre en pause';
  }
  if (tr) tr.classList.add('ligne-en-lecture');

  if (p.src !== e.mp3_url) {
    p.src = e.mp3_url;
  }
  p.play().catch(() => {
    arreteLectureCapture();
  });
  $('captures-player-sub').textContent = 'Capture n°' + e.id + (isRefused ? ' (refusé YAMNet)' : '');
}

const pagination = {
  samples: {
    page: 1,
    taille: 20,
    items: [],
  },
  captures: {
    page: 1,
    taille: 20,
    items: [],
  },
};

// ------------------------------------------------------ filtres de colonnes
//
// Un moteur unique pour les trois tableaux de travail. Chaque colonne
// filtrable est décrite UNE fois dans COLONNES ; le filtre et le tri s'en
// déduisent. Sans ça il faudrait réécrire huit listes à cocher trois fois.
//
// ⚠️ Les filtres ne modifient JAMAIS `pagination.X.items` : ce tableau est
// réassigné toutes les 4 s par le rafraîchissement automatique. Ils
// s'appliquent à la volée, dans les fonctions de rendu.
//
// `valeur` renvoie null/undefined quand la donnée manque : la ligne tombe
// alors dans « (vide) », qui est un choix comme un autre — beaucoup
// d'événements n'ont jamais été évalués par le QC.

const COLONNES = {
  samples: {
    name:        { libelle: 'Fichier',  type: 'texte',  valeur: (s) => s.name },
    duration_ms: { libelle: 'Durée',    type: 'nombre', valeur: (s) => s.duration_ms },
    noisy_score: { libelle: 'Score',    type: 'nombre', valeur: (s) => (s.analysis ? s.analysis.noisy_score : null) },
    qc_score:    { libelle: 'Score QC', type: 'nombre', valeur: (s) => (s.analysis ? s.analysis.qc_score : null) },
    qc_valid:    { libelle: 'QC',       type: 'enum',   valeur: (s) => (s.analysis ? s.analysis.qc_valid : null) },
    densite:     { libelle: 'Densité',  type: 'nombre', valeur: (s) => (s.analysis ? s.analysis.windows_retenues : null) },
    niveau:      { libelle: 'Niveau',   type: 'nombre', valeur: (s) => (s.analysis ? s.analysis.peak_dbfs : null) },
    'analysé':   { libelle: 'Analysé',  type: 'enum',
                   valeur: (s) => (!s.analysis ? 'non' : (s.stale ? 'périmé' : 'oui')) },
  },
  captures: {
    id:          { libelle: 'N°',       type: 'nombre', valeur: (e) => e.id },
    detected_at: { libelle: 'Quand',    type: 'date',   valeur: (e) => new Date(e.detected_at) },
    duration_ms: { libelle: 'Durée',    type: 'nombre', valeur: (e) => e.duration_ms },
    noisy_score: { libelle: 'Score',    type: 'nombre', valeur: (e) => e.noisy_score },
    qc_score:    { libelle: 'Score QC', type: 'nombre', valeur: (e) => e.qc_score },
    qc_valid:    { libelle: 'QC',       type: 'enum',   valeur: (e) => e.qc_valid },
    noisy_count: { libelle: 'Rafales',  type: 'nombre', valeur: (e) => e.noisy_count },
  },
  candidats: {
    id:             { libelle: 'N°',       type: 'nombre', valeur: (c) => c.id },
    detected_at:    { libelle: 'Quand',    type: 'date',   valeur: (c) => new Date(c.detected_at) },
    duration_ms:    { libelle: 'Durée',    type: 'nombre', valeur: (c) => c.duration_ms },
    qc_score:       { libelle: 'Score QC', type: 'nombre', valeur: (c) => c.qc_score },
    snippets_count: { libelle: 'Extraits', type: 'nombre', valeur: (c) => c.snippets_count },
  },
};

const TABLE_ID = { samples: 'samples', captures: 'captures', candidats: 'qc-table-candidats' };

// L'état des filtres vit ici et survit au rafraîchissement de 4 s,
// contrairement aux données qu'il filtre.
const filtres = {
  samples:   { actif: {}, tri: null, masquerRefus: false },
  captures:  { actif: {}, tri: null, masquerRefus: false },
  candidats: { actif: {}, tri: null, masquerRefus: false },
};

// Liste brute des candidats QC : afficheCandidatsQC() la reçoit en argument et
// n'en gardait rien, or le moteur doit pouvoir la relire pour filtrer.
let candidatsQC = [];
// Colonne dont le menu est ouvert, ou null.
let popCible = null;

function cleValeur(v) {
  if (v === null || v === undefined || (typeof v === 'number' && Number.isNaN(v))) return '';
  if (v === true) return 'true';
  if (v === false) return 'false';
  return String(v);
}

function libelleValeur(v) {
  const c = cleValeur(v);
  if (c === '') return '(vide)';
  if (c === 'true') return 'Vrai';
  if (c === 'false') return 'Faux';
  return c;
}

function filtreActif(table, col) {
  const f = filtres[table].actif[col];
  if (!f) return false;
  if (f.valeurs && f.valeurs.size) return true;
  if (f.min != null || f.max != null) return true;
  if (f.du != null || f.au != null) return true;
  return false;
}

function nbFiltresActifs(table) {
  return Object.keys(filtres[table].actif).filter((c) => filtreActif(table, c)).length
    + (filtres[table].masquerRefus ? 1 : 0);
}

function donneesTable(table) {
  if (table === 'samples') return pagination.samples.items;
  if (table === 'captures') return pagination.captures.items;
  if (table === 'candidats') return candidatsQC;
  return [];
}

function filtreLignes(table, items) {
  const etat = filtres[table];
  const cols = COLONNES[table];
  let out = items;

  if (etat.masquerRefus) {
    out = out.filter((e) => !(e.backend && String(e.backend).indexOf('refused/') === 0));
  }

  for (const col of Object.keys(cols)) {
    const f = etat.actif[col];
    if (!f) continue;
    const desc = cols[col];

    if (f.valeurs && f.valeurs.size) {
      out = out.filter((it) => f.valeurs.has(cleValeur(desc.valeur(it))));
    }
    if (desc.type === 'nombre') {
      if (f.min != null) out = out.filter((it) => { const v = desc.valeur(it); return v != null && v >= f.min; });
      if (f.max != null) out = out.filter((it) => { const v = desc.valeur(it); return v != null && v <= f.max; });
    }
    if (desc.type === 'date' && (f.du != null || f.au != null)) {
      out = out.filter((it) => {
        const v = desc.valeur(it);
        if (!(v instanceof Date) || Number.isNaN(v.getTime())) return false;
        if (f.du != null && v < f.du) return false;
        if (f.au != null && v > f.au) return false;
        return true;
      });
    }
  }
  return out;
}

function trieLignes(table, items) {
  const t = filtres[table].tri;
  if (!t || !t.col) return items;
  const desc = COLONNES[table][t.col];
  if (!desc) return items;
  const signe = t.sens === 'desc' ? -1 : 1;
  // Copie : on ne trie jamais le tableau source, il est réassigné par le
  // rafraîchissement automatique.
  return items.slice().sort((a, b) => {
    const va = desc.valeur(a);
    const vb = desc.valeur(b);
    const vite = (v) => v == null || (typeof v === 'number' && Number.isNaN(v));
    // Les valeurs absentes finissent en queue DANS LES DEUX SENS : les
    // remonter en tête au tri décroissant ferait croire à une donnée.
    if (vite(va)) return vite(vb) ? 0 : 1;
    if (vite(vb)) return -1;
    if (typeof va === 'number' && typeof vb === 'number') return signe * (va - vb);
    if (va instanceof Date && vb instanceof Date) return signe * (va - vb);
    return signe * String(va).localeCompare(String(vb), 'fr', { numeric: true });
  });
}

function lignesVisibles(table, items) {
  return trieLignes(table, filtreLignes(table, items));
}

function majBarreFiltre(table, affichees, total) {
  const barre = document.querySelector(`.barre-filtre[data-table="${table}"]`);
  if (!barre) return;
  const compte = barre.querySelector('[data-role="compte"]');
  const reset = barre.querySelector('[data-role="reset"]');
  const n = nbFiltresActifs(table);
  const restreint = affichees !== total;
  if (compte) {
    // Le TEXTE dit l'état : dans ce projet, jamais la couleur seule.
    compte.textContent = restreint
      ? `${affichees} / ${total} lignes affichées — ${n} filtre${n > 1 ? 's' : ''} actif${n > 1 ? 's' : ''}`
      : `${total} ligne${total > 1 ? 's' : ''}`;
    compte.classList.toggle('filtre-compte--actif', restreint);
  }
  if (reset) reset.hidden = n === 0;
}

function majEntetesTri() {
  for (const table of Object.keys(COLONNES)) {
    const el = $(TABLE_ID[table]);
    if (!el) continue;
    el.querySelectorAll('th[data-col]').forEach((th) => {
      const col = th.dataset.col;
      if (!COLONNES[table][col]) return;
      const actif = filtreActif(table, col);
      th.classList.toggle('th-filtre-actif', actif);
      const btn = th.querySelector('.btn-filtre');
      if (btn) {
        btn.classList.toggle('btn-filtre--actif', actif);
        btn.title = actif
          ? `Filtre actif sur « ${COLONNES[table][col].libelle} » — cliquer pour le modifier`
          : `Filtrer « ${COLONNES[table][col].libelle} »`;
      }
      const t = filtres[table].tri;
      th.classList.remove('th-tri-asc', 'th-tri-desc');
      if (t && t.col === col) th.classList.add(t.sens === 'desc' ? 'th-tri-desc' : 'th-tri-asc');
    });
  }
}

function rafraichisTable(table) {
  if (pagination[table]) pagination[table].page = 1;   // le filtre change le nombre de pages
  if (table === 'samples') renduPageSamples();
  else if (table === 'captures') renduPageCaptures();
  else if (table === 'candidats') afficheCandidatsQC(candidatsQC);
  majEntetesTri();
}

function basculeTri(table, col) {
  const t = filtres[table].tri;
  // Cycle croissant → décroissant → aucun. Revenir à « aucun » compte :
  // l'ordre d'origine (du plus récent au plus ancien) a un sens.
  if (!t || t.col !== col) filtres[table].tri = { col, sens: 'asc' };
  else if (t.sens === 'asc') filtres[table].tri = { col, sens: 'desc' };
  else filtres[table].tri = null;
  rafraichisTable(table);
}

function versInputDate(d) {
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

function positionnePopover(ancre) {
  const pop = $('filtre-popover');
  if (!pop || !ancre) return;
  const r = ancre.getBoundingClientRect();
  const largeur = pop.offsetWidth || 260;
  const hauteur = pop.offsetHeight || 260;
  const x = Math.max(8, Math.min(r.left, window.innerWidth - largeur - 8));
  let y = r.bottom + 4;
  // S'il ne reste pas la place en dessous, on ouvre vers le haut : c'est ce
  // qui compte pour les tableaux en bas de page.
  if (y + hauteur > window.innerHeight - 8) y = Math.max(8, r.top - hauteur - 4);
  pop.style.left = `${x}px`;
  pop.style.top = `${y}px`;
}

function dessineFiltre() {
  if (!popCible) return;
  const { table, col } = popCible;
  const desc = COLONNES[table][col];
  if (!desc) return;
  const corps = $('filtre-corps');
  corps.textContent = '';
  const rows = donneesTable(table);
  const f = filtres[table].actif[col] || {};

  // --- Trier ---
  const blocTri = document.createElement('div');
  blocTri.className = 'filtre-section';
  const titreTri = document.createElement('p');
  titreTri.className = 'filtre-section-titre';
  titreTri.textContent = 'Trier';
  blocTri.appendChild(titreTri);
  const courant = filtres[table].tri;
  for (const [sens, libelle] of [['asc', '↑ Croissant'], ['desc', '↓ Décroissant']]) {
    const actif = !!(courant && courant.col === col && courant.sens === sens);
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'filtre-choix' + (actif ? ' filtre-choix--actif' : '');
    b.setAttribute('aria-pressed', actif ? 'true' : 'false');
    b.textContent = libelle;
    b.addEventListener('click', () => {
      filtres[table].tri = actif ? null : { col, sens };
      rafraichisTable(table);
      dessineFiltre();
    });
    blocTri.appendChild(b);
  }
  corps.appendChild(blocTri);

  // --- Filtrer ---
  const blocF = document.createElement('div');
  blocF.className = 'filtre-section';
  const titreF = document.createElement('p');
  titreF.className = 'filtre-section-titre';
  titreF.textContent = 'Filtrer';
  blocF.appendChild(titreF);

  if (desc.type === 'nombre' || desc.type === 'date') {
    const wrap = document.createElement('div');
    wrap.className = 'filtre-plage';
    const champs = desc.type === 'nombre'
      ? [['min', 'de', 'number'], ['max', 'à', 'number']]
      : [['du', 'du', 'datetime-local'], ['au', 'au', 'datetime-local']];
    for (const [role, libelle, type] of champs) {
      const lab = document.createElement('label');
      lab.className = 'filtre-plage-champ';
      const span = document.createElement('span');
      span.textContent = libelle;
      const inp = document.createElement('input');
      inp.type = type;
      if (type === 'number') inp.step = 'any';
      inp.dataset.rolePlage = role;
      const val = f[role];
      if (val != null) inp.value = type === 'number' ? String(val) : versInputDate(val);
      lab.appendChild(span);
      lab.appendChild(inp);
      wrap.appendChild(lab);
    }
    blocF.appendChild(wrap);
  } else {
    // enum ou texte : recherche + cases à cocher, comme Excel.
    const valeurs = new Map();
    for (const it of rows) {
      const v = desc.valeur(it);
      const c = cleValeur(v);
      if (!valeurs.has(c)) valeurs.set(c, libelleValeur(v));
    }
    const cles = [...valeurs.keys()].sort((a, b) =>
      valeurs.get(a).localeCompare(valeurs.get(b), 'fr', { numeric: true }));

    const rech = document.createElement('input');
    rech.type = 'search';
    rech.className = 'filtre-recherche';
    rech.placeholder = 'Rechercher…';
    rech.setAttribute('aria-label', 'Rechercher une valeur');
    blocF.appendChild(rech);

    const actions = document.createElement('div');
    actions.className = 'filtre-actions';
    for (const [role, libelle] of [['tout', 'Tout cocher'], ['rien', 'Tout décocher']]) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'filtre-lien';
      b.dataset.roleCases = role;
      b.textContent = libelle;
      actions.appendChild(b);
    }
    blocF.appendChild(actions);

    const liste = document.createElement('div');
    liste.className = 'filtre-liste';
    const selection = f.valeurs || null;
    for (const c of cles) {
      const lab = document.createElement('label');
      lab.className = 'filtre-item';
      lab.dataset.recherche = valeurs.get(c).toLowerCase();
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.value = c;
      // Aucun filtre posé = tout est coché : c'est l'état de repos, et il
      // doit se voir comme tel.
      cb.checked = selection ? selection.has(c) : true;
      const span = document.createElement('span');
      span.textContent = valeurs.get(c);
      lab.appendChild(cb);
      lab.appendChild(span);
      liste.appendChild(lab);
    }
    blocF.appendChild(liste);

    rech.addEventListener('input', () => {
      const q = rech.value.trim().toLowerCase();
      liste.querySelectorAll('.filtre-item').forEach((el) => {
        el.hidden = !!q && el.dataset.recherche.indexOf(q) === -1;
      });
    });
    actions.querySelector('[data-role-cases="tout"]').addEventListener('click', () => {
      liste.querySelectorAll('input[type=checkbox]').forEach((cb) => { cb.checked = true; });
    });
    actions.querySelector('[data-role-cases="rien"]').addEventListener('click', () => {
      liste.querySelectorAll('input[type=checkbox]').forEach((cb) => { cb.checked = false; });
    });
  }
  corps.appendChild(blocF);
}

function ouvreFiltre(table, col, ancre) {
  const pop = $('filtre-popover');
  if (!pop) return;
  popCible = { table, col };
  $('filtre-titre').textContent = `Colonne « ${COLONNES[table][col].libelle} »`;
  dessineFiltre();
  if (typeof pop.showPopover === 'function') {
    if (!pop.matches(':popover-open')) pop.showPopover();
  } else {
    pop.hidden = false;   // repli si l'API popover manque : au moins ça s'affiche
    pop.classList.add('filtre-popover--repli');
  }
  positionnePopover(ancre);
}

function appliqueFiltre() {
  if (!popCible) return;
  const { table, col } = popCible;
  const desc = COLONNES[table][col];
  const corps = $('filtre-corps');
  const etat = {};

  if (desc.type === 'nombre') {
    corps.querySelectorAll('input[data-role-plage]').forEach((inp) => {
      if (inp.value === '') return;
      const v = Number(inp.value);
      if (!Number.isNaN(v)) etat[inp.dataset.rolePlage] = v;
    });
  } else if (desc.type === 'date') {
    corps.querySelectorAll('input[data-role-plage]').forEach((inp) => {
      if (inp.value === '') return;
      const d = new Date(inp.value);
      if (!Number.isNaN(d.getTime())) etat[inp.dataset.rolePlage] = d;
    });
  } else {
    const toutes = [...corps.querySelectorAll('.filtre-liste input[type=checkbox]')];
    if (toutes.length) {
      const cochees = new Set(toutes.filter((cb) => cb.checked).map((cb) => cb.value));
      // Tout coché = aucun filtre : sinon un `Set` plein ferait exactement la
      // même chose mais s'afficherait comme « filtre actif ».
      if (cochees.size < toutes.length) etat.valeurs = cochees;
    }
  }

  if (Object.keys(etat).length) filtres[table].actif[col] = etat;
  else delete filtres[table].actif[col];

  rafraichisTable(table);
  fermeFiltre();
}

function fermeFiltre() {
  const pop = $('filtre-popover');
  if (pop && typeof pop.hidePopover === 'function') {
    if (pop.matches(':popover-open')) pop.hidePopover();
  } else if (pop) {
    pop.hidden = true;
  }
  popCible = null;
}

function initFiltres() {
  const pop = $('filtre-popover');
  if (!pop) return;

  for (const table of Object.keys(COLONNES)) {
    const el = $(TABLE_ID[table]);
    if (!el) continue;
    el.querySelectorAll('th[data-col]').forEach((th) => {
      const col = th.dataset.col;
      // data-col orphelin : on n'invente pas de descripteur, on ignore.
      if (!COLONNES[table][col]) return;
      th.classList.add('th-filtrable');

      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'btn-filtre';
      btn.textContent = '▾';
      btn.setAttribute('aria-label', `Filtrer la colonne ${COLONNES[table][col].libelle}`);
      btn.title = `Filtrer « ${COLONNES[table][col].libelle} »`;
      btn.addEventListener('click', (ev) => {
        ev.stopPropagation();
        if (popCible && popCible.table === table && popCible.col === col) fermeFiltre();
        else ouvreFiltre(table, col, th);
      });
      th.appendChild(btn);

      // Cliquer le TITRE trie, comme dans Excel. Le bouton reste pour le
      // filtre : les deux entrées ne font pas la même chose.
      th.classList.add('th-triable');
      th.addEventListener('click', (ev) => {
        if (ev.target.closest('.btn-filtre')) return;
        basculeTri(table, col);
      });
    });
  }

  pop.querySelector('[data-role="annuler"]').addEventListener('click', fermeFiltre);
  pop.querySelector('[data-role="appliquer"]').addEventListener('click', appliqueFiltre);
  // Le popover se ferme au clic extérieur : on oublie alors la colonne
  // courante, sinon un second clic dessus la croirait encore ouverte.
  pop.addEventListener('toggle', (ev) => { if (ev.newState === 'closed') popCible = null; });

  for (const table of Object.keys(COLONNES)) {
    const barre = document.querySelector(`.barre-filtre[data-table="${table}"]`);
    if (!barre) continue;
    const reset = barre.querySelector('[data-role="reset"]');
    if (reset) {
      reset.addEventListener('click', () => {
        filtres[table] = { actif: {}, tri: null, masquerRefus: false };
        const c = barre.querySelector('[data-role="masquer-refus"]');
        if (c) c.checked = false;
        rafraichisTable(table);
      });
    }
    const masquer = barre.querySelector('[data-role="masquer-refus"]');
    if (masquer) {
      masquer.addEventListener('change', () => {
        filtres[table].masquerRefus = masquer.checked;
        rafraichisTable(table);
      });
    }
  }

  majEntetesTri();
}

function activeOngletTableau(onglet) {
  const estDirects = onglet === 'directs';
  const secDirects = $('section-directs');
  const secCaptures = $('section-captures');
  const tabDirects = $('tab-directs');
  const tabCaptures = $('tab-captures');

  if (secDirects) secDirects.hidden = !estDirects;
  if (secCaptures) secCaptures.hidden = estDirects;

  if (tabDirects) {
    tabDirects.classList.toggle('active', estDirects);
    tabDirects.setAttribute('aria-selected', String(estDirects));
  }
  if (tabCaptures) {
    tabCaptures.classList.toggle('active', !estDirects);
    tabCaptures.setAttribute('aria-selected', String(!estDirects));
  }

  try {
    localStorage.setItem('noisygram.listen_tab', estDirects ? 'directs' : 'captures');
  } catch (e) {}
}

function basculeOngletTableau() {
  const secDirects = $('section-directs');
  const estDirects = secDirects && !secDirects.hidden;
  activeOngletTableau(estDirects ? 'captures' : 'directs');
}

function renduPageSamples() {
  const pState = pagination.samples;
  // Filtre et tri s'appliquent ICI, et surtout PAS sur pState.items : le
  // rafraîchissement de 4 s réassigne ce tableau, ce qui effacerait un filtre
  // écrit dedans.
  const items = lignesVisibles('samples', pState.items);
  const total = items.length;
  majBarreFiltre('samples', total, pState.items.length);
  const totalPages = Math.max(1, Math.ceil(total / pState.taille));
  if (pState.page > totalPages) pState.page = totalPages;
  if (pState.page < 1) pState.page = 1;

  $('samples-indicateur').textContent = `Page ${pState.page} / ${totalPages}`;
  $('samples-premier').disabled = pState.page <= 1;
  $('samples-precedent').disabled = pState.page <= 1;
  $('samples-suivant').disabled = pState.page >= totalPages;
  $('samples-dernier').disabled = pState.page >= totalPages;

  const debut = (pState.page - 1) * pState.taille;
  const tranche = items.slice(debut, debut + pState.taille);

  const corps = $('samples-corps');
  corps.textContent = '';
  $('samples-vide').hidden = total > 0;

  const pDirects = $('player');
  for (const s of tranche) {
    const tr = document.createElement('tr');
    tr.title = 'Cliquer pour écouter ce sample';

    const cel = (texte, classe) => {
      const td = document.createElement('td');
      td.textContent = texte;
      if (classe) td.className = classe;
      return td;
    };

    // Bouton play à gauche de la ligne
    const tdPlay = document.createElement('td');
    tdPlay.className = 'td-play';
    const bPlay = document.createElement('button');
    bPlay.type = 'button';
    bPlay.className = 'btn-play-row';
    bPlay.title = 'Écouter ' + s.name;
    bPlay.setAttribute('aria-label', 'Écouter ' + s.name);

    const estEnLecture = sampleEnLectureNom === s.name && pDirects && !pDirects.paused;
    bPlay.innerHTML = estEnLecture ? SVG_PAUSE : SVG_PLAY;
    if (estEnLecture) {
      bPlay.classList.add('en-lecture');
      bPlay.title = 'Mettre en pause';
      tr.classList.add('ligne-en-lecture');
      sampleBtnEnLecture = bPlay;
      sampleLigneEnLecture = tr;
    }

    bPlay.addEventListener('click', (ev) => {
      ev.stopPropagation();
      basculeLectureSample(s, bPlay, tr);
    });
    tdPlay.appendChild(bPlay);
    tr.appendChild(tdPlay);

    // Colonnes d'information
    const a = s.analysis;
    tr.appendChild(cel(s.name));
    tr.appendChild(cel(s.duration_ms == null ? '—' : decimal((s.duration_ms / 1000).toFixed(1)) + ' s'));
    tr.appendChild(cel(a && a.noisy_score != null ? decimal(a.noisy_score.toFixed(3)) : '—'));

    // Score QC
    const qcScoreTxt = a && a.qc_score != null ? decimal((a.qc_score * 100).toFixed(1)) + ' %' : '—';
    tr.appendChild(cel(qcScoreTxt));

    // QC (Vrai / Faux)
    const tdQc = document.createElement('td');
    if (a && a.qc_valid === true) {
      tdQc.innerHTML = '<span class="qc-badge-vrai">Vrai</span>';
    } else if (a && a.qc_valid === false) {
      tdQc.innerHTML = '<span class="qc-badge-faux">Faux</span>';
    } else {
      tdQc.textContent = '—';
    }
    tr.appendChild(tdQc);

    tr.appendChild(
      cel(
        a && a.windows
          ? (a.windows_retenues || 0) + '/' + a.windows
          : '—'
      )
    );

    const db = a && a.peak_dbfs != null ? a.peak_dbfs : null;
    tr.appendChild(
      cel(
        db == null ? '—' : decimal(db.toFixed(1)) + ' dB',
        db != null && db < -40 ? 'avert' : null
      )
    );

    const tdYam = document.createElement('td');
    if (!a) {
      tdYam.textContent = 'non';
    } else if (s.stale) {
      tdYam.textContent = 'oui (seuil ' + decimal((a.threshold || 0).toFixed(2)) + ')';
      tdYam.className = 'avert';
      tdYam.title = 'Analysé avec un autre seuil que le seuil courant — relancez pour comparer.';
    } else {
      tdYam.textContent = 'oui';
    }
    tr.appendChild(tdYam);

    // Colonne Actions : uniquement « Analyser »
    const tdActions = document.createElement('td');
    const bAnalyse = document.createElement('button');
    bAnalyse.type = 'button';
    bAnalyse.className = 'lien';
    bAnalyse.textContent = 'Analyser';
    bAnalyse.addEventListener('click', (ev) => {
      ev.stopPropagation();
      ouvreAnalyse(s.name);
    });
    tdActions.appendChild(bAnalyse);
    tr.appendChild(tdActions);

    tr.style.cursor = 'pointer';
    tr.addEventListener('click', () => {
      basculeLectureSample(s, bPlay, tr);
    });

    corps.appendChild(tr);
  }
}

async function chargeSamples(silencieux = false) {
  const btnRaf = $('rafraichir');
  if (!silencieux && btnRaf) btnRaf.classList.add('en-cours');
  try {
    const data = await api('/api/ondemand');
    pagination.samples.items = data.samples || [];

    const badge = $('badge-directs');
    if (badge) badge.textContent = pagination.samples.items.length;

    $('samples-sub').textContent =
      pagination.samples.items.length + ' sample(s) dans ' + data.dir + ' — seuil courant ' +
      decimal(data.threshold.toFixed(2));

    $('poids').textContent =
      (data.total_bytes / 1048576).toFixed(1) + ' Mo' +
      (data.disk_free_bytes != null
        ? ' — ' + (data.disk_free_bytes / 1073741824).toFixed(1) + ' Go libres'
        : '');

    renduPageSamples();
  } catch (err) {
    $('samples-sub').textContent = 'Liste impossible : ' + err.message;
  } finally {
    if (btnRaf) btnRaf.classList.remove('en-cours');
  }
}

// ------------------------------------------------------- les capturés

function renduPageCaptures() {
  const pState = pagination.captures;
  const items = lignesVisibles('captures', pState.items);
  const total = items.length;
  majBarreFiltre('captures', total, pState.items.length);
  const totalPages = Math.max(1, Math.ceil(total / pState.taille));

  if (pState.page > totalPages) pState.page = totalPages;
  if (pState.page < 1) pState.page = 1;

  $('captures-indicateur').textContent = 'Page ' + pState.page + ' / ' + totalPages;
  $('captures-premier').disabled = pState.page <= 1;
  $('captures-precedent').disabled = pState.page <= 1;
  $('captures-suivant').disabled = pState.page >= totalPages;
  $('captures-dernier').disabled = pState.page >= totalPages;

  const debut = (pState.page - 1) * pState.taille;
  const tranche = items.slice(debut, debut + pState.taille);

  const corps = $('captures-corps');
  corps.textContent = '';
  $('captures-vide').hidden = total > 0;

  const pCaptures = $('captures-player');
  for (const e of tranche) {
    const tr = document.createElement('tr');
    tr.title = 'Cliquer pour écouter la capture';

    const cel = (texte, classe) => {
      const td = document.createElement('td');
      td.textContent = texte;
      if (classe) td.className = classe;
      return td;
    };

    // 2° Bouton play à gauche de chaque ligne
    const tdPlay = document.createElement('td');
    tdPlay.className = 'td-play';
    const bPlay = document.createElement('button');
    bPlay.type = 'button';
    bPlay.className = 'btn-play-row';
    bPlay.title = 'Écouter la capture n°' + e.id;
    bPlay.setAttribute('aria-label', 'Écouter la capture n°' + e.id);

    const estEnLecture = captureEnLectureId === e.id && pCaptures && !pCaptures.paused;
    bPlay.innerHTML = estEnLecture ? SVG_PAUSE : SVG_PLAY;
    if (estEnLecture) {
      bPlay.classList.add('en-lecture');
      bPlay.title = 'Mettre en pause';
      tr.classList.add('ligne-en-lecture');
      captureBtnEnLecture = bPlay;
      captureLigneEnLecture = tr;
    }

    bPlay.addEventListener('click', (ev) => {
      ev.stopPropagation();
      basculeLectureCapture(e, bPlay, tr);
    });
    tdPlay.appendChild(bPlay);
    tr.appendChild(tdPlay);

    // 1° Colonne N° de capture
    tr.appendChild(cel('n° ' + e.id, 'td-num'));

    // Colonnes Quand, Durée, Score, Rafales
    const isRefused = e.backend && e.backend.includes('refused');
    const quand = new Date(e.detected_at);
    tr.appendChild(cel(quand.toLocaleString('fr-FR')));
    tr.appendChild(cel(decimal((e.duration_ms / 1000).toFixed(1)) + ' s'));
    if (isRefused) {
      tr.appendChild(cel(decimal(e.noisy_score.toFixed(3)) + ' (refusé)', 'avert'));
    } else {
      tr.appendChild(cel(decimal(e.noisy_score.toFixed(3))));
    }

    // Score QC
    const qcScoreTxt = e.qc_score != null ? decimal((e.qc_score * 100).toFixed(1)) + ' %' : '—';
    tr.appendChild(cel(qcScoreTxt));

    // QC (Vrai / Faux)
    const tdQc = document.createElement('td');
    if (e.qc_valid === true) {
      tdQc.innerHTML = '<span class="qc-badge-vrai">Vrai</span>';
    } else if (e.qc_valid === false) {
      tdQc.innerHTML = '<span class="qc-badge-faux">Faux</span>';
    } else {
      tdQc.textContent = '—';
    }
    tr.appendChild(tdQc);

    // Rafales
    if (isRefused) {
      tr.appendChild(cel('0', 'avert'));
    } else {
      tr.appendChild(cel(String(e.noisy_count == null ? '—' : e.noisy_count)));
    }

    // Colonne 1 : Faux (à gauche d'Analyser)
    const tdFaux = document.createElement('td');
    tdFaux.style.textAlign = 'center';
    tdFaux.style.width = '76px';
    if (!isRefused && e.qc_valid !== false) {
      const bFaux = document.createElement('button');
      bFaux.type = 'button';
      bFaux.className = 'btn-danger';
      bFaux.textContent = 'Exclure';
      bFaux.title = 'Exclure ce son du comptage et du dashboard';
      bFaux.addEventListener('click', async (ev) => {
        ev.stopPropagation();
        if (confirm('Exclure la capture n°' + e.id + ' du comptage et du dashboard ?')) {
          try {
            await api('/api/events/' + e.id + '/disapprove', { method: 'POST' });
            await chargeCaptures(true);
          } catch (err) {
            alert('Erreur: ' + err);
          }
        }
      });
      tdFaux.appendChild(bFaux);
    } else if (e.backend === 'refused/user_rejected' || e.qc_valid === false) {
      const spanExclu = document.createElement('span');
      spanExclu.className = 'card-sub';
      spanExclu.style.fontSize = '11px';
      spanExclu.style.opacity = '0.6';
      spanExclu.textContent = 'Exclu';
      spanExclu.title = 'Faux positif exclu (clic droit pour réhabiliter)';
      tdFaux.appendChild(spanExclu);
    }
    tr.appendChild(tdFaux);

    // Colonne 2 : Analyser (à droite)
    const tdAnalyse = document.createElement('td');
    tdAnalyse.style.textAlign = 'right';
    tdAnalyse.style.width = '76px';
    const bAnalyse = document.createElement('button');
    bAnalyse.type = 'button';
    bAnalyse.className = 'lien';
    bAnalyse.textContent = 'Analyser';
    if (e.wav_name) {
      bAnalyse.addEventListener('click', (ev) => {
        ev.stopPropagation();
        ouvreAnalyse(e.wav_name);
      });
    } else {
      bAnalyse.disabled = true;
      bAnalyse.title = 'WAV non disponible pour cette capture ancienne';
      bAnalyse.style.opacity = '0.5';
    }
    tdAnalyse.appendChild(bAnalyse);
    tr.appendChild(tdAnalyse);

    tr.style.cursor = 'pointer';
    tr.addEventListener('click', () => {
      basculeLectureCapture(e, bPlay, tr);
    });

    tr.addEventListener('contextmenu', (ev) => {
      ev.preventDefault();
      if (!isRefused) {
        if (confirm('Marquer la capture n°' + e.id + ' comme faux positif ?')) {
          api('/api/events/' + e.id + '/disapprove', { method: 'POST' }).then(() => {
            chargeCaptures(true);
          });
        }
      } else if (e.backend === 'refused/user_rejected') {
        if (confirm('Réhabiliter la capture n°' + e.id + ' comme détection valide ?')) {
          api('/api/events/' + e.id + '/approve', { method: 'POST' }).then(() => {
            chargeCaptures(true);
          });
        }
      }
    });

    corps.appendChild(tr);
  }
}

async function chargeCaptures(silencieux = false) {
  const btnRaf = $('captures-rafraichir');
  if (!silencieux && btnRaf) btnRaf.classList.add('en-cours');
  try {
    // ⚠️ PLAFOND, et il est atteint : au-delà, le tableau se tronque EN
    // SILENCE — les événements les plus anciens disparaissent sans que rien
    // ne le dise. 1000 est le maximum que l'API accepte (events.py, `le=1000`).
    // C'est un dépannage : le vrai correctif est de filtrer côté serveur, avec
    // la pagination qui va avec. À ouvrir quand le corpus dépassera ~900.
    const data = await api('/api/events?limit=1000&from=2020-01-01T00:00:00');
    pagination.captures.items = data.items || [];

    const badge = $('badge-captures');
    if (badge) badge.textContent = pagination.captures.items.length;

    const rafales = pagination.captures.items.reduce((a, e) => a + (e.noisy_count || 0), 0);
    $('captures-sub').textContent =
      pagination.captures.items.length + ' capture(s) — ' + rafales + ' rafale(s) au total.';

    renduPageCaptures();
  } catch (err) {
    $('captures-sub').textContent = 'Liste impossible : ' + err.message;
  } finally {
    if (btnRaf) btnRaf.classList.remove('en-cours');
  }
}

// ---------------------------------------------------------------- démarrage

function decimal(texte) {
  return String(texte).replace('.', ',');
}

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
  const lienDashboard = document.querySelector('a[href*="/dashboard/"]');
  if (lienDashboard) {
    lienDashboard.href = 'https://noisy.virtuaworld.org/dashboard/' + (theme ? '?theme=' + encodeURIComponent(theme) : '');
  }
  majBoutonTheme();
}

function init() {
  // Les filtres s'installent avant tout rendu : ils ajoutent leurs boutons
  // dans les en-têtes, que les fonctions de rendu ne touchent jamais (elles
  // ne vident que les <tbody>).
  initFiltres();
  // Le popover est positionné en dur sous son en-tête. Si la page défile ou
  // est redimensionnée, il se détacherait de sa colonne : on le ferme plutôt
  // que de le laisser mentir sur ce qu'il filtre. Le défilement DANS le
  // popover, lui, ne doit pas le fermer — d'où le filtre sur la cible.
  const surMouvement = (ev) => {
    if (!popCible) return;
    if (ev.target && ev.target.closest && ev.target.closest('#filtre-popover')) return;
    fermeFiltre();
  };
  window.addEventListener('resize', surMouvement);
  window.addEventListener('scroll', surMouvement, true);

  const urlTheme = new URLSearchParams(window.location.search).get('theme');
  const themeEnregistre = urlTheme || (() => {
    try {
      return localStorage.getItem('noisygram.theme') || localStorage.getItem('aboigramme.theme');
    } catch (e) { return null; }
  })();
  if (themeEnregistre) {
    appliqueTheme(themeEnregistre);
  } else {
    majBoutonTheme();
  }
  const lienDashboard = document.querySelector('a[href*="/dashboard/"]');
  if (lienDashboard && themeEnregistre) {
    lienDashboard.href = 'https://noisy.virtuaworld.org/dashboard/?theme=' + encodeURIComponent(themeEnregistre);
  }

  $('theme').addEventListener('click', () => {
    appliqueTheme(estSombre() ? 'light' : 'dark');
  });

  window.addEventListener('storage', (e) => {
    if (e.key === 'noisygram.theme' || e.key === 'aboigramme.theme') {
      appliqueTheme(e.newValue || null);
    }
  });

  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    if (!document.documentElement.dataset.theme) majBoutonTheme();
  });

  // Sélecteur d'onglet (Directs / Capturés)
  const ongletMémorise = (() => {
    try { return localStorage.getItem('noisygram.listen_tab'); } catch (e) { return null; }
  })() || 'directs';
  activeOngletTableau(ongletMémorise);

  $('tab-directs').addEventListener('click', () => activeOngletTableau('directs'));
  $('tab-captures').addEventListener('click', () => activeOngletTableau('captures'));
  $('btn-switch-tables').addEventListener('click', basculeOngletTableau);

  // Pagination des directs
  $('samples-taille').addEventListener('change', (e) => {
    pagination.samples.taille = Number(e.target.value);
    pagination.samples.page = 1;
    renduPageSamples();
  });
  $('samples-premier').addEventListener('click', () => {
    pagination.samples.page = 1;
    renduPageSamples();
  });
  $('samples-precedent').addEventListener('click', () => {
    if (pagination.samples.page > 1) {
      pagination.samples.page--;
      renduPageSamples();
    }
  });
  $('samples-suivant').addEventListener('click', () => {
    const max = Math.ceil(pagination.samples.items.length / pagination.samples.taille);
    if (pagination.samples.page < max) {
      pagination.samples.page++;
      renduPageSamples();
    }
  });
  $('samples-dernier').addEventListener('click', () => {
    pagination.samples.page = Math.max(1, Math.ceil(pagination.samples.items.length / pagination.samples.taille));
    renduPageSamples();
  });

  // Pagination des capturés
  $('captures-taille').addEventListener('change', (e) => {
    pagination.captures.taille = Number(e.target.value);
    pagination.captures.page = 1;
    renduPageCaptures();
  });
  $('captures-premier').addEventListener('click', () => {
    pagination.captures.page = 1;
    renduPageCaptures();
  });
  $('captures-precedent').addEventListener('click', () => {
    if (pagination.captures.page > 1) {
      pagination.captures.page--;
      renduPageCaptures();
    }
  });
  $('captures-suivant').addEventListener('click', () => {
    const max = Math.ceil(pagination.captures.items.length / pagination.captures.taille);
    if (pagination.captures.page < max) {
      pagination.captures.page++;
      renduPageCaptures();
    }
  });
  $('captures-dernier').addEventListener('click', () => {
    pagination.captures.page = Math.max(1, Math.ceil(pagination.captures.items.length / pagination.captures.taille));
    renduPageCaptures();
  });

  // Rafraîchissement périodique réactif en arrière-plan (toutes les 4 s si visible)
  setInterval(() => {
    if (!document.hidden) {
      chargeSamples(true);
      chargeCaptures(true);
    }
  }, 4000);

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
      chargeSamples(true);
      chargeCaptures(true);
    }
  });

  $('ecouter').addEventListener('click', ecoute);
  $('arreter').addEventListener('click', arret);
  $('rafraichir').addEventListener('click', () => chargeSamples(false));
  $('captures-rafraichir').addEventListener('click', () => chargeCaptures(false));

  // Deux lecteurs, un seul son. On coupe l'autre AU MOMENT où celui-ci
  // démarre — pas à l'ouverture — sinon on interromprait une écoute en cours.
  // Deux pistes superposées sont incompréhensibles, et le même fichier joué des
  // deux côtés ferait un écho.
  const pDirects = $('player');
  const pCaptures = $('captures-player');

  if (pDirects) {
    pDirects.addEventListener('pause', () => {
      setTimeout(() => {
        if (pDirects.paused) arreteLectureSample();
      }, 50);
    });
    pDirects.addEventListener('ended', arreteLectureSample);
  }

  if (pCaptures) {
    pCaptures.addEventListener('pause', () => {
      setTimeout(() => {
        if (pCaptures.paused) arreteLectureCapture();
      }, 50);
    });
    pCaptures.addEventListener('ended', arreteLectureCapture);
  }

  for (const [celui, autre] of [
    ['player', 'captures-player'],
    ['captures-player', 'player'],
  ]) {
    $(celui).addEventListener('play', () => {
      const a = $(autre);
      if (a && !a.paused) a.pause();
    });
  }

  $('muet').addEventListener('click', () => {
    etat.muet = !etat.muet;
    $('muet').setAttribute('aria-pressed', String(etat.muet));
    $('muet').textContent = etat.muet ? 'Rétablir le son' : 'Couper le son';
    if (etat.gain) etat.gain.gain.value = etat.muet ? 0 : etat.volume;
  });

  $('volume').addEventListener('input', (e) => {
    etat.volume = Number(e.target.value) / 100;
    // Ajuster le volume ne doit pas ANNULER la coupure : « muet » reste muet.
    if (etat.gain && !etat.muet) etat.gain.gain.value = etat.volume;
  });

  connecte();
  chargeSamples();
  chargeCaptures();
  majBoutons();
  initQC();
}

// ------------------------------------------------------------- Quality Center
window.qcActiveSnippets = [];

function initQC() {
  const btnQC = $('btn-qc');
  const modalQC = $('modal-qc');
  const btnFermer = $('qc-fermer');
  if (!btnQC || !modalQC) return;

  btnQC.addEventListener('click', () => {
    modalQC.showModal();
    const p1 = $('player');
    const p2 = $('captures-player');
    if (p1 && !p1.paused) p1.pause();
    if (p2 && !p2.paused) p2.pause();
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
      candidatsQC = candidats;
      afficheCandidatsQC(candidats);
      const candCount = $('qc-candidats-count');
      if (candCount) candCount.textContent = String(candidats.length);
    }

    const resSnip = await fetch('/api/qc/snippets');
    if (resSnip.ok) {
      const snippets = await resSnip.json();
      window.qcActiveSnippets = snippets;
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
window.chargeQC = chargeQC;

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

  // Pas de pagination ici — le serveur plafonne à 100. Le filtre s'applique
  // donc directement à ce qui est rendu.
  const visibles = lignesVisibles('candidats', candidats);
  majBarreFiltre('candidats', visibles.length, candidats.length);

  const svgPlay = '<svg viewBox="0 0 24 24" width="11" height="11" fill="currentColor"><polygon points="6 3 20 12 6 21 6 3"/></svg>';
  const svgPause = '<svg viewBox="0 0 24 24" width="11" height="11" fill="currentColor"><rect x="5" y="4" width="4" height="16" rx="1"/><rect x="15" y="4" width="4" height="16" rx="1"/></svg>';

  for (const c of visibles) {
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

    // Bouton Analyser
    const tdAnalyse = document.createElement('td');
    tdAnalyse.style.textAlign = 'center';
    if (c.wav_name) {
      const btnAnalyse = document.createElement('button');
      btnAnalyse.type = 'button';
      btnAnalyse.className = 'btn-analyser-qc';
      btnAnalyse.innerHTML = '🔍 Analyser';
      btnAnalyse.title = 'Examiner les segments détectés et sélectionner les extraits au Best-of';
      btnAnalyse.addEventListener('click', (ev) => {
        ev.stopPropagation();
        if (typeof ouvreAnalyse === 'function') {
          ouvreAnalyse(c.wav_name, c.id);
        }
      });
      tdAnalyse.appendChild(btnAnalyse);
    } else {
      tdAnalyse.textContent = '—';
      tdAnalyse.style.opacity = '0.4';
    }
    tr.appendChild(tdAnalyse);

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
            chargeCaptures(true);
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

/**
 * Fabrique le bouton [★ En réf] ou [+ Réf] pour chaque segment de la modale d'analyse.
 * Appelé depuis timeline.js pour garder timeline.js sans appel fetch direct.
 */
window.qcRendBoutonRef = function(s, wavName) {
  let debut = s.debut_s;
  let fin = s.fin_s;
  if (debut == null || fin == null) {
    const p = typeof parseIntervalle === 'function' ? parseIntervalle(s.interval) : { debut: 0, fin: 0 };
    debut = p.debut;
    fin = p.fin;
  }
  if (fin <= debut) return null;

  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'btn-ref-segment';

  const findActive = () => {
    return (window.qcActiveSnippets || []).find((sn) =>
      sn.wav_name === wavName &&
      Math.abs(sn.debut_s - debut) < 0.05 &&
      Math.abs(sn.fin_s - fin) < 0.05
    );
  };

  let active = findActive();
  const majEtat = () => {
    if (active) {
      btn.classList.add('en-ref');
      btn.textContent = '★ En réf';
      btn.title = 'Extrait actif dans le catalogue Best-of. Cliquer pour le retirer.';
    } else {
      btn.classList.remove('en-ref');
      btn.textContent = '+ Réf';
      btn.title = 'Ajouter ce segment au catalogue de référence Best-of';
    }
  };
  majEtat();

  btn.addEventListener('click', async (ev) => {
    ev.stopPropagation();
    btn.disabled = true;
    try {
      if (active) {
        // Suppression
        const res = await fetch('/api/qc/snippets/' + active.id, { method: 'DELETE' });
        if (res.ok) {
          active = null;
          majEtat();
          await chargeQC();
        }
      } else {
        // Ajout
        const payload = {
          wav_name: wavName,
          debut_s: debut,
          fin_s: fin,
          sound: s.sound || null,
          score: s.score || null,
          event_id: window.eventIdAnalyseCourant || null,
        };
        const res = await fetch('/api/qc/snippets/extract', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        if (res.ok) {
          const data = await res.json();
          active = data.snippet;
          majEtat();
          await chargeQC();
        } else {
          alert('Erreur lors de l\'extraction du segment : HTTP ' + res.status);
        }
      }
    } catch (err) {
      alert('Erreur réseau lors de la gestion du segment : ' + err);
    } finally {
      btn.disabled = false;
    }
  });

  return btn;
};

init();
