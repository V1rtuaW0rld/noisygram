# Noisygram

Détection et historisation d'événements sonores. Un vieux PC Windows en extérieur capte
le son ; ce service qualifie les extraits et alimente un dashboard.

Le client fait le **pré-tri** (détection RMS, très peu de CPU) et n'envoie que
les extraits candidats ; le serveur fait la **qualification** (YAMNet) et
l'**historisation**. On ne streame jamais d'audio en permanence : ni réseau ni
RAM saturés.

---

## Démarrage

```bash
cp .env.example .env      # régler POSTGRES_PASSWORD et APP_TZ
docker compose up -d --build
docker compose ps         # db (healthy) et api (healthy)
```

| Service | Port | Rôle |
|---|---|---|
| **capture** | 4466 | Page de capture, WebSocket, écriture des MP3. Charge YAMNet. |
| **admin** | 4467 | Dashboard, écoute directe, API REST, procédures. Ne charge **pas** le modèle. |
| db | *aucun* | Le 5432 de l'hôte est déjà pris par un autre projet. |

| | |
|---|---|
| Dashboard | http://\<ip\>:4467/dashboard/ |
| Écoute directe | http://\<ip\>:4467/listen/ |
| Capture | http://\<ip\>:4466/client/ |
| API | http://\<ip\>:4467/docs |

Le port 4466 ne sert que le poste de terrain : sa page, son WebSocket, et les
deux points de relais que l'admin appelle en interne. **Rien d'autre n'y est
exposé**, et c'est délibéré — c'est le seul des deux qu'il ne faut jamais
rendre public.

Le premier `up --build` prend 2 à 4 minutes (wheels, 4 Mo de modèle, vérification
SHA-256). Les suivants sont immédiats.

### Pourquoi deux processus

Les deux pages ne partagent **aucun** endpoint : `client/app.js` n'appelle que
`/ws/audio`, `dashboard.js` n'appelle que `/api/*`. Le découpage suit cette
couture, qui existait déjà.

Ce qu'il apporte :

1. **L'admin n'embarque pas le classifieur** — ni LiteRT, ni l'`Interpreter`
   non thread-safe. Un blocage d'inférence côté capture ne peut plus emporter le
   dashboard avec lui. (Gain mesuré : ~20 Mo de RSS, ce qui est modeste. Le
   bénéfice réel est le découplage, pas la mémoire.)
2. **Le micro exige une origine sécurisée**, donc la page de capture *doit*
   passer en HTTPS. Le dashboard n'en a aucun besoin. Découper les ports permet
   de mettre Caddy devant la **capture seule**.
3. **Le CORS n'apparaît pas.** C'était le point à vérifier : chaque port sert
   l'API et les MP3 dont *sa* page a besoin, donc aucune requête n'est
   cross-origin. La règle du « pas de CORS » tient toujours.

⚠️ **Deux ports ne sont pas un cloisonnement.** Il n'y a aucune
authentification, et les deux ports sont publiés sur le LAN. Les séparer
réellement demande un pare-feu, une adresse d'écoute (`127.0.0.1:`), ou ton
Caddy.

Pour tout remettre sur un seul port : `APP_ROLE=all` (c'est le défaut hors
compose).

---

## Sécurité — à lire avant d'exposer quoi que ce soit

**Il n'y a AUCUNE authentification.** C'est une hypothèse LAN-only assumée : le
service accepte des uploads audio et les stocke. **Ne jamais le publier sur
Internet** par redirection de port, DMZ ou reverse proxy public. Quiconque
atteint le port 8000 peut envoyer de l'audio, lire et supprimer l'historique.

Il n'y a pas non plus de CORS, volontairement : tout est same-origin, et une
politique permissive sur un service qui accepte des uploads serait un vrai
risque pour zéro bénéfice.

---

## Le seuil de détection

`NOISY_THRESHOLD` vaut **0,35 par défaut, et c'est un point de départ, pas une
vérité**. Les scores YAMNet sont des sigmoïdes entraînées sur AudioSet, **pas
des probabilités calibrées** — un « 0,87 » n'est pas « 87 % de chances d'un
événement » au sens fréquentiste.

Le score seuillé est le **maximum sur le groupe surveillé** (`Dog`, `Bark`, `Yip`,
`Howl`, `Bow-wow`, `Growling`, `Whimper`), et non la seule classe `Bark`. Sur
les enregistrements réels du terrain, `Bark` s'effondre à 0,26 là où `Dog` monte
à 0,59 sur les **mêmes** segments : c'est le moins bon discriminateur du groupe.
La colonne `bark_score` reste stockée en diagnostic, pour pouvoir comparer les
deux critères sur des données accumulées.

### Calibrer

Le dossier `samples/` sert à ça — voir `samples/README.md` pour la convention.

```bash
# le côté POSITIF : de vrais événements
docker compose run --rm --no-deps -v $PWD/samples/reference:/ref:ro \
  capture python -m app.tools.selftest /ref/événements.wav --segment 3

# le côté NÉGATIF : le fond sonore du terrain, SANS source
docker compose run --rm --no-deps -v $PWD/samples/negative:/neg:ro \
  capture python -m app.tools.selftest /neg/ambiance.wav --segment 3
```

> Le service `capture` est celui qui porte le modèle. `admin` ne saurait pas
> qualifier un fichier : il n'a pas de classifieur.

Le second doit sortir **0 segment accepté**. C'est ce chiffre-là, et pas le
premier, qui fixe le seuil : on le place juste au-dessus du score maximal du
bruit de fond, en gardant de la marge sous le score minimal des événements.

Le format attendu est du **WAV PCM 16 bits**, n'importe quelle fréquence. Le
serveur n'embarque **aucun décodeur MP3** — c'est délibéré, ça évite 350 Mo de
ffmpeg. Convertir un MP3 avant de le déposer.

`SAVE_REJECTED=true` écrit en plus un WAV de chaque refus (à 16 kHz, exactement
ce que le classifieur a reçu : le rejouer redonne le score au chiffre près).

---

## Exploitation du poste extérieur

Ces réglages ne sont pas optionnels — sans eux, la boîte s'arrête en silence.

- **Désactiver la veille et l'hibernation.** Un PC qui s'endort ne capture plus.
- **Désactiver la suspension sélective USB.** Le microphone ne se réveille pas
  toujours.
- **Lancer Chrome en kiosque** :
  ```
  chrome --kiosk --autoplay-policy=no-user-gesture-required http://<ip>:4466/client/
  ```
- **Démarrer la capture.** La page ne démarre pas toute seule : cliquer sur
  `Démarrer`. Si la page est rechargée, il faut recliquer.

### Le micro doit être accessible sur une origine sécurisée

Chrome et Firefox ne donnent accès au microphone que sur `https://` ou
`localhost`. Depuis `http://<ip>:4466`, l'objet `navigator.mediaDevices` est
**indéfini** et la page affiche un bandeau nommant l'origine à autoriser.

Deux procédures de contournement, sans rien installer :
[`docs/CHROME_INSECURE_ORIGIN.md`](docs/CHROME_INSECURE_ORIGIN.md).

La solution propre, si tu as déjà un Caddy : [`docs/CADDY.md`](docs/CADDY.md).

---

## Écoute directe

Le poste n'envoie rien tant qu'il n'a pas déclenché : c'est ce qui rend le
système économe, et c'est aussi ce qui le rend **aveugle**. Quand rien n'arrive,
« l'ambiance est calme », « le micro est débranché » et « le seuil est mal réglé »
se ressemblent — et pour savoir lequel, il fallait aller lire le journal sur le
poste, c'est-à-dire sur la machine qu'on ne peut pas atteindre.

Le bouton **Écouter** répond aux deux : il fait streamer le poste **60 secondes**
en continu, qu'on écoute en direct (~250-290 ms de latence), pendant que l'audio
s'écrit dans `export/` et se fait classer par YAMNet au passage.

| | |
|---|---|
| Page d'écoute | http://\<ip\>:4467/listen/ |
| Latence | ~250-290 ms (morceaux de 200 ms + 120 ms de tampon de gigue) |
| Bande passante | 32 Ko/s, en s16le 16 kHz mono — uniquement pendant l'écoute |

**L'interface vit sur le port 4467**, avec le dashboard, donc sur `noisy` — et
jamais sur `noisymic`, qui ne doit rester joignable que par le poste de terrain.

Elle ne peut pourtant pas tout faire seule, et c'est le prix à payer : le poste
est connecté à **capture** (4466), et le modèle n'y tourne que là. La page
**relaie** donc les deux choses qui lui manquent — l'analyse d'un sample
(`_relais_vers_capture`) et le direct (`ws/proxy.py`, qui fait passer le
WebSocket dans les deux sens). Elle lit en revanche les WAV et la table
`events` directement : `./export` est monté sur les deux services, et la base
est partagée.

### Ce qui se passe pendant l'écoute

- La détection par déclenchement est **relocalisée**, pas arrêtée : le serveur
  classe la minute entière. Si quelque chose en ressort, il en fait un épisode normal
  — ligne `events` et MP3 — en plus du WAV de travail.
- Un seul flux à la fois : le micro n'a qu'un consommateur. Un déclenchement qui
  survient pendant une écoute est ignoré, et un épisode en cours fait refuser
  l'écoute avec un message.
- Le WAV est publié **quoi qu'il arrive** — durée atteinte, arrêt de
  l'opérateur, déconnexion du poste, ou même `kill -9` du conteneur : dans ce
  dernier cas il est récupéré au démarrage suivant, parce que l'en-tête est écrit
  en premier et que le fichier est alors réparable.

### Les deux tableaux

Sous le direct, deux listes qui se ressemblent à l'écoute et n'ont pas du tout
la même origine :

| | Origine |
|---|---|
| **Les directs** | les WAV de `export/` — un fichier par écoute **que tu as demandée** |
| **Les capturés** | la table `events` — ce que le **poste a jugé** digne d'être gardé, tout seul |

Le drapeau « analysé » des directs est porté par la présence d'une entrée
d'index, pas par un champ séparé.

### Analyser un sample

« Analyser » ouvre une modale qui montre **ce que le système croit avoir
entendu, tranche par tranche** : `6.83s à 8.29s — Whistling — 0.891`. Les
fenêtres consécutives de même étiquette sont fusionnées, sinon la timeline
serait une pile d'intervalles qui se chevauchent.

**C'est de l'affichage seul : rien n'est écrit en base, rien n'est compté.**
Le noisygram est alimenté tout seul par le YAMNet embarqué, à la fin de chaque
écoute. Compter aussi ici serait un doublon — la même source comptée deux fois
pour le même audio.

Deux points de lecture :

- **Le « score max » ne vient pas de la timeline.** La timeline liste
  l'étiquette dominante de chaque fenêtre ; le score principal est le **max du
  groupe surveillé** sur la même fenêtre. Les deux diffèrent, et c'est voulu : une
  fenêtre où `Dog` marque 0,40 et `Speech` 0,46 s'appelle `Speech` dans la
  timeline, et la source y serait invisible. La modale surligne ces fenêtres.
- **Le seuil d'affichage (`ANALYZE_TIMELINE_MIN_SCORE`, 0,3) n'est pas le seuil
  de détection (`NOISY_THRESHOLD`, 0,35).** Le premier choisit ce qu'on montre, le
  second décide.

Le calcul se fait **dans le conteneur**, avec le même modèle que la détection :
mesuré à **8,9 ms par seconde d'audio**, soit ~0,5 s pour une minute. Un
`ANALYZE_BACKEND=remote` permet de déléguer à un service HTTP (YAMNet sur GPU) —
il doit accepter un POST multipart `file=@….wav` et rendre
`{total_duration_sec, timeline:[{interval, sound, score}]}`. Le résultat est mis
en cache dans `ondemand_index.json`, donc rouvrir la modale est instantané ;
« Relancer » force un nouveau calcul.

L'index (`ondemand_index.json`, dans le même dossier) est indexé par **sha256 du
contenu**, donc :

- il **survit** à un renommage et à un déplacement dans le dossier ;
- il **ne survit pas** à une modification du WAV (voulu : toucher l'audio
  invalide l'analyse) ;
- il **ne survit pas** à une sortie du dossier.

⚠️ **Le son sort par tes haut-parleurs.** Si tu es sur place, coupe le son ou
mets un casque : le micro du poste reprend tes haut-parleurs et cela siffle.

---

## Volumes — ce que `down -v` détruit

| Volume | Contenu | Détruit par `down -v` |
|---|---|---|
| `pgdata` | la base : qui, quand, quel score | **oui** |
| `media` | les MP3 | **non** |
| `./export` (bind mount, capture seul) | les WAV d'écoute et les dumps de debug | **non** (ce n'est pas un volume Docker) |

Deux volumes séparés, volontairement. Ne pas supposer que `down -v` est sans
danger, ni qu'il est total.

`export/` est un **bind mount sur un dossier du projet**, monté sur le rôle
capture seulement. C'est délibéré : les samples d'écoute sont faits pour être
ouverts et manipulés à la main, sans `docker cp`.

### Croissance disque

**Le raisonnement a changé de base.** Une ligne n'est plus un clip de 3 s mais
un **épisode** — un enregistrement continu qui dure tant qu'il y a du bruit. La bonne
unité n'est donc plus le nombre d'événements, mais les **minutes cumulées
d'audio** : à 64 kbps mono, c'est **480 Ko par minute**, quel que soit le
découpage.

| Cumul d'événements | Par jour | Par an |
|---|---|---|
| 10 min | 4,8 Mo | ~1,7 Go |
| 1 h | 29 Mo | ~10,5 Go |
| 10 h | 288 Mo | ~105 Go |

Ce qui borne la croissance, ce n'est donc plus le garde-tempête (il compte des
*déclenchements*, et un épisode de trois minutes n'en produit aucun) mais
**`client_stream_max_ms`** : un épisode est coupé à 3 minutes par défaut, et ce
plafond se règle depuis le serveur via `config_patch`, sans toucher au poste.

Un épisode refusé n'est jamais conservé, sauf si `SAVE_REJECTED=true` : dans ce
cas seules ses `save_rejected_max_ms` premières secondes sont écrites, sous
`rejected/`. Ce corpus est le **fond sonore réel** qui manque pour régler le
seuil — `samples/negative/` est vide, et c'est le point ouvert n°1 du projet.

### Les samples d'écoute, eux, n'ont aucune rétention par défaut

L'écoute directe écrit **1,9 Mo par minute** dans `export/`, et `ONDEMAND_KEEP`
vaut **0 — c'est-à-dire qu'on ne supprime jamais rien**. C'est la bonne valeur
tant qu'on constitue le corpus de calibration, puisque c'est précisément ce
corpus qui manquait. En exploitation établie, poser une borne : vingt écoutes
par jour font ~14 Go/an.

⚠️ Les samples portent le suffixe `_ondemand` et les dumps de debug `_debug`,
**et ce n'est pas cosmétique** : `debugdump._elaguer` supprime les `*_debug.wav`
au-delà de `DEBUG_DUMP_KEEP`. Deux suffixes distincts, donc deux élagages
distincts — sans quoi régler l'élagage du debug effacerait tes samples.

Le MVP ne livre pas d'ordonnanceur de rétention, mais la requête est prête à
être planifiée :

```sql
-- supprimer les événements de plus d'un an
DELETE FROM events WHERE detected_at < now() - interval '1 year';
```

⚠️ Elle supprime les **lignes**, pas les **fichiers**. Pour récupérer l'espace,
supprimer aussi les fichiers correspondants :

```bash
# lister les MP3 sans ligne en base, et les dates concernées
docker compose exec db psql -U noisygram -d noisygram -tAc \
  "SELECT mp3_path FROM events WHERE detected_at < now() - interval '1 year'"
```

---

## Diagnostic

| Symptôme | Cause probable |
|---|---|
| Page morte, aucune erreur | Origine non sécurisée. Le bandeau doit nommer l'origine ; sinon voir `docs/CHROME_INSECURE_ORIGIN.md`. |
| Client sain, compteur à zéro | Les contraintes `getUserMedia`. `echoCancellation`, `noiseSuppression` et `autoGainControl` sont **tous à `true` par défaut dans Chrome** — la suppression de bruit est entraînée à retirer précisément les transitoires non-parole, donc un événement. |
| Tout est classé au hasard | Fréquence micro ≠ 48 kHz. Le client envoie la fréquence **native**, le serveur rééchantillonne depuis elle. Vérifier `sample_rate` en base. |
| `up` échoue, « address already in use » | Le port 5432 de l'hôte est pris par un autre projet. Le service `db` ne publie **aucun** port, il ne devrait pas y avoir de conflit. |
| Le conteneur redémarre en boucle alors que la base hoquette | `/api/health` renvoie `degraded` en **HTTP 200**, jamais 503 : un 503 ferait redémarrer le conteneur sur un incident transitoire. |
| Micro perdu (USB débranché) | Le client le détecte (`track.onended`) et ré-acquiert. Visible dans le journal de la page. |
| Capture muette, voyant « écoute » allumé | Worklet mort. Un watchdog côté page le détecte en 2 s et relance le graphe audio. |
| L'écoute s'arrête vers 51 s au lieu de 60 | `max_stream_chunks` (256) a été appliqué au chemin d'écoute. Il a ses propres bornes, dérivées de la durée — voir `config.py`, propriétés `listen_*`. |
| L'écoute s'arrête à la 30ᵉ seconde sur un champ calme | `stream_silence_ms` s'est invité dans le watchdog d'écoute. Il ne doit PAS y avoir de condition de silence : « rien » est une réponse valide. |
| « aucune réponse du poste en 5 s » | Le poste n'a pas accusé réception. Trois causes, dans l'ordre : page du poste pas rechargée (worklet ancien), page arrêtée, poste déconnecté. Le message les nomme toutes les trois. |
| L'écoute refuse : « worklet pas à jour » | `recorder-worklet.js` et `app.js` sont mis en cache **séparément**. Recharger la page du poste. |
| Ça siffle | Larsen. Le micro du poste reprend tes haut-parleurs. Bouton « Couper le son ». |
| Le WAV d'écoute n'apparaît pas dans `export/` | Vérifier que le processus qui écrit est bien `capture` : `export/` n'est monté que là, et le panneau n'existe que sur 4466. |

### Commandes utiles

```bash
# le modèle sort-il un top-5 correct, sans navigateur ?
docker compose exec capture python -m app.tools.selftest

# le protocole WebSocket tient-il, y compris les chemins d'erreur ?
docker compose cp samples/reference/événements.wav capture:/tmp/
docker compose exec capture python -m app.tools.wstest /tmp/événements.wav

# santé — le champ `role` dit à qui on parle, et `classifier_ready: false`
# est NORMAL sur l'admin (il n'a pas de modèle)
curl -s localhost:4466/api/health | python3 -m json.tool
curl -s localhost:4467/api/health | python3 -m json.tool

# acceptés depuis 24 h
docker compose logs --since 24h capture | grep -c accepté

# les écoutes à la demande, et ce qu'elles ont donné
docker compose logs --since 24h capture | grep -i 'écoute'

# durée réelle d'un sample d'écoute — contrôle du piège des 51 s
docker compose exec capture python -c "
import wave,sys; w=wave.open(sys.argv[1]); print(w.getnframes()/w.getframerate(),'s')" \
  /data/debug/20260918-143205-123_ondemand_60s.wav
```

### Tests sans navigateur

```bash
node backend/tests/worklet.test.js        # worklet : pré-roll, post-roll, downmix
node backend/tests/static-wiring.test.js  # id HTML ↔ JS, variables CSS ↔ JS
```

---

## Architecture, en bref

```
Navigateur (poste extérieur)          Serveur (VM Linux)
┌────────────────────────────┐        ┌───────────────────────────────────┐
│ MediaStream                │        │ capture  :4466                    │
│  └─ AudioWorklet           │   WS   │  ├─ validation                    │
│      · downmix (L+R)/2     │ ─────► │  ├─ soxr → 16 kHz (no-op en 16 k) │
│      · ring 60 s           │ PCM    │  ├─ YAMNet, fenêtre par fenêtre   │
│      · RMS par trame       │ 16 kHz │  ├─ lameenc → MP3                 │
│  └─ médiane + déclenchement│ s16le  │  ├─ export/*.wav (écoute)         │
└────────────────────────────┘        │  └─ PostgreSQL ◄──┐               │
                                      ├───────────────────┼───────────────┤
Navigateur (toi)                      │ admin    :4467    │               │
┌────────────────────────────┐        │  └─ API REST ─────┘  (même base,  │
│ dashboard :4467/dashboard/ │ ─────► │     dashboard          même       │
└────────────────────────────┘        │     /media (MP3)       volume)    │
Navigateur (toi, sur place)           └───────────────────────────────────┘
┌────────────────────────────┐   WS   ┌───────────────────────────────────┐
│ listen :4467/listen/       │ ◄────► │ /ws/listen — diffuse le direct,   │
│  └─ Web Audio, 16 kHz      │  PCM   │  écrit le WAV, classe les fenêtres│
└────────────────────────────┘        └───────────────────────────────────┘
```

L'écoute est le **seul** chemin où le serveur renvoie de l'audio. Il le fait
depuis `capture`, jamais depuis `admin` : `export/` n'est monté que là, et le
classifieur non plus.

Le client n'envoie **que** les extraits candidats, en PCM brut **16 kHz**. Le
pré-roll d'1 s est correct par construction : il est déjà dans le ring buffer au
moment du déclenchement — et le ring fait 60 s, donc un pré-roll plus long ne
demanderait qu'un réglage.

Le 16 kHz n'est pas un compromis : YAMNet ne voit jamais au-dessus de 8 kHz, et
le MP3 archivé est encodé depuis le 16 kHz. Envoyer du 48 kHz revenait à jeter
les deux tiers de chaque envoi. `soxr → 16 kHz` reste dans le chemin parce qu'un
poste dont la page n'a pas été rechargée envoie encore du 48 kHz — la fonction
court-circuite alors, et le MP3 reste juste.

### Les épisodes : un enregistrement continu, pas des clips recollés

Quand une source sonore ne s'arrête pas, le client **streame** : il ouvre un
épisode, envoie l'audio au fil de l'eau, et le clôt après 30 s de silence. Le
serveur classe les fenêtres à la volée, rogne la tête et la queue, et produit
**un seul MP3** — ou détruit tout si aucune fenêtre n'est surveillée.

Le client envoie **16 kHz** et le serveur refuse tout autre taux sur ce chemin :
`soxr` est stateful, et l'appeler morceau par morceau introduirait une
discontinuité de filtre à chaque frontière — un clic par seconde, avec des
scores qui restent globalement bons.

**Pourquoi pas des clips de 3 s recollés côté serveur ?** Parce que deux clips
déclenchés indépendamment ne se touchent pas. Mesuré sur les enregistrements du
terrain : il manque **9 à 52 ms de son à chaque couture** — le déclencheur a une
gigue d'une trame et chaque clip couvre exactement sa durée nominale. Quarante
coutures dans un épisode de deux minutes feraient quarante micro-coupures, et
une pièce à conviction trouée n'en est plus une.

Le chemin des segments de 3 s **reste en place** : les `.js` sont servis par un
CDN qui les garde quatre heures, donc un client d'avant doit continuer à
fonctionner. Le client neuf bascule tout seul en mode segment si le serveur
répond `unknown_type` — sans ce repli, un rollback perdrait silencieusement
chaque déclenchement.

Décisions structurantes, et pourquoi :

- **YAMNet plutôt qu'Ollama** — Ollama sait transcrire de la parole, il
  n'expose aucun modèle de classification d'événements sonores.
- **`ai-edge-litert` plutôt que TensorFlow** — 21 Mo contre 3,2 Go. La machine
  est partagée avec un autre projet.
- **PCM brut plutôt que MediaRecorder** — un `MediaRecorder` continu ne met
  l'en-tête WebM que dans le premier chunk, et ne sait pas produire de pré-roll.
- **Pas de ffmpeg** — `lameenc` (0,2 Mo, zéro dépendance) encode, `soxr`
  rééchantillonne, on ne décode jamais.
- **Décider avant d'encoder** — un refus n'écrit rien du tout, ce qui économise
  un encodage, une écriture et un `unlink` sur chaque faux positif.

Le document de conception complet est dans [`historic/state.md`](historic/state.md).
