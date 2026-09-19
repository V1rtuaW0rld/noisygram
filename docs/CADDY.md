# Servir Aboigramme en HTTPS avec Caddy

Documentation seule : **le compose ne contient aucun reverse proxy**. Si tu as
déjà un Caddy, voici comment le brancher.

C'est la solution **propre** au problème d'origine sécurisée décrit dans
[`CHROME_INSECURE_ORIGIN.md`](CHROME_INSECURE_ORIGIN.md) : plutôt que de
contourner la règle du navigateur, on la satisfait.

---

## Quel port mettre derrière Caddy

Le service tourne en deux rôles, sur deux ports :

| Port | Rôle | Derrière Caddy ? |
|---|---|---|
| **4466** | capture — page + WebSocket | **Oui.** C'est le seul qui a besoin d'une origine sécurisée, parce qu'il demande le microphone. |
| **4467** | admin — dashboard + API | **Pas nécessaire.** Aucune API du navigateur n'y est soumise. |

Le Caddyfile ci-dessous ne met donc en HTTPS **que la capture**. C'est tout
l'intérêt du découpage : on n'est pas obligé de sécuriser le dashboard pour
pouvoir utiliser un microphone.

Si tu veux aussi le dashboard en HTTPS, ajouter un second bloc `reverse_proxy
127.0.0.1:4467` sous un autre nom d'hôte. C'est là qu'une authentification
Caddy (`basic_auth`) trouverait sa place — le service, lui, n'en a aucune.

---

## Le Caddyfile

```caddy
aboigramme.lan {
    tls internal

    reverse_proxy 127.0.0.1:4466 {
        # Le WebSocket a besoin de ces deux en-têtes : sans eux, le handshake
        # est traité comme une requête HTTP ordinaire et échoue en 400.
        header_up Host {host}
        header_up X-Real-IP {remote_host}
    }

    # Les MP3 sont déjà servis avec un cache immutable par l'application ;
    # inutile d'en rajouter une couche ici.
}
```

`tls internal` fait émettre à Caddy un certificat signé par **son propre CA
racine** — c'est ce qui distingue cette approche d'un certificat auto-signé
quelconque, et c'est précisément ce qui la rend utilisable.

---

## Les trois étapes, dans l'ordre

### 1. Un vrai nom d'hôte

Chrome refuse une chaîne de certification qu'il ne peut pas valider, et
« accepter le risque » dans l'interface ne rend **pas** l'origine sécurisée pour
autant. Il faut donc un nom, pas une adresse IP.

Sur le poste extérieur, ajouter dans
`C:\Windows\System32\drivers\etc\hosts` :

```
192.168.1.42   aboigramme.lan
```

(ou une entrée DNS si la box le permet — c'est plus propre si d'autres machines
doivent s'y connecter.)

### 2. Importer le CA racine de Caddy dans Windows

C'est **l'étape qu'on oublie**, et sans elle rien ne fonctionne.

Caddy écrit son CA racine dans son répertoire de données
(`/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt` pour une
installation par paquets Debian).

1. Copier `root.crt` sur le poste extérieur.
2. Double-cliquer → **Installer le certificat** → *Ordinateur local* →
   *Placer tous les certificats dans le magasin suivant* →
   **Autorités de certification racines de confiance**.
3. Confirmer l'avertissement de sécurité.
4. **Fermer complètement Chrome** (tous les processus) et le relancer : il ne
   relit pas le magasin de certificats à chaud.

### 3. Faire pointer la page de capture vers le nom

```
https://aboigramme.lan/client/
```

Le préflight passera, et `navigator.mediaDevices` existera.

---

## Vérifier que ça a marché

Dans la console de la page (F12) :

```js
window.isSecureContext          // doit valoir true
navigator.mediaDevices          // doit être un objet, pas undefined
```

Si `isSecureContext` est `true` mais que le micro échoue quand même, le problème
est ailleurs : c'est un vrai refus de permission, et le message du navigateur
est alors fiable.

---

## Points d'attention

- **Le WebSocket passe tout seul** si le `reverse_proxy` est configuré comme
  ci-dessus : Caddy détecte la mise à niveau `Upgrade: websocket` et la relaie.
  Ne pas ajouter de règle `handle` qui court-circuiterait `/ws/`.
- **Ne pas exposer ce service sur Internet.** Il n'a **aucune
  authentification** — une hypothèse LAN-only assumée. `aboigramme.lan` doit
  rester un nom interne. Utiliser un nom en `.lan` ou `.home.arpa`, jamais un
  domaine public.
- **`tls internal` ne convient pas à un domaine public**, et n'a pas à le faire.
- Si le serveur a déjà un Caddy pour un autre service, ajouter simplement ce
  bloc à côté : Caddy gère plusieurs sites dans un seul Caddyfile.
