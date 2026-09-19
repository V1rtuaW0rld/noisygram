/**
 * Noisygram — client de capture.
 *
 * Répartition des rôles (§9.5) : le WORKLET possède le buffer audio et ne fait
 * que trois choses — downmixer, tenir le ring, poster un RMS par trame. Le
 * THREAD PRINCIPAL décide. C'est lui qui tient la médiane de fond, applique la
 * règle de déclenchement, débounce, et parle au serveur.
 *
 * Aucun de ces choix n'est décoratif : chaque garde-fou correspond à un mode
 * de défaillance qui produit un client d'apparence saine et un compteur à
 * zéro. Ils sont commentés là où ils vivent.
 */
'use strict';

// ---------------------------------------------------------------- constantes

const CONFIG = {
  // Identifiant du poste. DOIT être unique par machine : c'est lui qui porte
  // la déduplication (client_id, client_seq) côté serveur.
  clientId: 'poste-exterieur-01',

  preRollMs: 1000,
  postRollMs: 2000,

  // La géométrie (frameSize, framesPerSecond, ringSlots, medianEveryFrames)
  // n'est PAS ici : elle dépend du taux du contexte, connu seulement à
  // startAudio(). Elle vit dans state.geo, créée par configureGeometry() — les
  // mettre à jour séparément produit une médiane NaN et un client muet qui
  // affiche « écoute ». Voir configureGeometry.
  ringSeconds: 10.9, // profondeur de l'historique servant à la médiane de fond
  medianEveryMs: 170, // cadence de recalcul de la médiane de fond
  warmupMs: 10000, // le temps que la médiane soit une statistique

  backoffBaseMs: 1000,
  backoffMaxMs: 30000,
  backoffJitter: 0.2,

  maxQueue: 20, // ~1,9 Mo en 16 kHz ; au-delà on jette les PLUS ANCIENS
  watchdogMs: 2000,
  pingMs: 15000,
  uiRefreshMs: 200,
  logLines: 12,

  // Valeurs par défaut, remplacées par le config_patch du serveur.
  cooldownMs: 3000,
  micGain: 1.0,
  triggerRatio: 2.5,
  minRmsFloor: 0.004,

  // Garde-tempête, réglable lui aussi. À 12 déclenchements par minute, la
  // capture se coupait au bout de 36 s : une source sonore de deux minutes
  // n'était jamais enregistré d'un seul tenant, et aucun recollage ne pouvait
  // le reconstituer. 40 laisse ~2 min continues, tout en gardant le filet de
  // sécurité contre un déclencheur bloqué par une alarme de voiture.
  stormWindowMs: 60000,
  stormMax: 40,
  stormSuspendMs: 60000,

  // Épisode. Poussées par le serveur ; ces valeurs ne valent qu'au premier
  // démarrage, avant le hello_ack.
  //
  // streamSilenceMs est ce qui CLÔT un épisode : la source peut continuer, se
  // taire 15 s, reprendre — c'est le même épisode, et le fichier gardera la
  // pause, parce qu'un fichier recollé mentirait sur l'axe du temps.
  streamSilenceMs: 30000,
  streamMaxMs: 180000,

  // Droit d'être écouté à la demande par l'opérateur. Poussé par le serveur :
  // couper la fonctionnalité là-bas sans que le poste le sache laisserait la
  // page d'écoute attendre un audio qui ne viendrait jamais.
  listenEnabled: true,
};

// Le fond sonore d'un micro muet ou débranché donne une médiane à 0, donc
// `rms > 0` devient vrai sur n'importe quel échantillon non nul et le client
// DÉCLENCHE EN CONTINU sur du dither. Ce plancher est le garde-fou (§9.3).
const BG_FLOOR = 1e-5;

// ---------------------------------------------------------------- utilitaires

const $ = (id) => document.getElementById(id);

function log(message, level) {
  const list = $('log');
  const li = document.createElement('li');
  const heure = new Date().toLocaleTimeString('fr-FR', { hour12: false });
  li.className = 'log-line' + (level ? ' log-line--' + level : '');
  li.textContent = heure + '  ' + message;
  list.prepend(li);
  while (list.children.length > CONFIG.logLines) list.removeChild(list.lastChild);
}

function dot(pillId, state, text) {
  const pill = $(pillId);
  pill.dataset.state = state;
  pill.querySelector('.val').textContent = text;
}

function fmt(v, digits) {
  return v === null || v === undefined || Number.isNaN(v)
    ? '—'
    : v.toFixed(digits === undefined ? 4 : digits);
}

// ---------------------------------------------------------------- état

const state = {
  running: false,
  context: null,
  stream: null,
  source: null,
  micGainNode: null, // gain logiciel inséré entre source et node (préampli)
  node: null,
  sink: null, // gain à 0 : garde le graphe « tiré » sans réinjecter le micro

  ws: null,
  wsReady: false,
  wsAttempts: 0,
  reconnectTimer: null,
  pingTimer: null,
  watchdogTimer: null,

  // Géométrie + ses tableaux, créés ensemble par configureGeometry(). null tant
  // que startAudio() n'a pas ouvert le contexte : on ne connaît pas le taux
  // avant, donc toute allocation faite ici serait une supposition.
  geo: null,
  frameCount: 0,

  bgMedian: 0,
  lastRms: 0,
  lastPeak: 0,
  armedForNext: true,

  capturing: false,
  cooldownUntil: 0,
  stormSuspendedUntil: 0,
  eventTimes: [],

  // Épisode en cours. Le client streame tant qu'il y a du bruit, et clôt après
  // assez de silence : le fichier produit couvre la scène d'un bout à l'autre,
  // sans couture.
  streaming: false,
  streamSeq: null,
  streamChunks: 0,
  streamSamples: 0,
  streamStartedAt: 0,
  lastLoudAt: 0,
  // 'stream' tant que le serveur accepte les flux ; 'legacy' s'il répond
  // « type inconnu » (rollback). Sans ce repli, chaque déclenchement serait
  // perdu en silence jusqu'à ce que quelqu'un vide le cache.
  streamMode: 'stream',

  // Écoute à la demande, déclenchée par l'opérateur depuis le serveur. Le poste
  // ne fait que streamer : il ignore qui écoute, et n'a rien à en savoir.
  listening: false,
  listenId: null,
  listenSeq: null,
  listenChunks: 0,
  listenSamples: 0,
  // Le worklet sait-il faire une écoute ? Renseigné par `configured`. Faux tant
  // qu'on ne le sait pas, pour refuser franchement plutôt que d'attendre un son
  // qui ne viendra jamais.
  workletListenOk: false,

  lastRmsAt: 0,
  queue: [],
  // Segments envoyés, en attente de réponse, indexés par seq. Sert à REMETTRE
  // en file ce que le serveur refuse en « busy » — sans ça le segment est
  // définitivement perdu, et rien ne le signale.
  inflight: {},
  counters: { sent: 0, accepted: 0, rejected: 0, errors: 0 },

  // Le compteur de séquence est PERSISTÉ. Sans ça, un rechargement de page le
  // ramènerait à 0, et le serveur — qui déduplique sur (client_id,
  // client_seq) — prendrait le premier segment de la nouvelle session pour un
  // rejeu de l'ancienne : il répondrait « duplicate » et JETTERAIT l'audio.
  // Une perte d'événement silencieuse, exactement ce qu'on cherche à éviter.
  seq: Number(lireReglage('seq') || 0),
};

function nextSeq() {
  state.seq += 1;
  try {
    window.localStorage.setItem('noisygram.seq', String(state.seq));
  } catch (err) {
    /* mode privé : on continue, au risque d'un doublon après rechargement */
  }
  return state.seq;
}

// ---------------------------------------------------------------- préflight

// AVANT tout le reste. Le piège classique est d'écrire
// `try { await navigator.mediaDevices.getUserMedia(...) } catch {}` : sur une
// origine non sécurisée, `navigator.mediaDevices` est undefined, l'accès lève
// un TypeError, et le catch l'affiche comme « permission refusée » — ce qui
// blâme le microphone pour un problème qui n'a rien à voir (§2.2).
function preflight() {
  const securise = window.isSecureContext;
  const dispo = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
  if (securise && dispo) return true;

  $('pf-origin').textContent = window.location.origin;
  $('preflight').hidden = false;
  log('origine non sécurisée — micro inaccessible', 'error');
  return false;
}

// ---------------------------------------------------------------- audio

// Fréquence visée pour la capture. YAMNet ne voit jamais au-dessus de 8 kHz —
// le serveur rééchantillonne en 16 kHz dès la réception et encode le MP3 depuis
// ce 16 kHz. Envoyer du 48 kHz revenait donc à jeter les deux tiers de chaque
// envoi. Le contexte fait la conversion, une seule fois, avec son propre filtre
// anti-repliement.
const SR_CIBLE = 16000;

// Durée visée d'une fenêtre RMS. Ce n'est pas un nombre d'échantillons : la
// trame s'en déduit, selon le taux que le contexte a réellement accepté.
const FRAME_MS = 20;

/**
 * Crée la géométrie de traitement ET ses tableaux, ensemble.
 *
 * Les séparer est la faute qui produit une panne muette : `ring` est parcouru
 * sur `ringSlots` cases par computeMedian(), donc mettre à jour la constante
 * sans réallouer le tableau fait lire au-delà de la fin — `undefined`, médiane
 * NaN, `bg >= BG_FLOOR` faux, et le client ne déclenche plus JAMAIS tout en
 * affichant « écoute ». Une seule fonction, donc, et elle alloue.
 *
 * `frameCount` et `bgMedian` ne sont PAS remis à zéro : un redémarrage audio
 * (micro débranché puis rebranché) ne doit pas aveugler la boîte le temps d'un
 * nouveau warm-up.
 */
function configureGeometry(sampleRate, frameSize) {
  const geo = { sampleRate, frameSize, framesPerSecond: sampleRate / frameSize };
  geo.ringSlots = Math.round(CONFIG.ringSeconds * geo.framesPerSecond);
  geo.medianEveryFrames = Math.max(
    1,
    Math.round((CONFIG.medianEveryMs / 1000) * geo.framesPerSecond)
  );
  geo.ring = new Float32Array(geo.ringSlots);
  geo.scratch = new Float32Array(geo.ringSlots);
  geo.ringIndex = 0;
  geo.ringFilled = 0;
  state.geo = geo;
  return geo;
}

/** La géométrie en place. Lève si startAudio() n'a pas encore tourné : un appel
 *  avant l'ouverture du contexte est un bug, pas un cas à rattraper. */
function geo() {
  if (!state.geo) throw new Error('géométrie non configurée — startAudio() d’abord');
  return state.geo;
}

async function startAudio() {
  // ① Les trois contraintes. echoCancellation, noiseSuppression et
  // autoGainControl sont TOUS à true par défaut dans Chrome. L'AGC fait
  // baisser le gain sur une scène calme jusqu'à ce qu'un événement bouge à
  // peine le vumètre ; la suppression de bruit est entraînée à retirer
  // précisément les transitoires non-parole — un événement, exactement.
  // Résultat sans ça : un client parfaitement sain dont le compteur reste à 0.
  state.stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      echoCancellation: false,
      noiseSuppression: false,
      autoGainControl: false,
      channelCount: 1,
      sampleRate: 48000,
    },
  });
  dot('pill-mic', 'ok', 'ouvert');

  const piste = state.stream.getAudioTracks()[0];
  let srPiste = null;
  if (piste) {
    const r = piste.getSettings ? piste.getSettings() : {};
    srPiste = r.sampleRate || null;
    // Perte de micro : USB débranché, accès révoqué, veille. Une boîte
    // extérieure non surveillée verra ça.
    piste.onended = () => {
      log('micro perdu — ré-acquisition', 'error');
      dot('pill-mic', 'error', 'perdu');
      restartAudio();
    };
  }

  // Le contexte est ouvert à la fréquence visée. Deux précautions :
  //   • sous try — un AudioContext peut LEVER au lieu d'ignorer la demande, et
  //     une exception ici tuerait tout startAudio(), donc l'écoute, avec pour
  //     seul message « démarrage impossible » ;
  //   • le taux RÉELLEMENT obtenu est relu, jamais supposé — le navigateur a le
  //     droit d'ignorer la demande.
  const Ctx = window.AudioContext || window.webkitAudioContext;
  try {
    state.context = new Ctx({ sampleRate: SR_CIBLE });
  } catch (err) {
    log('contexte à ' + SR_CIBLE + ' Hz refusé (' + err.message + ') — repli', 'warn');
    state.context = new Ctx();
  }
  if (state.context.sampleRate !== SR_CIBLE) {
    log('contexte à ' + state.context.sampleRate + ' Hz — ' + SR_CIBLE + ' ignoré', 'warn');
  }

  // La géométrie se cale sur le taux du CONTEXTE, pas sur celui de la piste :
  // c'est le contexte qui cadence les messages du worklet. Prendre le taux de
  // la piste ferait calculer un warm-up sur une cadence qui n'existe pas — dix
  // secondes deviendraient trente, sans une seule erreur au journal.
  const g = configureGeometry(
    state.context.sampleRate,
    Math.round((state.context.sampleRate * FRAME_MS) / 1000)
  );
  dot('pill-device', 'ok', (srPiste ? srPiste + ' → ' : '') + g.sampleRate + ' Hz');

  await state.context.audioWorklet.addModule('recorder-worklet.js');

  state.source = state.context.createMediaStreamSource(state.stream);
  state.micGainNode = state.context.createGain();
  state.micGainNode.gain.value = CONFIG.micGain;

  state.node = new AudioWorkletNode(state.context, 'noisygram-recorder', {
    numberOfInputs: 1,
    numberOfOutputs: 1,
    outputChannelCount: [1],
    processorOptions: {
      preRollMs: CONFIG.preRollMs,
      postRollMs: CONFIG.postRollMs,
      // La trame voyage avec la commande : le worklet n'a plus de constante
      // homologue à tenir à jour de son côté.
      frameSize: g.frameSize,
    },
  });
  state.node.port.onmessage = onWorkletMessage;

  // Un AudioWorkletNode n'est « tiré » par le moteur que s'il participe au
  // graphe de rendu. Le connecter directement à destination réinjecterait le
  // micro dans les haut-parleurs — larsen immédiat. D'où ce gain à zéro :
  // le nœud est rendu, mais rien n'est audible.
  state.sink = state.context.createGain();
  state.sink.gain.value = 0;
  state.source.connect(state.micGainNode);
  state.micGainNode.connect(state.node);
  state.node.connect(state.sink);
  state.sink.connect(state.context.destination);

  // Politique d'autoplay après un redémarrage du navigateur, ou audio suspendu
  // par l'OS : le contexte passe `suspended` et plus rien n'arrive, en silence.
  state.context.onstatechange = () => {
    if (!state.running) return;
    if (state.context.state === 'suspended') {
      log('contexte audio suspendu — reprise', 'warn');
      dot('pill-capture', 'warn', 'suspendu');
      state.context.resume().catch(() => {
        log('resume() refusé — un clic sera nécessaire', 'error');
        dot('pill-capture', 'error', 'à réveiller');
      });
    }
  };

  state.node.port.postMessage({
    type: 'configure',
    preRollMs: CONFIG.preRollMs,
    postRollMs: CONFIG.postRollMs,
  });

  state.lastRmsAt = Date.now();
  dot('pill-capture', 'ok', 'écoute');
  log('écoute démarrée (' + state.context.sampleRate + ' Hz)');
}

async function stopAudio() {
  if (state.node) {
    state.node.port.onmessage = null;
    try { state.node.disconnect(); } catch (err) { /* déjà démonté */ }
  }
  if (state.source) { try { state.source.disconnect(); } catch (err) {} }
  if (state.micGainNode) { try { state.micGainNode.disconnect(); } catch (err) {} }
  if (state.sink) { try { state.sink.disconnect(); } catch (err) {} }
  if (state.stream) state.stream.getTracks().forEach((t) => t.stop());
  if (state.context && state.context.state !== 'closed') await state.context.close();

  state.node = state.source = state.micGainNode = state.sink = state.context = state.stream = null;
  dot('pill-capture', 'idle', 'arrêté');
  dot('pill-live', 'idle', '—');
  dot('pill-mic', 'idle', 'fermé');
}

let restarting = false;
async function restartAudio() {
  // Le watchdog et onended peuvent se déclencher ensemble : sans ce verrou on
  // démonterait deux fois le même graphe.
  if (restarting || !state.running) return;
  restarting = true;
  try {
    // Le worklet va mourir avec le graphe : sans ça, le serveur attendrait la
    // fin d'un flux qui ne viendra jamais, en immobilisant un fichier
    // temporaire jusqu'à son délai d'inactivité.
    if (state.streaming) endStream('client_final');
    // Le worklet meurt avec le graphe : sans ça, le serveur attendrait la fin
    // d'une écoute qui ne viendra jamais, et le WAV ne serait publié qu'à
    // l'expiration de son délai d'inactivité — cinq secondes plus tard, sans
    // jugement.
    if (state.listening) endListen('client_final');
    await stopAudio();
    await startAudio();
  } catch (err) {
    log('redémarrage audio impossible : ' + err.message, 'error');
    await new Promise((r) => setTimeout(r, 2000));
  } finally {
    restarting = false;
  }
}

// ------------------------------------------------------- messages du worklet

function onWorkletMessage(event) {
  const msg = event.data;
  if (!msg) return;

  if (msg.type === 'rms') {
    state.lastRmsAt = Date.now();
    pushRms(msg.rms, msg.peak);
    evaluateTrigger();
    return;
  }

  if (msg.type === 'captured') {
    state.capturing = false;
    dot('pill-capture', 'ok', 'écoute');
    handleCaptured(msg);
    return;
  }

  if (msg.type === 'stream_chunk') {
    handleStreamChunk(msg);
    return;
  }

  if (msg.type === 'stream_stopped') {
    // Bilan du worklet. Il n'y a rien à en faire : le thread principal tient
    // déjà ses propres compteurs, et le serveur reçoit les siens.
    return;
  }

  if (msg.type === 'listen_started') {
    // Le worklet a ouvert son tampon d'écoute. On note la taille RÉELLE qu'il
    // applique, comme pour la trame : c'est lui qui a raison sur la géométrie.
    state.workletListenOk = true;
    if (msg.chunkSize) state.listenChunkSize = msg.chunkSize;
    return;
  }

  if (msg.type === 'listen_chunk') {
    handleListenChunk(msg);
    return;
  }

  if (msg.type === 'listen_stopped') {
    return;
  }

  if (msg.type === 'configured') {
    // Le worklet est propriétaire de l'audio, donc c'est lui qui a raison sur
    // la géométrie. Si les deux fichiers ne sont pas de la même version — ils
    // sont mis en cache séparément par le CDN — on se réaligne sur ce qu'il
    // annonce, au lieu de supposer. Peut arriver APRÈS des messages `rms` :
    // configureGeometry préserve frameCount et bgMedian, donc la seule séquelle
    // est une médiane calculée sur quelques trames.
    if (msg.frameSize && state.geo && msg.frameSize !== state.geo.frameSize) {
      log('worklet : trame de ' + msg.frameSize + ' éch. — géométrie réalignée', 'warn');
      configureGeometry(msg.sampleRate, msg.frameSize);
    }
    // Worklet d'AVANT le mode épisode : il ignorerait `stream_begin` en
    // silence, n'émettrait aucun morceau, et le serveur recevrait un épisode
    // vide qu'il refuserait — tout en affichant « écoute » ici. On repasse au
    // clip de 3 s, qui marche avec les deux versions.
    if (!msg.streaming && state.streamMode === 'stream') {
      state.streamMode = 'legacy';
      log('worklet sans mode épisode — retour au clip de 3 s', 'warn');
    }
    // Le pendant exact pour l'écoute. `app.js` et le worklet sont deux fichiers
    // mis en cache SÉPARÉMENT par le CDN : ils peuvent ne pas être de la même
    // version, et un worklet d'avant ignorerait `listen_begin` en silence. Sans
    // cette note, le poste croirait savoir écouter et laisserait l'opérateur
    // attendre un son qui ne viendrait jamais.
    state.workletListenOk = msg.listening === true;
    if (!msg.listening) {
      log('worklet sans mode écoute — rechargez la page du poste', 'warn');
    }
    return;
  }

  if (msg.type === 'error') {
    // Une exception dans process() tue le processeur : sans ce message, la
    // page garderait son voyant « écoute » et aucun audio n'arriverait plus.
    log('worklet : ' + msg.message, 'error');
    dot('pill-capture', 'error', 'worklet mort');
    restartAudio();
  }
}

// ------------------------------------------------------- niveau et médiane

function pushRms(rms, peak) {
  state.lastRms = rms;
  state.lastPeak = peak;

  const g = geo();
  g.ring[g.ringIndex] = rms;
  g.ringIndex = (g.ringIndex + 1) % g.ringSlots;
  if (g.ringFilled < g.ringSlots) g.ringFilled++;
  state.frameCount++;

  // Médiane EXACTE, pas approchée : ~30 µs pour ~550 floats, soit 0,15 % d'un
  // cœur. Recalculée 1 trame sur 9 et mise en cache entre-temps → 9× moins.
  // La médiane d'une statistique de 10,9 s rafraîchie 5,5×/s au lieu de 50×/s
  // ne change rigoureusement rien.
  if (state.frameCount % g.medianEveryFrames === 0) {
    state.bgMedian = computeMedian();
  }
}

function computeMedian() {
  const g = geo();
  const n = Math.min(g.ringFilled, g.ringSlots);
  if (n === 0) return 0;
  const start = g.ringFilled < g.ringSlots ? 0 : g.ringIndex;
  for (let i = 0; i < n; i++) {
    g.scratch[i] = g.ring[(start + i) % g.ringSlots];
  }
  // TypedArray.sort() est numérique par défaut — pas de piège de comparateur.
  const vue = g.scratch.subarray(0, n);
  vue.sort();
  return n % 2 ? vue[(n - 1) >> 1] : (vue[n / 2 - 1] + vue[n / 2]) / 2;
}

/** Le niveau courant dépasse-t-il la règle de déclenchement ? */
function estBruyant() {
  const bg = state.bgMedian;
  if (bg < BG_FLOOR) return false; // micro muet : aucun seuil n'a de sens
  return state.lastRms > Math.max(bg * CONFIG.triggerRatio, CONFIG.minRmsFloor);
}

function evaluateTrigger() {
  if (!state.running) return;

  // Pendant un épisode, ce n'est plus le DÉCLENCHEMENT qui nous occupe mais la
  // FIN. On suit le dernier instant bruyant ; l'épisode se clôt après assez de
  // silence, ou sur la durée maximale (garde-fou disque).
  if (state.streaming) {
    const now = Date.now();
    if (estBruyant()) state.lastLoudAt = now;
    if (now - state.lastLoudAt >= CONFIG.streamSilenceMs) endStream('silence');
    else if (now - state.streamStartedAt >= CONFIG.streamMaxMs) endStream('max_duration');
    return;
  }

  if (state.capturing) return;

  // Pendant une écoute, on ne déclenche PAS — mais rien n'est perdu pour
  // autant : le serveur enregistre la minute entière et la classe fenêtre par
  // fenêtre. Si quelque chose en ressort, il en fait un épisode à la fin. La
  // détection n'est pas arrêtée, elle est RELOCALISÉE.
  //
  // Le worklet refuse de toute façon un `trigger` pendant une écoute ; ce test
  // évite en plus de consommer le garde-tempête et le compteur d'événements
  // pour des déclenchements qui n'auraient pas lieu.
  if (state.listening) return;

  const now = Date.now();
  const bg = state.bgMedian;
  const rms = state.lastRms;

  // Vraie hystérésis : ne se réarmer que lorsque le niveau est retombé, sinon
  // la boîte se réarme sur la queue décroissante du même bruit.
  if (!state.armedForNext) {
    if (rms < bg * 1.5) state.armedForNext = true;
    return;
  }

  if (now < state.cooldownUntil) return;
  if (now < state.stormSuspendedUntil) return;

  // Warm-up COMPLET de 10 s : sans lui, l'estimateur déclenche sur son propre
  // remplissage, quand la médiane porte encore sur trois trames.
  const warmupFrames = (CONFIG.warmupMs / 1000) * geo().framesPerSecond;
  if (state.frameCount < warmupFrames) return;

  const usable = bg >= BG_FLOOR; // garde-fou micro muet
  if (!usable) return;

  const hot = rms > Math.max(bg * CONFIG.triggerRatio, CONFIG.minRmsFloor);
  if (!hot) return;

  triggerCapture();
}

function triggerCapture() {
  const now = Date.now();

  // Garde-tempête : au-delà de 12 événements en 60 s, suspension. C'est ce qui
  // protège le serveur quand une alarme de voiture ou un souffleur de feuilles
  // s'installe dehors pour dix minutes.
  state.eventTimes = state.eventTimes.filter((t) => now - t < CONFIG.stormWindowMs);
  state.eventTimes.push(now);
  if (state.eventTimes.length > CONFIG.stormMax) {
    state.stormSuspendedUntil = now + CONFIG.stormSuspendMs;
    log('garde-tempête : ' + state.eventTimes.length + ' événements/min — suspension 60 s', 'warn');
    dot('pill-capture', 'warn', 'suspendu');
    return;
  }

  // Repli : le serveur ne connaît pas les flux. On refait alors exactement ce
  // qu'on faisait avant — un clip de 3 s — plutôt que de tout perdre.
  if (state.streamMode === 'legacy') {
    state.capturing = true;
    state.armedForNext = false;
    state.cooldownUntil = now + CONFIG.cooldownMs;
    state.captureTriggeredAt = now;
    dot('pill-capture', 'busy', 'capture');
    state.node.port.postMessage({ type: 'trigger' });
    return;
  }

  startStream(now);
}

// ---------------------------------------------------------------- épisode

function startStream(now) {
  const g = geo();
  state.streaming = true;
  state.armedForNext = false;
  // Le cooldown n'est PAS repositionné : il ajouterait jusqu'à 3 s de zone
  // morte après chaque épisode, et sur des événements espacés le client
  // manquerait le suivant — un compteur à zéro sur une boîte qui a l'air saine.
  state.streamSeq = nextSeq();
  state.streamChunks = 0;
  state.streamSamples = 0;
  state.streamStartedAt = now;
  state.lastLoudAt = now;
  state.captureTriggeredAt = now;
  dot('pill-capture', 'busy', 'épisode');

  // Les métadonnées partent AVANT que le worklet ne soit prévenu : le serveur
  // doit avoir ouvert le flux quand le premier morceau arrive.
  const meta = {
    type: 'stream_start',
    seq: state.streamSeq,
    captured_at_ms: now,
    sample_rate: g.sampleRate,
    channels: 1,
    format: 's16le',
    // Une DURÉE, pas un instant : le serveur en déduit l'instant du premier
    // échantillon sur SA propre horloge, et une durée ne peut pas dériver.
    pre_roll_samples: Math.ceil((CONFIG.preRollMs / 1000) * g.sampleRate),
    chunk_samples: g.chunkSize,
    rms: Number(state.lastRms.toFixed(6)),
    background_rms: Number(state.bgMedian.toFixed(6)),
    trigger_ratio:
      state.bgMedian > 0 ? Number((state.lastRms / state.bgMedian).toFixed(3)) : null,
  };

  if (!(state.wsReady && state.ws && state.ws.readyState === WebSocket.OPEN)) {
    // Sans lien, inutile d'ouvrir un épisode : on le dit et on n'amasse rien.
    state.streaming = false;
    log('déclenchement hors ligne — épisode abandonné', 'warn');
    return;
  }
  state.ws.send(JSON.stringify(meta));
  state.inflight[state.streamSeq] = meta;
  state.counters.sent++;
  updateCounters();

  state.node.port.postMessage({ type: 'stream_begin' });
}

function endStream(reason) {
  if (!state.streaming) return;
  state.streaming = false;
  if (state.node) state.node.port.postMessage({ type: 'stream_stop' });

  if (state.wsReady && state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(
      JSON.stringify({
        type: 'stream_end',
        seq: state.streamSeq,
        chunks: state.streamChunks,
        num_samples: state.streamSamples,
        stopped_reason: reason,
      })
    );
  }
  const secondes = state.streamSamples / geo().sampleRate;
  log(
    'épisode clos (' + reason + ') — ' + state.streamChunks + ' morceaux, ' +
      secondes.toFixed(1) + ' s'
  );
  dot('pill-capture', 'ok', 'écoute');
  state.streamSeq = null;
}

/**
 * Un morceau. Converti et envoyé à la volée — c'est ce qui fait qu'un épisode
 * de trois minutes ne tient jamais entier en mémoire côté client.
 */
function handleStreamChunk(msg) {
  if (!state.streaming || !state.node) return; // morceau en vol après la fin
  const f32 = msg.pcm;
  const n = msg.numSamples;
  const i16 = new Int16Array(n);
  for (let i = 0; i < n; i++) {
    const s = f32[i];
    const v = s < -1 ? -1 : s > 1 ? 1 : s;
    i16[i] = v < 0 ? v * 0x8000 : v * 0x7fff;
  }
  state.streamChunks++;
  state.streamSamples += n;

  if (state.wsReady && state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(i16.buffer);
    updateCounters();
    return;
  }
  // Lien perdu en plein épisode : on abandonne. Un flux partiel n'est JAMAIS
  // mis dans la file hors ligne — elle stocke des objets complets, et un
  // fragment produirait un épisode recollé, à la durée fausse, avec un seq
  // déjà vu que le serveur jetterait.
  endStream('client_final');
  log('lien perdu en plein épisode — abandonné, jamais rejoué', 'error');
}

// ------------------------------------------------------- écoute à la demande

/**
 * Ouvre une écoute, sur ordre du serveur.
 *
 * Le `listen_id` reçu est RÉÉMIS tel quel : c'est ce qui permet au serveur
 * d'apparier la réponse, alors qu'un déclenchement a parfaitement le droit
 * d'avoir eu lieu entre l'ordre et la réponse.
 */
function startListen(msg) {
  const g = geo();

  // Refus francs, et chacun NOMMÉ. Sans ça, l'opérateur voit une page muette et
  // va chercher une panne réseau là où il n'y en a pas.
  if (!CONFIG.listenEnabled) {
    refuseListen(msg.listen_id, 'l\'écoute à la demande est désactivée sur le serveur');
    return;
  }
  if (!state.running || !state.node) {
    refuseListen(msg.listen_id, 'le poste n\'est pas en écoute — cliquez sur Démarrer');
    return;
  }
  if (!state.workletListenOk) {
    refuseListen(msg.listen_id, 'worklet pas à jour — rechargez la page du poste');
    return;
  }
  if (state.listening) {
    refuseListen(msg.listen_id, 'une écoute est déjà en cours');
    return;
  }

  // ⚠️ L'écoute INTERROMPT l'épisode — elle ne l'attend pas et ne le refuse pas.
  //
  // Et c'est ICI que ça se séquence, pas côté serveur : le client est le seul à
  // tenir les deux états, donc il clôt le flux et ouvre l'écoute dans le MÊME
  // tick. Le serveur reçoit `stream_end` puis `listen_start` dans cet ordre, sur
  // le même socket, et sa boucle de réception est strictement séquentielle —
  // aucun échantillon ne peut se glisser entre les deux.
  //
  // L'épisode n'est PAS perdu : le serveur le juge et l'archive sur l'audio reçu
  // jusque-là, exactement comme s'il s'était terminé seul. Le son continue dans
  // le WAV de l'écoute.
  //
  // Refuser serait le pire des deux mondes : c'est quand ça détecte qu'on a envie
  // d'écouter, et sur un terrain actif un épisode est ouvert presque toujours.
  if (state.streaming) {
    log('écoute demandée — l\'épisode en cours est clos et archivé', 'warn');
    endStream('listen_preempt');
  }

  // Ce que le serveur a RETENU, pas ce qu'on aurait voulu : il borne la durée,
  // et un compte à rebours faux ferait croire à une panne quand le flux
  // s'arrête à l'heure prévue par le serveur.
  const duree = msg.duration_ms || 60000;
  const chunkMs = msg.chunk_ms || 200;

  state.listening = true;
  state.listenId = msg.listen_id;
  state.listenSeq = nextSeq();
  state.listenChunks = 0;
  state.listenSamples = 0;
  dot('pill-live', 'busy', 'en cours');

  // Les métadonnées AVANT de prévenir le worklet : le serveur doit avoir ouvert
  // l'écoute quand le premier morceau arrive.
  state.ws.send(
    JSON.stringify({
      type: 'listen_start',
      listen_id: msg.listen_id,
      seq: state.listenSeq,
      captured_at_ms: Date.now(),
      sample_rate: g.sampleRate,
      channels: 1,
      format: 's16le',
      chunk_samples: Math.round((chunkMs / 1000) * g.sampleRate),
      // PAS de pré-roll : on veut le direct, et reculer d'une seconde daterait
      // le fichier avant son propre horodatage.
      pre_roll_samples: 0,
      rms: Number(state.lastRms.toFixed(6)),
      background_rms: Number(state.bgMedian.toFixed(6)),
    })
  );

  state.node.port.postMessage({
    type: 'listen_begin',
    chunkSamples: Math.round((chunkMs / 1000) * g.sampleRate),
  });
  log('écoute demandée par l\'opérateur — ' + Math.round(duree / 1000) + ' s');
}

function refuseListen(listenId, raison) {
  log('écoute refusée : ' + raison, 'warn');
  dot('pill-live', 'warn', 'refusée');
  if (!state.wsReady || !state.ws || state.ws.readyState !== WebSocket.OPEN) return;
  // On répond quand même, en portant le MOTIF : le serveur résout son attente
  // tout de suite et l'annonce tel quel à l'opérateur, au lieu de le laisser
  // cinq secondes devant une page muette puis lui servir un message générique.
  const seq = nextSeq();
  state.ws.send(
    JSON.stringify({
      type: 'listen_start',
      listen_id: listenId,
      seq: seq,
      sample_rate: 16000,
      channels: 1,
      format: 's16le',
      chunk_samples: 0,
      refus: raison,
    })
  );
  // Le `listen_end` referme le drapeau d'avalement côté serveur tout de suite,
  // plutôt que de le laisser courir jusqu'à la prochaine trame.
  state.ws.send(
    JSON.stringify({
      type: 'listen_end',
      listen_id: listenId,
      seq: seq,
      chunks: 0,
      num_samples: 0,
      stopped_reason: 'refus',
    })
  );
}

function endListen(reason) {
  if (!state.listening) return;
  state.listening = false;
  if (state.node) state.node.port.postMessage({ type: 'listen_stop' });

  if (state.wsReady && state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(
      JSON.stringify({
        type: 'listen_end',
        listen_id: state.listenId,
        seq: state.listenSeq,
        chunks: state.listenChunks,
        num_samples: state.listenSamples,
        stopped_reason: reason,
      })
    );
  }
  log(
    'écoute close (' + reason + ') — ' + state.listenChunks + ' morceaux, ' +
      (state.listenSamples / geo().sampleRate).toFixed(1) + ' s'
  );
  dot('pill-live', 'idle', '—');
  state.listenId = null;
  state.listenSeq = null;
}

/**
 * Un morceau d'écoute. Même conversion que `handleStreamChunk`, mais AUCUNE
 * mise en file hors ligne : une écoute est un direct, et un morceau rejoué une
 * heure plus tard n'a plus aucun sens.
 */
function handleListenChunk(msg) {
  if (!state.listening || !state.node) return; // morceau en vol après la fin
  const f32 = msg.pcm;
  const n = msg.numSamples;
  const i16 = new Int16Array(n);
  for (let i = 0; i < n; i++) {
    const s = f32[i];
    const v = s < -1 ? -1 : s > 1 ? 1 : s;
    i16[i] = v < 0 ? v * 0x8000 : v * 0x7fff;
  }
  state.listenChunks++;
  state.listenSamples += n;

  if (state.wsReady && state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(i16.buffer);
    return;
  }
  endListen('client_final');
  log('lien perdu en pleine écoute — close', 'error');
}

// ------------------------------------------------------- envoi d'un segment

function handleCaptured(msg) {
  const f32 = msg.pcm;
  const n = msg.numSamples;
  const i16 = new Int16Array(n);
  for (let i = 0; i < n; i++) {
    const s = f32[i];
    const v = s < -1 ? -1 : s > 1 ? 1 : s;
    i16[i] = v < 0 ? v * 0x8000 : v * 0x7fff;
  }

  const segment = {
    seq: nextSeq(),
    captured_at_ms: state.captureTriggeredAt || Date.now(),
    sample_rate: msg.sampleRate,
    channels: 1,
    format: 's16le',
    num_samples: n,
    rms: Number(state.lastRms.toFixed(6)),
    background_rms: Number(state.bgMedian.toFixed(6)),
    trigger_ratio: state.bgMedian > 0 ? Number((state.lastRms / state.bgMedian).toFixed(3)) : null,
    post_roll_ms: msg.postRollMs,
    pcm: i16,
  };

  if (state.wsReady && state.ws && state.ws.readyState === WebSocket.OPEN) {
    sendSegment(segment);
  } else {
    // File bornée, vidée à la reconnexion, PLUS ANCIEN D'ABORD : quand elle
    // est pleine on jette les plus anciens. Une file non bornée ferait OOM
    // l'onglet après une longue panne ; jeter les plus RÉCENTS perdrait les
    // événements les plus intéressants.
    state.queue.push(segment);
    while (state.queue.length > CONFIG.maxQueue) state.queue.shift();
    log('hors ligne — segment en file (' + state.queue.length + '/' + CONFIG.maxQueue + ')', 'warn');
  }
  updateCounters();
}

function sendSegment(segment) {
  const meta = Object.assign({}, segment);
  delete meta.pcm;
  meta.type = 'segment_start';

  state.ws.send(JSON.stringify(meta));
  state.ws.send(segment.pcm.buffer);
  state.counters.sent++;
  state.pendingScores = state.pendingScores || {};
  state.pendingScores[segment.seq] = {
    rms: segment.rms,
    bg: segment.background_rms,
  };
  state.inflight[segment.seq] = segment;
  updateCounters();
}

// Le serveur n'accepte que `max_pending` segments en vol (4 par défaut) et
// refuse les suivants en « busy ». Vider toute la file d'un coup, comme avant,
// en faisait donc rejeter la majorité — et un segment refusé n'était PAS remis
// en file : seize segments sur vingt disparaissaient sans autre trace qu'un
// compteur d'erreurs que personne ne regarde.
//
// On n'en envoie que deux, et on repompe sur chaque réponse : le serveur
// répond exactement une fois par segment, c'est donc lui qui cadence.
const QUEUE_PUMP = 2;

function flushQueue() {
  if (!state.wsReady || !state.queue.length) return;
  const restant = state.queue.length;
  for (let i = 0; i < QUEUE_PUMP && state.queue.length; i++) {
    const item = state.queue.shift();
    // Ceinture et bretelles : la file ne doit contenir QUE des segments
    // complets. Le cas qui l'a rendue nécessaire — un `busy` sur un
    // `stream_start` remettant des MÉTADONNÉES de flux en file, sans `pcm` —
    // est désormais écarté à la source par un code d'erreur dédié
    // (`listening`). Mais `sendSegment` sur un objet sans PCM lèverait un
    // TypeError au milieu du gestionnaire, et perdre un épisode entier pour ça
    // serait absurde.
    if (!item || !item.pcm) continue;
    sendSegment(item);
  }
  if (!state.queue.length) log('file vidée : ' + restant + ' segment(s) envoyé(s)');
  updateCounters();
}

// ---------------------------------------------------------------- WebSocket

function connect() {
  if (!state.running) return;
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const url = proto + '//' + window.location.host + '/ws/audio';

  dot('pill-ws', 'busy', 'connexion');
  const ws = new WebSocket(url);
  // Le défaut est 'blob' : l'oublier rend tous les tests instanceof ArrayBuffer
  // silencieusement faux (G11).
  ws.binaryType = 'arraybuffer';
  state.ws = ws;

  ws.onopen = () => {
    state.wsAttempts = 0;
    const ua = navigator.userAgent;
    ws.send(JSON.stringify({
      type: 'hello',
      protocol_version: 1,
      client_id: CONFIG.clientId,
      app_version: '1.0.0',
      user_agent: ua,
      device_sample_rate: state.context ? state.context.sampleRate : null,
    }));
  };

  ws.onmessage = (event) => {
    let msg;
    try { msg = JSON.parse(event.data); } catch (err) { return; }
    handleServerMessage(msg);
  };

  ws.onclose = () => {
    state.wsReady = false;
    dot('pill-ws', 'error', 'fermé');
    // Un épisode en cours meurt avec le lien : le serveur finalise de son côté
    // ce qu'il a déjà reçu, et un flux partiel n'est jamais rejoué.
    if (state.streaming) endStream('connection_lost');
    scheduleReconnect();
  };

  ws.onerror = () => {
    // onclose suit toujours : inutile de journaliser deux fois.
  };
}

function scheduleReconnect() {
  if (!state.running || state.reconnectTimer) return;
  const base = Math.min(CONFIG.backoffBaseMs * Math.pow(2, state.wsAttempts), CONFIG.backoffMaxMs);
  // La gigue ne sert pas pour un client unique, mais pour la tempête de
  // reconnexions qui suit un redémarrage serveur.
  const jitter = base * CONFIG.backoffJitter * (Math.random() * 2 - 1);
  const delay = Math.max(500, Math.round(base + jitter));
  state.wsAttempts++;
  log('reconnexion dans ' + (delay / 1000).toFixed(1) + ' s', 'warn');
  state.reconnectTimer = setTimeout(() => {
    state.reconnectTimer = null;
    connect();
  }, delay);
}

function handleServerMessage(msg) {
  if (msg.type === 'hello_ack') {
    state.wsReady = true;
    dot('pill-ws', 'ok', 'connecté');
    const c = msg.classifier || {};
    log('serveur ' + msg.server_version + ' — seuil ' + c.threshold);
    // Les seuils du client se règlent SANS redéploiement : c'est ce qui évite
    // d'aller physiquement changer un curseur dans une boîte dehors.
    const patch = msg.config_patch || {};
    state.serverConfigPatch = patch;
    if (patch.cooldown_ms) CONFIG.cooldownMs = patch.cooldown_ms;
    const hasLocalRatio = lireReglage('triggerRatio') !== null;
    const hasLocalFloor = lireReglage('minRmsFloor') !== null;
    if (!hasLocalRatio && patch.trigger_ratio) {
      CONFIG.triggerRatio = patch.trigger_ratio;
      if ($('cfg-trigger-ratio')) $('cfg-trigger-ratio').value = patch.trigger_ratio;
      if ($('val-trigger-ratio')) $('val-trigger-ratio').textContent = patch.trigger_ratio.toFixed(1) + '×';
    }
    if (!hasLocalFloor && patch.min_rms_floor) {
      CONFIG.minRmsFloor = patch.min_rms_floor;
      if ($('cfg-min-floor')) $('cfg-min-floor').value = patch.min_rms_floor;
      if ($('val-min-floor')) $('val-min-floor').textContent = patch.min_rms_floor.toFixed(4);
    }
    // Le garde-tempête aussi : c'est lui qui décidait qu'une rafale de deux
    // minutes n'était pas enregistrable d'un seul tenant.
    if (patch.storm_max) CONFIG.stormMax = patch.storm_max;
    if (patch.storm_window_ms) CONFIG.stormWindowMs = patch.storm_window_ms;
    if (patch.storm_suspend_ms) CONFIG.stormSuspendMs = patch.storm_suspend_ms;
    // La longueur d'un épisode se règle depuis le serveur : c'est elle qui
    // décide de la place occupée sur le disque.
    if (patch.stream_silence_ms) CONFIG.streamSilenceMs = patch.stream_silence_ms;
    if (patch.stream_max_ms) CONFIG.streamMaxMs = patch.stream_max_ms;
    if (patch.stream_enabled === false) state.streamMode = 'legacy';
    if (patch.listen_enabled === false) CONFIG.listenEnabled = false;
    state.serverThreshold = c.threshold;
    $('ro-threshold').textContent = fmt(c.threshold, 2);
    flushQueue();
    startPing();
    return;
  }

  if (msg.type === 'stream_ack') {
    // Le serveur a ouvert le flux. Sans cet acquittement le client resterait
    // aveugle pendant tout l'épisode : il ne saurait ni si l'audio arrive, ni
    // à quelle durée maximale s'arrêter.
    if (msg.max_stream_ms) CONFIG.streamMaxMs = msg.max_stream_ms;
    dot('pill-capture', 'ok', 'épisode ' + state.streamChunks);
    return;
  }

  if (msg.type === 'stream_progress') {
    dot('pill-capture', 'ok', 'épisode ' + Math.round(msg.received_ms / 1000) + ' s');
    return;
  }

  if (msg.type === 'stream_stop') {
    // Le serveur clôt de son propre chef (silence mesuré chez lui, ou durée
    // maximale). Le client obéit et envoie son stream_end.
    endStream(msg.reason || 'server_stop');
    return;
  }

  if (msg.type === 'listen_request') {
    startListen(msg);
    return;
  }

  if (msg.type === 'listen_stop') {
    // Durée atteinte, ou l'opérateur a cliqué « arrêter ». Le client obéit et
    // envoie son listen_end.
    endListen(msg.reason || 'server_stop');
    return;
  }

  if (msg.type === 'segment_result') {
    delete state.inflight[msg.seq];
    const f = msg.accepted ? 'accepté' : 'refusé';
    if (msg.accepted) {
      state.counters.accepted++;
      $('player').src = msg.mp3_url;
      $('player-hint').textContent = 'Événement ' + msg.event_id + ' — ' + f;
    } else {
      state.counters.rejected++;
    }
    $('c-score').textContent = fmt(msg.noisy_score, 3) + ' / bark ' + fmt(msg.bark_score, 3);
    log('seq ' + msg.seq + ' ' + f + ' — score ' + fmt(msg.noisy_score, 3) +
        ', bark ' + fmt(msg.bark_score, 3) + ' (' + msg.reason + ')',
        msg.accepted ? 'ok' : null);
    updateCounters();
    // Une réponse = un segment terminé = une place libre. C'est ce qui cadence
    // la vidange de la file ; sans ça, elle resterait bloquée à deux segments.
    flushQueue();
    return;
  }

  if (msg.type === 'pong') return;

  if (msg.type === 'error') {
    state.counters.errors++;
    log('erreur ' + msg.code + ' : ' + msg.message, 'error');

    // « busy » n'est pas une panne : c'est le serveur qui dit « pas plus de N
    // segments en vol ». Le segment n'a jamais été classé, donc on le REMET en
    // file au lieu de le perdre. On ne relance PAS la vidange dans la foulée :
    // ce serait marteler un serveur déjà saturé — c'est la prochaine réponse
    // qui repompera.
    if (msg.code === 'busy' && msg.seq != null && state.inflight[msg.seq]) {
      if (state.queue.length < CONFIG.maxQueue) {
        state.queue.unshift(state.inflight[msg.seq]);
      }
      delete state.inflight[msg.seq];
    }

    // Le serveur ne connaît pas les flux — c'est un ROLLBACK, pas une panne.
    // On repasse au chemin hérité, sinon chaque déclenchement serait perdu en
    // silence jusqu'à ce que quelqu'un pense à vider le cache.
    if (msg.code === 'unknown_type' && state.streamMode === 'stream') {
      state.streamMode = 'legacy';
      log('serveur sans mode épisode — retour au clip de 3 s', 'warn');
      if (state.streaming) endStream('client_final');
    }

    // Un refus d'écoute porte le MOTIF DU POSTE, remonté tel quel par le
    // serveur. On l'affiche sans le traduire : c'est déjà une phrase lisible, et
    // la réécrire perdrait le détail qui permet d'agir.
    if (msg.code === 'listen_refused' || msg.code === 'listen_unknown') {
      dot('pill-live', 'warn', 'refusée');
      state.listening = false;
      state.listenId = null;
    }

    // Un flux refusé (disque plein, bornes) a été ouvert pour rien : on le
    // referme proprement côté client plutôt que de continuer à envoyer.
    if (state.streaming && msg.seq != null && msg.seq === state.streamSeq) {
      endStream('refusé : ' + msg.code);
    }

    if (msg.fatal) {
      // Le client s'ARRÊTE et affiche une erreur permanente plutôt que de
      // reboucler en reconnexion infinie.
      stop('erreur fatale : ' + msg.message);
      dot('pill-ws', 'error', 'incompatible');
    }
    updateCounters();
  }
}

function startPing() {
  stopPing();
  state.pingTimer = setInterval(() => {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
      state.ws.send(JSON.stringify({ type: 'ping', t: Date.now() }));
    }
  }, CONFIG.pingMs);
}

function stopPing() {
  if (state.pingTimer) clearInterval(state.pingTimer);
  state.pingTimer = null;
}

// ---------------------------------------------------------------- watchdog

// Si aucun message `rms` n'est arrivé depuis 2 s alors que le contexte est
// `running`, le worklet est mort sans le dire. On démonte et on relance.
function startWatchdog() {
  stopWatchdog();
  state.watchdogTimer = setInterval(() => {
    if (!state.running || !state.context) return;
    if (state.context.state !== 'running') return;
    if (Date.now() - state.lastRmsAt > CONFIG.watchdogMs) {
      log('aucun message du worklet depuis 2 s — relance du graphe', 'error');
      state.lastRmsAt = Date.now();
      restartAudio();
    }
  }, 1000);
}

function stopWatchdog() {
  if (state.watchdogTimer) clearInterval(state.watchdogTimer);
  state.watchdogTimer = null;
}

// ---------------------------------------------------------------- interface

function updateCounters() {
  $('c-sent').textContent = state.counters.sent;
  $('c-accepted').textContent = state.counters.accepted;
  $('c-rejected').textContent = state.counters.rejected;
  $('c-errors').textContent = state.counters.errors;
  $('c-queue').textContent = state.queue.length;
}

function updateReadout() {
  const bg = state.bgMedian;
  const rms = state.lastRms;
  const ratio = bg > 0 ? rms / bg : 0;

  $('ro-rms').textContent = fmt(rms);
  $('ro-bg').textContent = fmt(bg);
  $('ro-ratio').textContent = bg > 0 ? fmt(ratio, 2) + '×' : '—';
  $('ro-peak').textContent = fmt(state.lastPeak);
  $('ro-frames').textContent = Math.floor(state.frameCount);

  // La barre est graduée sur le seuil RÉEL, pas sur une échelle arbitraire :
  // elle devient rouge quand la règle de déclenchement est satisfaite.
  const seuil = Math.max(bg * CONFIG.triggerRatio, CONFIG.minRmsFloor);
  const pct = seuil > 0 ? Math.min(100, (rms / (seuil * 2)) * 100) : 0;
  const meter = $('meter');
  meter.style.width = pct.toFixed(1) + '%';
  meter.dataset.hot = rms > seuil ? 'oui' : 'non';

  const effEl = $('val-effective-threshold');
  if (effEl) effEl.textContent = fmt(seuil);
}

function formatMicGain(g) {
  if (g <= 1.0) return '1.0×';
  const db = Math.round(20 * Math.log10(g));
  return g.toFixed(1) + '× (+' + db + ' dB)';
}

// Les clés de réglage ont suivi le renommage du produit (`noisygram.*`). On
// relit les anciennes (`aboigramme.*`) en REPLI : sans ça, un poste de terrain
// qui a déjà sa calibration — gain micro, déclencheur, plancher RMS — la
// perdrait au premier rechargement, et il faudrait retourner régler la boîte.
// Le repli est en lecture seule : l'écriture suivante migre la clé d'elle-même.
function lireReglage(cle) {
  try {
    const v = window.localStorage.getItem('noisygram.' + cle);
    if (v !== null) return v;
    return window.localStorage.getItem('aboigramme.' + cle);
  } catch (_) {
    return null;
  }
}

function initThresholdSettings() {
  const savedGain = lireReglage('micGain');
  const savedRatio = lireReglage('triggerRatio');
  const savedFloor = lireReglage('minRmsFloor');
  if (savedGain !== null) {
    const val = parseFloat(savedGain);
    if (!Number.isNaN(val) && val >= 1.0 && val <= 10.0) CONFIG.micGain = val;
  }
  if (savedRatio !== null) {
    const val = parseFloat(savedRatio);
    if (!Number.isNaN(val) && val >= 1.0 && val <= 10.0) CONFIG.triggerRatio = val;
  }
  if (savedFloor !== null) {
    const val = parseFloat(savedFloor);
    if (!Number.isNaN(val) && val >= 0.0001 && val <= 0.1) CONFIG.minRmsFloor = val;
  }

  const sliderGain = $('cfg-mic-gain');
  const valGain = $('val-mic-gain');
  const sliderRatio = $('cfg-trigger-ratio');
  const valRatio = $('val-trigger-ratio');
  const sliderFloor = $('cfg-min-floor');
  const valFloor = $('val-min-floor');
  const btnReset = $('cfg-reset');

  if (sliderGain && valGain) {
    sliderGain.value = CONFIG.micGain;
    valGain.textContent = formatMicGain(CONFIG.micGain);
    sliderGain.addEventListener('input', (e) => {
      const v = parseFloat(e.target.value);
      CONFIG.micGain = v;
      valGain.textContent = formatMicGain(v);
      if (state.micGainNode && state.context) {
        try {
          state.micGainNode.gain.setTargetAtTime(v, state.context.currentTime, 0.01);
        } catch (_) {
          state.micGainNode.gain.value = v;
        }
      }
      try { window.localStorage.setItem('noisygram.micGain', String(v)); } catch (_) {}
    });
  }

  if (sliderRatio && valRatio) {
    sliderRatio.value = CONFIG.triggerRatio;
    valRatio.textContent = CONFIG.triggerRatio.toFixed(1) + '×';
    sliderRatio.addEventListener('input', (e) => {
      const v = parseFloat(e.target.value);
      CONFIG.triggerRatio = v;
      valRatio.textContent = v.toFixed(1) + '×';
      try { window.localStorage.setItem('noisygram.triggerRatio', String(v)); } catch (_) {}
      updateReadout();
    });
  }

  if (sliderFloor && valFloor) {
    sliderFloor.value = CONFIG.minRmsFloor;
    valFloor.textContent = CONFIG.minRmsFloor.toFixed(4);
    sliderFloor.addEventListener('input', (e) => {
      const v = parseFloat(e.target.value);
      CONFIG.minRmsFloor = v;
      valFloor.textContent = v.toFixed(4);
      try { window.localStorage.setItem('noisygram.minRmsFloor', String(v)); } catch (_) {}
      updateReadout();
    });
  }

  if (btnReset) {
    btnReset.addEventListener('click', () => {
      try {
        window.localStorage.removeItem('noisygram.micGain');
        window.localStorage.removeItem('noisygram.triggerRatio');
        window.localStorage.removeItem('noisygram.minRmsFloor');
        // Les clés d'avant le renommage aussi, sinon le repli de `lireReglage`
        // ressusciterait un réglage qu'on vient de remettre à zéro.
        window.localStorage.removeItem('aboigramme.micGain');
        window.localStorage.removeItem('aboigramme.triggerRatio');
        window.localStorage.removeItem('aboigramme.minRmsFloor');
      } catch (_) {}
      CONFIG.micGain = 1.0;
      if (state.micGainNode && state.context) {
        try {
          state.micGainNode.gain.setTargetAtTime(1.0, state.context.currentTime, 0.01);
        } catch (_) {
          state.micGainNode.gain.value = 1.0;
        }
      }
      if (sliderGain && valGain) {
        sliderGain.value = 1.0;
        valGain.textContent = formatMicGain(1.0);
      }
      CONFIG.triggerRatio = (state.serverConfigPatch && state.serverConfigPatch.trigger_ratio) || 2.5;
      CONFIG.minRmsFloor = (state.serverConfigPatch && state.serverConfigPatch.min_rms_floor) || 0.004;
      if (sliderRatio && valRatio) {
        sliderRatio.value = CONFIG.triggerRatio;
        valRatio.textContent = CONFIG.triggerRatio.toFixed(1) + '×';
      }
      if (sliderFloor && valFloor) {
        sliderFloor.value = CONFIG.minRmsFloor;
        valFloor.textContent = CONFIG.minRmsFloor.toFixed(4);
      }
      log('seuil et gain : réinitialisés par défaut');
      updateReadout();
    });
  }
}

// ---------------------------------------------------------------- pilotage

async function start() {
  $('toggle').disabled = true;
  state.running = true;
  try {
    await startAudio();
  } catch (err) {
    state.running = false;
    log('démarrage impossible : ' + err.message, 'error');
    dot('pill-mic', 'error', 'refusé');
    $('toggle').disabled = false;
    return;
  }
  connect();
  startWatchdog();
  $('toggle').textContent = 'Arrêter';
  $('toggle').disabled = false;
}

async function stop(raison) {
  if (state.streaming) endStream('client_final');
  if (state.listening) endListen('arret_demande');
  state.running = false;
  stopWatchdog();
  stopPing();
  if (state.reconnectTimer) { clearTimeout(state.reconnectTimer); state.reconnectTimer = null; }
  if (state.ws) {
    try { state.ws.close(1000, 'arrêt demandé'); } catch (err) {}
    state.ws = null;
  }
  state.wsReady = false;
  await stopAudio();
  dot('pill-ws', 'idle', 'arrêté');
  $('toggle').textContent = 'Démarrer';
  log(raison ? 'arrêt — ' + raison : 'arrêt demandé');
}

// ---------------------------------------------------------------- amorçage

function estSombre() {
  const actuel = document.documentElement.dataset.theme;
  return actuel
    ? actuel === 'dark'
    : window.matchMedia('(prefers-color-scheme: dark)').matches;
}

function majBoutonTheme() {
  const btn = document.getElementById('theme');
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
  majBoutonTheme();
}

function initTheme() {
  const urlTheme = new URLSearchParams(window.location.search).get('theme');
  const enregistre = urlTheme || (() => {
    try { return localStorage.getItem('noisygram.theme') || localStorage.getItem('aboigramme.theme'); } catch (e) { return null; }
  })();
  if (enregistre) {
    appliqueTheme(enregistre);
  } else {
    majBoutonTheme();
  }

  const btnTheme = document.getElementById('theme');
  if (btnTheme) {
    btnTheme.addEventListener('click', () => {
      appliqueTheme(estSombre() ? 'light' : 'dark');
    });
  }

  window.addEventListener('storage', (e) => {
    if (e.key === 'noisygram.theme' || e.key === 'aboigramme.theme') {
      appliqueTheme(e.newValue || null);
    }
  });

  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    if (!document.documentElement.dataset.theme) majBoutonTheme();
  });
}

(function init() {
  initTheme();
  initThresholdSettings();
  updateCounters();
  updateReadout();

  if (!preflight()) return;
  $('toggle').disabled = false;

  $('toggle').addEventListener('click', () => {
    if (state.running) stop();
    else start();
  });

  setInterval(updateReadout, CONFIG.uiRefreshMs);

  // Un poste extérieur qu'on rouvre après une coupure doit repartir seul.
  // Sans ça, la boîte reste muette jusqu'à ce que quelqu'un aille cliquer.
  log('prêt — cliquez sur Démarrer');
})();

window.addEventListener('beforeunload', () => {
  // Fermer la page en plein épisode doit le dire au serveur : sinon il attend
  // la suite jusqu'à son délai d'inactivité, en gardant un fichier temporaire
  // ouvert. C'est un envoi au mieux — un onglet qui se ferme n'attend personne.
  if (state.streaming) {
    endStream('unload');
  }
  // Même raison pour l'écoute : sans ce `listen_end`, le serveur laisserait
  // courir son délai d'inactivité avant de publier le WAV.
  if (state.listening) {
    endListen('unload');
  }
  if (state.ws) { try { state.ws.close(); } catch (err) {} }
});
