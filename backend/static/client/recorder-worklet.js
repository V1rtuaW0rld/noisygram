/**
 * Aboigramme — worklet d'enregistrement.
 *
 * Responsabilités, et rien d'autre :
 *   1. downmixer en mono          — (L+R)/2, JAMAIS input[0][0] en aveugle
 *   2. tenir un ring buffer de 60 s
 *   3. poster un {rms, peak} par trame (la taille est choisie par app.js
 *      selon le taux du contexte, et annoncée dans « configured »)
 *   4. sur ordre du thread principal, soit geler le ring et collecter un
 *      pré+post-roll (chemin HÉRITÉ), soit émettre un flux continu de morceaux
 *      (chemin ÉPISODE)
 *
 * Le worklet POSSÈDE le buffer (design (a) du §9.5) : au déclenchement on
 * recopie le pré-roll hors du ring, puis on remplit la suite dans le buffer de
 * sortie. Aucune arithmétique de wrap-around, et impossible de relire des
 * échantillons que le curseur d'écriture a déjà écrasés.
 *
 * DEUX CHEMINS COEXISTENT, et ce n'est pas un luxe : les .js sont servis par un
 * CDN qui les garde quatre heures, donc un client d'avant peut encore tourner
 * pendant tout ce temps. Le chemin hérité reste donc intact.
 *
 * Pourquoi un FLUX plutôt que des clips recollés : deux clips déclenchés
 * indépendamment ne se touchent pas. Mesuré sur le terrain, il manque 9 à 52 ms
 * de son à chaque couture — le déclencheur a une gigue d'une trame et chaque
 * clip couvre exactement sa durée nominale. Quarante coutures dans un épisode
 * de deux minutes feraient quarante micro-coupures, et une pièce à conviction
 * trouée n'est plus une pièce à conviction.
 *
 * Pas de SharedArrayBuffer : il exigerait un contexte sécurisé ET l'isolation
 * cross-origin via COOP/COEP, et postMessage() lève une exception pour un SAB
 * sans ces en-têtes. Le transfert se fait une fois par événement, en
 * zero-copie.
 *
 * Règles de survie du thread audio, toutes apprises à la dure :
 *   • ne JAMAIS allouer dans process() — les allocations du thread audio
 *     causent des pauses GC, donc des glitches, donc des échantillons perdus ;
 *   • TOUJOURS retourner true — retourner false démonte le nœud pour de bon ;
 *   • gérer inputs[0] vide (le nœud tourne même sans source connectée) ;
 *   • try/catch autour de tout : une exception dans process() tue le
 *     processeur EN SILENCE. La page garderait son voyant « écoute » et aucun
 *     audio n'arriverait plus.
 */

// Profondeur du ring : le pré-roll maximal qu'on peut restituer. Un épisode
// commence à l'instant du déclenchement, moins le pré-roll — au-delà, il n'y a
// rien à récupérer.
const RING_SECONDS = 60;

// Durée visée pour une fenêtre RMS, en millisecondes. Ce n'est PAS un nombre
// d'échantillons : la taille de trame qui l'incarne dépend du taux du contexte,
// qui n'est connu qu'à l'exécution. app.js la calcule et la passe ici.
//
// Une constante de trame en dur des deux côtés serait deux sources de vérité
// qui divergent au premier changement de fréquence — et la divergence est
// silencieuse (voir la validation dans le constructeur).
const FRAME_MS = 20;

class RecorderProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const opts = (options && options.processorOptions) || {};

    // La trame est VALIDÉE, pas crue sur parole. À 0, NaN ou négatif,
    // `_frameCount >= frameSize` ne serait jamais vrai : plus aucun message
    // `rms` ne partirait, le watchdog relancerait le graphe toutes les 2 s,
    // indéfiniment — sans exception, sans log, sans audio. C'est exactement le
    // mode de panne que ce fichier documente partout.
    //
    // Le repli est DÉRIVÉ du taux plutôt que fixé à 1024 : si app.js et ce
    // fichier ne sont pas de la même version (ils sont mis en cache séparément
    // par le CDN), le désaccord reste inoffensif.
    const demande = Number(opts.frameSize);
    this.frameSize =
      Number.isFinite(demande) && demande >= 32 && demande <= 16384
        ? Math.floor(demande)
        : Math.round((sampleRate * FRAME_MS) / 1000);

    this.preRollMs = opts.preRollMs || 1000;
    this.postRollMs = opts.postRollMs || 2000;

    // Un pré-roll plus long que le ring est REFUSÉ, bruyamment. Le chemin
    // hérité complète par des zéros devant, ce qui est acceptable pour un
    // warm-up mais toxique ici : l'épisode commencerait par du silence
    // numérique, et l'instant du premier échantillon — calculé côté serveur
    // depuis cette longueur — décalerait toute la grille de fenêtres.
    if (this.preRollMs > RING_SECONDS * 1000) {
      throw new Error(
        'pré-roll ' + this.preRollMs + ' ms au-delà du ring de ' + RING_SECONDS + ' s'
      );
    }

    // Taille d'un morceau de flux. 1 s à 16 kHz vaut 2,05 hops de YAMNet
    // (7 800 échantillons) : le serveur produit donc exactement deux nouvelles
    // fenêtres par seconde, sans recouvrement gaspillé. Latence sans objet —
    // rien n'est publié avant la fin de l'épisode.
    const morceau = Number(opts.chunkSize);
    this.chunkSize =
      Number.isFinite(morceau) && morceau >= 1024 && morceau <= 1048576
        ? Math.floor(morceau)
        : Math.round(sampleRate);

    this._ring = new Float32Array(Math.ceil(sampleRate * RING_SECONDS));
    this._ringWrite = 0;
    this._ringFilled = 0;

    this._sumSq = 0;
    this._framePeak = 0;
    this._frameCount = 0;

    this._capturing = false;
    this._out = null;
    this._spare = null;
    this._outWrite = 0;

    // État du flux d'épisode.
    this._streaming = false;
    this._chunk = null;
    this._chunkWrite = 0;
    this._streamSamples = 0;
    this._streamChunks = 0;

    // État de l'écoute à la demande. SECOND mode d'émission, avec son propre
    // tampon : sa taille de morceau est bien plus courte (200 ms contre 1 s),
    // parce qu'ici la latence est le sujet — on écoute en direct.
    this._listening = false;
    this._listenChunk = null;
    this._listenChunkWrite = 0;
    this._listenChunkSize = Math.round(sampleRate / 5);
    this._listenSamples = 0;
    this._listenChunks = 0;

    this.port.onmessage = (event) => this._onCommand(event.data);
    this.port.postMessage({ type: 'ready', sampleRate });
  }

  _onCommand(msg) {
    if (!msg) return;
    switch (msg.type) {
      case 'configure':
        if (msg.preRollMs) this.preRollMs = msg.preRollMs;
        if (msg.postRollMs) this.postRollMs = msg.postRollMs;
        // DEUX tampons alloués ici, et pas un seul : l'un capture, l'autre
        // attend. À la fin d'une capture on transfère le premier et on bascule
        // sur le second, donc process() n'alloue JAMAIS — pas même une fois
        // par événement. ~1,1 Mo en tout, largement mérité.
        {
          const taille = Math.ceil((sampleRate * (this.preRollMs + this.postRollMs)) / 1000);
          this._out = new Float32Array(taille);
          this._spare = new Float32Array(taille);
        }
        this._capturing = false;
        this._outWrite = 0;
        this._streaming = false;
        this._chunk = null;
        this._chunkWrite = 0;
        this._listening = false;
        this._listenChunk = null;
        this._listenChunkWrite = 0;
        // 3,84 Mo de memset à 16 kHz, une fois par configure — donc à chaque
        // redémarrage audio. Quelques millisecondes, hors de process().
        this._ring.fill(0);
        this._ringWrite = 0;
        this._ringFilled = 0;
        this.port.postMessage({
          type: 'configured',
          sampleRate,
          // La trame RÉELLE, telle que ce processeur l'applique. C'est par ce
          // champ que app.js apprend la géométrie et se réaligne si les deux
          // fichiers ne sont pas de la même version.
          frameSize: this.frameSize,
          // app.js et ce fichier sont DEUX fichiers, mis en cache séparément
          // par le CDN : ils peuvent ne pas être de la même version. Sans cette
          // annonce, un app.js neuf face à un worklet ancien lui enverrait
          // `stream_begin` — que l'ancien ignore en silence, sans un mot. Le
          // serveur recevrait un épisode, aucun son, et refuserait tout.
          streaming: true,
          // Même rôle que `streaming` ci-dessus, pour l'écoute à la demande :
          // sans cette annonce, un app.js neuf face à un worklet resté dans le
          // cache du CDN lui enverrait `listen_begin`, que l'ancien ignorerait
          // en silence — et l'opérateur attendrait cinq secondes un son qui ne
          // viendrait jamais, sans savoir pourquoi.
          listening: true,
          chunkSize: this.chunkSize,
          ringSeconds: RING_SECONDS,
          preRollSamples: this._preRollSamples(),
          postRollSamples: this._postRollSamples(),
        });
        break;

      case 'trigger':
        // Un épisode en cours prime, et une ÉCOUTE aussi : le micro n'a qu'un
        // consommateur à la fois. Sans le second test, un déclenchement pendant
        // une écoute remplirait `_out` ET `_listenChunk` en parallèle — deux
        // captures du même micro, dont l'une transférerait son tampon au milieu
        // de l'autre.
        if (!this._streaming && !this._listening) this._beginCapture();
        break;

      case 'cancel':
        this._capturing = false;
        this._outWrite = 0;
        break;

      case 'stream_begin':
        this._beginStream();
        break;

      case 'stream_stop':
        this._streaming = false;
        this._chunk = null;
        this._chunkWrite = 0;
        this.port.postMessage({
          type: 'stream_stopped',
          numSamples: this._streamSamples,
          chunks: this._streamChunks,
        });
        break;

      case 'listen_begin':
        this._beginListen(msg.chunkSamples);
        break;

      case 'listen_stop':
        this._endListen();
        break;

      default:
        break;
    }
  }

  _preRollSamples() {
    return Math.ceil((sampleRate * this.preRollMs) / 1000);
  }

  _postRollSamples() {
    return Math.ceil((sampleRate * this.postRollMs) / 1000);
  }

  _beginCapture() {
    if (!this._out || this._capturing) return;

    // Le tampon d'attente est DÉTACHÉ par le transfert de la capture
    // précédente : sa byteLength vaut alors 0, et y écrire serait un no-op
    // silencieux — la capture suivante serait vide, sans la moindre erreur.
    // On le réalloue donc ici, dans un gestionnaire de message, donc HORS de
    // process() : contrainte respectée, et une allocation par événement.
    if (!this._spare || this._spare.byteLength === 0) {
      this._spare = new Float32Array(this._out.length);
    }

    const pre = Math.min(this._preRollSamples(), this._out.length);

    // Recopie du pré-roll, du plus ancien au plus récent. Si le ring n'est pas
    // encore plein (moins de 2 s depuis le démarrage), on complète par des
    // zéros DEVANT : le signal reste aligné sur la fin, donc le post-roll qui
    // suit est correctement positionné. En pratique le warm-up de 10 s du
    // thread principal rend ce cas inatteignable.
    const disponibles = Math.min(this._ringFilled, pre);
    const zeros = pre - disponibles;
    for (let i = 0; i < zeros; i++) this._out[i] = 0;
    const debut = (this._ringWrite - disponibles + this._ring.length) % this._ring.length;
    for (let i = 0; i < disponibles; i++) {
      this._out[zeros + i] = this._ring[(debut + i) % this._ring.length];
    }

    this._outWrite = pre;
    this._capturing = true;
  }

  _finishCapture() {
    this._capturing = false;

    // On transfère le Float32Array TEL QUEL et on bascule sur le tampon
    // d'attente. La conversion en int16 se fait sur le thread principal.
    //
    // Convertir ici aurait été plus proche du §9.5, mais ça mettrait une
    // boucle de 48 000 itérations (~0,7 ms) dans un callback audio de 8 ms à
    // 16 kHz — un underrun garanti, donc un glitch et des échantillons perdus.
    // Le transfert, lui, ne copie rien : le tampon change simplement de monde.
    const plein = this._out;
    const n = this._outWrite;
    this._out = this._spare;
    this._spare = plein;
    this._outWrite = 0;

    this.port.postMessage(
      {
        type: 'captured',
        pcm: plein,
        numSamples: n,
        sampleRate,
        preRollMs: this.preRollMs,
        postRollMs: this.postRollMs,
      },
      [plein.buffer]
    );
  }

  // ------------------------------------------------------------ flux

  /**
   * Ouvre un épisode. Le pré-roll devient le PREMIER morceau, et les suivants
   * s'enchaînent à l'échantillon IMMÉDIATEMENT après lui — pas de trou, pas de
   * recouvrement.
   *
   * Le ring CONTINUE d'être écrit pendant tout le flux : sinon le pré-roll du
   * déclenchement suivant serait périmé, et l'épisode d'après commencerait par
   * du silence.
   */
  _beginStream() {
    if (this._streaming || this._listening) return;

    const pre = Math.min(this._preRollSamples(), this._ringFilled);
    const premier = new Float32Array(pre);
    const debut = (this._ringWrite - pre + this._ring.length) % this._ring.length;
    for (let i = 0; i < pre; i++) {
      premier[i] = this._ring[(debut + i) % this._ring.length];
    }

    this._streaming = true;
    this._streamSamples = pre;
    this._streamChunks = 1;
    // Le tampon du morceau suivant est alloué MAINTENANT, dans un gestionnaire
    // de message, donc hors de process().
    this._chunk = new Float32Array(this.chunkSize);
    this._chunkWrite = 0;

    this.port.postMessage(
      { type: 'stream_chunk', pcm: premier, numSamples: pre, first: true },
      [premier.buffer]
    );
  }

  /**
   * Émet un morceau plein et en prépare un neuf.
   *
   * Appelée À LA FIN de la boucle par échantillon, jamais dedans. Allouer 64 Ko
   * une fois par seconde est un bump-pointer, très en dessous du budget d'un
   * quantum de 8 ms — la règle du fichier vise la boucle par échantillon, pas
   * le taux par seconde (le message `rms` alloue déjà cinquante fois par
   * seconde).
   *
   * Un pool de morceaux préalloués ne servirait à rien : transférer DÉTACHE le
   * tampon, et poster sans transfert alloue quand même le clone structuré.
   */
  _finishChunk() {
    const plein = this._chunk;
    const n = this._chunkWrite;
    this._chunk = new Float32Array(this.chunkSize);
    this._chunkWrite = 0;
    this._streamSamples += n;
    this._streamChunks++;
    this.port.postMessage(
      { type: 'stream_chunk', pcm: plein, numSamples: n },
      [plein.buffer]
    );
  }

  // ------------------------------------------------------------ écoute

  /**
   * Ouvre une écoute à la demande.
   *
   * Aucun pré-roll, à la différence de `_beginStream` : on veut le direct, et
   * reculer d'une seconde daterait le fichier avant son propre horodatage. Le
   * premier morceau part donc après `chunkSamples` échantillons, soit 200 ms de
   * silence au casque — acceptable pour une écoute qu'on déclenche soi-même.
   *
   * Le ring continue d'être écrit : il sert au déclenchement suivant, et une
   * écoute ne doit pas le laisser se périmer.
   */
  _beginListen(chunkSamples) {
    if (this._listening || this._streaming) return;

    const demande = Number(chunkSamples);
    this._listenChunkSize =
      Number.isFinite(demande) && demande >= 128 && demande <= 1048576
        ? Math.floor(demande)
        : Math.round(sampleRate / 5);

    this._listening = true;
    this._listenSamples = 0;
    this._listenChunks = 0;
    // Alloué ICI, dans un gestionnaire de message, donc hors de process().
    this._listenChunk = new Float32Array(this._listenChunkSize);
    this._listenChunkWrite = 0;

    this.port.postMessage({ type: 'listen_started', chunkSize: this._listenChunkSize });
  }

  /**
   * Émet un morceau d'écoute plein et en prépare un neuf.
   *
   * Même raisonnement que `_finishChunk` : une allocation par 200 ms est un
   * bump-pointer, très en dessous du budget d'un quantum de 8 ms. La règle
   * « aucune allocation dans process() » vise la boucle par échantillon.
   */
  _finishListenChunk() {
    const plein = this._listenChunk;
    const n = this._listenChunkWrite;
    this._listenChunk = new Float32Array(this._listenChunkSize);
    this._listenChunkWrite = 0;
    this._listenSamples += n;
    this._listenChunks++;
    this.port.postMessage(
      { type: 'listen_chunk', pcm: plein, numSamples: n },
      [plein.buffer]
    );
  }

  /**
   * Ferme l'écoute. ÉMET LE MORCEAU PARTIEL avant de s'arrêter.
   *
   * Les 200 derniers millisecondes sont du son réel : les jeter ferait une fin
   * de fichier tronquée d'un cinquième de seconde, sans rien pour le signaler.
   */
  _endListen() {
    if (!this._listening) return;
    if (this._listenChunkWrite > 0 && this._listenChunk) {
      const plein = new Float32Array(this._listenChunkWrite);
      plein.set(this._listenChunk.subarray(0, this._listenChunkWrite));
      this._listenSamples += this._listenChunkWrite;
      this._listenChunks++;
      this.port.postMessage(
        { type: 'listen_chunk', pcm: plein, numSamples: plein.length },
        [plein.buffer]
      );
    }
    this._listening = false;
    this._listenChunk = null;
    this._listenChunkWrite = 0;
    this.port.postMessage({
      type: 'listen_stopped',
      numSamples: this._listenSamples,
      chunks: this._listenChunks,
    });
  }

  process(inputs) {
    try {
      const input = inputs[0];
      // Le nœud tourne même sans source connectée : inputs[0] peut être un
      // tableau vide, ou contenir un canal vide.
      if (!input || input.length === 0 || !input[0]) return true;

      const gauche = input[0];
      const droite = input.length > 1 ? input[1] : null;
      const n = gauche.length;

      for (let i = 0; i < n; i++) {
        // Downmix SYSTÉMATIQUE : prendre input[0][0] en aveugle donne du
        // silence numérique sur un micro stéréo câblé à droite.
        const s = droite ? (gauche[i] + droite[i]) * 0.5 : gauche[i];

        this._ring[this._ringWrite] = s;
        this._ringWrite++;
        if (this._ringWrite >= this._ring.length) this._ringWrite = 0;
        if (this._ringFilled < this._ring.length) this._ringFilled++;

        this._sumSq += s * s;
        const a = s < 0 ? -s : s;
        if (a > this._framePeak) this._framePeak = a;

        this._frameCount++;
        if (this._frameCount >= this.frameSize) {
          // ~60 octets, 47 fois par seconde : négligeable.
          this.port.postMessage({
            type: 'rms',
            rms: Math.sqrt(this._sumSq / this.frameSize),
            peak: this._framePeak,
          });
          this._sumSq = 0;
          this._framePeak = 0;
          this._frameCount = 0;
        }

        if (this._capturing && this._out) {
          this._out[this._outWrite++] = s;
          if (this._outWrite >= this._out.length) this._finishCapture();
        }

        // Écrire PUIS tester : inverser l'ordre perdrait un échantillon par
        // morceau, soit une seconde toutes les 16 000 — invisible jusqu'à ce
        // que la grille de fenêtres du serveur serve décalée.
        if (this._streaming && this._chunk) {
          this._chunk[this._chunkWrite++] = s;
          if (this._chunkWrite >= this._chunk.length) this._finishChunk();
        }

        // Écoute à la demande. Exclusif de `_streaming` par les gardes
        // ci-dessus, donc les deux ne peuvent pas écrire en même temps.
        if (this._listening && this._listenChunk) {
          this._listenChunk[this._listenChunkWrite++] = s;
          if (this._listenChunkWrite >= this._listenChunk.length) this._finishListenChunk();
        }
      }
      return true;
    } catch (err) {
      // Sans ce catch, le processeur meurt sans rien dire et la page continue
      // d'afficher « écoute ». Le thread principal a un watchdog en plus, mais
      // autant lui dire franchement ce qui s'est passé.
      this.port.postMessage({
        type: 'error',
        message: (err && err.message) || String(err),
      });
      return true;
    }
  }
}

registerProcessor('aboigramme-recorder', RecorderProcessor);
