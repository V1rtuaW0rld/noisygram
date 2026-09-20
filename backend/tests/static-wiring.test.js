/**
 * Vérification statique du câblage HTML ↔ JS ↔ CSS.
 *
 * Ces deux pannes-là sont silencieuses et coûteuses :
 *   • un id absent du HTML → $('…') renvoie null, et le TypeError qui suit
 *     arrive au milieu d'une fonction de rendu, donc après un rendu partiel ;
 *   • une variable CSS mal orthographiée → cssVar() renvoie une chaîne vide,
 *     Chart.js reçoit une couleur invalide, et le graphique se dessine…
 *     invisible. Aucune erreur nulle part.
 *
 * Ni l'une ni l'autre ne se voit à la lecture. Les deux se voient ici.
 *
 *     node backend/tests/static-wiring.test.js
 */
'use strict';

const fs = require('fs');
const path = require('path');

const STATIC = path.join(__dirname, '..', 'static');

let failures = 0;
let checks = 0;
function check(label, ok, detail) {
  checks++;
  console.log(`[${ok ? '  ok  ' : ' ÉCHEC'}] ${label}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures++;
}

const lire = (p) => fs.readFileSync(p, 'utf8');

function idsDuHtml(html) {
  const ids = new Set();
  for (const m of html.matchAll(/\bid="([^"]+)"/g)) ids.add(m[1]);
  return ids;
}

function idsUtilises(js) {
  const utilises = new Set();
  for (const m of js.matchAll(/\$\('([^']+)'\)/g)) utilises.add(m[1]);
  for (const m of js.matchAll(/getElementById\('([^']+)'\)/g)) utilises.add(m[1]);
  return utilises;
}

function varsDeclarees(css) {
  const vars = new Set();
  for (const m of css.matchAll(/(--[a-z0-9-]+)\s*:/g)) vars.add(m[1]);
  return vars;
}

function varsUtilisees(js) {
  const vars = new Set();
  for (const m of js.matchAll(/cssVar\('([^']+)'\)/g)) vars.add(m[1]);
  return vars;
}

/**
 * Retire les commentaires avant une vérification d'ABSENCE.
 *
 * Ces vérifications portent sur le code, pas sur la prose. Un commentaire qui
 * dit « cette page n'appelle jamais getUserMedia » ne doit pas faire échouer le
 * test qui le vérifie — sans quoi on finirait par supprimer l'explication pour
 * faire passer le test, ce qui est exactement l'inverse du but.
 *
 * La garde `[^:]` évite d'avaler le `//` de `https://`.
 */
function sansCommentaires(js) {
  return js.replace(/\/\*[\s\S]*?\*\//g, ' ').replace(/(^|[^:])\/\/[^\n]*/g, '$1');
}

/** Le pendant HTML : une vérification d'ABSENCE doit porter sur le balisage,
 *  pas sur un commentaire qui parle de ce qu'on évite. */
function sansCommentairesHtml(html) {
  return html.replace(/<!--[\s\S]*?-->/g, ' ');
}

// -------------------------------------------------------------- page d'écoute
//
// Cette page est NEUVE, donc elle n'a aucune couverture tant qu'on ne l'ajoute
// pas ici. Elle a ses propres pièges, et deux d'entre eux sont exactement
// l'inverse de ceux de la page de capture.

console.log('\n■ Page d\'écoute directe');
{
  const html = lire(path.join(STATIC, 'listen', 'index.html'));
  const js = lire(path.join(STATIC, 'listen', 'listen.js'));
  const css = lire(path.join(STATIC, 'listen', 'style.css'));

  const presents = idsDuHtml(html);
  const manquants = [...idsUtilises(js)].filter((i) => !presents.has(i));
  check('tous les id utilisés par listen.js existent', manquants.length === 0, manquants.join(', '));

  const declarees = varsDeclarees(css);
  const utilisees = varsUtilisees(js);
  const varsManquantes = [...utilisees].filter((v) => !declarees.has(v));
  check('toutes les variables CSS lues existent', varsManquantes.length === 0, varsManquantes.join(', '));

  // --- Filtres de colonne -------------------------------------------------
  // Les descripteurs vivent dans listen.js (COLONNES), les colonnes dans le
  // HTML (data-col). Les deux moitiés doivent rester en vis-à-vis : un
  // data-col sans descripteur ne ferait RIEN au clic — un bouton inerte, la
  // panne la plus difficile à voir — et un descripteur sans data-col serait
  // une colonne décrite mais introuvable.
  const clesDesc = [...js.matchAll(/^\s{4}(?:'([^']+)'|([A-Za-z_]\w*)):\s*\{\s*libelle/gm)]
    .map((m) => m[1] || m[2]);
  const clesHtml = [...html.matchAll(/\bdata-col="([^"]+)"/g)].map((m) => m[1]);
  const sansDesc = clesHtml.filter((c) => !clesDesc.includes(c));
  const sansHtml = clesDesc.filter((c) => !clesHtml.includes(c));
  check('chaque data-col du HTML a un descripteur',
    clesHtml.length > 0 && sansDesc.length === 0,
    sansDesc.length ? `sans descripteur : ${sansDesc.join(', ')}` : `${clesHtml.length} colonne(s)`);
  check('chaque descripteur a une colonne dans le HTML', sansHtml.length === 0,
    sansHtml.join(', '));

  // Le piège de ce moteur : filtrer `pagination.X.items` au lieu de la copie
  // rendue. Le rafraîchissement de 4 s réassigne ce tableau, donc un filtre
  // écrit dedans disparaît sans un mot — et le tableau semble « oublier » ce
  // qu'on vient de cocher.
  for (const t of ['samples', 'captures', 'candidats']) {
    check(`le rendu « ${t} » passe par le moteur de filtre`,
      new RegExp(`lignesVisibles\\('${t}'`).test(js));
  }

  // Le thème sombre doit être déclaré sous les DEUX portées, sinon il n'est
  // appliqué qu'à moitié — le défaut est déjà documenté pour le dashboard.
  check('valeurs sombres sous prefers-color-scheme',
    /@media\s*\(prefers-color-scheme:\s*dark\)/.test(css));
  check('valeurs sombres sous [data-theme="dark"]', /\[data-theme="dark"\]/.test(css));

  // ⚠️ LE piège de cette page. La page de capture fait `JSON.parse` sous
  // try/catch et avale ce qui n'est pas du JSON ; ici une trame binaire est du
  // PCM légitime, et recopier ce motif la jetterait en silence — « ça ne joue
  // pas, aucune erreur ».
  check('binaryType arraybuffer est posé', /\.binaryType\s*=\s*'arraybuffer'/.test(js));
  check('dispatch sur le TYPE, pas un parse à l\'aveugle',
    /typeof\s+event\.data\s*===\s*'string'/.test(js));

  // Le réflexe inverse, aussi : cette page ne CAPTURE pas, elle ne doit donc
  // demander aucun micro. Si quelqu'un y colle le préflight de la page de
  // capture, il exige une origine sécurisée pour rien — et la page cessera de
  // marcher en HTTP simple sans qu'on comprenne pourquoi.
  const code = sansCommentaires(js);
  check('aucun getUserMedia sur la page d\'écoute', !/getUserMedia/.test(code));
  check('aucun préflight isSecureContext', !/isSecureContext/.test(code));

  // Le lecteur des samples reste NATIF, comme celui de la rafale du dashboard.
  check('le lecteur de samples est un <audio> natif', /<audio id="player"/.test(html));

  // La page dépend de `static/listen/` : un chemin de script relatif, sinon le
  // montage `html=True` sert un index sans son script.
  check('le script est chargé en relatif', /<script src="listen\.js(\?[^"]*)?">/.test(html));

  // Les liens inter-pages sont absolus et EN DUR, dans le HTML et pas dans le
  // JS : un .js est mis en cache quatre heures par le CDN, pas le .html.
  check('lien absolu vers le dashboard', /href="https:\/\/[^"]+\/dashboard\/"/.test(html));

  // ⚠️ INVARIANT DE SÉCURITÉ : `noisymic` — la page du poste de terrain — ne
  // doit être joignable que par ce poste. Il ne doit donc apparaître NULLE PART
  // dans ce qui est servi depuis `noisy`, pas même dans un commentaire : un
  // commentaire se lit dans le source d'une page par n'importe qui.
  check('aucune mention de l\'hôte privé dans la page d\'écoute',
    !/noisymic/i.test(sansCommentairesHtml(html)) && !/noisymic/i.test(html));
}

// La modale d'analyse. Même filet que ci-dessus, sur un fichier de plus : elle
// est chargée par la même page et partage sa portée, donc un id manquant y
// produit exactement le même TypeError silencieux.
console.log('\n■ Modale d\'analyse');
{
  const html = lire(path.join(STATIC, 'listen', 'index.html'));
  const js = lire(path.join(STATIC, 'listen', 'timeline.js'));

  const presents = idsDuHtml(html);
  const manquants = [...idsUtilises(js)].filter((i) => !presents.has(i));
  check('tous les id utilisés par timeline.js existent', manquants.length === 0, manquants.join(', '));

  // `listen.js` doit être chargé AVANT : `timeline.js` réutilise son helper `$`
  // et `decimal()`, et redéclarer `const $` serait une SyntaxError, pas une
  // redondance. L'ordre des balises est donc porteur.
  // `?v=N` toléré : les scripts SONT versionnés, c'est même vérifié plus bas.
  const iListen = html.search(/src="listen\.js(\?[^"]*)?"/);
  const iTimeline = html.search(/src="timeline\.js(\?[^"]*)?"/);
  check('timeline.js est chargé après listen.js',
    iListen !== -1 && iTimeline !== -1 && iListen < iTimeline,
    `listen@${iListen}, timeline@${iTimeline}`);

  // `<dialog>` natif : il apporte Échap, le piège de focus et `aria-modal` sans
  // une ligne de JS. Une div bricolée les réimplémenterait, mal.
  check('la modale est un <dialog> natif', /<dialog\s+id="analyse"/.test(html));
  check('le dialogue est étiqueté', /aria-labelledby="analyse-titre"/.test(html));
  check('la croix a un libellé accessible', /id="analyse-fermer"[^>]*aria-label="Fermer"/.test(html));

  const code = sansCommentaires(js);

  // La modale a SON lecteur, et il joue le fichier de la ligne cliquée.
  //
  // Un `<audio>` dans un `<dialog>` fonctionne — vérifié sur un cas minimal
  // dans les deux modes de rendu, `src` posé avant comme après `showModal()`.
  // La panne qu'on a cherchée longtemps n'était donc pas là, et ce test
  // verrouille le fait qu'on ne la cherche plus là.
  {
    const iOuvre = html.indexOf('<dialog');
    const iFerme = html.indexOf('</dialog>');
    const boite = iOuvre !== -1 && iFerme > iOuvre ? html.slice(iOuvre, iFerme) : '';
    const propre = sansCommentairesHtml(boite);
    check('la modale a son propre lecteur',
      /<audio id="analyse-player"[^>]*controls/.test(propre));
    check('il joue le fichier de la ligne cliquée',
      /\.src\s*=\s*'\/ondemand\/'\s*\+\s*encodeURIComponent\(nom\)/.test(code));
    // Trois lecteurs sur la page : un seul son à la fois, sinon deux pistes se
    // superposent et deviennent incompréhensibles.
    check('la modale coupe les lecteurs de la page',
      /addEventListener\('play'/.test(code) && /\$\('captures-player'\)/.test(code));
    // Un média qui échoue ne dit rien de lui-même.
    check('une erreur de lecture est affichée dans la modale',
      /id="analyse-player-note"/.test(html) && /addEventListener\('error'/.test(code));
  }

  // Les `.js` sont servis par un CDN qui les garde QUATRE HEURES. Deux
  // corrections successives n'ont jamais atteint le navigateur à cause de ça,
  // et le symptôme est indiscernable d'un correctif qui ne marche pas. Une
  // version dans l'URL est la seule chose qui règle ce problème-là.
  check('les scripts sont versionnés (cache CDN)',
    /<script src="listen\.js\?v=\d+">/.test(html) &&
    /<script src="timeline\.js\?v=\d+">/.test(html));

  // Un média qui échoue ne dit rien de lui-même : sans ce gestionnaire, la
  // panne est indiscernable d'un fichier muet.
  check('une erreur de lecture est affichée', /addEventListener\('error'/.test(code));

  // Et le code ne doit contenir aucun appel qui écrive : ni DELETE, ni un POST
  // vers autre chose que l'analyse.
  check('aucune suppression depuis la modale', !/method:\s*'DELETE'/.test(code));
  check('un seul appel réseau, vers l\'analyse',
    (code.match(/fetch\(/g) || []).length === 1 && /\/analyser/.test(code));
}

// Le dashboard doit pointer vers la page d'écoute, sinon elle n'est atteignable
// que par qui connaît l'URL. Symétrique des deux vérifications ci-dessus.
{
  const dashboard = lire(path.join(STATIC, 'dashboard', 'index.html'));
  check('le dashboard renvoie vers l\'écoute directe',
    /href="https:\/\/[^"]+\/listen\/"/.test(dashboard));
}

// ---------------------------------------------------------------- page client

console.log('\n■ Page de capture');
{
  const html = lire(path.join(STATIC, 'client', 'index.html'));
  const js = lire(path.join(STATIC, 'client', 'app.js'));
  const css = lire(path.join(STATIC, 'client', 'style.css'));

  const presents = idsDuHtml(html);
  const attendus = idsUtilises(js);
  const manquants = [...attendus].filter((i) => !presents.has(i));
  check('tous les id utilisés par app.js existent', manquants.length === 0, manquants.join(', '));

  // Le préflight doit être CÂBLÉ : s'il ne l'est pas, la page affiche un
  // bouton Démarrer qui échouera sans explication sur une origine non sûre.
  check('le bandeau de préflight existe et est masqué par défaut',
    /id="preflight"[^>]*hidden/.test(html));
  check("l'origine fautive est nommée dans le bandeau", /id="pf-origin"/.test(html));

  // Les quatre pastilles d'état.
  const pastilles = ['pill-mic', 'pill-ws', 'pill-capture', 'pill-device'];
  check('les 4 pastilles d\'état existent',
    pastilles.every((p) => presents.has(p)), pastilles.join(', '));

  // La cible tactile du bouton : on est dehors, souvent avec des gants.
  check('le bouton fait au moins 120×60 px', /min-width:\s*120px/.test(css) && /min-height:\s*60px/.test(css));

  // Le worklet doit être chargé par un chemin RELATIF : un chemin absolu
  // casserait la page si elle était servie sous un préfixe.
  check('le worklet est chargé en relatif', /addModule\('recorder-worklet\.js'\)/.test(js));

  // Les trois contraintes getUserMedia, sans lesquelles le client paraît sain
  // et ne détecte rien.
  for (const c of ['echoCancellation', 'noiseSuppression', 'autoGainControl']) {
    check(`${c} est explicitement à false`, new RegExp(c + ':\\s*false').test(js));
  }
  check('binaryType est mis à arraybuffer', /binaryType\s*=\s*'arraybuffer'/.test(js));

  // --- capture en 16 kHz -----------------------------------------------------
  // La géométrie (trame, historique, cadence de médiane) doit être dérivée du
  // taux du CONTEXTE. La prendre du taux de la PISTE — ce que faisait la
  // version d'avant — calcule un warm-up sur une cadence qui n'existe pas : dix
  // secondes en deviennent trente, sans une seule erreur au journal.
  check('la géométrie vient du taux du contexte',
    /configureGeometry\(\s*state\.context\.sampleRate/.test(js));
  check('le taux de la piste ne pilote plus la géométrie',
    !/framesPerSecond\s*=\s*r\.sampleRate/.test(js));

  check('fréquence visée : 16 kHz', /SR_CIBLE\s*=\s*16000/.test(js));
  check('le contexte est demandé à cette fréquence',
    /new Ctx\(\{\s*sampleRate:\s*SR_CIBLE\s*\}\)/.test(js));
  // Un contexte qui LÈVE au lieu d'ignorer tuerait tout startAudio(), donc
  // l'écoute, avec pour seul message « démarrage impossible ».
  check('la création du contexte est sous try',
    /try\s*\{\s*state\.context = new Ctx/.test(js));
  // Le repli doit être DÉRIVÉ du taux, jamais une constante : une constante
  // désaccorderait app.js et le worklet dès qu'ils ne sont pas de la même
  // version — et ils sont mis en cache séparément par le CDN.
  check('le repli de trame est dérivé du taux',
    /Math\.round\(\(state\.context\.sampleRate \* FRAME_MS\) \/ 1000\)/.test(js));
  check('la trame voyage avec la commande du worklet',
    /frameSize:\s*g\.frameSize/.test(js));

  const worklet = lire(path.join(STATIC, 'client', 'recorder-worklet.js'));
  check('le worklet n\'a plus de constante de trame en dur', !/const\s+FRAME_SIZE/.test(worklet));
  check('le worklet lit la trame reçue', /opts\.frameSize/.test(worklet));
  // La validation : un frameSize nul ou négatif arrêterait TOUT le flux rms,
  // et le watchdog relancerait le graphe toutes les 2 s, indéfiniment.
  check('le worklet valide la trame reçue', /Number\.isFinite\(demande\)/.test(worklet));

  // --- file d'attente --------------------------------------------------------
  // Le serveur n'accepte que quelques segments en vol : les envoyer tous d'un
  // coup en fait perdre la majorité, et un « busy » n'était pas remis en file.
  check('la file est vidée par petits lots', /QUEUE_PUMP/.test(js));
  check('un segment refusé en « busy » est remis en file',
    /msg\.code === 'busy'/.test(js) && /state\.queue\.unshift/.test(js));

  // Le lien vers le dashboard doit être ABSOLU. Relatif, il viserait l'hôte de
  // la capture, qui répond 404 — les deux rôles sont sur deux hôtes distincts.
  // Et il doit être dans le HTML, pas posé par le JS : Cloudflare met les .js
  // en cache quatre heures, le .html non.
  check('le lien vers le dashboard est une URL absolue',
    /<a href="https:\/\/[^"]+\/dashboard\/"[^>]*>Dashboard/.test(html),
    (html.match(/<a href="[^"]*"[^>]*>Dashboard/) || ['introuvable'])[0]);

  // --- suivi du projet surveillé ---------------------------------------------
  // Le titre dit ce que CETTE capture surveille. Trois sources le renseignent,
  // et aucune n'est redondante : la requête au chargement, parce que le
  // WebSocket ne s'ouvre qu'à Démarrer ; le `hello_ack` à la connexion ; et
  // `projet_change` quand la capture bascule pendant qu'on écoute.
  check('le titre du projet existe dans le HTML', presents.has('projet-courant'));
  check('le nom est demandé dès le chargement',
    /fetch\('\/api\/projets\/courant'/.test(js));

  // L'URL demandée doit correspondre à une route RÉELLEMENT déclarée. Un
  // préfixe changé d'un côté seulement rendrait un 404 que la page avale
  // (l'échec est silencieux, exprès) : le titre resterait à « … » pour
  // toujours, et personne ne saurait pourquoi.
  const apiProjet = lire(path.join(__dirname, '..', 'app', 'api', 'projet.py'));
  const prefixeApi = (apiProjet.match(/APIRouter\(prefix="([^"]+)"/) || [])[1];
  const urlDemandee = (js.match(/fetch\('([^']*\/courant)'/) || [])[1];
  check("l'URL demandée correspond à la route déclarée",
    !!prefixeApi && urlDemandee === prefixeApi + '/courant',
    `${urlDemandee} vs ${prefixeApi}/courant`);

  // La bascule de projet, poussée par le serveur. On isole la branche : sans
  // ça, un `restartAudio()` trouvé ailleurs dans le fichier ferait passer la
  // vérification alors que rien ne le déclenche ici.
  // `indexOf` rend -1 quand la branche a disparu, et `slice(-1, n)` rendrait
  // alors UN caractère — donc `length > 0` passerait sur un fichier où la
  // branche n'existe plus. C'est le genre de vérification qui ne vérifie rien.
  const iBascule = js.indexOf("msg.type === 'projet_change'");
  const iApres = js.indexOf("msg.type === 'pong'");
  const bascule = iBascule !== -1 && iApres > iBascule ? js.slice(iBascule, iApres) : '';
  check('la bascule de projet est traitée', bascule.length > 0);
  check('la bascule met le titre à jour', /afficherProjet\(msg\.projet\)/.test(bascule));
  // Le groupe et le seuil avec lesquels nos segments seront jugés viennent de
  // changer : un épisode à cheval sur les deux serait jugé moitié par l'un,
  // moitié par l'autre.
  check('la bascule redémarre la capture audio', /restartAudio\(\)/.test(bascule));
  // Mais un projet REFUSÉ ne redémarre RIEN : côté serveur le groupe n'a pas
  // bougé, et couper le micro pour ça ferait perdre un épisode pour rien. Le
  // refus doit être traité AVANT toute mise à jour du titre — sinon le poste
  // afficherait un projet que la capture ne surveille pas.
  const iRefus = bascule.indexOf('applique === false');
  const iTitre = bascule.indexOf('afficherProjet(msg.projet)');
  check('un projet refusé est traité avant toute mise à jour du titre',
    iRefus !== -1 && iTitre !== -1 && iRefus < iTitre);
  check('un projet refusé ne redémarre pas la capture',
    /applique === false[\s\S]*?return;/.test(bascule));
}

// ---------------------------------------------------------------- dashboard

console.log('\n■ Dashboard');
{
  const html = lire(path.join(STATIC, 'dashboard', 'index.html'));
  const js = lire(path.join(STATIC, 'dashboard', 'dashboard.js'));
  const css = lire(path.join(STATIC, 'dashboard', 'style.css'));

  const presents = idsDuHtml(html);
  const attendus = idsUtilises(js);
  const manquants = [...attendus].filter((i) => !presents.has(i));
  check('tous les id utilisés par dashboard.js existent', manquants.length === 0, manquants.join(', '));

  const vars = varsDeclarees(css);
  const utilisees = varsUtilisees(js);
  const absentes = [...utilisees].filter((v) => !vars.has(v));
  check('toutes les variables CSS lues existent', absentes.length === 0, absentes.join(', '));

  // Les variables de la rampe séquentielle sont lues par construction
  // (`--seq-' + n`), donc invisibles au grep : on vérifie les six à la main.
  const rampe = [1, 2, 3, 4, 5, 6].map((n) => '--seq-' + n);
  check('les 6 paliers de la rampe séquentielle sont déclarés',
    rampe.every((v) => vars.has(v)), rampe.join(', '));

  // Le thème sombre doit être déclaré sous LES DEUX portées, sinon le bouton
  // ne peut pas contredire le réglage du système (ou l'inverse).
  check('valeurs sombres sous la requête média', /@media \(prefers-color-scheme: dark\)/.test(css));
  check('valeurs sombres sous [data-theme="dark"]', /:root\[data-theme="dark"\]/.test(css));

  // Chart.js doit être servi localement : le vieux PC derrière une box grand
  // public est la machine la plus susceptible de ne pas avoir de DNS.
  check('Chart.js est chargé en local, pas depuis un CDN',
    /src="\/vendor\/chart\.umd\.min\.js"/.test(html) && !/https?:\/\//.test(html.match(/<script[^>]*>/g).join('')));

  // Un axe y auto-adapté ferait paraître un jour calme comme un jour bruyant.
  check('l\'axe y de la timeline est épinglé à [0,1]', /min:\s*0,\s*max:\s*1/.test(js));

  // Aucun double axe nulle part.
  const doubles = (js.match(/y1\s*:/g) || []).length + (js.match(/yAxisID/g) || []).length;
  check('aucun second axe y', doubles === 0, doubles ? doubles + ' occurrence(s)' : '');

  // La ligne de seuil ne doit jamais être pointillée : le pointillé se lit
  // comme « projection ».
  check('la ligne de seuil n\'est pas pointillée',
    !/setLineDash/.test(js) && !/borderDash/.test(js));

  // Chaque graphique a sa vue tableau : la couleur seule ne doit jamais être
  // le seul moyen d'accéder à une valeur.
  const tableaux = (html.match(/<details>/g) || []).length;
  check('chaque graphique a une vue tableau', tableaux >= 3, tableaux + ' tableaux');

  // Chaque bouton d'export doit avoir sa contrepartie dans le registre
  // `exportables`. Un data-export sans clé ne fait RIEN au clic — pas
  // d'erreur, pas de message : le bouton a l'air mort. L'inverse est du code
  // mort. Les deux sens sont vérifiés, comme pour les id.
  const boutons = new Set([...html.matchAll(/data-export="([^"]+)"/g)].map((m) => m[1]));
  const cles = new Set([...js.matchAll(/exportables\.(\w+)\s*=/g)].map((m) => m[1]));
  check('chaque bouton d\'export a sa clé dans exportables',
    [...boutons].every((b) => cles.has(b)),
    [...boutons].filter((b) => !cles.has(b)).join(', '));
  check('chaque clé d\'export est câblée à un bouton',
    [...cles].every((c) => boutons.has(c)),
    [...cles].filter((c) => !boutons.has(c)).join(', '));

  // Les deux conventions qu'impose Excel FR. Les perdre ne casse rien
  // visiblement : le fichier s'ouvre, mais en une seule colonne et avec des
  // accents cassés — donc on ne s'en aperçoit qu'au moment de s'en servir.
  check('le CSV suit les conventions d\'Excel FR',
    /join\(';'\)/.test(js) && /\\ufeff/.test(js));

  // ⚠️ INVARIANT DE SÉCURITÉ, symétrique de celui de la page d'écoute :
  // `noisymic` ne doit apparaître NULLE PART dans ce qui est servi depuis
  // `noisy` — y compris dans un commentaire, qui se lit dans le source de la
  // page. Et surtout pas un `<a href>` vers /client/ : un lien, même discret,
  // suffit à rendre l'hôte privé découvrable.
  check('aucune mention de l\'hôte privé dans le dashboard',
    !/noisymic/i.test(html));
  check('aucun lien vers la page du poste de terrain',
    !/href="[^"]*\/client\/"/.test(sansCommentairesHtml(html)));

  // --- écoute d'une rafale ---------------------------------------------------
  // Le bouton doit être MASQUÉ au chargement : tant qu'aucun point n'est
  // cliqué, on ne sait pas de quelle rafale on parlerait.
  check('le bouton de rafale existe et est masqué',
    /id="rafale"[^>]*hidden/.test(html));
  check('la rafale se lit avec les contrôles natifs, pas un lecteur maison',
    /<audio id="player"/.test(html));
  // L'en-tête WAV doit dire la fréquence RÉELLEMENT décodée. La figer à 16 000
  // produirait un WAV illisible si le navigateur décodait autrement — c'est le
  // même piège que le taux de la piste côté capture, un étage plus loin.
  check('la fréquence du WAV vient du décodage, pas d\'une constante',
    /sampleRate\).*getChannelData|clips\[0\]\.buf\.sampleRate/.test(js));
  // Chaque clip est ramené à sa durée annoncée : sans ça le délai et le
  // remplissage du codec s'accumulent à chaque couture.
  check('les clips sont ramenés à leur durée annoncée',
    /Math\.round\(\(c\.durationMs \/ 1000\) \* rate\)/.test(js));
}

// ---------------------------------------------------------------- routes

// Ces routes sont dans un fichier Python, mais il n'y a qu'un harnais de test
// ici (pas de pytest, décision assumée) et la règle qu'on vérifie est trop
// facile à casser en silence pour la laisser sans filet.
console.log('\n■ Routes');
{
  const py = lire(path.join(__dirname, '..', 'app', 'api', 'events.py'));
  const routes = [...py.matchAll(/@router\.get\("([^"]+)"/g)].map((m) => ({
    chemin: m[1],
    at: m.index,
  }));
  const parametree = routes.find((r) => r.chemin === '/events/{event_id}');

  // FastAPI apparie dans l'ordre d'enregistrement : une route littérale
  // déclarée APRÈS la route paramétrée ne serait jamais atteinte — « since »
  // ne se convertit pas en entier, et le client recevrait un 422 sur une URL
  // qui existe pourtant dans le code.
  const fautives = parametree
    ? routes
        .filter((r) => r !== parametree && !r.chemin.replace('/events/', '').startsWith('{'))
        .filter((r) => r.at > parametree.at)
    : [];
  check('toute route littérale précède /events/{event_id}',
    !!parametree && fautives.length === 0,
    fautives.map((r) => r.chemin).join(', ') ||
      routes.map((r) => r.chemin).join(' '));

  check('la route de rafale est déclarée', routes.some((r) => r.chemin === '/events/sequence/{event_id}'));
}

// ---------------------------------------------------------------- worklet

console.log('\n■ Worklet');
{
  const js = lire(path.join(STATIC, 'client', 'recorder-worklet.js'));
  check('registerProcessor appelé', /registerProcessor\('noisygram-recorder'/.test(js));
  check('downmix (L+R)/2 présent', /\(gauche\[i\] \+ droite\[i\]\) \* 0\.5/.test(js));
  check('try/catch autour de process()', /process\(inputs\)\s*\{\s*try/.test(js));
  check('aucune allocation dans process() (new … dans la boucle)',
    !/process\(inputs\)[\s\S]*?for \([\s\S]*?new (Float32Array|Int16Array)/.test(js));
}

console.log('\n' + '='.repeat(72));
if (failures) {
  console.log(`  ${failures} ÉCHEC(S) sur ${checks} vérifications`);
  process.exit(1);
}
console.log(`  ${checks} vérifications, toutes OK`);
