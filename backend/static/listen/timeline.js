/**
 * Modale d'analyse — ce que le système croit avoir entendu, tranche par tranche.
 *
 * Chargé APRÈS `listen.js`, dont il réutilise le helper `$` et `decimal()` : les
 * deux fichiers sont de simples scripts sur la même page, donc ils partagent la
 * portée. Redéclarer `const $` ici serait une SyntaxError, pas une redondance.
 *
 * ⚠️ CETTE MODALE N'ÉCRIT RIEN. Ni en base, ni au compteur. Le noisygram est
 * alimenté tout seul par le YAMNet embarqué, à la fin de chaque écoute — si on
 * comptait aussi ici, le même événement serait compté deux fois pour le même audio.
 * Le bandeau le dit à l'écran, parce qu'un bouton « Analyser » qui ne compte
 * pas est contre-intuitif.
 */

'use strict';

/*
 * Un `<dialog>` natif plutôt qu'une div bricolée : il apporte la touche Échap,
 * le piège de focus, l'inertie de l'arrière-plan et `aria-modal` GRATUITEMENT.
 * Le style moderne se fait en CSS, pas en réimplémentant ce que le navigateur
 * sait déjà faire — même raisonnement que le `<audio>` natif pour la rafale.
 */
let dialogue = null;
let fichierCourant = null;

/*
 * En dessous de ce pic, l'enregistrement est inaudible au casque comme aux
 * haut-parleurs — mesuré : les prises normales de ce terrain culminent entre
 * -11 et -26 dBFS, une prise au micro muet à -48.
 *
 * Ce seuil n'est pas une vérité acoustique, c'est un repère : il sépare « il ne
 * s'est rien passé dehors » de « le micro n'a rien capté », et ces deux choses
 * se ressemblent à l'oreille.
 */
const SILENCE_DBFS = -40;

// SVG_PLAY et SVG_PAUSE sont déclarés par listen.js (chargé juste avant ce script).

let segmentActif = null;
let debutSegmentActif = 0;
let finSegmentActif = 0;
let boutonActif = null;
let ligneActive = null;
let segmentTimer = null;

function arreteSegment() {
  if (segmentTimer) {
    cancelAnimationFrame(segmentTimer);
    segmentTimer = null;
  }
  if (boutonActif) {
    boutonActif.innerHTML = SVG_PLAY;
    boutonActif.classList.remove('en-lecture');
    boutonActif.title = 'Écouter ce segment';
    boutonActif.setAttribute('aria-label', 'Écouter ce segment');
  }
  if (ligneActive) {
    ligneActive.classList.remove('segment-en-cours');
  }
  segmentActif = null;
  boutonActif = null;
  ligneActive = null;
}

function surveilleSegment() {
  const lecteur = $('analyse-player');
  if (!segmentActif || !lecteur) return;
  if (lecteur.currentTime >= finSegmentActif - 0.02 || lecteur.paused || lecteur.ended) {
    if (!lecteur.paused && lecteur.currentTime >= finSegmentActif - 0.02) {
      lecteur.pause();
    }
    arreteSegment();
    return;
  }
  segmentTimer = requestAnimationFrame(surveilleSegment);
}

function parseIntervalle(intervalStr) {
  if (!intervalStr) return { debut: 0, fin: 0 };
  const m = String(intervalStr).match(/([\d.]+)\s*s?\s*à\s*([\d.]+)\s*s?/);
  if (m) {
    return { debut: parseFloat(m[1]), fin: parseFloat(m[2]) };
  }
  return { debut: 0, fin: 0 };
}

function joueSegment(s, btn, tr) {
  const lecteur = $('analyse-player');
  if (!lecteur) return;

  let debut = s.debut_s;
  let fin = s.fin_s;
  if (debut == null || fin == null) {
    const p = parseIntervalle(s.interval);
    debut = p.debut;
    fin = p.fin;
  }

  // Si ce segment est déjà en cours de lecture, un clic le met en pause
  if (segmentActif === s && !lecteur.paused) {
    lecteur.pause();
    arreteSegment();
    return;
  }

  arreteSegment();
  segmentActif = s;
  debutSegmentActif = debut;
  finSegmentActif = fin;
  boutonActif = btn;
  ligneActive = tr;

  btn.innerHTML = SVG_PAUSE;
  btn.classList.add('en-lecture');
  btn.title = 'Mettre en pause';
  btn.setAttribute('aria-label', 'Mettre en pause ce segment');
  if (tr) tr.classList.add('segment-en-cours');

  const lanceLecture = () => {
    try {
      lecteur.currentTime = Math.max(0, debut);
    } catch (_) {}
    lecteur.play().then(() => {
      cancelAnimationFrame(segmentTimer);
      segmentTimer = requestAnimationFrame(surveilleSegment);
    }).catch(() => {
      arreteSegment();
    });
  };

  if (lecteur.readyState >= 1) {
    lanceLecture();
  } else {
    lecteur.addEventListener('loadedmetadata', lanceLecture, { once: true });
    lecteur.load();
  }
}

function ouvreAnalyse(nom, eventId = null) {
  arreteSegment();
  fichierCourant = nom;
  window.fichierAnalyseCourant = nom;
  window.eventIdAnalyseCourant = eventId;
  dialogue = dialogue || $('analyse');

  $('analyse-fichier').textContent = nom;
  $('analyse-etat').textContent = 'Analyse en cours…';
  $('analyse-corps').hidden = true;
  $('analyse-vide').hidden = true;
  $('analyse-horodatage').textContent = '—';
  $('analyse-relancer').disabled = true;

  $('analyse-player-note').hidden = true;
  dialogue.showModal();

  const p1 = $('player');
  const p2 = $('captures-player');
  if (p1 && !p1.paused) p1.pause();
  if (p2 && !p2.paused) p2.pause();

  // Le lecteur de CETTE modale joue le fichier de la ligne cliquée.
  //
  // `src` après `showModal()` : ce n'est pas ce qui causait la panne (un cas
  // minimal le charge dans les deux sens), mais l'élément est alors dans
  // l'arbre de rendu, ce qui ne peut pas nuire.
  const lecteur = $('analyse-player');
  lecteur.pause();
  lecteur.src = '/ondemand/' + encodeURIComponent(nom);

  lance(nom, false);
}

async function lance(nom, relancer) {
  const url = '/api/ondemand/' + encodeURIComponent(nom) + '/analyser' +
    (relancer ? '?relancer=true' : '');
  try {
    const r = await fetch(url, { method: 'POST', headers: { Accept: 'application/json' } });
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
    const data = await r.json();
    rend(data);
  } catch (err) {
    $('analyse-etat').textContent = 'Analyse impossible : ' + err.message;
  } finally {
    $('analyse-relancer').disabled = false;
  }
}

function rend(data) {
  arreteSegment();
  const a = data.analysis || {};
  const segments = a.timeline || [];

  $('analyse-etat').textContent = data.cached
    ? 'Analyse relue du cache (déjà calculée pour ce fichier).'
    : 'Analyse terminée' + (a.source === 'remote' ? ' (service distant).' : '.');
  $('analyse-horodatage').textContent = data.analysed_at
    ? 'analysé le ' + data.analysed_at.replace('T', ' à ')
    : '—';
  $('analyse-corps').hidden = false;

  // --- résumé
  $('a-duree').textContent = a.total_duration_sec == null
    ? '—' : decimal(a.total_duration_sec.toFixed(1)) + ' s';
  $('a-fenetres').textContent = a.frames == null ? '—' : String(a.frames);
  $('a-canin').textContent = a.dog_max == null ? '—' : decimal(a.dog_max.toFixed(3));
  const canines = (a.dog_frames || []).length;
  $('a-retenues').textContent = a.frames == null ? '—' : String(canines);
  $('a-niveau').textContent = a.peak_dbfs == null
    ? '—' : decimal(a.peak_dbfs.toFixed(1)) + ' dBFS';

  // ⚠️ Un sample quasiment muet se comporte EXACTEMENT comme un lecteur cassé :
  // on appuie sur play, la flèche devient pause, et on n'entend rien. Le dire
  // ici évite de chercher la panne dans le navigateur au lieu du microphone.
  const noteNiveau = $('a-niveau-note');
  const muet = a.peak_dbfs != null && a.peak_dbfs < SILENCE_DBFS;
  noteNiveau.hidden = !muet;
  if (muet) {
    noteNiveau.textContent =
      'Le micro n\'a quasiment rien capté sur cette minute (pic ' +
      decimal(a.peak_dbfs.toFixed(1)) + ' dBFS). Le lecteur fonctionne — c\'est ' +
      'l\'enregistrement qui est muet. À vérifier côté poste : gain d\'entrée, ' +
      'capsule obstruée.';
  }

  // Le score cible, séparément de l'argmax : c'est ici qu'on voit l'écart entre « ce
  // qu'il a entendu » et « ce qu'il a décidé ».
  const note = $('a-canin-note');
  if (a.dog_best) {
    const b = a.dog_best;
    note.hidden = false;
    if (canines > 0) {
      note.className = 'card-sub canin';
      note.textContent =
        'Événement détecté : ' + canines + ' fenêtre(s) au-dessus du seuil ' +
        decimal((a.dog_threshold || 0).toFixed(2)) + '.';
    } else {
      note.className = 'card-sub';
      note.textContent =
        'Aucune fenêtre au-dessus du seuil de détection ' +
        decimal((a.dog_threshold || 0).toFixed(2)) +
        '. Le plus proche : ' + b.interval + ', score ' + decimal(b.dog.toFixed(3)) +
        ' — où l\'étiquette dominante était « ' + b.sound + ' » (' +
        decimal(b.sound_score.toFixed(3)) + ').';
    }
  } else {
    note.hidden = true;
  }

  // --- la timeline
  const corps = $('analyse-lignes');
  corps.textContent = '';
  $('analyse-vide').hidden = segments.length > 0;

  // ⚠️ On marque par NUMÉRO DE FENÊTRE, pas par chevauchement d'intervalles.
  //
  // Les fenêtres se recouvrent à 50 % (fenêtre 0,975 s, pas 0,4875 s) : deux
  // fenêtres voisines partagent la moitié de leur audio. Comparer les
  // intervalles marquerait donc la fenêtre d'à côté, qui n'est PAS la fenêtre cible — et
  // on afficherait « détection ici » sur une ligne dont le score cible est sous le seuil.
  const hop = a.hop_s || 0.4875;
  const departsRetenus = (a.dog_frames || []).map((f) => f.debut_s);
  const estRetenue = (s) => {
    if (s.debut_s == null) return false;
    const dernier = s.debut_s + Math.max(0, (s.frames || 1) - 1) * hop;
    return departsRetenus.some((o) => o >= s.debut_s - 1e-6 && o <= dernier + 1e-6);
  };

  for (const s of segments) {
    const tr = document.createElement('tr');
    tr.title = 'Cliquer pour écouter ce segment';

    const tdI = document.createElement('td');
    const wrapI = document.createElement('div');
    wrapI.className = 'intervalle-wrap';

    const bPlay = document.createElement('button');
    bPlay.type = 'button';
    bPlay.className = 'btn-play-segment';
    bPlay.title = 'Écouter ce segment';
    bPlay.setAttribute('aria-label', 'Écouter le segment ' + (s.interval || ''));
    bPlay.innerHTML = SVG_PLAY;
    bPlay.addEventListener('click', (e) => {
      e.stopPropagation();
      joueSegment(s, bPlay, tr);
    });

    const spanI = document.createElement('span');
    spanI.className = 'interval-texte';
    spanI.textContent = s.interval || (decimal(s.debut_s) + 's à ' + decimal(s.fin_s) + 's');

    wrapI.append(bPlay, spanI);
    tdI.appendChild(wrapI);

    const tdS = document.createElement('td');
    tdS.textContent = s.sound == null ? '—' : s.sound;

    const tdScore = document.createElement('td');
    tdScore.textContent = s.score == null ? '—' : decimal(s.score.toFixed(3));

    // La fenêtre est retenue, même si son étiquette dominante dit autre chose.
    // C'est le seul endroit où l'on voit qu'un événement peut se cacher sous
    // une classe parente ou sous du bruit ambiant.
    if (estRetenue(s)) {
      tr.className = 'canin';
      tdS.textContent += '  ⚠ événement détecté dans cette fenêtre';
    }

    tr.addEventListener('click', () => {
      joueSegment(s, bPlay, tr);
    });

    const tdBestof = document.createElement('td');
    tdBestof.style.textAlign = 'center';
    if (typeof window.qcRendBoutonRef === 'function') {
      const btnRef = window.qcRendBoutonRef(s, fichierCourant);
      if (btnRef) tdBestof.appendChild(btnRef);
    }

    tr.append(tdI, tdS, tdScore, tdBestof);
    corps.appendChild(tr);
  }
}

function initModale() {
  dialogue = $('analyse');

  $('analyse-fermer').addEventListener('click', () => dialogue.close());

  // ---- le lecteur de la modale
  const lecteur = $('analyse-player');

  // Trois lecteurs sur cette page, un seul son à la fois. On coupe les autres
  // AU MOMENT où celui-ci démarre — pas à l'ouverture de la modale, sinon on
  // interromprait une écoute en cours pour rien.
  lecteur.addEventListener('play', () => {
    for (const autre of [$('player'), $('captures-player')]) {
      if (autre && !autre.paused) autre.pause();
    }
  });

  // Et réciproquement : relancer un lecteur de la page pendant que la modale
  // joue doit la faire taire. Trois lecteurs, une seule règle.
  for (const autre of [$('player'), $('captures-player')]) {
    if (autre) {
      autre.addEventListener('play', () => {
        if (!lecteur.paused) lecteur.pause();
      });
    }
  }

  // Arrêt du segment quand le lecteur est mis en pause ou déplacé
  lecteur.addEventListener('pause', () => {
    if (segmentActif && (lecteur.currentTime >= finSegmentActif - 0.05 || lecteur.currentTime < debutSegmentActif - 0.2)) {
      arreteSegment();
    }
  });

  lecteur.addEventListener('seeked', () => {
    if (segmentActif && (lecteur.currentTime < debutSegmentActif - 0.1 || lecteur.currentTime > finSegmentActif + 0.1)) {
      arreteSegment();
    }
  });

  lecteur.addEventListener('ended', () => {
    arreteSegment();
  });

  // Sans ça, fermer la modale laisse le son continuer derrière.
  dialogue.addEventListener('close', () => {
    arreteSegment();
    lecteur.pause();
    if (typeof chargeSamples === 'function') chargeSamples(true);
    if (typeof window.chargeQC === 'function') window.chargeQC();
  });

  // Un média qui ne charge pas ne dit rien de lui-même : le bouton bascule en
  // pause et rien ne se passe. Sans ce message, la panne est indiscernable d'un
  // fichier muet — et on cherche au mauvais endroit.
  lecteur.addEventListener('error', () => {
    const note = $('analyse-player-note');
    note.hidden = false;
    note.textContent =
      'Lecture impossible' + (lecteur.error ? ' (code ' + lecteur.error.code + ')' : '') +
      ' — ' + (fichierCourant || 'ce fichier') + ' est-il toujours dans le dossier ?';
  });

  $('analyse-relancer').addEventListener('click', () => {
    if (!fichierCourant) return;
    $('analyse-etat').textContent = 'Nouvelle analyse en cours…';
    $('analyse-relancer').disabled = true;
    lance(fichierCourant, true);
  });

  // Clic sur le fond. Le `<dialog>` a `padding: 0` et ne contient qu'une boîte :
  // tout clic dont la cible EST le dialogue est donc un clic à côté.
  dialogue.addEventListener('click', (e) => {
    if (e.target === dialogue) dialogue.close();
  });

  // Échap est géré nativement ; on ne l'ajoute pas, ce serait le gérer deux fois.
}

initModale();
