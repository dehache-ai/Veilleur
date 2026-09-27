"""Narration vocale des verdicts Veilleur, via Gradium (`GradiumClient.tts()`).

Pas de streaming temps réel ni de gestion de barge-in pour le MVP : un simple
appel TTS non-streaming après verdict (`GradiumClient.tts`, voir la documentation de Gradium).

Mode de secours texte (tâche 6) : si Gradium échoue pour une raison
quelconque (réseau, quota, clé absente...), `narrate_verdict` lève une
exception que l'appelant est censé attraper pour retomber sur le texte déjà
affiché à l'écran — la narration vocale est un bonus, jamais un point de
blocage de la démo.
"""

from __future__ import annotations

import struct
import sys
import time
from pathlib import Path

if sys.platform == "win32":
    import winsound

from gradium.client import GradiumClient

from victim_agent.env import load_project_env
from victim_agent.veilleur_gate import RiskVerdict

# Clé GRADIUM_API_KEY : `.env` du projet (cf. env.py).
load_project_env()

# Voix française du catalogue Gradium (identifiant fixe).
DEFAULT_VOICE_ID_FR = "1VAVLmmbQFDw7TMn"

AUDIO_OUTPUT_DIR = Path(__file__).resolve().parent.parent / "results" / "audio"

# Cf. architecture du brief : seuls "block" et "ask_user" déclenchent une
# narration vocale ("si block ou ask_user : Gradium TTS explique à voix
# haute ce qui a été détecté et bloqué"). "allow" reste silencieux.
NARRATED_ACTIONS = {"block", "ask_user"}


def _finalize_wav_header(data: bytes) -> bytes:
    """Corrige l'en-tête RIFF/WAVE renvoyé par Gradium : les tailles de chunk
    ('RIFF' et 'data') arrivent parfois à 0xFFFFFFFF ("taille inconnue", un
    reliquat de flux streaming jamais finalisé côté serveur). La plupart des
    lecteurs tolèrent ça en lisant jusqu'à l'EOF (ex: le lecteur ouvert par
    `os.startfile`), mais l'API Windows PlaySound (via `winsound`) la refuse
    silencieusement et joue le bip système par défaut à la place — d'où ce
    correctif, appliqué une fois pour toutes à l'écriture du fichier."""
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return data  # pas un WAV standard, on ne touche à rien

    riff_size = len(data) - 8
    data = data[:4] + struct.pack("<I", riff_size) + data[8:]

    data_chunk_pos = data.find(b"data")
    if data_chunk_pos != -1:
        size_pos = data_chunk_pos + 4
        data_size = len(data) - size_pos - 4
        data = data[:size_pos] + struct.pack("<I", data_size) + data[size_pos + 4 :]
    return data


def build_narration_text(verdict: RiskVerdict) -> str:
    """Texte prononcé à voix haute pour un verdict bloquant ou ambigu.
    Fonction volontairement sûre même appelée hors du flux normal (où
    `narrate_verdict` ne l'invoque déjà que pour block/ask_user) : un verdict
    "allow" reçoit son propre préfixe plutôt que de retomber par défaut sur
    celui d'ask_user."""
    if verdict.recommended_action == "block":
        prefix = "Action bloquée par Veilleur."
    elif verdict.recommended_action == "ask_user":
        prefix = "Veilleur a besoin de votre confirmation."
    else:
        prefix = "Veilleur n'a rien détecté d'anormal."
    return f"{prefix} {verdict.explanation}"


async def narrate_verdict(verdict: RiskVerdict, *, voice_id: str = DEFAULT_VOICE_ID_FR) -> Path | None:
    """Synthétise la narration du verdict s'il est bloquant ou ambigu (rien
    n'est fait pour "allow"). Renvoie le chemin du fichier audio généré, ou
    None si aucune narration n'était nécessaire pour ce verdict.

    Peut lever une exception (réseau, credentials Gradium...) — voir la note
    "Mode de secours texte" en tête de fichier : à l'appelant de l'attraper.
    """
    if verdict.recommended_action not in NARRATED_ACTIONS:
        return None

    text = build_narration_text(verdict)
    client = GradiumClient()
    result = await client.tts(setup={"voice_id": voice_id, "output_format": "wav"}, text=text)

    AUDIO_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"verdict_{verdict.recommended_action}_{int(time.time())}"
    out_path = AUDIO_OUTPUT_DIR / f"{stem}.wav"
    out_path.write_bytes(_finalize_wav_header(result.raw_data))
    out_path.with_suffix(".txt").write_text(text, encoding="utf-8")
    return out_path


async def synthesize(text: str, *, stem: str, voice_id: str = DEFAULT_VOICE_ID_FR) -> tuple[Path, bytes]:
    """Synthétise `text` en voix (Gradium TTS) et l'écrit dans results/audio/.
    Renvoie (chemin du fichier, octets WAV corrigés). Sert aux questions de
    confirmation (victim_agent/confirmation.py), en plus de `narrate_verdict`."""
    client = GradiumClient()
    result = await client.tts(setup={"voice_id": voice_id, "output_format": "wav"}, text=text)
    wav_bytes = _finalize_wav_header(result.raw_data)
    AUDIO_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = AUDIO_OUTPUT_DIR / f"{stem}.wav"
    out_path.write_bytes(wav_bytes)
    out_path.with_suffix(".txt").write_text(text, encoding="utf-8")
    return out_path, wav_bytes


def play_audio(path: Path) -> None:
    """Lit un fichier audio WAV généré par `narrate_verdict`, sans ouvrir
    aucune fenêtre (contrairement à `os.startfile`, qui lance le lecteur
    associé aux .wav et laisse sa fenêtre ouverte jusqu'à fermeture
    manuelle). `winsound` est un module Windows intégré à Python, aucune
    dépendance à installer.

    Lecture synchrone (bloquante) : `SND_ASYNC` a été testé et ne joue rien
    si le processus appelant se termine juste après l'appel (le son se
    joue dans un thread géré par l'OS, coupé net si le process meurt avant
    la fin) — inadapté à un script qui doit juste narrer puis continuer.
    Bloquer jusqu'à la fin du son est de toute façon ce qu'on veut : on
    laisse le temps à l'utilisateur d'entendre le message en entier."""
    if sys.platform == "win32":
        winsound.PlaySound(str(path), winsound.SND_FILENAME)
