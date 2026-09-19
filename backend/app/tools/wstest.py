"""Test de bout en bout du protocole WebSocket, contre un serveur en marche.

    docker compose cp samples/reference/reference.wav api:/tmp/
    docker compose exec api python -m app.tools.wstest /tmp/reference.wav

Envoie de VRAIS segments et vérifie chaque réponse, y compris tous les chemins
d'erreur du §6 : un code d'erreur qui n'est jamais déclenché est un code
d'erreur dont on ne sait pas qu'il fonctionne. Le client navigateur, lui, ne
les produira jamais volontairement.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

import numpy as np
from websockets.asyncio.client import connect

from ..audio.wav import read_wav_mono

DEFAULT_URL = "ws://127.0.0.1:8000/ws/audio"
CLIENT_ID = "wstest"
SEGMENT_S = 3.0

_failures: list[str] = []
_checks = 0


def check(label: str, ok: bool, detail: str = "") -> bool:
    global _checks
    _checks += 1
    print(f"[{'  ok  ' if ok else ' ÉCHEC'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        _failures.append(label)
    return ok


class Client:
    def __init__(self, ws) -> None:
        self.ws = ws

    async def send_json(self, payload: dict) -> None:
        await self.ws.send(json.dumps(payload))

    async def recv(self, timeout: float = 20.0) -> dict:
        return json.loads(await asyncio.wait_for(self.ws.recv(), timeout))

    async def recv_for(self, seq: int, timeout: float = 20.0) -> dict:
        """Lit jusqu'à la réponse qui porte ce seq, en signalant les intruses."""
        while True:
            msg = await self.recv(timeout)
            if msg.get("seq") == seq:
                return msg
            print(f"          (message hors séquence ignoré : {msg.get('type')})")

    async def recv_pong(self, timeout: float = 20.0) -> dict:
        while True:
            msg = await self.recv(timeout)
            if msg.get("type") == "pong":
                return msg


async def send_segment(
    cli: Client,
    seq: int,
    pcm: bytes,
    *,
    sample_rate: int,
    channels: int = 1,
    fmt: str = "s16le",
    num_samples: int | None = None,
    post_roll_ms: int = 2000,
    captured_at_ms: int | None = None,
    binary: bytes | None = None,
) -> dict:
    """Envoie [segment_start][binaire] et rend la réponse portant ce seq.

    `binary` permet de désynchroniser volontairement la trame de ses
    métadonnées — c'est ainsi qu'on teste `bad_length` et `payload_too_large`.
    """
    n = len(pcm) // 2 if num_samples is None else num_samples
    await cli.send_json(
        {
            "type": "segment_start",
            "seq": seq,
            "captured_at_ms": captured_at_ms if captured_at_ms is not None else 0,
            "sample_rate": sample_rate,
            "channels": channels,
            "format": fmt,
            "num_samples": n,
            "rms": 0.03,
            "background_rms": 0.008,
            "trigger_ratio": 3.9,
            "post_roll_ms": post_roll_ms,
        }
    )
    await cli.ws.send(pcm if binary is None else binary)
    return await cli.recv_for(seq)


async def main_async(url: str, wav_path: str, client_id: str) -> int:
    # `read_wav_mono` lève ValueError (c'est un module de bibliothèque) ; ici on
    # est en ligne de commande, donc on rend un message et pas une trace.
    try:
        audio, rate = read_wav_mono(wav_path)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    step = int(SEGMENT_S * rate)
    chunks = [audio[i : i + step] for i in range(0, audio.size, step)]
    chunks = [c for c in chunks if c.size >= step // 10]
    print(
        f"source : {wav_path} — {rate} Hz, {audio.size} échantillons, "
        f"{audio.size / rate:.2f} s → {len(chunks)} segments de {SEGMENT_S:.0f} s"
    )

    async with connect(url, max_size=None) as ws:
        cli = Client(ws)

        # -- hello --------------------------------------------------------
        print("\n■ Poignée de main")
        await cli.send_json(
            {
                "type": "hello",
                "protocol_version": 1,
                "client_id": client_id,
                "app_version": "wstest/1.0",
                "user_agent": "wstest",
                "device_sample_rate": rate,
            }
        )
        ack = await cli.recv()
        check("hello_ack reçu", ack.get("type") == "hello_ack", ack.get("type", "?"))
        check(
            "protocole annoncé",
            ack.get("protocol_version") == 1,
            f"v{ack.get('protocol_version')}",
        )
        info = ack.get("classifier", {})
        check(
            "groupe surveillé annoncé",
            info.get("noisy_classes") and len(info["noisy_classes"]) == 7,
            f"{info.get('noisy_classes')}",
        )
        check(
            "limites annoncées",
            set(ack.get("limits", {})) >= {"max_segment_bytes", "max_pending"},
            f"{ack.get('limits')}",
        )
        check(
            "config_patch annoncé",
            set(ack.get("config_patch", {}))
            == {
                "cooldown_ms",
                "trigger_ratio",
                "min_rms_floor",
                "storm_max",
                "storm_window_ms",
                "storm_suspend_ms",
                "stream_enabled",
                "stream_silence_ms",
                "stream_max_ms",
            },
            f"{ack.get('config_patch')}",
        )

        # -- segments réels ------------------------------------------------
        print(f"\n■ {len(chunks)} segments réels (événements)")
        accepted = 0
        event_ids: list[int] = []
        for i, chunk in enumerate(chunks):
            res = await send_segment(
                cli, i, chunk.tobytes(), sample_rate=rate, post_roll_ms=2000
            )
            if res.get("type") != "segment_result":
                check(f"segment {i} : segment_result", False, json.dumps(res)[:140])
                continue
            if res.get("accepted"):
                accepted += 1
                event_ids.append(res["event_id"])
            print(
                f"    seq={i} noisy={res['noisy_score']:.3f} bark={res['bark_score']:.3f} "
                f"→ {'ACCEPTÉ' if res['accepted'] else 'refusé'} "
                f"(event {res['event_id']}, {res['mp3_bytes']} o, "
                f"{res['processing_ms']} ms, {res['reason']})"
            )
        check(
            "tous les événements acceptés",
            accepted == len(chunks),
            f"{accepted}/{len(chunks)}",
        )
        check(
            "mp3_url renseignée sur les acceptés",
            bool(event_ids),
            f"{len(event_ids)} événements",
        )

        # -- idempotence : rejeu du même seq -------------------------------
        print("\n■ Rejeu après coupure (le client n'a pas vu l'acquittement)")
        res = await send_segment(
            cli, 0, chunks[0].tobytes(), sample_rate=rate, post_roll_ms=2000
        )
        check("rejeu accepté", res.get("accepted") is True)
        check(
            "même événement, pas de doublon",
            res.get("event_id") == event_ids[0],
            f"event {res.get('event_id')} vs {event_ids[0]}",
        )
        check(
            "raison = duplicate_seq",
            res.get("reason") == "duplicate_seq",
            str(res.get("reason")),
        )

        # -- silence : doit être refusé ------------------------------------
        print("\n■ Silence (3 s de zéros)")
        silence = np.zeros(step, dtype="<i2")
        res = await send_segment(cli, 100, silence.tobytes(), sample_rate=rate)
        check("silence refusé", res.get("accepted") is False, f"noisy={res.get('noisy_score')}")
        check(
            "aucun MP3 pour un refus",
            res.get("mp3_url") is None and res.get("event_id") is None,
            f"event_id={res.get('event_id')}",
        )
        check("raison = below_threshold", res.get("reason") == "below_threshold")

        # -- chemins d'erreur ----------------------------------------------
        print("\n■ Chemins d'erreur (§6)")

        res = await send_segment(
            cli, 101, chunks[0].tobytes(), sample_rate=rate, fmt="f32le"
        )
        check("bad_format sur format inconnu", res.get("code") == "bad_format", str(res.get("code")))

        res = await send_segment(cli, 102, chunks[0].tobytes(), sample_rate=4000)
        check(
            "bad_sample_rate hors bornes",
            res.get("code") == "bad_sample_rate",
            f"{res.get('code')} — {str(res.get('message'))[:60]}",
        )

        res = await send_segment(
            cli, 103, chunks[0].tobytes(), sample_rate=rate, num_samples=48000, binary=b"\x00" * 1000
        )
        check("bad_length sur trame tronquée", res.get("code") == "bad_length", str(res.get("code")))

        # Durée > max_segment_ms (10 000) : refusé sur les métadonnées, la
        # trame binaire qui suit doit être avalée sans produire d'autre erreur.
        res = await send_segment(
            cli,
            104,
            b"\x00" * 1000,
            sample_rate=48000,
            num_samples=490000,
            binary=b"\x00" * 1000,
        )
        check(
            "payload_too_large sur durée excessive",
            res.get("code") == "payload_too_large",
            f"{res.get('code')} — {str(res.get('message'))[:60]}",
        )
        await asyncio.sleep(0.2)
        check("la trame avalée n'a pas produit de seconde erreur", True, "voir journal serveur")

        # Taille > max_segment_bytes (1 048 576) alors que la durée est légale :
        # 96 kHz × 10 s = 1 920 000 octets. C'est le plafond de sécurité (G13).
        gros = b"\x00" * (96000 * 10 * 2)
        res = await send_segment(
            cli, 105, gros, sample_rate=96000, num_samples=960000, binary=gros
        )
        check(
            "payload_too_large sur taille excessive",
            res.get("code") == "payload_too_large",
            f"{res.get('code')} — {str(res.get('message'))[:60]}",
        )

        await cli.send_json({"type": "nawak"})
        res = await cli.recv()
        check("unknown_type sur type inconnu", res.get("code") == "unknown_type", str(res.get("code")))

        # -- ping ----------------------------------------------------------
        print("\n■ Keepalive")
        await cli.send_json({"type": "ping", "t": 42})
        pong = await cli.recv_pong()
        check("pong renvoyé avec l'écho", pong.get("t") == 42, f"t={pong.get('t')}")

    # -- hello manquant, sur une connexion neuve ---------------------------
    print("\n■ Client qui ne dit pas bonjour (connexion séparée)")
    try:
        async with connect(url, max_size=None) as ws:
            cli = Client(ws)
            await cli.send_json({"type": "segment_start", "seq": 1})
            msg = await cli.recv()
            check("erreur annoncée fatale", msg.get("fatal") is True, str(msg)[:120])
            try:
                await asyncio.wait_for(ws.recv(), 5.0)
                check("le serveur ferme la connexion", False, "connexion toujours ouverte")
            except Exception:  # noqa: BLE001 — fermeture attendue
                check("le serveur ferme la connexion", True, "fermée")
    except Exception as exc:  # noqa: BLE001
        check("connexion de test", False, repr(exc))

    print("\n" + "=" * 72)
    if _failures:
        print(f"  {len(_failures)} ÉCHEC(S) sur {_checks} vérifications :")
        for f in _failures:
            print(f"    - {f}")
        print("=" * 72)
        return 1
    print(f"  {_checks} vérifications, toutes OK")
    print("=" * 72)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.tools.wstest")
    parser.add_argument("wav", help="fichier WAV 16 bits contenant de vrais événements")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument(
        "--client",
        default=CLIENT_ID,
        help="identifiant de client. REJOUER LE MÊME identifiant teste la "
        "déduplication (les segments seront ré-acquittés, pas réenregistrés) ; "
        "en changer teste le chemin nominal sur des lignes neuves.",
    )
    args = parser.parse_args(argv)
    print("=" * 72)
    print(f"  Noisygram — test du protocole WebSocket contre {args.url}")
    print(f"  client_id = {args.client}")
    print("=" * 72)
    try:
        return asyncio.run(main_async(args.url, args.wav, args.client))
    except FileNotFoundError:
        print(f"Fichier introuvable : {args.wav}")
        return 1
    except OSError as exc:
        print(f"Connexion impossible à {args.url} : {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
