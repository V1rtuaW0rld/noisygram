# Échantillons de référence

Enregistrements servant à **valider et calibrer** la détection. Ils ne sont pas
versionnés (voir `.gitignore`) : ce sont tes chiens, ton terrain, ton micro.

## Convention

    samples/
      reference/    des aboiements à DÉTECTER (le côté positif)
      negative/     le fond sonore du terrain, SANS chien (le côté négatif)

Le dossier `negative/` est le plus important et le plus souvent oublié : un
seuil ne se règle pas sur les positifs seuls. Sans lui, on sait qu'un aboiement
franchit le seuil, on ne sait pas si le vent le franchit aussi.

## Format

**WAV PCM 16 bits**, n'importe quelle fréquence, mono ou stéréo. Le pipeline
n'embarque **aucun décodeur MP3** (§3.4) : un MP3 doit être converti avant.
N'importe quel outil fait l'affaire — le client navigateur lui-même sait
décoder du MP3, mais le serveur non, et c'est délibéré.

## Usage

```bash
# le côté positif : chaque segment de 3 s est classé séparément
docker compose run --rm --no-deps \
  -v $PWD/samples/reference:/ref:ro \
  capture python -m app.tools.selftest /ref/Aboiements.wav --segment 3

# le côté négatif : ce qui NE DOIT PAS franchir le seuil
docker compose run --rm --no-deps \
  -v $PWD/samples/negative:/neg:ro \
  capture python -m app.tools.selftest /neg/ambiance.wav --segment 3
```

Le second doit sortir **0 segment accepté**. Si ce n'est pas le cas, le seuil
est trop bas — et c'est ce chiffre-là, pas le premier, qui le fixe.
