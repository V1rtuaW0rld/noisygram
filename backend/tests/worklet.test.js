/**
 * Harnais de test du worklet, hors navigateur.
 *
 * On stubbe AudioWorkletProcessor / registerProcessor / sampleRate, et on
 * utilise structuredClone avec transfert : les tampons sont RÉELLEMENT
 * détachés, exactement comme dans le navigateur. C'est ce qui permet de
 * vérifier qu'on ne réécrit pas dans un tampon déjà transféré — un bug
 * silencieux qui ne produit aucune erreur, juste des captures vides.
 */
'use strict';

const fs = require('fs');
const vm = require('vm');

const SR = 48000;
const QUANTUM = 128;
const PRE_MS = 1000;
const POST_MS = 2000;
const PRE = (SR * PRE_MS) / 1000;   // 48000
const POST = (SR * POST_MS) / 1000; // 96000

const WORKLET = require('path').join(__dirname, '..', 'static', 'client', 'recorder-worklet.js');
const SOURCE = fs.readFileSync(WORKLET, 'utf8');

/**
 * Charge le worklet dans un bac à sable à une fréquence donnée.
 *
 * Une fréquence par bac, et non une globale : `sampleRate` est un global du
 * contexte d'AudioWorkletGlobalScope, que le processeur lit pour convertir les
 * millisecondes en échantillons. Le tester à 48 kHz seulement laisserait tout
 * le chemin 16 kHz — celui qui sert réellement — non couvert.
 */
function charge(sampleRate) {
  const boite = { messages: [], ProcessorClass: null };
  const sandbox = {
    sampleRate,
    console,
    Float32Array,
    Int16Array,
    structuredClone,
    registerProcessor: (name, cls) => {
      boite.ProcessorClass = cls;
    },
    AudioWorkletProcessor: class {
      constructor() {
        this.port = {
          onmessage: null,
          postMessage(msg, transfer) {
            boite.messages.push(
              transfer && transfer.length
                ? structuredClone(msg, { transfer })
                : structuredClone(msg)
            );
          },
        };
      }
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(SOURCE, sandbox);
  return boite;
}

const _h = charge(SR);
const ProcessorClass = _h.ProcessorClass;
const messages = _h.messages;

let failures = 0;
let checks = 0;
function check(label, ok, detail) {
  checks++;
  console.log(`[${ok ? '  ok  ' : ' ÉCHEC'}] ${label}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures++;
}

// Signal déterministe et indexable : chaque échantillon encode sa position
// globale modulo 997, ce qui permet de vérifier l'ALIGNEMENT du pré-roll, pas
// seulement sa longueur.
const sig = (i) => Math.fround(((i % 997) / 997) * 0.5);

function makeProc() {
  const proc = new ProcessorClass({
    processorOptions: { preRollMs: PRE_MS, postRollMs: POST_MS },
  });
  proc.port.onmessage({ data: { type: 'configure', preRollMs: PRE_MS, postRollMs: POST_MS } });
  return proc;
}

function feed(proc, state, numSamples) {
  let reste = numSamples;
  while (reste > 0) {
    const n = Math.min(QUANTUM, reste);
    const buf = new Float32Array(QUANTUM);
    for (let i = 0; i < n; i++) {
      buf[i] = sig(state.g);
      state.g++;
    }
    proc.process([[buf]]);
    reste -= n;
  }
}

console.log('='.repeat(72));
console.log('  worklet — pré-roll, post-roll, double tampon');
console.log('='.repeat(72));

// --- capture 1 -------------------------------------------------------------
const proc = makeProc();
const state = { g: 0 };

check('configured annoncé', messages.some((m) => m.type === 'configured'));

feed(proc, state, 3 * SR); // 3 s d'historique
const triggerGlobal = state.g;

proc.port.onmessage({ data: { type: 'trigger' } });
check('aucune capture avant la fin du post-roll',
  !messages.some((m) => m.type === 'captured'));

feed(proc, state, POST); // les 2 s de post-roll

const cap1 = messages.find((m) => m.type === 'captured');
check('capture émise', !!cap1);
check(`longueur = pré + post (${PRE + POST} échantillons)`,
  cap1 && cap1.numSamples === PRE + POST, cap1 ? String(cap1.numSamples) : '—');
check('tampon transféré en Float32Array', cap1 && cap1.pcm instanceof Float32Array);

if (cap1) {
  // Le pré-roll doit être EXACTEMENT la seconde qui précède le déclenchement.
  const debutAttendu = triggerGlobal - PRE;
  let mauvais = -1;
  for (let k = 0; k < PRE + POST; k++) {
    const attendu = sig(debutAttendu + k);
    if (Math.abs(cap1.pcm[k] - attendu) > 1e-7) { mauvais = k; break; }
  }
  check('pré-roll aligné sur la seconde précédente', mauvais === -1,
    mauvais === -1 ? `${PRE} échantillons conformes` : `désaligné à k=${mauvais}`);
  check('post-roll présent et à sa place',
    Math.abs(cap1.pcm[PRE] - sig(triggerGlobal)) < 1e-7);
}

// --- capture 2 : le tampon permuté était-il bien réalloué ? ----------------
messages.length = 0;
feed(proc, state, SR);
const trigger2 = state.g;
proc.port.onmessage({ data: { type: 'trigger' } });
feed(proc, state, POST);

const cap2 = messages.find((m) => m.type === 'captured');
check('seconde capture émise', !!cap2);
if (cap2) {
  // C'est LE test du bug de détachement : sans réallocation, ce tampon est
  // détaché (byteLength 0) et toutes les valeurs valent 0.
  const nonNulles = Array.from(cap2.pcm.slice(0, 1000)).filter((v) => v !== 0).length;
  check('seconde capture NON VIDE (double tampon)', nonNulles > 900,
    `${nonNulles}/1000 valeurs non nulles`);
  const debut2 = trigger2 - PRE;
  let ok2 = true;
  for (let k = 0; k < PRE + POST; k++) {
    if (Math.abs(cap2.pcm[k] - sig(debut2 + k)) > 1e-7) { ok2 = false; break; }
  }
  check('seconde capture correctement alignée', ok2);
}

// --- trames RMS ------------------------------------------------------------
const rmsCount = messages.filter((m) => m.type === 'rms').length +
  (cap1 ? 1 : 0); // les messages ont été vidés entre-temps
check('des trames RMS sont produites', rmsCount > 0, `${rmsCount} après vidage`);

// --- stéréo : downmix (L+R)/2 ---------------------------------------------
const proc2 = makeProc();
proc2.port.onmessage({ data: { type: 'configure', preRollMs: PRE_MS, postRollMs: POST_MS } });
const L = new Float32Array(QUANTUM).fill(0.5);
const R = new Float32Array(QUANTUM).fill(-0.5);
for (let i = 0; i < 2000; i++) proc2.process([[L, R]]);
proc2.port.onmessage({ data: { type: 'trigger' } });
for (let i = 0; i < 1000; i++) proc2.process([[L, R]]);
const capStereo = messages.filter((m) => m.type === 'captured').pop();
check('stéréo opposée → silence (downmix (L+R)/2)',
  capStereo && Math.abs(capStereo.pcm[0]) < 1e-7 && Math.abs(capStereo.pcm[500]) < 1e-7,
  capStereo ? `valeur ${capStereo.pcm[0]}` : '—');

// --- entrée absente --------------------------------------------------------
const proc3 = makeProc();
let survecu = true;
try {
  for (let i = 0; i < 50; i++) proc3.process([[]]);
  for (let i = 0; i < 50; i++) proc3.process([]);
} catch (err) {
  survecu = false;
}
check('inputs vide géré sans exception', survecu);
check('process() retourne toujours true', proc3.process([[]]) === true);

// --- transmission brute ----------------------------------------------------
// Le worklet transfère les FLOTTANTS tels quels ; l'écrêtage et la conversion
// en int16 sont faits par le thread principal (app.js), pour ne pas mettre une
// boucle de 144 000 itérations dans un callback audio de 2,67 ms. Ce test
// constate la répartition : si un jour quelqu'un remet l'écrêtage ici, il
// échouera et devra se demander pourquoi.
const proc4 = makeProc();
proc4.port.onmessage({ data: { type: 'configure', preRollMs: PRE_MS, postRollMs: POST_MS } });
const fort = new Float32Array(QUANTUM).fill(0.75);
for (let i = 0; i < 2000; i++) proc4.process([[fort]]);
proc4.port.onmessage({ data: { type: 'trigger' } });
for (let i = 0; i < 1000; i++) proc4.process([[fort]]);
const capFort = messages.filter((m) => m.type === 'captured').pop();
check('signal transmis sans altération (l\'écrêtage est côté app.js)',
  capFort && Math.abs(capFort.pcm[0] - 0.75) < 1e-7 && Math.abs(capFort.pcm[1000] - 0.75) < 1e-7,
  capFort ? `valeur ${capFort.pcm[0]}` : '—');

// --------------------------------------------------------- capture 16 kHz
// Le chemin réellement servi depuis que le client n'envoie plus du 48 kHz.
console.log('\n■ Capture à 16 kHz');
{
  const H = charge(16000);
  const proc16 = new H.ProcessorClass({
    processorOptions: { preRollMs: PRE_MS, postRollMs: POST_MS, frameSize: 320 },
  });
  proc16.port.onmessage({
    data: { type: 'configure', preRollMs: PRE_MS, postRollMs: POST_MS },
  });

  const cfg = H.messages.find((m) => m.type === 'configured');
  check('configured annonce la trame réelle', cfg && cfg.frameSize === 320,
    cfg ? String(cfg.frameSize) : '—');
  check('pré-roll 16 000 et post-roll 32 000 échantillons',
    cfg && cfg.preRollSamples === 16000 && cfg.postRollSamples === 32000,
    cfg ? `${cfg.preRollSamples}/${cfg.postRollSamples}` : '—');

  // 320 échantillons à 16 kHz = 20 ms, soit exactement 50 trames par seconde.
  H.messages.length = 0;
  const st16 = { g: 0 };
  feed(proc16, st16, 16000);
  const rms16 = H.messages.filter((m) => m.type === 'rms').length;
  check('50 trames RMS par seconde', Math.abs(rms16 - 50) <= 1, String(rms16));

  // L'alignement du pré-roll doit tenir à la nouvelle fréquence comme à l'autre.
  const trig16 = st16.g;
  proc16.port.onmessage({ data: { type: 'trigger' } });
  feed(proc16, st16, 32000);
  const cap16 = H.messages.find((m) => m.type === 'captured');
  check('capture 16 kHz : 48 000 échantillons', cap16 && cap16.numSamples === 48000,
    cap16 ? String(cap16.numSamples) : '—');
  if (cap16) {
    const debut = trig16 - 16000;
    let mauvais = -1;
    for (let k = 0; k < 48000; k++) {
      if (Math.abs(cap16.pcm[k] - sig(debut + k)) > 1e-7) { mauvais = k; break; }
    }
    check('pré-roll aligné (16 kHz)', mauvais === -1,
      mauvais === -1 ? '48 000 échantillons conformes' : `désaligné à k=${mauvais}`);
  }
}

// ------------------------------------------- trame absente ou invalide
// app.js et le worklet sont DEUX fichiers, mis en cache séparément par le CDN :
// ils peuvent ne pas être de la même version. Le worklet doit donc se replier,
// et surtout CONTINUER À ÉMETTRE — sans quoi le watchdog relance le graphe
// toutes les 2 s, indéfiniment, sans une seule erreur au journal.
console.log('\n■ Trame absente ou invalide');
{
  // Sans frameSize : repli à 20 ms, soit 960 échantillons à 48 kHz — et non la
  // valeur historique 1024, qui n'aurait aucun sens à une autre fréquence.
  const H = charge(48000);
  const p = new H.ProcessorClass({
    processorOptions: { preRollMs: PRE_MS, postRollMs: POST_MS },
  });
  p.port.onmessage({ data: { type: 'configure', preRollMs: PRE_MS, postRollMs: POST_MS } });
  const cfg = H.messages.find((m) => m.type === 'configured');
  check('sans frameSize : repli sur 20 ms (960 à 48 kHz)',
    cfg && cfg.frameSize === 960, cfg ? String(cfg.frameSize) : '—');
}

for (const mauvais of [0, NaN, -1, 1e9, 'x']) {
  const H = charge(16000);
  const p = new H.ProcessorClass({
    processorOptions: { frameSize: mauvais, preRollMs: PRE_MS, postRollMs: POST_MS },
  });
  H.messages.length = 0;
  const st = { g: 0 };
  feed(p, st, 16000);
  const n = H.messages.filter((m) => m.type === 'rms').length;
  check(`frameSize ${String(mauvais)} → repli, flux maintenu`, n > 40, `${n} trames/s`);
}

// ------------------------------------------------------------- flux
// Le test qui compte : entre deux morceaux, aucun échantillon ne doit manquer
// ni être répété. C'est exactement le défaut qu'on remplace — les clips
// déclenchés indépendamment perdaient 9 à 52 ms de son à chaque couture.
console.log('\n■ Flux d\'épisode');
{
  const CHUNK = 16000;
  const H = charge(16000);
  const proc = new H.ProcessorClass({
    processorOptions: { preRollMs: PRE_MS, postRollMs: POST_MS, frameSize: 320, chunkSize: CHUNK },
  });
  proc.port.onmessage({ data: { type: 'configure', preRollMs: PRE_MS, postRollMs: POST_MS } });

  const cfg = H.messages.filter((m) => m.type === 'configured').pop();
  check('configured annonce la taille de morceau', cfg && cfg.chunkSize === CHUNK,
    cfg ? String(cfg.chunkSize) : '—');
  check('configured annonce 60 s de ring', cfg && cfg.ringSeconds === 60,
    cfg ? String(cfg.ringSeconds) : '—');

  // 3 s d'historique, dont 1 s deviendra le pré-roll.
  const st = { g: 0 };
  feed(proc, st, 3 * 16000);
  const declenche = st.g;

  H.messages.length = 0;
  proc.port.onmessage({ data: { type: 'stream_begin' } });

  const premier = H.messages.find((m) => m.type === 'stream_chunk' && m.first);
  check('le pré-roll est le premier morceau', !!premier && premier.numSamples === 16000,
    premier ? String(premier.numSamples) : '—');
  if (premier) {
    const debut = declenche - 16000;
    let mauvais = -1;
    for (let k = 0; k < premier.numSamples; k++) {
      if (Math.abs(premier.pcm[k] - sig(debut + k)) > 1e-7) { mauvais = k; break; }
    }
    check('pré-roll aligné sur la seconde qui précède', mauvais === -1,
      mauvais === -1 ? '16 000 échantillons conformes' : `désaligné à k=${mauvais}`);
  }

  // Deux morceaux vivants. Le premier doit démarrer EXACTEMENT à l'échantillon
  // qui suit le dernier du pré-roll.
  H.messages.length = 0;
  feed(proc, st, 2 * CHUNK);
  const morceaux = H.messages.filter((m) => m.type === 'stream_chunk');
  check('deux morceaux vivants émis', morceaux.length === 2,
    `${morceaux.length} morceau(x)`);

  let premierManquant = -1;
  morceaux.forEach((m, n) => {
    if (premierManquant !== -1) return;
    for (let k = 0; k < m.numSamples; k++) {
      if (Math.abs(m.pcm[k] - sig(declenche + n * CHUNK + k)) > 1e-7) {
        premierManquant = n * CHUNK + k;
        break;
      }
    }
  });
  check('aucun trou ni recouvrement entre morceaux', premierManquant === -1,
    premierManquant === -1
      ? '32 000 échantillons continus'
      : `rupture à l'échantillon ${premierManquant}`);

  // Le compte, en plus de la continuité : un morceau qui n'atteint jamais sa
  // taille nominale passerait la boucle ci-dessus sans la déclencher.
  const totalVivant = morceaux.reduce((a, m) => a + m.numSamples, 0);
  check('autant d\'échantillons émis que captés', totalVivant === 2 * CHUNK,
    `${totalVivant} sur ${2 * CHUNK}`);

  // Le watchdog du thread principal démonte le graphe après 2 s sans `rms` :
  // si le flux les supprimait, le flux serait détruit toutes les 2 s, en
  // boucle, sans une ligne d'erreur.
  const rmsPendant = H.messages.filter((m) => m.type === 'rms').length;
  check('les trames rms continuent pendant le flux', rmsPendant >= 90, String(rmsPendant));

  H.messages.length = 0;
  proc.port.onmessage({ data: { type: 'stream_stop' } });
  const fin = H.messages.find((m) => m.type === 'stream_stopped');
  check('stream_stop rend le bilan du flux', fin && fin.chunks === 3 && fin.numSamples === 48000,
    fin ? `${fin.chunks} morceaux, ${fin.numSamples} échantillons` : '—');

  feed(proc, st, 16000);
  check('plus aucun morceau après stream_stop',
    H.messages.filter((m) => m.type === 'stream_chunk').length === 0);
}

// ------------------------------------------------------- écoute à la demande
// Le mode direct. Ce qui compte ici n'est pas la continuité — il n'y a qu'un
// consommateur — mais l'EXCLUSIVITÉ : le micro n'a qu'un seul lecteur, et deux
// modes qui écriraient en parallèle produiraient deux captures entrelacées du
// même son, sans la moindre erreur pour le signaler.
console.log('\n■ Écoute à la demande');
{
  const CHUNK = 3200; // 200 ms à 16 kHz : la taille qui donne ~250 ms de latence
  const H = charge(16000);
  const proc = new H.ProcessorClass({
    processorOptions: { preRollMs: PRE_MS, postRollMs: POST_MS, frameSize: 320 },
  });
  proc.port.onmessage({ data: { type: 'configure', preRollMs: PRE_MS, postRollMs: POST_MS } });

  const cfg = H.messages.filter((m) => m.type === 'configured').pop();
  check('configured annonce le mode écoute', cfg && cfg.listening === true,
    cfg ? String(cfg.listening) : '—');

  const st = { g: 0 };

  // Rien tant que l'ordre n'est pas donné : le poste ne streame pas en
  // permanence, c'est tout l'intérêt du pré-tri côté client.
  feed(proc, st, 16000);
  check('aucun morceau d\'écoute avant listen_begin',
    H.messages.filter((m) => m.type === 'listen_chunk').length === 0);

  H.messages.length = 0;
  proc.port.onmessage({ data: { type: 'listen_begin', chunkSamples: CHUNK } });
  const debut = st.g;

  const ack = H.messages.find((m) => m.type === 'listen_started');
  check('listen_begin est acquitté', !!ack && ack.chunkSize === CHUNK,
    ack ? String(ack.chunkSize) : '—');

  // Aucun pré-roll : les morceaux commencent à l'échantillon qui SUIT l'ordre.
  feed(proc, st, 3 * CHUNK);
  const morceaux = H.messages.filter((m) => m.type === 'listen_chunk');
  check('trois morceaux pleins émis', morceaux.length === 3, `${morceaux.length}`);

  let desaligne = -1;
  morceaux.forEach((m, n) => {
    if (desaligne !== -1) return;
    for (let k = 0; k < m.numSamples; k++) {
      if (Math.abs(m.pcm[k] - sig(debut + n * CHUNK + k)) > 1e-7) {
        desaligne = n * CHUNK + k;
        break;
      }
    }
  });
  check('morceaux continus, sans pré-roll', desaligne === -1,
    desaligne === -1 ? '9 600 échantillons alignés' : `rupture à ${desaligne}`);

  const total = morceaux.reduce((a, m) => a + m.numSamples, 0);
  check('autant d\'échantillons émis que captés', total === 3 * CHUNK,
    `${total} sur ${3 * CHUNK}`);

  // Le watchdog de la page démonte le graphe après 2 s sans `rms`. Si l'écoute
  // les supprimait, elle serait détruite toutes les 2 s, en boucle.
  // 9 600 échantillons à 320 par trame font exactement 30 messages.
  const rmsPendant = H.messages.filter((m) => m.type === 'rms').length;
  check('les trames rms continuent pendant l\'écoute', rmsPendant === 30, String(rmsPendant));

  // ── EXCLUSIVITÉ. Les trois assertions qui suivent sont le cœur du test.
  H.messages.length = 0;
  proc.port.onmessage({ data: { type: 'trigger' } });
  proc.port.onmessage({ data: { type: 'stream_begin' } });
  feed(proc, st, 2 * CHUNK);
  check('trigger ignoré pendant une écoute',
    H.messages.filter((m) => m.type === 'captured').length === 0);
  check('stream_begin ignoré pendant une écoute',
    H.messages.filter((m) => m.type === 'stream_chunk').length === 0);
  check('l\'écoute continue malgré les ordres parasites',
    H.messages.filter((m) => m.type === 'listen_chunk').length === 2,
    String(H.messages.filter((m) => m.type === 'listen_chunk').length));

  // Le morceau PARTIEL doit sortir : les dernières millisecondes sont du son
  // réel, les jeter tronquerait la fin du fichier sans rien pour le dire.
  //
  // `feed` travaille par quanta de 128 : un nombre non multiple de 128 est
  // arrondi AU-DESSUS (le dernier quantum est traité en entier). On prend donc
  // un multiple exact, sinon on testerait le harnais plutôt que le worklet.
  const PARTIEL = 12 * 128; // 1536
  H.messages.length = 0;
  feed(proc, st, PARTIEL);
  proc.port.onmessage({ data: { type: 'listen_stop' } });
  const partiel = H.messages.filter((m) => m.type === 'listen_chunk');
  check('listen_stop émet le morceau partiel',
    partiel.length === 1 && partiel[0].numSamples === PARTIEL,
    partiel.length ? String(partiel[0].numSamples) : 'aucun');

  const bilan = H.messages.find((m) => m.type === 'listen_stopped');
  // 3 morceaux du début + 2 morceaux « parasites » + ce demi-morceau.
  check('listen_stopped rend le bilan',
    bilan && bilan.chunks === 6 && bilan.numSamples === 3 * CHUNK + 2 * CHUNK + PARTIEL,
    bilan ? `${bilan.chunks} morceaux, ${bilan.numSamples} échantillons` : '—');

  H.messages.length = 0;
  feed(proc, st, 16000);
  check('plus aucun morceau après listen_stop',
    H.messages.filter((m) => m.type === 'listen_chunk').length === 0);
}

// Une taille de morceau invalide ne doit pas tuer l'écoute : on retombe sur un
// cinquième de seconde, comme `frameSize` retombe sur la trame dérivée du taux.
{
  const H = charge(16000);
  const proc = new H.ProcessorClass({ processorOptions: { preRollMs: PRE_MS, postRollMs: POST_MS } });
  proc.port.onmessage({ data: { type: 'configure', preRollMs: PRE_MS, postRollMs: POST_MS } });
  proc.port.onmessage({ data: { type: 'listen_begin', chunkSamples: 'x' } });
  const ack = H.messages.filter((m) => m.type === 'listen_started').pop();
  check('chunkSamples invalide → repli à 200 ms', ack && ack.chunkSize === 3200,
    ack ? String(ack.chunkSize) : '—');
}

// Un épisode ouvert refuse l'écoute : le serveur absorbe toute trame binaire
// dans l'épisode, donc les deux flux se mélangeraient à la même fréquence —
// un enregistrement faux qui a l'air juste.
{
  const H = charge(16000);
  const proc = new H.ProcessorClass({ processorOptions: { preRollMs: PRE_MS, postRollMs: POST_MS } });
  proc.port.onmessage({ data: { type: 'configure', preRollMs: PRE_MS, postRollMs: POST_MS } });
  proc.port.onmessage({ data: { type: 'stream_begin' } });
  H.messages.length = 0;
  proc.port.onmessage({ data: { type: 'listen_begin', chunkSamples: 3200 } });
  check('listen_begin ignoré pendant un épisode',
    H.messages.filter((m) => m.type === 'listen_started').length === 0);

  const st = { g: 0 };
  // Un morceau d'épisode fait 1 s (16 000 échantillons, aucune option
  // `chunkSize` ici) : il faut donc en nourrir autant pour en voir sortir un.
  feed(proc, st, 16000);
  check('et aucun morceau d\'écoute n\'est émis',
    H.messages.filter((m) => m.type === 'listen_chunk').length === 0);
  check('l\'épisode, lui, continue',
    H.messages.filter((m) => m.type === 'stream_chunk').length === 1,
    String(H.messages.filter((m) => m.type === 'stream_chunk').length));
}

// Un pré-roll plus long que le ring ne peut pas être restitué. Le chemin hérité
// complète par des zéros devant, ce qui décalerait l'instant du premier
// échantillon et toute la grille de fenêtres du serveur : on refuse.
{
  const H = charge(16000);
  let rejete = false;
  let message = '';
  try {
    new H.ProcessorClass({ processorOptions: { preRollMs: 120000, postRollMs: 2000 } });
  } catch (err) {
    rejete = true;
    message = err.message;
  }
  check('un pré-roll plus long que le ring est refusé', rejete, message);
}

console.log('\n' + '='.repeat(72));
if (failures) {
  console.log(`  ${failures} ÉCHEC(S) sur ${checks} vérifications`);
  process.exit(1);
}
console.log(`  ${checks} vérifications, toutes OK`);
