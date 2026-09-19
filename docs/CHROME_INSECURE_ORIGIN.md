# Autoriser le micro depuis une origine HTTP

## Le problème, exactement

Chrome et Firefox ne donnent accès au microphone que sur une **origine
sécurisée** : `https://`, ou `localhost` / `127.0.0.1`.

Depuis `http://192.168.1.42:4466`, l'objet `navigator.mediaDevices` est
**indéfini**. Pas vide, pas restreint : absent. L'échec est **total, pas
partiel** — et `AudioWorklet` est soumis à la même règle.

**Ce n'est pas une question de permission.** Aucun réglage de permission dans la
page n'y changera quoi que ce soit, et il n'y a rien à « autoriser » : le
navigateur ne propose même pas la demande.

### Le piège qui coûte une heure

```js
try {
  await navigator.mediaDevices.getUserMedia({ audio: true });
} catch (err) {
  affiche('Permission refusée');   // ← MENSONGE
}
```

Sur une origine non sécurisée, `navigator.mediaDevices` est `undefined`, donc
l'accès `.getUserMedia` lève un **`TypeError`** — que le `catch` attrape et
affiche comme un refus de permission. On cherche alors du côté du microphone,
des pilotes, des réglages Windows, pendant que le problème est ailleurs.

C'est pour ça que la page de capture fait un **préflight** avant tout le reste
et nomme l'origine exacte à autoriser.

---

## Forme 1 — le drapeau de lancement (recommandée)

Ajouter l'option au raccourci qui lance Chrome :

```
--unsafely-treat-insecure-origin-as-secure=http://192.168.1.42:4466
```

Remplacer l'adresse par celle du serveur. Concrètement, dans un `.bat` ou un
raccourci Windows :

```bat
"C:\Program Files\Google\Chrome\Application\chrome.exe" ^
  --kiosk ^
  --autoplay-policy=no-user-gesture-required ^
  --unsafely-treat-insecure-origin-as-secure=http://192.168.1.42:4466 ^
  http://192.168.1.42:4466/client/
```

**C'est la forme à retenir pour le poste extérieur** : elle ne dépend d'aucun
état du profil utilisateur, donc elle survit à une réinstallation, à un
nettoyage de profil, et à un changement de compte Windows.

---

## Forme 2 — `chrome://flags` (dépannage ponctuel)

Pour un portable qu'on apporte dehors le temps de tester, sans toucher au
raccourci :

1. Ouvrir `chrome://flags/#unsafely-treat-insecure-origin-as-secure`
2. Dans le champ **« Insecure origins treated as secure »**, saisir
   `http://192.168.1.42:4466`
3. Passer le drapeau à **Enabled**
4. **Relancer Chrome** — un simple rechargement de page ne suffit pas

### Les deux erreurs classiques

- **Le port fait partie de l'origine.** `http://192.168.1.42` ne couvre pas
  `http://192.168.1.42:4466`. C'est l'origine complète qu'il faut saisir.
- **Pas de barre oblique finale.** `http://192.168.1.42:4466/` n'est pas
  reconnu de façon fiable ; écrire `http://192.168.1.42:4466`.

---

## Ce qui ne marche PAS

**`ScriptProcessorNode` n'est pas un contournement.** Il n'est certes pas
soumis au contexte sécurisé, mais il est inutile ici : si l'origine n'est pas
sécurisée, on n'obtient **jamais** de `MediaStream` à lui donner. Il ne servait
que de repli pour un Chrome antérieur à la v66 (avril 2018) — un problème
d'**âge de navigateur**, pas de contexte de sécurité.

**Un certificat auto-signé ne suffit pas.** Chrome refuse une chaîne qu'il ne
peut pas valider, et « accepter le risque » dans l'interface ne rend pas
l'origine sécurisée pour autant. La voie HTTPS exige un vrai nom d'hôte et
l'import du CA racine dans le magasin de certificats — voir
[`CADDY.md`](CADDY.md).

---

## Le cas de `localhost`

Si Chrome tourne **sur la machine du serveur**, utiliser
`http://localhost:4466/client/` : `localhost` est une origine sécurisée par
définition, et aucun drapeau n'est nécessaire.

Ça ne dépanne pas pour le poste extérieur, qui doit atteindre le serveur par son
adresse réseau — mais ça permet de tester toute la chaîne audio sans rien
configurer.
