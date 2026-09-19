# Aboigramme — Document de conception et de reprise

> **Date** : 18 septembre 2026
> **État** : **implémenté, déployé, et utilisé sur le terrain.** Les huit étapes du §11
> sont faites, plus l'écoute directe (§6.1-6.3) et le panneau d'analyse (§18).
> ⚠️ **Le §17 liste ce que l'implémentation a corrigé dans ce document.** Plusieurs
> affirmations de la conception se sont révélées fausses à la mesure — les lire avant de
> s'appuyer sur les sections concernées.

---

## 0. État et reprise

Le service tourne : `docker compose up -d --build`, puis `/dashboard/`, `/listen/`,
`/client/`.

**Deux hôtes, et ce n'est pas du rangement.** `noisy` (admin, 4467) sert tout ce que
l'humain regarde — dashboard, écoute directe, API. `noisymic` (capture, 4466) ne sert que
le poste de terrain, et **ne doit jamais être public**. L'écoute a donc été déplacée de
l'un à l'autre le 18/09, ce qui a demandé deux relais (§18.3) — un pour l'analyse, un pour
le WebSocket du direct. Un test garde l'invariant : l'hôte privé ne doit apparaître nulle
part dans ce qui est servi depuis `noisy`, pas même dans un commentaire HTML.

**Ce qui reste à faire n'est pas du code :**

1. **Le test d'acceptation décisif** (§13.7) : taper dans ses mains depuis un vrai
   navigateur. Fait ici sur signaux synthétiques, pas à la main.
2. **Le soak de 24 h** (§13.11).
3. **Trancher le sort des épisodes refusés** — les garder tous pour les analyser soi-même,
   ce qui coûte 50 à 130 Mo par jour (§18.5). Trois options, aucune retenue.

**Sur la calibration** — longtemps le point ouvert n°1 : **elle est largement faite.** Le
18/09, quatre enregistrements réels du terrain ont été passés au modèle : le pire négatif
vaut **0,0312**, le positif **0,8516**. Le seuil est passé de 0,35 à **0,25**, au milieu
d'un écart de 27×. Ce n'est plus une extrapolation depuis des négatifs synthétiques.
Réserve : ces fichiers sont des dumps d'*épisodes*, donc le détecteur RMS avait déclenché
dessus — `samples/negative/` au sens strict (du calme sans rien) n'est toujours pas
couvert.

**Décisions déjà arbitrées avec l'utilisateur — ne pas les rouvrir :**

| Sujet | Décision |
|---|---|
| Moteur de classification | **YAMNet**, pas Ollama |
| Stack dashboard | **HTML/JS + Chart.js**, pas Svelte |
| Réseau | **HTTP sans reverse proxy** dans le compose (l'utilisateur a déjà son Caddy) |
| Périmètre | **MVP propre et exécutable** — pas d'Alembic, pas de CI, pas de suite pytest |
| Analyse d'un sample | **dans le conteneur**, pas sur le GPU de l'utilisateur (§18.2) |
| Écoute directe | **sur `noisy`**, jamais sur `noisymic` |
| Modale d'analyse | **affichage seul** — rien en base, rien au compteur |

---

## 1. Le besoin

Un vieux PC Windows en extérieur, allumé H24, capte le son via son micro. Une VM Linux
(sans passthrough CUDA) reçoit les extraits, les qualifie et alimente un dashboard
d'historisation.

L'architecture hybride a un vrai intérêt : le client léger fait le **pré-tri** (détection de
bruit, très peu de CPU) et n'envoie que les extraits candidats ; le serveur lourd fait la
**qualification** (coûteuse) et l'**historisation**. On ne streame donc jamais d'audio en
permanence — ni réseau ni RAM saturés.

---

## 2. Deux corrections à la demande initiale

### 2.1 Ollama ne sait pas classifier un son

Ollama v0.20 a bien ajouté l'audio (Gemma 4, encodeur USM), mais **uniquement pour de la
transcription parole→texte**. Aucun modèle de classification d'événements sonores n'est
exposé. Envoyer un spectrogramme à un VLM serait lent, cher et non fiable.

→ On utilise **YAMNet** (modèle AudioSet de Google, classes `Dog` et `Bark` natives), derrière
une interface `ClassifierBackend` pour que le pont GPU distant reste branchable plus tard sans
toucher au reste (`classifier/remote_http.py`).

### 2.2 `getUserMedia` exige un contexte sécurisé — le réseau local n'y change rien

Chrome et Firefox ne donnent le micro que sur `https://` ou `localhost`. Depuis le PC en
`http://<ip>:4466`, **`navigator.mediaDevices` est `undefined`** : l'échec est total, pas
partiel. Et `AudioWorklet` est lui aussi soumis au contexte sécurisé.

**Piège** : `try { await navigator.mediaDevices.getUserMedia(...) } catch {}` lève un
`TypeError` qui est attrapé par le `catch` et affiché comme « permission refusée » — ce qui
blâme le microphone pour un problème d'origine.

→ Décision retenue : **pas de service reverse proxy dans le compose**. En contrepartie :
l'UI fait un **préflight** (`window.isSecureContext` + `!!navigator.mediaDevices`) et affiche
un bandeau nommant l'origine exacte à autoriser ; le README documente le flag Chrome et un
Caddyfile prêt à coller.

**À savoir** : `ScriptProcessorNode` n'est **pas** un contournement. Il n'est certes pas
soumis au contexte sécurisé, mais il est inutile ici : si l'origine n'est pas sécurisée, on
n'obtient jamais de `MediaStream` à lui donner. Il ne sert que de repli pour un Chrome
antérieur à la v66 (avril 2018) — un problème d'**âge de navigateur**, pas de contexte.

**Un certificat auto-signé ne suffit pas pour Chrome.** La voie HTTPS exige un vrai nom
(`aboigramme.lan` + `tls internal` dans Caddy + import du CA racine dans le magasin de
certificats Windows).

### 2.3 Découverte bloquante : le port 5432 est déjà pris

`calendrier-db-1` publie `0.0.0.0:5432->5432/tcp` sur cet hôte **en ce moment** (projet sans
rapport, en service). Le service `db` ne doit **publier aucun port** — `ports: ["5432:5432"]`
ferait échouer `docker compose up` avec `address already in use`, une erreur qui ressemble à un
bug Postgres et coûte une heure à diagnostiquer.

Ports 5432 et 8100 occupés ; **8000 et 8080 sont libres**.

---

## 3. Faits vérifiés en direct (pas supposés)

Tout ce qui suit a été vérifié en interrogeant les endpoints réels le 17/09/2026.

### 3.1 YAMNet : où est réellement le `.tflite`

| Endpoint | Résultat |
|---|---|
| `tfhub.dev/google/yamnet/1` | 302 → page HTML Kaggle, **pas** un modèle |
| `tfhub.dev/google/yamnet/1?tf-hub-format=compressed` | 302 → tarball signé GCS — fonctionne |
| `storage.googleapis.com/tfhub-modules/google/yamnet/1.tar.gz` | **403** — miroir mort |
| `kaggle.com/api/v1/models/google/yamnet/tensorFlow2/yamnet/1/download` | 200, 14,2 Mo, **sans authentification** |
| `tfhub.dev/google/lite-model/yamnet/classification/tflite/1?lite-format=tflite` | 302 → URL **signée expirant en 3 h** |
| `storage.googleapis.com/mediapipe-models/audio_classifier/yamnet/float32/1/yamnet.tflite` | **200, 4 126 810 octets — la source retenue** |

**Le tarball SavedModel tfhub/Kaggle ne contient PAS de `.tflite`.** Inventaire vérifié par
`tar -tzv` : `assets/yamnet_class_map.csv`, `saved_model.pb`, `variables/…` — rien d'autre.
La croyance « télécharger le tarball tfhub pour récupérer `yamnet.tflite` » est fausse.

> ⚠️ **Corrigé — voir §17.1.** Les noms de tenseurs annoncés plus bas
> (`audio_clip` / `scores`, signature `yamnet/classification`) décrivent la variante
> **tfhub-lite**, pas le fichier réellement épinglé. Celui-ci déclare
> `waveform_binary` de forme **`(15600,)`, rang 1** — et **aucune signature TFLite**.

**D'où le bake au build** depuis l'URL GCS stable (non signée), avec vérification SHA-256 :

```
yamnet.tflite            4 126 810 o   sha256 4d8b4a53282dc83ef04e3e7dbc4fbc98082e34e44ed798e16c3a0cdd4c584faf
yamnet_class_map.csv        14 096 o   sha256 cdf24d193e196d9e95912a2667051ae203e92a2ba09449218ccb40ef787c6df2
```

Class map épinglée par hash :
`raw.githubusercontent.com/tensorflow/models/dfffd623b6be8d1d9744b8e261fbac370d17c46d/research/audioset/yamnet/yamnet_class_map.csv`
(dernier commit touchant le fichier : 2019 — gelé depuis 7 ans, mais on épingle quand même).

En-tête du flatbuffer lue directement : `TFL3`, `min_runtime_version 1.3.0`, tenseur d'entrée
`audio_clip`, tenseur de sortie `scores`, signature `yamnet/classification`. Aucun op custom.

### 3.2 Indices de classes — résolus par nom, jamais codés en dur

521 classes (522 lignes avec l'en-tête). **L'index 0 est `Speech`, pas `Animal`.**

| Index | MID | Nom |
|---|---|---|
| 67 | `/m/0jbk` | Animal |
| 68 | `/m/068hy` | Domestic animals, pets |
| **69** | `/m/0bt9lr` | **Dog** |
| **70** | `/m/05tny_` | **Bark** |
| 71 | `/m/07r_k2n` | Yip |
| 72 | `/m/07qf0zm` | Howl |
| 73 | `/m/07rc7d9` | Bow-wow |
| 74 | `/m/0ghcn6` | Growling |
| 75 | `/t/dd00136` | Whimper (dog) |

On fait `names.index("Bark")` / `names.index("Dog")` au démarrage et on log un **WARNING** si
ça ne vaut pas 70/69. Comme ça un fichier de labels différent fonctionne quand même.

### 3.3 Runtime : `ai-edge-litert`, pas TensorFlow

- `tflite-runtime` est une **impasse** : dernier wheel x86_64 en **cp39**. Aucun wheel cp311.
- `ai-edge-litert==2.2.0` a bien un `cp311-manylinux_2_27_x86_64.whl` de **21,3 Mo**, sans
  TensorFlow. Import : `from ai_edge_litert.interpreter import Interpreter`.
- **Image finale ~500 Mo au lieu de ~3,2 Go.** La machine est partagée avec un autre projet
  (`calendrier`), donc c'est aussi une question de bon voisinage.

### 3.4 Encodage MP3 sans ffmpeg

`lameenc==1.8.4` : wheel de 0,2 Mo, **zéro dépendance**, LAME compilé dedans. Le
rééchantillonnage est fait par `soxr`. Le décodage est inutile (le client envoie du PCM brut).
→ **Pas de `apt-get install ffmpeg`**, qui coûterait ~350 Mo.

Le seul cas qui forcerait ffmpeg : accepter de l'Opus/WebM depuis un client. À documenter
comme tel, pour qu'un futur changement de transport ne casse pas silencieusement l'encodage.

---

## 4. Arborescence cible

> ⚠️ **Mis à jour — voir §17.8.** Le compose déclare désormais **deux services**
> (`capture` sur 4466, `admin` sur 4467) construits sur la même image, et distingués par
> `APP_ROLE`. L'arborescence des fichiers ci-dessous est inchangée ; c'est le nombre de
> processus qui a changé.

```
noisy/
├── docker-compose.yml
├── .env  .env.example  .gitignore  .dockerignore  README.md
├── claude/claude.md                          ← ce document
├── export/                                   # bind mount capture+admin, les WAV d'écoute
├── docs/
│   ├── CADDY.md                              # Caddyfile + import du CA racine (doc seule)
│   └── CHROME_INSECURE_ORIGIN.md             # procédure chrome://flags, 2 formes
└── backend/
    ├── Dockerfile  requirements.txt  .dockerignore
    ├── models/.gitkeep                       # yamnet.tflite + csv téléchargés AU BUILD
    ├── tests/                                # node, sans framework
    │   ├── worklet.test.js                   # bac à sable vm, transfert réel des buffers
    │   └── static-wiring.test.js             # id HTML ↔ JS, cssVar ↔ CSS, invariants
    ├── app/
    │   ├── __init__.py  main.py  config.py  logging_setup.py  db.py  schemas.py
    │   ├── classifier/
    │   │   ├── base.py                       # ClassifierBackend, ClassificationResult
    │   │   ├── yamnet_litert.py              # backend par défaut + score_matrix(521)
    │   │   ├── remote_http.py                # pont GPU distant (non sélectionné)
    │   │   │                                 #  + analyze_timeline_remote (multipart)
    │   │   └── factory.py
    │   ├── analysis/timeline.py              # timeline d'un fichier, FONCTION PURE
    │   ├── audio/{pcm.py, resample.py, mp3.py, wav.py}
    │   ├── ws/
    │   │   ├── protocol.py  session.py  routes.py
    │   │   ├── hub.py                        # diffusion vers les auditeurs (drop-oldest)
    │   │   ├── listener.py                   # session de l'opérateur (/ws/listen)
    │   │   └── proxy.py                      # relais du direct, admin → capture
    │   ├── api/{health.py, events.py, stats.py, ondemand.py}
    │   ├── storage/{media.py, ondemand.py}   # WavSpool, index sha256, élagage
    │   └── tools/
    │       ├── selftest.py                   # test du modèle SANS navigateur
    │       ├── wstest.py                     # protocole WebSocket, contre un serveur
    │       └── timelinetest.py               # timeline — sans modèle ni réseau
    └── static/
        ├── vendor/chart.umd.min.js           # Chart.js 4.5.1, vendu au build
        ├── client/{index.html, app.js, recorder-worklet.js, style.css}
        ├── listen/{index.html, listen.js, timeline.js, style.css}
        └── dashboard/{index.html, dashboard.js, style.css}
```

`static/` vit sous `backend/` : un seul contexte de build, une seule image.

`export/` est monté sur **les deux** services, mais **un seul écrit** : le balayage des
`.part` orphelins reste sur capture (`main.py`), sans quoi les deux se disputeraient les
mêmes fichiers au démarrage.

---

## 5. Décisions d'architecture

### 5.1 Transport audio : PCM brut, pas MediaRecorder

Avec un `MediaRecorder` continu, **seul le premier chunk porte l'en-tête WebM/EBML**
(`EbmlHeader`, `Segment`, `Info`, `Tracks`) ; les suivants sont des `Cluster` bruts,
indécodables isolément. Et surtout il **ne peut pas produire le pré-roll d'1 seconde** :
aucune API ne permet de demander « les N dernières ms ». Le contourner imposerait un second
enregistreur toujours actif — deux pipelines à maintenir.

Le PCM brut gagne parce que **le pré-roll est correct par construction** : il est déjà dans le
ring buffer.

- `Int16LE`, mono, à la fréquence **native** de l'`AudioContext`.
- 3 s @ 48 kHz = **288 000 octets**. La limite `ws_max_size` d'uvicorn est de 16 777 216 par
  défaut → 58× de marge. On ne la relève pas : elle sert de plafond de sécurité.
- Côté serveur, **aucun décodeur** : `np.frombuffer(payload,'<i2')` → `/32768.0` →
  `soxr.resample(x, sr, 16000)` → YAMNet.
- On envoie la fréquence native, **pas** un 16 kHz rééchantillonné côté client : le
  rééchantillonneur de Chrome est de qualité variable selon la plateforme, `soxr` non.

### 5.2 Décider AVANT d'encoder — écart assumé au cahier des charges

Le brief dit « convertir en MP3, puis si le score ne valide pas, supprimer le fichier ».
**On inverse** : on encode seulement si accepté.

Le comportement observable est identique (aucun MP3 ne reste sur disque pour un rejet) mais on
économise un encodage + une écriture + un `unlink` sur **chaque** faux positif — soit la
majorité des déclenchements par jour de vent.

Le chemin littéral « écrire puis supprimer » existe derrière `SAVE_REJECTED=true`, qui écrit un
**WAV et non un MP3** : le but est de régler le seuil, un ré-encodage lossy fausserait
l'analyse.

### 5.3 Framing YAMNet

Le `.tflite` est figé à **15 600 échantillons** (0,975 s @ 16 kHz), contrairement à la version
TF qui fenêtre en interne. On réplique le hop natif de 0,48 s : fenêtres aux offsets
`0, 7800, 15600, …`, dernière fenêtre partielle **complétée par des zéros**.

3 s @ 16 kHz (48 000 échantillons) → `1 + floor((48000-15600)/7800)` = **5 fenêtres**.

L'`Interpreter` **n'est pas thread-safe** → `threading.Lock` + `ThreadPoolExecutor(1)` avec un
`UVICORN_WORKERS=1` obligatoire (plusieurs workers chargeraient chacun une copie du modèle).

**Normalisation de niveau optionnelle** : `if peak < 0.1: gain = min(0.5/peak, 10^(12/20))`.
Un micro extérieur derrière un bonnette sort souvent un pic à 0,02 et les scores s'effondrent.
Plafonnée à +12 dB pour qu'un clip quasi silencieux ne soit pas amplifié en faux positif.
**Attention** : ça déplace le point de fonctionnement, donc changer ce flag invalide un seuil
déjà réglé.

### 5.4 Décision : le score seuillé est le **max** sur les fenêtres, pas la moyenne

> ⚠️ **Corrigé — voir §17.2.** Le *max sur les fenêtres* est conservé, mais il porte sur le
> **groupe canin** (`Dog`, `Bark`, `Yip`, `Howl`, `Bow-wow`, `Growling`, `Whimper`), **pas
> sur la seule classe `Bark`**. Mesuré sur les enregistrements réels du terrain, `Bark`
> s'effondre à 0,262 là où `Dog` monte à 0,586 sur les **mêmes** segments : c'est le moins
> bon discriminateur du groupe, et la seule classe qui rate de vrais aboiements. La variable
> d'environnement s'appelle désormais `DOG_THRESHOLD` ; `bark_score` reste stockée en
> **diagnostic**, pour comparer les deux critères sur des données accumulées.

Un clip de 3 s contenant un aboiement de 0,5 s et 2,5 s de vent a un pic élevé et une moyenne
basse. Moyenner le rejetterait. On stocke `mean_dog_score` **en plus**, pour le réglage
ultérieur — c'est la colonne qui dira si 0,35 était le bon seuil.

### 5.5 Persistance et temps

`asyncpg` brut, **pas de SQLAlchemy** : cinq requêtes dans toute l'app, aucune valeur d'ORM à
en tirer. Ça économise une dépendance, un greenlet et une couche.

**Tout en `TIMESTAMPTZ`**, stockage UTC (`SET TIME ZONE 'UTC'` dans le callback `init=` du
pool) ; tout découpage horaire se fait explicitement avec `AT TIME ZONE $tz`, donc la réponse
**ne dépend pas** du `TZ` du conteneur — c'est tout l'intérêt.

`detected_at` est calculé **côté serveur** (`now() - post_roll`) : l'horloge du vieux PC Windows
est exactement le genre de chose qui dérive de plusieurs heures en silence.
`client_captured_at` est stocké à part, en **diagnostic seulement** — après un mois il dira si
l'horloge du PC a dérivé, ce qui serait autrement invisible et corromprait le KPI « aboiements
du jour ».

### 5.6 Pas de CORS

Tout est same-origin (dashboard, page d'enregistrement, API, MP3 servis par le même process).
Il n'existe aucun scénario légitime de requête cross-origin dans le MVP. **Ne pas ajouter
`CORSMiddleware` « au cas où »** : une politique permissive sur un service qui accepte des
uploads audio est un vrai risque pour zéro bénéfice.

---

## 6. Protocole WebSocket

Chaque segment = **[une trame texte `segment_start`][une trame binaire PCM]**, dans cet ordre.
L'ordonnancement WebSocket garantit l'appairage, et les métadonnées restent lisibles dans
l'inspecteur — ce qui compte pour une machine qu'il faut aller déboguer dehors.

### Client → serveur

```jsonc
// 1. hello — premier message, obligatoire
{"type":"hello","protocol_version":1,"client_id":"poste-exterieur-01",
 "app_version":"1.0.0","user_agent":"…","device_sample_rate":48000}

// 2. segment_start — immédiatement avant la trame binaire
{"type":"segment_start","seq":142,"captured_at_ms":1758110591482,
 "sample_rate":48000,"channels":1,"format":"s16le","num_samples":144000,
 "rms":0.0312,"background_rms":0.0079,"trigger_ratio":3.95,"post_roll_ms":2000}

// 3. trame binaire — PCM brut s16le, sans en-tête ni compression
// 4. ping toutes les 15 s
{"type":"ping","t":1758110600000}
```

`num_samples` est **par canal** ; la trame binaire doit faire exactement
`num_samples * channels * 2` octets.

### Serveur → client

```jsonc
// hello_ack — envoyé immédiatement à l'acceptation
{"type":"hello_ack","protocol_version":1,"server_version":"0.1.0",
 "session_id":"0f2c…","server_time_ms":1758110591490,
 "classifier":{"backend":"yamnet-litert","model":"yamnet.tflite",
               "bark_index":70,"dog_index":69,"threshold":0.35,
               "window_samples":15600,"hop_samples":7800},
 "limits":{"max_segment_bytes":1048576,"min_sample_rate":8000,
           "max_sample_rate":96000,"max_segment_ms":10000,"max_pending":4},
 "config_patch":{"cooldown_ms":3000,"trigger_ratio":2.5,"min_rms_floor":0.004}}

// segment_result — exactement un par trame binaire, ACCEPTÉ OU NON
{"type":"segment_result","seq":142,"event_id":87,"accepted":true,
 "bark_score":0.871,"dog_score":0.402,"mean_bark_score":0.514,
 "top_classes":[["Bark",70,0.871],["Dog",69,0.402],["Animal",67,0.113]],
 "duration_ms":3000,"mp3_url":"/media/2026/09/17/000087.mp3","mp3_bytes":24112,
 "processing_ms":184,"reason":"bark_score_ok","server_time_ms":1758110591674}

// error
{"type":"error","seq":142,"code":"payload_too_large",
 "message":"segment is 2097152 bytes, limit is 1048576","fatal":false}
```

Codes d'erreur : `bad_json`, `unknown_type`, `bad_length`, `bad_sample_rate`, `bad_format`,
`payload_too_large`, `busy`, `decode_error`, `classify_error`, `internal`.
`fatal: true` **uniquement** pour un mismatch de `protocol_version` — le client s'arrête et
affiche une erreur permanente plutôt que de reboucler en reconnexion infinie.

`config_patch` permet de régler les seuils du client **sans redéploiement** — utile pour une
boîte qu'il faut aller visiter physiquement pour changer un curseur.

### 6.1 L'écoute à la demande — un SECOND vocabulaire, purement additif

`PROTOCOL_VERSION` **ne bouge pas**, et ce n'est pas une timidité : l'incrémenter tuerait
tous les clients en vol, c'est-à-dire exactement ceux que le cache du CDN sert encore. Le
chemin est additif au sens strict — un client d'avant **ignore `listen_request` en silence**
(`handleServerMessage` n'a pas de `else`), et ce silence est traité par un délai de 5 s côté
serveur, pas laissé au hasard.

**Descendant (serveur → poste)**

```jsonc
{"type":"listen_request","listen_id":"9f3c…","duration_ms":60000,
 "sample_rate":16000,"channels":1,"format":"s16le","chunk_ms":200,"max_chunks":519}
{"type":"listen_stop","listen_id":"9f3c…","reason":"duration"}   // ou operator_cancel
```

**Montant (poste → serveur)**

```jsonc
{"type":"listen_start","listen_id":"9f3c…","seq":4183,"sample_rate":16000,
 "channels":1,"format":"s16le","chunk_samples":3200}
// N trames binaires s16le
{"type":"listen_end","listen_id":"9f3c…","seq":4183,"chunks":300,"num_samples":96000,
 "stopped_reason":"duration"}
```

`listen_id` est **obligatoirement réémis** par le client — pas d'appariement implicite « le
prochain flux est l'écoute ». Entre l'ordre et la réponse, un `trigger` a parfaitement le
droit d'avoir eu lieu, et l'appariement implicite se tromperait une fois sur mille, d'une
façon indiagnosticable.

**Nouveaux types plutôt que réemploi de `stream_start`** : un épisode est jugé, rogné, et son
temporaire est JETÉ s'il n'y a pas de chien ; une écoute est PUBLIÉE en WAV quoi qu'il
arrive, et peut **en plus** produire un épisode. Deux finalisations différentes, donc deux
états différents — et l'exclusivité devient structurelle (`self._stream` / `self._listen`)
au lieu d'un drapeau à ne pas oublier de tester. Bénéfice secondaire : un rollback serveur
échoue **visiblement** (`unknown_type`), au lieu de transformer chaque écoute en épisode de
60 s archivé en silence.

### 6.2 Le canal opérateur (`/ws/listen`) — un protocole à part

```jsonc
// montant
{"type":"listen_begin","duration_ms":60000}
{"type":"listen_cancel"}
// descendant
{"type":"listen_started","listen_id":"…","remaining_ms":60000,"chunk_ms":200,"joined":false}
// trames binaires s16le 16 kHz
{"type":"listen_progress","received_ms":8000,"windows":16,"max_dog_score":0.79,
 "dropped_chunks":0,"dropped_windows":0}
{"type":"listen_ended","listen_id":"…","wav_name":"20260918-143205-123_ondemand_60s.wav",
 "reason":"duration","windows":123,"partial":false,"analysis":{…},"event_id":412,
 "mp3_url":"/media/2026/09/18/000412.mp3"}
```

Les deux vocabulaires sont **disjoints**, et c'est le but : l'opérateur n'est pas un client
de capture. Il ne fait pas de `hello`, il n'envoie jamais d'audio, et une session d'écoute ne
peut pas se faire passer pour un poste de terrain — l'usurpation est rendue *structurelle*
plutôt que *vérifiée*.

`listen_started` annonce la durée **réellement retenue** : si la demande sortait des bornes,
on l'annonce corrigée plutôt que d'échouer, sinon le compte à rebours affiché ment et
l'opérateur croit à une panne quand le flux s'arrête à l'heure prévue par le serveur.

Un second opérateur qui arrive pendant une écoute s'y **branche** (`joined: true`) au lieu de
la refuser : c'est gratuit, la diffusion sait déjà le faire, et un onglet oublié ne verrouille
plus la fonctionnalité pendant une minute.

### 6.3 Ce que le chemin d'écoute ne fait PAS

- **Pas de pré-roll.** Une seconde de pré-roll daterait le fichier avant son propre
  horodatage — un mensonge gratuit sur l'axe du temps, pour une prise de son dont l'intérêt
  est justement d'être datée.
- **Pas de condition de silence.** Voir §17.19 ①.
- **Pas de `segment_result` vers le poste.** Le poste n'a pas ouvert d'écoute, il n'attend
  aucun accusé ; lui en envoyer un afficherait « seq N accepté » sur le kiosque pour une
  action de l'opérateur, et laisserait un `inflight[seq]` orphelin. L'opérateur apprend le
  verdict par `listen_ended`.

### Backpressure

Au-delà de **4 segments en vol**, réponse `busy` et rejet du payload. Une boîte 24/7 par nuit
de vent peut produire plus vite qu'un classifieur CPU : une file silencieuse finirait en
croissance mémoire non bornée puis en OOM kill.

---

## 7. Base de données

DDL idempotent (`IF NOT EXISTS`), exécuté au démarrage via `lifespan`, en **un seul**
`pool.execute()`. Pas d'Alembic (choix utilisateur).

> ⚠️ `asyncpg` accepte plusieurs instructions séparées par `;` dans un `execute()`, **mais pas
> si elles contiennent des paramètres `$1`** — il bascule alors sur le protocole étendu et
> rejette le multi-instruction. Le DDL reste donc sans paramètres ; le timezone n'apparaît que
> dans les `SELECT`, paramétrés normalement.

```sql
CREATE TABLE IF NOT EXISTS schema_meta (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS events (
    id                 BIGSERIAL   PRIMARY KEY,
    detected_at        TIMESTAMPTZ NOT NULL,          -- instant serveur de l'aboiement
    received_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    client_captured_at TIMESTAMPTZ,                   -- horloge client : diagnostic only
    client_id          TEXT,
    client_seq         BIGINT,
    bark_score         REAL        NOT NULL,
    dog_score          REAL,
    mean_bark_score    REAL,
    duration_ms        INTEGER     NOT NULL,
    sample_rate        INTEGER     NOT NULL,
    mp3_path           TEXT        NOT NULL,
    mp3_bytes          INTEGER,
    backend            TEXT        NOT NULL DEFAULT 'yamnet-litert',
    model_version      TEXT,
    top_classes        JSONB,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT events_bark_score_chk CHECK (bark_score >= 0.0 AND bark_score <= 1.0),
    CONSTRAINT events_dog_score_chk  CHECK (dog_score IS NULL OR (dog_score >= 0.0 AND dog_score <= 1.0)),
    CONSTRAINT events_duration_chk   CHECK (duration_ms BETWEEN 100 AND 10000),
    CONSTRAINT events_rate_chk       CHECK (sample_rate BETWEEN 8000 AND 96000)
);

-- ré-ack idempotente après coupure socket : un retry ne duplique pas l'événement
CREATE UNIQUE INDEX IF NOT EXISTS events_client_seq_uniq
    ON events (client_id, client_seq) WHERE client_seq IS NOT NULL;

CREATE INDEX IF NOT EXISTS events_detected_at_desc ON events (detected_at DESC);
CREATE INDEX IF NOT EXISTS events_bark_score_idx   ON events (bark_score DESC);

INSERT INTO schema_meta (key, value) VALUES ('schema_version', '1')
ON CONFLICT (key) DO NOTHING;
```

`bark_score` est `REAL` (float4) : c'est exactement la précision d'un score YAMNet float32, il
n'y a aucune raison de payer pour un `double precision`.

Requête d'insertion correspondante :

```sql
INSERT INTO events (
    detected_at, received_at, client_captured_at, client_id, client_seq,
    bark_score, dog_score, mean_bark_score, duration_ms, sample_rate,
    mp3_path, mp3_bytes, backend, model_version, top_classes
) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
ON CONFLICT (client_id, client_seq) DO NOTHING
RETURNING id;
```

> ⚠️ **Corrigé — voir §17.3.** La clause ci-dessus est **incomplète et échoue telle quelle**.
> L'unicité `(client_id, client_seq)` est portée par un index **PARTIEL**
> (`WHERE client_seq IS NOT NULL`) : sans répéter ce prédicat, PostgreSQL ne trouve aucun
> index correspondant et rejette la requête avec « there is no unique or exclusion
> constraint matching the ON CONFLICT specification ». La forme correcte est :
> `ON CONFLICT (client_id, client_seq) WHERE client_seq IS NOT NULL DO NOTHING`.
>
> L'implémentation réserve en outre l'identifiant **avant** l'insertion
> (`SELECT nextval('events_id_seq')`), parce que le nom du MP3 en dépend — d'où une colonne
> `id` explicite dans l'INSERT, et non plus le `RETURNING id` seul.

Un résultat vide = `seq` dupliqué → relire l'id existant et ré-acquitter.

**Pas d'index d'expression sur `date_trunc('hour', detected_at AT TIME ZONE 'Europe/Paris')`.**
Il *serait* valide (`timezone(text, timestamptz)` est `IMMUTABLE`), mais le fuseau serait figé
dans l'index et le volume est de quelques centaines de lignes par jour. À ajouter seulement si
une requête devient réellement lente.

**Croissance disque** : ~24 Ko par événement en 64 kbps mono. Pire cas réaliste ~1 000
événements/jour ≈ 24 Mo/jour ≈ **8,8 Go/an**. Le garde-tempête côté client borne l'explosion.
À documenter + fournir une requête de rétention, même si le MVP ne livre pas d'ordonnanceur.

---

## 8. API REST

Toutes sous `/api`, same-origin. Temps rendus dans le `tz` demandé (défaut `APP_TZ`).

| Endpoint | Rôle |
|---|---|
| `GET /api/health` | Liveness, utilisé par le healthcheck du compose |
| `GET /api/events?from&to&tz&limit&offset&min_score` | Liste paginée, tri `detected_at DESC` |
| `GET /api/events/{id}` | Un événement, ou 404 |
| `DELETE /api/events/{id}` | Purge d'un faux positif évident, supprime le MP3 |
| `GET /api/stats/summary?date&tz` | KPI du jour |
| `GET /api/stats/histogram?from&to&tz` | 24 buckets, fenêtre arbitraire |
| `GET /api/stats/daily?from&to&tz` | Tendance quotidienne |
| `GET /api/stats/heatmap?from&to&tz` | Grille jour-de-semaine × heure |
| `GET /api/stats/timeline?from&to&tz&max_points` | Points cliquables de la timeline |
| `GET /media/{path}` | Statique, `audio/mpeg`, cache immutable |

**`/api/health` ne touche pas au classifieur** (qui est derrière un lock et pourrait être en
plein calcul). Il renvoie `degraded` en **HTTP 200** plutôt qu'un 503 si la DB ou le disque
pose problème : un 503 ferait redémarrer le conteneur en boucle sur un hoquet transitoire,
alors que c'est au dashboard d'afficher la dégradation.

`GET /api/stats/timeline` renvoie les **N plus forts** avec `truncated: true` quand la plage
dépasse `max_points`. Renvoyer les N premiers chronologiquement n'afficherait que janvier et
laisserait croire que le reste de l'année est vide.

**Moyenne par heure** : `barks_today / heures_écoulées`, **pas `/24`**. Diviser par 24 à 9 h
fait paraître chaque matin calme et chaque soir alarmant. Le dénominateur est écrit dans le
sous-titre de la tuile.

**Tendance quotidienne** : `generate_series` + `LEFT JOIN` pour **inclure les jours à zéro**.
Une courbe qui saute les jours calmes trace une ligne droite au-dessus du trou et ment sur la
forme.

```sql
SELECT d::date AS date, coalesce(count(e.id), 0) AS count
FROM generate_series($1::date, $2::date, '1 day') d
LEFT JOIN events e ON timezone($3, e.detected_at)::date = d::date
GROUP BY d ORDER BY d;
```

**Ordre des mounts** : `app.mount()` doit venir **après** les `include_router`. FastAPI matche
dans l'ordre d'enregistrement ; un mount trop tôt avale silencieusement les routes d'API.

---

## 9. Client navigateur

### 9.1 Les trois pièges silencieux

C'est la partie qui décide si tout le reste fonctionne. Aucun des trois ne produit d'erreur.

**① Les contraintes `getUserMedia`.** `echoCancellation`, `noiseSuppression` et
`autoGainControl` sont **tous à `true` par défaut dans Chrome**. L'AGC fait baisser le gain sur
une scène calme jusqu'à ce qu'un aboiement bouge à peine le vumètre ; la suppression de bruit
est entraînée à retirer précisément les transitoires non-parole — un aboiement, exactement.
Résultat : un client qui a l'air parfaitement sain et dont le compteur reste à zéro.

```js
navigator.mediaDevices.getUserMedia({ audio: {
    echoCancellation: false, noiseSuppression: false, autoGainControl: false,
    channelCount: 1, sampleRate: 48000
}});
```

C'est la première cause probable de « ça marchait au bureau et pas dehors ».

**② Ne jamais coder 48000 en dur.** `audioContext.sampleRate` vaut 48000 sur la plupart des PC
Windows mais **44100 sur beaucoup**, et 16/96 kHz sur certains périphériques USB. Le client
envoie la valeur réelle, le serveur rééchantillonne depuis celle-là. Sinon l'audio est transposé
d'environ 9 % et **tout classe comme du bruit** — sans erreur ni exception.

**③ Le downmix mono dans le worklet.** Prendre `input[0][0]` en aveugle donne du silence
numérique sur un micro stéréo câblé à droite. Toujours `(L+R)/2`, et demander `channelCount: 1`
comme indication sans s'y fier.

### 9.2 Détection RMS et médiane

- Trames de **1024 échantillons**, hop = trame (pas de recouvrement) → 21,33 ms, **46,875
  trames/s**.
- Le worklet accumule somme des carrés et pic sur chaque quantum de 128, et poste un
  `{rms, peak}` par trame (~60 octets, 47×/s — négligeable).
- Le **thread principal** tient un `Float32Array(512)` (512 / 46,875 = **10,92 s**).
- **Médiane exacte, pas d'approximation** : copier le préfixe rempli dans un scratch
  `Float32Array`, `.sort()` (numérique par défaut, pas de piège de comparateur), prendre le
  milieu. ~30 µs pour 470 floats, soit **0,14 % d'un cœur**. Recalculée **1 trame sur 8**
  (~170 ms) et mise en cache entre-temps → encore 8× moins. La médiane d'une statistique de
  10 s rafraîchie 6×/s au lieu de 47×/s ne change rien.
- Mémoire **fixe : 4 Ko, pour toujours**. Pas d'historique, pas de croissance.
- L'approche par histogramme (256 bins log) n'est **pas** à construire d'avance ; elle ne
  servirait que si un profilage montrait que le tri coûte.

### 9.3 Règle de déclenchement, avec les garde-fous

```js
const bg     = median(ring);              // médiane des ~470 slots remplis
const ready  = frameCount >= 10 * 46.875; // warm-up complet de 10 s
const usable = bg >= 1e-5;                // GARDE-FOU MICRO MUET
const hot    = rms > Math.max(bg * 2.5, MIN_RMS_FLOOR);  // MIN_RMS_FLOOR = 0.004
const armed  = armedForNext;              // hystérésis de front descendant
```

**Le garde-fou `bg >= 1e-5` est indispensable.** Sans lui, un micro muet ou débranché donne
une médiane à 0, donc `rms > 0` est vrai sur n'importe quel échantillon non nul et **le client
déclenche en continu** sur du dither. Le warm-up de 10 s empêche en plus que l'estimateur
déclenche sur son propre remplissage.

### 9.4 Débounce — trois couches, toutes nécessaires

1. **Verrou de capture** : de déclenchement jusqu'à `pré + post` (3 s), aucun nouveau
   déclenchement accepté. Sans lui, un seul aboiement génère 4 à 6 segments qui se chevauchent.
2. **Cooldown** de 3 s après la fin d'une capture, mesuré depuis le *début* de capture (donc
   1 s de temps mort effectif après le post-roll). Les aboiements à moins de 3 s d'écart
   fusionnent en un seul « épisode », dont le clip de 3 s les contient tous — c'est l'unité
   sémantiquement utile.
3. **Garde-tempête** : compteur glissant sur 60 s ; au-delà de **12 événements/min**,
   suspension 60 s + indicateur d'état. C'est ce qui protège le serveur quand une alarme de
   voiture ou un souffleur de feuilles s'installe dehors pour dix minutes. Assurance peu
   coûteuse pour une boîte non surveillée.

Optionnel : exiger que le RMS retombe sous `bg * 1.5` entre deux événements (vraie hystérésis),
sinon la boîte se réarme sur la queue décroissante du même bruit. 3 lignes, activé par défaut.

### 9.5 Ring buffer du worklet

**4 secondes, pas 3.** Deux designs possibles : (a) geler le ring au déclenchement et
collecter le post-roll dans un buffer séparé ; (b) un seul ring ≥ 3 s relu en fenêtre.
**(a) est retenu** — aucune arithmétique de wrap-around, et impossible de relire des
échantillons que le curseur d'écriture a déjà écrasés. Concrètement : 2 s de ring (contient le
pré-roll d'1 s avec marge) + 2 s de buffer de post = 4 s de `Float32Array` ≈ 768 Ko. Trivial,
et l'argument de correction l'est aussi.

**Le worklet possède le buffer, pas le thread principal.** Pas de `SharedArrayBuffer` : il
exigerait un contexte sécurisé **et** l'isolation cross-origin via en-têtes COOP/COEP, et
`postMessage()` **lève une exception** pour un SAB sans ces en-têtes. Le transfert se fait une
fois par événement : `postMessage({…, pcm: i16}, [i16.buffer])` — zéro-copie, ~288 Ko cédés.

Le worklet **n'alloue jamais** dans `process()` (les allocations du thread audio causent des
pauses GC et des glitches audibles, donc des échantillons perdus), **retourne toujours `true`**
(retourner `false` démonte le nœud définitivement), et gère `inputs[0]` vide.

**Mort silencieuse du worklet** : une exception dans `process()` tue le processeur ; la page
garde son indicateur « écoute » et aucun audio n'arrive. → try/catch qui poste une erreur, plus
un **watchdog** côté thread principal : si aucun message `rms` n'est arrivé depuis 2 s alors que
le contexte est `running`, on démonte et on relance le graphe audio.

### 9.6 Reconnexion et robustesse

- Backoff exponentiel `min(1000 * 2^n, 30000)` avec gigue ±20 % — la gigue ne sert pas pour un
  client unique, mais pour la tempête de reconnexions après un redémarrage serveur.
- File de segments : **20 max** (≈5,8 Mo) en cas de socket fermée, vidée à la reconnexion,
  **plus ancien d'abord, en jetant les plus anciens** quand c'est plein. Une file non bornée
  ferait OOM l'onglet après une longue panne ; jeter les plus récents perdrait les événements
  les plus intéressants.
- `ws.binaryType = 'arraybuffer'` : le défaut est `'blob'`, et l'oublier rend tous les tests
  `instanceof ArrayBuffer` silencieusement faux.
- `track.onended` / `stream.oninactive` : perte de micro (USB débranché, accès révoqué) →
  ré-acquisition avec backoff. Une boîte extérieure non surveillée verra ça.
- `audioContext.onstatechange` : si le contexte passe `suspended` (politique d'autoplay après
  redémarrage du navigateur, ou audio suspendu par l'OS), appeler `resume()` et **loguer
  fort**. Si `resume()` échoue en boucle, bandeau exigeant un clic.

### 9.7 Interface

Ultra-minimaliste, comme demandé : un bouton `Démarrer` / `Arrêter` (120×60 px minimum), une
bande de quatre pastilles d'état (*Micro*, *WebSocket*, *Capture*, *Appareil*) — chacune avec
**point coloré + texte**, jamais la couleur seule —, un relevé live RMS / médiane de fond /
ratio en monospace rafraîchi 5×/s, un compteur d'événements envoyés/acceptés/refusés, et un
journal des 12 dernières lignes.

Le relevé live est **l'accessoire de débogage le plus utile** quand quelqu'un est dehors avec un
portable : on voit immédiatement si le seuil est sensé. Le journal compte parce que l'opérateur
n'ouvrira pas la console de développement.

Un `<audio controls preload="none">` permet d'écouter le dernier MP3 accepté — vérifie la
qualité audio de bout en bout sans passer par le dashboard.

**Préflight avant tout le reste** : `window.isSecureContext` + `!!navigator.mediaDevices`. En
cas d'échec, bandeau nommant `window.location.origin` et lien vers
`/docs/CHROME_INSECURE_ORIGIN.md`.

---

## 10. Dashboard

Chart.js **vendu au build** dans `static/vendor/chart.umd.min.js` (4.5.1, ~209 Ko) — pas de
CDN : le vieux PC derrière une box grand public est exactement la machine la plus susceptible
de ne pas avoir de DNS fonctionnel.

### 10.1 Forme choisie avant la couleur

| Besoin | Forme | Couleur | Pourquoi pas l'alternative |
|---|---|---|---|
| Chaque point = un aboiement, cliquable | **scatter**, x = temps, y = score | une seule teinte | Une ligne impliquerait une continuité entre événements discrets ; et un dégradé sur la teinte doublerait l'encodage du score déjà porté par y |
| Pics de nuisance par heure | **colonnes**, 0–23 | séquentiel, une teinte | Catégorie ordonnée → séquentiel correct ; un camembert de 24 parts est illisible |
| Tendance quotidienne | **ligne** 2 px | séquentiel | Une seule série → **pas de légende** ; le titre de la carte la nomme |
| Chiffres clés | **tuiles + un chiffre héros** | — | Une barre unique pour « aboiements du jour » est un anti-pattern : le nombre *est* le graphique |

### 10.2 Timeline

- `type:'scatter'`, `parsing:false`, données `{x: detected_at_ms, y: bark_score}`.
- Axe x `type:'linear'` avec `ticks.callback` formatant les ms via `Intl.DateTimeFormat`.
  **Pas d'adaptateur de date** : `chartjs-adapter-date-fns` fonctionne mais tire une seconde
  dépendance ; un axe linéaire + callback `Intl` produit un rendu identique pour un scatter à
  une infobulle par point, et reste honnête sur un filtre de plage (ce qu'un axe temps à
  saut automatique n'est pas).
- Axe y **épinglé à `[0,1]`** : le score du modèle est borné, et une hauteur de point doit
  vouloir dire le même score quel que soit le filtre. Un axe y auto-adapté fait paraître un
  jour calme comme un jour bruyant.
- `pointRadius: 5`, `pointBorderColor: surface`, `pointBorderWidth: 2` — l'anneau de 2 px
  garde les événements superposés lisibles.
- Clic → `getElementsAtEventForMode(e,'nearest',{intersect:false,radius:24},true)`. La cible de
  **24 px** est ce qui rend les points réellement cliquables — et c'est l'action principale de
  la page. Un point de 8 px est une cible inutilisable.
- Seuil `BARK_THRESHOLD` tracé comme une **hairline pleine** (jamais pointillée : le pointillé
  se lit comme « projection »), avec un label direct `seuil 0,35`.

### 10.3 Autres graphiques

- **Histogramme horaire** : 24 colonnes ≤ 24 px, sommet arrondi 4 px, base carrée, écart de
  2 px. L'heure de pointe est mise en évidence par la couleur + un label direct sur son
  sommet. C'est de l'**emphase**, la bonne réponse à « rendre le pic évident » — pas un dégradé
  sur les 24 barres, qui brûlerait le canal libre pour ré-encoder ce que la hauteur dit déjà.
- **Tendance quotidienne** : `tension:0.25`, `pointRadius:0` sauf le dernier point, `fill` à
  **10 % d'opacité** (un lavis, jamais un aplat saturé).
- **Heatmap** : un `<table>` HTML avec paliers séquentiels, **pas** de plugin `chartjs-chart-matrix`.
  C'est accessible par construction, ça double comme vue tableau, et ça évite une dépendance.
  Couleur du texte choisie sur la luminance du fond pour passer le contraste.
- **Thème** : `cssVar()` lit la propriété calculée, le toggle `data-theme` appelle
  `chart.update()`. **Ne jamais coder une hex dans une config Chart.js**, sinon le thème sombre
  sera à moitié appliqué. Les valeurs sombres sont déclarées sous `prefers-color-scheme`
  (garde `:where(:not([data-theme="light"]))`) **et** sous `[data-theme="dark"]`.
- État vide explicite (« Aucun aboiement sur cette période »), pas de canvas blanc. Pas de
  flash de skeleton au rechargement : on garde le rendu précédent à opacité réduite.
  `prefers-reduced-motion` respecté.
- **Aucun double axe nulle part.** Score (0–1) et comptage (0–N) ne partagent jamais un graphe.

Tous les graphiques sont **mono-série**, donc le problème d'adjacence daltonienne ne se pose
pas. **Si une deuxième série apparaît un jour** (par client, par type d'aboiement), cette
garantie tombe et la palette devra être validée avant livraison — à noter en commentaire dans
le code pour que personne n'ajoute une seconde série à la légère.

---

## 11. Ordre d'implémentation

1. `docker-compose.yml`, `.env.example`, `Dockerfile`, `requirements.txt` — et vérifier que
   `db` devient `healthy` **avant** d'écrire l'app.
2. `config.py`, `logging_setup.py`, `db.py` (+ DDL), `schemas.py`.
3. `audio/` puis **`tools/selftest.py`** → ⛔ **point d'arrêt volontaire : le modèle doit
   sortir un top-5 correct avant d'écrire la moindre ligne de WebSocket.**
4. `classifier/` : `base.py`, `yamnet_litert.py`, `factory.py`, `remote_http.py`.
5. `ws/` : `protocol.py`, `session.py`, `routes.py` — puis `api/`.
6. `static/client/` — **le worklet d'abord**, testable seul à l'oreille.
7. `static/dashboard/`.
8. `README.md` + `docs/`.

---

## 12. Fichiers de configuration

### `requirements.txt`

```
fastapi==0.141.1
uvicorn==0.53.0
websockets==17.1
pydantic==2.13.5
pydantic-settings==2.15.0
asyncpg==0.31.0
numpy==2.4.6
ai-edge-litert==2.2.0
soxr==1.1.0
lameenc==1.8.4
```

Tous vérifiés comme ayant un wheel cp311/manylinux x86_64 (ou pure-Python).
`uvicorn` **sans** `[standard]` : celui-ci tire uvloop, httptools, watchfiles et python-dotenv ;
seul `websockets` sert, il est listé explicitement.

**Si `ai_edge_litert` lève une erreur d'ABI NumPy** → repli sur `numpy==1.26.4` (wheel cp311
vérifié). `ai-edge-litert` déclare `numpy>=1.23.2` sans plafond.

Délibérément **absents** : `tensorflow`, `tensorflow-hub`, `tflite-runtime`, `torch`, `librosa`,
`scipy`, `pydub`, `sqlalchemy`, `alembic`, `pytest`, `aiofiles`.

### `docker-compose.yml` — points structurants

> ⚠️ **Mis à jour — voir §17.8.** Le bloc `api` unique ci-dessous est remplacé par deux
> services (`capture`, `admin`) partageant un ancrage YAML `x-api`, avec
> `APP_ROLE` en seule différence et `ports` publiés sur 4466 et 4467. Les points
> structurants commentés ici (pas de port sur `db`, `python` et non `curl` dans le
> healthcheck, `UVICORN_WORKERS=1`, volumes séparés) restent tous valides et s'appliquent
> aux deux services.

```yaml
name: aboigramme                    # le dossier s'appelle « noisy », on ne veut pas que ça
                                    # fuie dans les noms de réseau/volume, ni collision
                                    # avec le projet « calendrier » en cours
services:
  db:
    image: postgres:15-alpine       # DÉJÀ EN CACHE sur l'hôte → pull gratuit
    # PAS DE `ports:` — voir §2.3, le 5432 de l'hôte est pris
    environment:
      POSTGRES_USER: aboigramme
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-aboigramme_dev_pw}
      POSTGRES_DB: aboigramme
      TZ: ${APP_TZ:-Europe/Paris}
      PGTZ: ${APP_TZ:-Europe/Paris}
    volumes: [pgdata:/var/lib/postgresql/data]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U $${POSTGRES_USER} -d $${POSTGRES_DB} -h 127.0.0.1"]
      interval: 5s
      timeout: 5s
      retries: 12
      start_period: 20s

  api:
    build: {context: ./backend}
    image: aboigramme-api:latest
    depends_on:
      db: {condition: service_healthy}   # ← la garantie d'ordonnancement demandée
    environment:
      DATABASE_URL: postgresql://aboigramme:${POSTGRES_PASSWORD:-aboigramme_dev_pw}@db:5432/aboigramme
      MEDIA_DIR: /data/media
      MODEL_PATH: /app/models/yamnet.tflite
      CLASS_MAP_PATH: /app/models/yamnet_class_map.csv
      CLASSIFIER_BACKEND: yamnet_litert
      BARK_THRESHOLD: ${BARK_THRESHOLD:-0.35}
      APP_TZ: ${APP_TZ:-Europe/Paris}
      TZ: ${APP_TZ:-Europe/Paris}
      SAVE_REJECTED: "false"
      UVICORN_WORKERS: "1"        # OBLIGATOIRE, pas un placeholder (Interpreter non thread-safe)
    ports: ["${HOST_PORT:-8000}:8000"]
    volumes: [media:/data/media]
    healthcheck:
      test: ["CMD", "python", "-c",
             "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=4).status==200 else 1)"]
      interval: 15s
      timeout: 6s
      retries: 5
      start_period: 25s

volumes: {pgdata: , media: }
```

**Le healthcheck utilise `python` et non `curl`** : `python:3.11-slim-bookworm` ne fournit **ni
`curl` ni `wget`**. Ça vaut aussi dans le Dockerfile — d'où le téléchargement des modèles via
`urllib.request` et non `curl`.

**`pgdata` et `media` sont deux volumes séparés** : `docker compose down -v` détruit `pgdata`
**mais pas** les MP3. À dire explicitement dans le README, pour que personne ne suppose à tort
que c'est sûr — ou dangereux.

### `Dockerfile` — bake des modèles

```dockerfile
FROM python:3.11-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Bake au build : le runtime ne touche JAMAIS le réseau.
# (pas de curl dans l'image → urllib)
RUN python - <<'PY'
import hashlib, pathlib, urllib.request
ASSETS = [
    ("https://storage.googleapis.com/mediapipe-models/audio_classifier/yamnet/float32/1/yamnet.tflite",
     "yamnet.tflite",
     "4d8b4a53282dc83ef04e3e7dbc4fbc98082e34e44ed798e16c3a0cdd4c584faf"),
    ("https://raw.githubusercontent.com/tensorflow/models/"
     "dfffd623b6be8d1d9744b8e261fbac370d17c46d/research/audioset/yamnet/yamnet_class_map.csv",
     "yamnet_class_map.csv",
     "cdf24d193e196d9e95912a2667051ae203e92a2ba09449218ccb40ef787c6df2"),
]
out = pathlib.Path("/app/models"); out.mkdir(parents=True, exist_ok=True)
for url, name, want in ASSETS:
    req = urllib.request.Request(url, headers={"User-Agent": "aboigramme-build"})
    data = urllib.request.urlopen(req, timeout=180).read()
    got = hashlib.sha256(data).hexdigest()
    if got != want:
        raise SystemExit(f"checksum mismatch for {name}: got {got}, want {want}")
    (out / name).write_bytes(data)
    print(f"ok {name} {len(data)} bytes")
PY

COPY app    ./app
COPY static ./static
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
```

Chart.js est vendu de la même façon depuis
`https://cdn.jsdelivr.net/npm/chart.js@4.5.1/dist/chart.umd.min.js`.

---

## 13. Procédure de vérification de bout en bout

> ⚠️ **Ports mis à jour — voir §17.8.** Les commandes ci-dessous utilisent désormais
> `capture` (4466) et `admin` (4467) au lieu du service `api` unique sur 8000.

1. `cp .env.example .env`, régler `POSTGRES_PASSWORD` et `APP_TZ`.
2. `df -h /` et `ss -ltn | grep -E ':(4466|4467|5432)'` → 5432 occupé (attendu), 4466 et
   4467 libres.
3. `docker compose up -d --build` (~2-4 min : wheels + 4 Mo de modèle + vérif SHA-256).
4. `docker compose ps` → `db`, `capture` et `admin` tous `(healthy)`.
   `curl -s localhost:4466/api/health | python3 -m json.tool` → `"role": "capture",
   "classifier_ready": true, "bark_index": 70, "dog_index": 69`.
   `curl -s localhost:4467/api/health` → `"role": "admin", "classifier_ready": false` —
   **c'est normal**, l'admin n'a pas de modèle.
5. **`docker compose exec capture python -m app.tools.selftest`** — injecte un signal
   synthétique 16 kHz dans le pipeline et imprime le top-5. Prouve LiteRT + class map +
   rééchantillonneur **sans navigateur**.
6. Ouvrir `http://<ip>:4466/client/`. Si le préflight échoue, suivre
   `docs/CHROME_INSECURE_ORIGIN.md`.
7. **⭑ Test d'acceptation décisif : taper dans ses mains.** Un claquement est un transitoire
   large bande — la bonne forme, le mauvais contenu. Attendu : `accepted:false,
   reason:below_threshold`. Ça valide toute la chaîne **y compris le chemin de rejet**, sans
   avoir besoin d'un chien.
8. **Test positif** : jouer un enregistrement d'aboiement près du micro → `accepted:true`,
   `mp3_url` non nul, nouvelle ligne dans `/api/events`.
9. `docker compose exec capture ls -la /data/media/$(date +%Y/%m/%d)/` → le MP3 est là.
10. Dashboard sur `http://<ip>:4467/dashboard/` : le point apparaît sur la timeline, le clic
    lit le MP3, les KPI se mettent à jour.
11. **Soak 24 h** : `docker compose logs --since 24h capture | grep -c accepted`, et vérifier
    qu'aucun conteneur n'a redémarré.

**Réglage du seuil** : `BARK_THRESHOLD=0.35` est un **point de départ, pas une vérité**. Les
scores YAMNet sont des sigmoïdes entraînées sur AudioSet, **pas des probabilités calibrées** —
un « 0,87 Bark » n'est pas « 87 % de chances d'un aboiement » au sens fréquentiste. Le point de
fonctionnement doit être réglé empiriquement contre *ce* microphone, *cette* clôture et *ces*
chiens. Les colonnes `mean_bark_score` et `top_classes`, plus `SAVE_REJECTED=true`, sont les
outils : monter vers 0,5 si les faux positifs dominent, descendre vers 0,2 si les aboiements
sont rejetés.

---

## 14. Risques et pièges

| # | Risque | Effet | Mitigation |
|---|---|---|---|
| G1 | Port 5432 déjà pris | `up` échoue, ressemble à un bug Postgres | `db` sans `ports:` |
| G2 | Contexte non sécurisé | Page morte, `mediaDevices` `undefined` | Préflight + bandeau nommant l'origine |
| G3 | CORS ajouté « au cas où » | Surface d'attaque inutile | Rien n'est cross-origin ; ne pas en mettre |
| G4 | Fréquence micro ≠ 48000 | Audio transposé ~9 %, **tout** classé au hasard, sans erreur | Le client envoie la vraie valeur ; le serveur valide `[8000,96000]` et rééchantillonne depuis elle |
| G5 | Contraintes Chrome par défaut | Client sain qui ne détecte rien | Les trois à `false` |
| G6 | Horloge du PC client dérive | `client_captured_at` faux | `detected_at` calculé serveur, jamais depuis le client. Colonne gardée en diagnostic |
| G7 | Ajouter ffmpeg | +350 Mo | Inutile : PCM brut + `soxr` + `lameenc` |
| G8 | Croire que `ScriptProcessorNode` contourne le HTTP | Livrerait une page qui ne marche toujours pas | Il ne sert qu'aux Chrome < v66 |
| G9 | Exception dans le worklet | Mort silencieuse, l'UI dit « écoute » | try/catch + watchdog 2 s sans message `rms` |
| G10 | Micro stéréo câblé à droite | Silence numérique | Downmix `(L+R)/2` systématique |
| G11 | `ws.binaryType` oublié | Tests `ArrayBuffer` faux en silence | Le mettre explicitement à `'arraybuffer'` |
| G12 | Veille / suspension USB | Micro reapé | Désactiver veille, hibernation, suspension sélective USB ; Chrome en kiosque |
| G13 | Limite de payload WS | — | 288 Ko contre 16,7 Mo par défaut : garder la limite comme plafond |
| G14 | `asyncpg` lié à sa boucle | Reload cassé | Créer le pool dans `lifespan`, pas à l'import |
| G15 | `POSTGRES_PASSWORD` ignoré après le 1er boot | Erreurs d'auth déroutantes, conteneur « healthy » | `ALTER USER` dans le conteneur, ou `down -v` |
| G16 | Ordre des `mount()` | Routes d'API avalées en silence | Monter après les `include_router` |
| G17 | `asyncpg` multi-instruction + `$n` | Le DDL échoue | DDL sans paramètres |
| G18 | Croissance disque | ~8,8 Go/an au pire | Documenter la cadence + requête de rétention |
| G19 | Scores non calibrés | Seuil réglé au hasard | `mean_bark_score`, `top_classes`, `SAVE_REJECTED` |
| G20 | `peak_normalize` activé plus tard | Invalide un seuil déjà réglé | Documenter le couplage ; plafonné à +12 dB et sous un pic de 0,1 |

**Instructions d'exploitation à mettre au README** : désactiver la veille et l'hibernation,
désactiver la suspension sélective USB, lancer Chrome en kiosque avec
`--kiosk --autoplay-policy=no-user-gesture-required`. Le `track.onended` côté client est la
moitié logicielle de cette mitigation.

**Sécurité** : il n'y a **aucune authentification** dans le MVP (hypothèse LAN-only, assumée).
C'est la raison pour laquelle le service ne doit **pas** être exposé par redirection de port
depuis Internet. À écrire noir sur blanc dans le README.

---

## 15. Sources vérifiées

- [tensorflow/hub — migration tfhub.dev → Kaggle, issue #924](https://github.com/tensorflow/hub/issues/924)
- [Google AI Edge — migration vers LiteRT](https://developers.google.com/edge/litert/migration)
- [MediaPipe Audio Classifier — URL yamnet.tflite, entrée 1×15600](https://developers.google.com/edge/mediapipe/solutions/audio/audio_classifier)
- [MDN — AudioWorklet (contexte sécurisé)](https://developer.mozilla.org/en-US/docs/Web/API/AudioWorklet)
- [MDN — getUserMedia (contexte sécurisé, `mediaDevices` undefined)](https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/getUserMedia)
- [MDN — SharedArrayBuffer (isolation cross-origin, COOP/COEP)](https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Global_Objects/SharedArrayBuffer)
- [PyPI — ai-edge-litert](https://pypi.org/project/ai-edge-litert/) · [PyPI — tflite-runtime (impasse cp39)](https://pypi.org/project/tflite-runtime/)
- [PyPI — soxr](https://pypi.org/project/soxr/) · [PyPI — lameenc](https://pypi.org/project/lameenc/) · [github.com/chrisstaite/lameenc](https://github.com/chrisstaite/lameenc)
- [Uvicorn — `ws_max_size` par défaut 16 Mo](https://deepwiki.com/encode/uvicorn/6-websocket-protocol-implementations)
- [Kaggle Models — google/yamnet](https://www.kaggle.com/models/google/yamnet)

---

## 16. Environnement constaté (17/09/2026)

| | |
|---|---|
| Docker | 29.7.2 · Compose v5.4.0 |
| GPU | **aucun** (`nvidia-smi` absent) |
| Ollama | **non installé** |
| Python hôte | 3.11.2 · Node v20.20.2 (sans importance, tout tourne en conteneur) |
| Disque | 19 Go, **9,8 Go libres** au moment de la conception → extension de 10-20 Go prévue |
| Images en cache | `postgres:15-alpine` (417 Mo) — réutilisée telle quelle |
| Projets voisins | `calendrier-backend` + `calendrier-db-1` en service → **port 5432 occupé** |
| Utilisateur | membre du groupe `docker` |

---

## 17. Ce que l'implémentation a corrigé

Chaque point ci-dessous est un endroit où la conception affirmait quelque chose que la
mesure a démenti. Ils sont listés ici plutôt que réécrits en place, pour que le raisonnement
d'origine reste lisible à côté de sa correction.

### 17.1 Les tenseurs du modèle ne s'appellent pas comme annoncé (§3.1)

Le fichier réellement épinglé (`mediapipe-models/…/yamnet.tflite`, SHA-256 vérifié) déclare :

| | Nom | Forme | Type |
|---|---|---|---|
| Entrée | `waveform_binary` | **`(15600,)` — rang 1** | float32 |
| Sortie | `tower0/network/layer32/final_output` | `(1, 521)` | float32 |

`get_signature_list()` renvoie **`[]`** : ce fichier n'expose **aucune signature TFLite**, donc
pas de `yamnet/classification`. Les noms `audio_clip` / `scores` et la signature décrits au
§3.1 appartiennent à la variante **tfhub-lite**, qui n'est pas celle qu'on a épinglée.

Conséquence : `set_tensor` avec une forme `(1, 15600)` échoue sur
« Dimension mismatch. Got 2 but expected 1 ». L'implémentation ne code donc **aucune forme en
dur** — elle lit celle du modèle et vérifie seulement que le produit fait 15 600.

### 17.2 `Bark` est le pire discriminateur du groupe canin (§5.4)

Mesuré sur 6 segments de 3 s de vrais aboiements du terrain, max sur les fenêtres :

| classe | pire score | médiane |
|---|---|---|
| **`Bark` (70)** | **0,262** | 0,543 |
| `Dog` (69) | 0,586 | 0,801 |
| `Animal` (67) | 0,586 | 0,871 |

Le seuil de 0,35 laissait passer **2 vrais aboiements sur 6**. `Bark` est la seule classe du
groupe qui plonge sous le seuil : elle est entraînée sur des aboiements proches et isolés,
alors que ceux-ci sont lointains.

Le score principal est donc le **max sur le groupe canin**, résolu **par nom** au chargement
(jamais par index). `Animal` est écarté **malgré sa séparation encore meilleure** : il
réagirait aux chats et aux oiseaux, nombreux à la campagne.

Sur négatifs synthétiques (silence, bruit blanc, sinusoïde, transitoire large bande, bruit de
vent), **toutes** les classes restent ≤ 0,020. Mais aucun fond sonore **réel** du terrain n'a
encore été mesuré : le seuil reste provisoire.

### 17.3 La clause `ON CONFLICT` du §7 échoue telle quelle

Voir l'encadré au §7. L'index d'unicité est **partiel**, et PostgreSQL exige que le prédicat
soit répété dans la clause `ON CONFLICT` pour l'inférer. C'est exactement le genre d'erreur
qui ressemble à un bug de schéma et coûte une heure.

### 17.4 Le worklet ne peut pas convertir en int16 lui-même (§9.5)

Le §9.5 fait convertir le PCM par le worklet et transférer un `Int16Array`. C'est **2 ms de
calcul dans un callback audio de 2,67 ms** — un underrun garanti à chaque capture.

L'implémentation transfère le `Float32Array` **tel quel** (le transfert ne copie rien) et
convertit sur le thread principal. Conséquence directe : le worklet n'a plus le droit
d'allouer non plus, d'où un **double tampon** (`_out` / `_spare`) — le tampon transféré est
détaché, et le réutiliser donnerait des captures **vides sans aucune erreur**. Le harnais
`backend/tests/worklet.test.js` couvre précisément ce cas, en utilisant `structuredClone` avec
transfert pour reproduire le détachement réel.

### 17.5 Le compteur de séquence doit survivre au rechargement de page

Non prévu par la conception, et **perte de données silencieuse** s'il est oublié : le serveur
déduplique sur `(client_id, client_seq)`. Si la page se recharge et que le compteur repart à
0, le premier segment de la nouvelle session est pris pour un **rejeu** de l'ancienne — le
serveur répond « duplicate » et **jette l'audio**. Le compteur est donc persisté dans
`localStorage`.

### 17.6 En thème sombre, l'emphase de l'histogramme ne peut pas être une seconde couleur (§10.3)

Le §10.3 prévoit que l'heure de pointe soit mise en évidence **par la couleur**. Vérifié au
validateur de palette : sur la surface sombre, la bande de luminosité exploitable
(OKLCH L ≈ 0,48–0,67) est **trop étroite** pour loger deux paliers d'une même teinte qui
restent distinguables — aucun couple ne passe (le meilleur, `#3987e5`/`#1c5cab`, plafonne à
ΔE 14,4 en vision normale, sous le plancher de 15).

L'emphase est donc portée par l'**opacité** (une seule teinte, deux intensités) **et** par le
label direct sur le sommet — ce que le §10.3 demande aussi. En thème clair, le couple
`#3987e5` / `#184f95` passe tous les contrôles ; l'opacité est conservée dans les deux modes
pour que le rendu ne change pas de logique au basculement.

### 17.7 La latence réelle est très inférieure à l'estimation

Le §3.3 et le protocole tablaient sur ~184 ms par segment. Mesuré : **12 à 14 ms** pour 5
fenêtres à 16 kHz, chargement du modèle compris dans les 13 ms d'initialisation. La marge de
backpressure (`max_pending = 4`) est donc très large — elle reste, comme plafond de sécurité,
mais elle ne sera jamais atteinte par un client sain.

### 17.8 Le service tourne en DEUX rôles, sur deux ports (§4, §12)

Le §4 prévoyait un seul processus servant tout depuis une seule origine. C'est ce qui a été
construit d'abord, puis **découpé sur demande** : `capture` sur **4466**, `admin` sur
**4467**, deux services compose sur la même image, distingués par `APP_ROLE`.

Le découpage suit une couture qui existait déjà, et qui a été **vérifiée avant d'être
exploitée** :

| page | endpoints appelés |
|---|---|
| `client/app.js` | `/ws/audio` **uniquement** |
| `dashboard/dashboard.js` | `/api/*` **uniquement** |

Aucun endpoint partagé. Le seul point commun est la base, derrière.

**Ce que ça apporte, et qui est réel :**

1. L'`admin` n'instancie **pas** le classifieur — ni LiteRT, ni l'`Interpreter` non
   thread-safe. Un blocage d'inférence côté capture ne peut plus emporter le dashboard.
   *(Mesuré : 89 Mo de RSS pour capture, 69 Mo pour admin. Le gain marginal est donc
   d'environ **20 Mo**, pas les ~95 Mo qu'on pourrait lire hâtivement : ce chiffre-là est le
   processus entier, modèle compris.)*
2. **Le micro exige HTTPS**, donc la page de capture *doit* être derrière Caddy. Le
   dashboard, non. Découper les ports est ce qui permet de sécuriser la capture seule.
3. **Le CORS n'apparaît pas** — c'était le point à vérifier, puisque le §5.6 l'interdit.
   Chaque port sert l'API et les MP3 dont *sa* page a besoin, donc aucune requête n'est
   cross-origin. La règle tient.

**Ce que ça n'apporte PAS, et qu'il ne faut pas croire :**

- **Ce n'est pas un cloisonnement.** Il n'y a aucune authentification, et les deux ports
  sont publiés sur le LAN (choix explicite de l'utilisateur). Quiconque atteint 4466
  atteint 4467. Une frontière réelle demanderait une adresse d'écoute (`127.0.0.1:`), un
  pare-feu, ou Caddy.
- Le `DELETE /api/events/{id}` reste ouvert à quiconque atteint l'admin.

`APP_ROLE=all` (le défaut hors compose) restaure exactement l'ancien comportement
mono-port, ce qui garde le selftest et le développement sur un seul service.

### 17.9 `uvicorn` sans `[standard]` tire quand même `python-dotenv`

Non pas par uvicorn, mais comme dépendance transitive de `pydantic-settings`. Sans
conséquence : il n'est simplement pas utilisé. À savoir si l'on compare la liste des paquets
installés à celle du §12.

### 17.10 La capture passe à 16 kHz — le §5.1 dit l'inverse

Le §5.1 affirmait qu'on envoie « la fréquence native, pas un 16 kHz rééchantillonné côté
client », au motif que le rééchantillonneur de Chrome est de qualité variable selon la
plateforme. **La mesure contredit ce raisonnement**, pour une raison qui n'était pas
considérée : `session.py` encode le MP3 **depuis `x16`**, à `TARGET_SR = 16000`, et le
classifieur ne reçoit que `x16`. Rien au-dessus de 8 kHz n'est jamais classé ni conservé.

Envoyer du 48 kHz revenait donc à jeter **67 % de chaque envoi** — 281 ko par segment au
lieu de 94 — sans que rien ne les utilise. Et l'argument de qualité tombe : l'alternative
« rééchantillonner dans le navigateur » utilise *le même* rééchantillonneur que le contexte
à 16 kHz, donc ne gagne rien en qualité tout en ajoutant une seconde passe.

Ce qui a changé :

| | avant | après |
|---|---|---|
| `AudioContext` | défaut (48 kHz) | `{ sampleRate: 16000 }`, sous `try` |
| trame RMS | 1024 éch. (21,3 ms) | 320 éch. (20,0 ms) — **meilleure résolution** |
| `framesPerSecond` | dérivé du taux de la **piste** | dérivé du taux du **contexte** |
| historique de médiane | 10,9 s | 10,9 s (inchangé) |
| cadence de médiane | 171 ms | 180 ms (inchangée en esprit) |

`resample_to_16k` est **conservé** : un poste dont la page n'a pas été rechargée envoie
encore du 48 kHz, et sans ce court-circuit `encode_mp3` graverait 48 000 échantillons
étiquetés 16 kHz — un audio trois fois lent, sans aucune erreur.

Deux pièges qui n'ont pas de symptôme :

- `framesPerSecond` était calculé depuis `r.sampleRate` (la **piste**), alors que le worklet
  cadence ses messages sur le taux du **contexte**. À contexte 16 kHz et piste 48 kHz, le
  warm-up de 10 s en devenait 30, sans une ligne au journal.
- La géométrie et ses tableaux doivent être créés **ensemble** : `ring` est parcouru sur
  `ringSlots` cases, donc changer la constante sans réallouer fait lire au-delà de la fin →
  médiane `NaN` → `bg >= BG_FLOOR` faux → le client ne déclenche plus jamais, tout en
  affichant « écoute ».

### 17.11 Le chaînage d'une rafale demande 7 s, pas 3

`detected_at` est l'instant du déclenchement, et un clip couvre
`[detected_at − 1 s, detected_at + 2 s]` : deux événements espacés de 3,0 s sont donc
contigus à ~9 ms près. On en avait déduit qu'un seuil de 3 ou 4 s suffisait à les chaîner.

**C'est faux**, parce que la table ne contient que les événements **acceptés**. Un seul
aboiement refusé au milieu d'une rafale — et le serveur en refuse la majorité, c'est tout
l'intérêt de décider avant d'encoder — laisse un trou de 6 s. Mesuré sur les données
réelles : les événements 52→53 sont à **4,10 s**, donc un seuil de 4 s coupait la rafale
52-53-54-55 en deux. Le défaut est à **7 s**, ce qui tolère exactement un refus, et il est
exposable en paramètre (`?gap_ms=`) pour être réglé sans redéploiement.

### 17.12 Le garde-tempête plafonnait une rafale à 36 s

`stormMax: 12` sur `stormWindowMs: 60000` : au-delà de 12 **déclenchements** dans la minute,
le client suspend la capture 60 s. Comme le cooldown est de 3 s, cela faisait **36 s
d'écoute puis 60 s de trou**, en boucle. Un chien qui aboie deux minutes n'était donc
jamais enregistré d'un seul tenant — et aucune reconstitution côté serveur ne pouvait y
remédier, l'audio n'ayant pas été capturé.

Relevé à 40 (~2 min continues) et rendu réglable par `config_patch`, comme le cooldown et
le ratio. Le garde-fou reste : il protège d'une alarme de voiture ou d'un souffleur qui
s'installe dehors.

### 17.14 Les clips de 3 s ne se touchent pas — le recollage était impossible

On avait déduit, des écarts de 3,0 s entre `detected_at`, que les clips de 3 s
étaient contigus « à quelques millisecondes près », et qu'il suffisait de les
recoller pour restituer un enregistrement continu. **C'est faux**, et la mesure
le dit sans ambiguïté :

| entre | écart réel | trou |
|---|---|---|
| 54 → 55 | 3 009 ms | **9 ms** |
| 59 | 3 047 ms | **47 ms** |
| 49 → 50 | 3 052 ms | **52 ms** |

Le déclencheur a une gigue d'une trame (20 ms) et chaque clip couvre exactement
sa durée nominale : tout écart supérieur à zéro laisse un trou. Un épisode de
deux minutes recollé aurait **quarante micro-coupures**, et une pièce à
conviction trouée n'en est plus une.

C'est une propriété du **déclenchement indépendant**, pas un défaut
d'implémentation : aucun recollage ne peut être propre. D'où le passage au
**flux continu** — le client streame tant que ça aboie, s'arrête après 30 s de
silence, et le serveur produit **un seul fichier**.

### 17.15 La queue de classement tronquait l'enregistrement

Trouvé en testant : un épisode de 18 s est ressorti à **3,9 s**, avec
`window_count = 11` au lieu de 35. La file de fenêtres (8 places) débordait — le
test envoyait ses 18 morceaux d'un trait — et les fenêtres en trop étaient
**jetées**.

Or le fichier est rogné sur les fenêtres RETENUES. Jeter une fenêtre ne coûte
donc pas qu'un score : **ça ampute l'audio**. Un client qui renvoie son épisode
en rafale après une coupure réseau aurait vu son enregistrement tronqué, sans
rien pour le signaler.

Corrigé : on **attend** une place, avec un délai borné (5 s) au lieu de jeter.
Attendre remonte la pression jusqu'au client par TCP, ce qui est le
comportement correct ; le délai borne le pire cas si le classement cale.

La ligne de l'épisode tronqué s'était marquée `partial = true` toute seule —
c'est ce drapeau qui a rendu la panne visible a posteriori, et il justifie à lui
seul d'avoir été écrit.

### 17.16 La détection de rejeu ne répondait jamais

Sur un `seq` déjà stocké, le serveur renvoyait `stream_ack{duplicate:true}` et
**rien d'autre** : le client aurait attendu indéfiniment une réponse terminale,
en gardant son `inflight[seq]` pour toujours. La règle « un acquittement accepté
⇒ exactement un `segment_result` ou un `error` » n'était pas tenue.

Corrigé : le rejeu renvoie aussi le `segment_result` de l'événement existant,
avec `reason = duplicate_seq`.

### 17.17 Sept requêtes auraient changé d'unité en silence

Une ligne `events` était un aboiement ; c'en est désormais un **épisode**, qui
peut en contenir des dizaines. Sept requêtes comptaient des lignes —
`summary.count`, `count_prev_day`, `count_prev_week`, `histogram`, `daily`,
`heatmap`, `timeline.total` — plus `events_since.count`. Sans rien faire, « 27
aboiements » serait devenu « 2 » du jour au lendemain, sans erreur ni message.

D'où la colonne `bark_count`, qui compte les **rafales distinctes** et non les
fenêtres : la fenêtre fait 975 ms avec un hop de 487 ms, donc deux fenêtres
voisines se recouvrent à 50 % et un seul aboiement en allume deux ou trois. Une
rafale ne commence que si le dépassement précédent date de plus de 500 ms.

`DEFAULT 1` rend **toutes les lignes existantes correctes sans réécriture** :
vérifié après migration, `count(*)` et `sum(bark_count)` valent 36 tous les deux.
Les huit requêtes passent à `sum(bark_count)`.

### 17.18 La file d'attente perdait 80 % des segments après une coupure

`flushQueue()` envoyait les 20 segments de la file en boucle synchrone, alors que le serveur
refuse au-delà de `max_pending = 4` en vol (`busy`). Le serveur lit les paires
texte/binaire en quelques microsecondes pendant que les premières classifications tournent
encore : à partir du 5ᵉ segment, tout était refusé — et un segment refusé n'était **pas**
remis en file. Environ 16 segments sur 20 disparaissaient, avec pour seule trace un
compteur d'erreurs que personne ne regarde.

Corrigé : la file se vide par deux, et c'est la réponse du serveur à chaque segment qui
cadence la suite. Un `busy` remet désormais le segment en file.


### 17.19 L'écoute à la demande — quatre pièges qui ne se voient qu'en les écrivant

**① Deux bornes du chemin épisode tuaient une écoute de 60 s.** `max_stream_chunks = 256`
alors qu'à 200 ms par morceau, 60 s en font **300** : l'écoute se serait arrêtée à 51 s,
avec pour seul symptôme une durée trop courte — aucun rapport lisible avec la cause. Et
`stream_silence_ms = 30 000` aurait clos l'écoute à la trentième seconde d'un champ calme,
c'est-à-dire exactement le cas d'usage : on écoute dehors pour savoir ce qui s'y passe, et
« rien » est une réponse parfaitement valide. D'où des bornes **dérivées de la durée**
(propriétés `listen_max_*` de `config.py`) et un watchdog d'écoute à **deux** sorties
(idle, durée) là où celui d'épisode en a trois.

**② L'ordre des branches de `_on_binary` est porteur, et c'est le seul endroit où il l'est.**
Une écoute refusée parce qu'un épisode est ouvert pose `_discard_listen`, mais son `_stream`
à lui est non nul. Tester `_stream` en premier verserait les morceaux de l'écoute refusée
**dans l'épisode**. Les deux flux sont à 16 kHz mono : le résultat serait un enregistrement
pollué par du son étranger, à la bonne fréquence, donc **sans aucun signal d'erreur**. Un
fichier faux qui a l'air juste — la panne que ce projet refuse partout ailleurs. D'où
`_listen` testé avant `_stream`, et un test qui vérifie explicitement ce cas avec un témoin.

**③ `busy` ne peut pas servir à refuser un `stream_start`.** `app.js` traite `busy` en
remettant `inflight[seq]` dans la file hors ligne — or `inflight[streamSeq]` contient les
**métadonnées** d'un flux, sans `pcm`. `flushQueue()` aurait appelé `sendSegment()` dessus
et levé un `TypeError` au milieu du gestionnaire. D'où un code dédié, `listening`, plus un
garde dans `flushQueue` — ceinture et bretelles, parce que perdre un épisode entier pour un
`TypeError` serait absurde.

**④ `cancel()` sur soi-même, et pourquoi le code y survivait par accident.** Le watchdog
appelle `_finalize_stream` sur trois de ses sorties, et `_finalize_stream` faisait
`st.watchdog.cancel()` — c'est-à-dire l'annulation de la tâche **courante**, qui jette un
`CancelledError` au prochain `await`, donc au milieu de la finalisation. Le code y survivait
parce qu'un `except (TimeoutError, CancelledError)` plus bas avalait sa propre annulation.
Ça marchait, mais faisait dépendre la survie d'un épisode d'un `except` trop large. Corrigé
par `if st.watchdog is not asyncio.current_task()` dans les deux finalisations.

**Et deux choix qu'il vaut la peine d'écrire :**

- **Le WAV est publié AVANT tout jugement**, et le `.part` porte son en-tête dès le premier
  octet. Un `kill -9` en pleine écoute laisse donc un fichier **réparable** — `taille - 44`
  donne le nombre d'échantillons — que le démarrage suivant promeut au lieu de le jeter.
  À l'inverse de `media.sweep_spool`, qui supprime : un épisode non finalisé n'a pas été
  jugé, alors qu'une écoute est la seule copie d'une prise de son réelle.
- **La diffusion se fait avant le disque et avant le classifieur.** C'est ce qui rend
  l'écoute directe indépendante des deux : une réanalyse de 320 ms lancée depuis le panneau
  décale des scores, jamais le son. Le contraire — faire passer le PCM live par la file de
  classification — réintroduirait un trou d'une seconde dans le direct à chaque analyse. Le
  commentaire est posé au-dessus de `publish()` pour que personne ne « l'optimise ».

### 17.20 Une borne de volume jetait 132 secondes d'aboiements

`max_stream_bytes` valait **4 194 304**, soit 131 s à 16 kHz s16le mono. Or
`max_stream_ms` autorise **180 s**, qui en font 5 760 000. **La borne d'octets était donc
plus basse que celle de durée : c'est elle qui décidait, à la place de la durée.** Et elle
était traitée comme une borne de sécurité, donc `_abort_stream`, donc **le spool détruit**.

Mesuré sur le terrain, journal du client :

```
17:25:39  épisode clos (refusé : payload_too_large) — 132 morceaux, 132.0 s
17:25:39  erreur payload_too_large : épisode hors bornes : 4224000 octets, 132 morceaux
```

Deux fautes superposées, et la seconde est la vraie :

1. Les deux bornes se contredisaient — corrigé à 8 Mio, et **un validateur refuse
   désormais au démarrage** un `MAX_STREAM_BYTES` qui ne couvre pas `MAX_STREAM_MS`. Une
   incohérence entre deux réglages ne doit plus pouvoir jeter de l'audio en silence.
2. **Dépasser un volume ne dit rien de la qualité de l'audio.** C'était rangé avec les
   bornes de sécurité, à côté de « morceau de longueur impaire » — qui, lui, mérite
   vraiment le rejet, parce qu'on ne sait plus ce qu'on lit. Un épisode long est
   parfaitement légitime : il est maintenant **clos et conservé**, comme la borne de
   durée.

**Corollaire, trouvé en même temps** : sur un refus, le serveur renvoyait
`Bilan(0.0, None, 0.0)` — donc le client écrivait « chien 0.000 » dans son journal, même
pour un épisode qui avait frôlé le seuil. C'est le chiffre le plus utile pour régler le
seuil, et on le remplaçait par zéro. Il porte maintenant le vrai maximum.

### 17.21 Trois corrections livrées dans le vide, à cause du cache du CDN

Trois fois de suite, une correction a été déployée, vérifiée en local, annoncée — et
l'utilisateur a constaté que rien n'avait changé. La cause n'était pas le code : **les
`.js` sont servis par un CDN qui les garde quatre heures**, et le navigateur les garde
aussi. Il testait l'ancien fichier.

Le symptôme est traître : il est **indiscernable d'un correctif qui ne marche pas**. On
cherche alors un bug dans du code qui n'est plus celui qui tourne.

**Corrigé par une version dans l'URL** (`listen.js?v=3`), et un test vérifie que les
balises en portent une. C'est la seule chose qui règle ce problème-là — le handshake
`configured` du worklet traite le même problème côté client, mais il *signale* la
désynchronisation, il ne l'empêche pas.

**À retenir pour toute modification d'un `.js` de ce projet : incrémenter `?v=`.**

### 17.22 Une hypothèse non testée dans les bonnes conditions a coûté trois allers-retours

Le lecteur de la modale d'analyse ne lisait pas son fichier. J'ai formé une hypothèse —
un `<audio>` dans un `<dialog>` ne charge pas — et je l'ai « vérifiée » avec Chrome
headless : elle passait. J'en ai conclu que le code était bon, deux fois, et j'ai fini par
**contourner le problème** au lieu de le résoudre.

**L'hypothèse était fausse.** Un cas minimal l'a montré : un `<audio>` dans un `<dialog>`
charge dans les trois variantes (`src` avant `showModal`, après, après + `load()`), et
**dans les deux modes de rendu**. `xvfb-run` était installé depuis le début — il permet un
vrai Chrome avec rendu réel, que le headless n'est pas.

Deux erreurs de méthode, à ne pas refaire :

- **Un test qui passe dans des conditions différentes de celles de l'utilisateur ne
  prouve rien.** Ici : HTTP direct sur le port local, jamais HTTPS via le proxy, et
  headless au lieu du vrai moteur de rendu.
- **Une preuve peut être fausse.** « Aucune requête vers le WAV dans le journal du
  serveur » semblait décisif ; elle venait en fait du **cache du navigateur**, qui avait
  déjà le fichier depuis la liste. J'ai bâti une conclusion dessus.


---

## 18. L'écoute directe et le panneau d'analyse (18/09/2026)

### 18.1 Le besoin, et pourquoi il a fallu un second chemin

Le poste n'envoie rien tant qu'il n'a pas déclenché. C'est ce qui rend le système économe
— et **aveugle** : « le chien est calme », « le micro est débranché » et « le seuil est mal
réglé » se ressemblent. Pour savoir lequel, il fallait aller lire le journal sur le poste,
c'est-à-dire sur la machine qu'on ne peut pas atteindre.

Un second point, documenté depuis le début : `samples/negative/` était vide, aucun fond
sonore **réel** du terrain n'avait été mesuré.

Un bouton qui fait streamer le poste 60 s répond aux deux : on l'écoute en direct, l'audio
s'écrit dans `export/`, et il est classé au passage.

### 18.2 L'analyse reste DANS le conteneur

L'utilisateur a un service YAMNet sur GPU (Windows + CUDA) et a demandé s'il ne valait pas
mieux l'utiliser. **Réponse mesurée : non, pas pour la détection.**

| | |
|---|---|
| `score_matrix` sur 52,4 s d'audio | **468 ms** — 8,9 ms par seconde d'audio |
| Verdict sur le même fichier | identique à celui de son API |

Le CPU embarqué tourne à ~200-375× le temps réel. Le goulot n'est pas la classification,
et faire dépendre la détection d'une machine Windows qui dort ou gèle serait le vrai coût.
`remote_http.py` reste le point de bascule si le volume le justifie un jour
(`CLASSIFIER_BACKEND=remote_http`), et `ANALYZE_BACKEND=remote` délègue l'analyse à la
demande — les deux implémentés, aucun sélectionné.

**Deux faiblesses de l'API externe, à ne pas reproduire** : son `/classify` fait la
**moyenne** des scores (un aboiement de 3 s dans une minute de vent se dilue à ~5 % — c'est
l'inverse de la décision du §5.4), et son `/analyze-timeline` fait un `argmax` par fenêtre,
donc une fenêtre où `Dog` marque 0,40 et `Speech` 0,46 s'appelle `Speech` : **le chien
disparaît**. D'où `dog_frames`, calculé séparément par max du groupe canin et surligné dans
la modale.

### 18.3 L'interface est sur `noisy`, et elle RELAIE

`noisymic` ne doit rester joignable que par le poste de terrain. L'interface d'écoute ne
pouvait donc pas y rester — mais elle ne peut pas tout faire seule : le poste est connecté
à capture, et le modèle n'y tourne que là.

Deux relais, tous les deux dans `ws/` et `api/ondemand.py` :

| Ce qui manque à l'admin | Comment il l'obtient |
|---|---|
| Le modèle, pour analyser un sample | `_relais_vers_capture` — un POST vers capture, réponse relayée telle quelle |
| Le direct, parce que le poste est connecté là-bas | `ws/proxy.py` — un WebSocket que l'admin ouvre vers capture et fait passer dans les deux sens |

Le proxy **n'interprète rien** : il transporte des trames. Vérifié sur une vraie écoute —
254 trames binaires, 1,6 Mo, close proprement.

`./export` est monté sur les deux services, donc l'admin liste et sert les WAV lui-même.
**Un seul écrit** : le balayage des `.part` orphelins reste sur capture.

### 18.4 Les deux tableaux

| | Origine |
|---|---|
| **Les directs** | les WAV de `export/` — un fichier par écoute **demandée** |
| **Les capturés** | la table `events` — ce que le **poste a jugé** digne d'être gardé, seul |

Un lecteur **par table**, plus un dans la modale : **trois lecteurs, un seul son**. Dès que
l'un démarre, les autres se coupent — deux pistes superposées sont incompréhensibles, et le
même fichier joué des deux côtés ferait un écho.

En fin d'écoute, si le YAMNet embarqué a trouvé du canin, l'écoute produit **aussi** une
ligne `events` et un MP3 : la détection n'est pas arrêtée pendant qu'on écoute, elle est
**relocalisée**. Un vrai aboiement entendu pendant une écoute entre donc dans l'aboiegramme
tout seul.

### 18.5 Ce que l'écoute enregistre, et ce qu'elle ne garde pas

Nom : `AAAAMMJJ-HHMMSS-mmm_ondemand_Ns.wav` — horodatage **local** d'abord (c'est un humain
qui ouvrira ce dossier), **sans suffixe `_debug`** et sans `:`. Le suffixe distinct n'est
pas cosmétique : `debugdump._elaguer` supprime les `*_debug.wav`, et régler l'élagage du
debug effacerait les samples.

Le `.part` porte son **en-tête WAV dès le premier octet** et est promu en `.wav` en fin
d'écoute, **quoi qu'il arrive** — durée atteinte, arrêt de l'opérateur, déconnexion du
poste, ou `kill -9` : dans ce dernier cas il est **réparé** au démarrage suivant
(`taille - 44` donne le nombre d'échantillons). À l'inverse de `media.sweep_spool`, qui
supprime : un épisode non finalisé n'a pas été jugé, une écoute est la seule copie d'une
prise de son réelle.

**Ouvert** : les épisodes **refusés** sont encore effacés. L'utilisateur veut les garder
tous pour les analyser lui-même, ce qui coûte 50 à 130 Mo par jour. Trois options
(rétention, sans rétention, ou les N premières secondes), aucune tranchée.

### 18.6 Réglages ajoutés

| Réglage | Défaut | Rôle |
|---|---|---|
| `LISTEN_DURATION_MS` | 60 000 | Durée d'une écoute |
| `LISTEN_CHUNK_MS` | 200 | **Le réglage de latence** : ~250-290 ms de bout en bout |
| `LISTEN_IDLE_MS` | 5 000 | Poste muet → on clôt et on publie le WAV |
| `ONDEMAND_KEEP` | 0 | Samples conservés, 0 = jamais rien supprimer |
| `CLIENT_LISTEN_ENABLED` | true | Coupe le bouton côté poste |
| `ANALYZE_BACKEND` | local | `local` = notre YAMNet, où qu'il soit ; `remote` = service HTTP |
| `ANALYZE_REMOTE_URL` | — | Obligatoire si `remote` (multipart `file=@…`) |
| `ANALYZE_TIMELINE_MIN_SCORE` | 0,3 | Seuil d'**affichage**, à ne pas confondre avec `DOG_THRESHOLD` qui **décide** |
| `CAPTURE_URL` | `http://capture:8000` | Pour les deux relais admin → capture |

### 18.7 Tests ajoutés

| | |
|---|---|
| `app/tools/timelinetest.py` | La timeline, **sans modèle ni réseau** : fusion des fenêtres, chien vu malgré l'argmax, grille 0,4875 s |
| `tests/static-wiring.test.js` | Le câblage de la modale, l'ordre des scripts, **l'absence de l'hôte privé** dans tout ce qui est servi |
| `tests/worklet.test.js` | Le mode écoute du worklet : exclusivité avec l'épisode, morceau partiel, `rms` maintenues |

