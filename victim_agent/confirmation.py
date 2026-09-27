"""Confirmation vocale d'un verdict `ask_user` : la machine pose la question à
voix haute (Gradium TTS), l'utilisateur répond, sa voix est transcrite
(Gradium STT), et un LLM interprète la transcription (`guardians/confirmation.mthds`).

Flux :
1. la machine dit l'explication du verdict et demande « autorisez-vous cette
   action ? » ;
2. la réponse est recueillie : microphone (`MicrophoneAnswer`), clavier
   (`TypedAnswer`), ou réponse simulée passée par un aller-retour TTS puis STT
   (`SimulatedVoiceAnswer`, pour les tests sans micro) ;
3. un LLM classe la réponse : approuvée, refusée ou pas claire ;
4. réponse pas claire : une seule reprise, puis refus.

Choix de sécurité : toute incertitude devient un **refus** (transcription vide,
réponse ambiguë, échec de la reconnaissance vocale, échec du LLM). L'approbation
ne vaut que pour l'action précise en cause. La transcription est une donnée non
fiable, jamais une commande.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pipelex.pipeline.runner import PipelexMTHDSProtocol

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from victim_agent.narrator import DEFAULT_VOICE_ID_FR, GradiumClient, play_audio, synthesize  # noqa: E402
from victim_agent.veilleur_gate import RiskVerdict, log_verdict  # noqa: E402

GUARDIAN_NAME = "user_confirmation"
SAMPLE_RATE = 16000
MAX_ATTEMPTS = 2

QUESTION_SUFFIX = " Autorisez-vous cette action ? Répondez par oui ou par non."
RETRY_QUESTION = "Je n'ai pas bien compris votre réponse. Autorisez-vous cette action ? Répondez par oui ou par non."


@dataclass
class ConfirmationResult:
    approved: bool
    decision: str  # "approve", "deny" ou "unclear" (après les reprises : refus)
    transcript: str
    explanation: str
    attempts: int


class AnswerSource(Protocol):
    async def get(self, attempt: int) -> str:
        """Renvoie la transcription de la réponse de l'utilisateur."""


def _pad_with_silence(wav_bytes: bytes, seconds: float = 0.6) -> bytes:
    """Ajoute `seconds` de silence avant et après la parole.

    Constat mesuré (Gradium STT, 2026-09-25) : sans ce silence, le premier mot d'un
    enregistrement court est perdu (« Oui. » et « Non. » seuls donnent une
    transcription vide, « Oui, vous pouvez continuer. » devient « vous pouvez
    continuer. »). Avec 0,6 s de silence de chaque côté, la transcription est
    complète. Un enregistrement au micro qui démarre à l'attaque de la parole a le
    même défaut, donc le remplissage s'applique à tous les audios."""
    import io
    import wave

    try:
        with wave.open(io.BytesIO(wav_bytes)) as src:
            channels, width, rate, frames = src.getnchannels(), src.getsampwidth(), src.getframerate(), src.getnframes()
            data = src.readframes(frames)
        silence = b"\x00" * (int(rate * seconds) * channels * width)
        out = io.BytesIO()
        with wave.open(out, "wb") as dst:
            dst.setnchannels(channels)
            dst.setsampwidth(width)
            dst.setframerate(rate)
            dst.writeframes(silence + data + silence)
        return out.getvalue()
    except Exception:
        return wav_bytes  # format inattendu : on envoie l'audio tel quel


async def transcribe(wav_bytes: bytes, language: str = "fr") -> str:
    """Reconnaissance vocale Gradium. Renvoie une chaîne vide en cas d'échec."""
    try:
        wav_bytes = _pad_with_silence(wav_bytes)
        client = GradiumClient()
        result = await client.stt(setup={"json_config": {"language": language}}, audio=wav_bytes)
        return (result.text or "").strip()
    except Exception:
        return ""


class TypedAnswer:
    """Réponse tapée au clavier (secours quand le micro n'est pas disponible)."""

    async def get(self, attempt: int) -> str:
        return (await asyncio.to_thread(input, "Votre réponse (oui/non) : ")).strip()


class MicrophoneAnswer:
    """Réponse dite au micro, transcrite par Gradium. Nécessite la bibliothèque
    `sounddevice` (`pip install sounddevice`) et un micro. Enregistre jusqu'à
    `max_seconds`, en s'arrêtant après `silence_seconds` de silence une fois la
    parole commencée (détection simple d'énergie, pas de VAD avancée)."""

    def __init__(self, max_seconds: float = 8.0, silence_seconds: float = 1.2, threshold: float = 0.01) -> None:
        self.max_seconds = max_seconds
        self.silence_seconds = silence_seconds
        self.threshold = threshold

    def _record(self) -> bytes:
        import io
        import wave

        import numpy as np
        import sounddevice as sd

        block = int(SAMPLE_RATE * 0.1)
        frames, started, quiet = [], False, 0.0
        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=block) as stream:
            elapsed = 0.0
            while elapsed < self.max_seconds:
                data, _ = stream.read(block)
                elapsed += 0.1
                loud = float(np.abs(data).mean()) > self.threshold
                if loud:
                    started, quiet = True, 0.0
                elif started:
                    quiet += 0.1
                if started:
                    frames.append(data.copy())
                if started and quiet >= self.silence_seconds:
                    break
        if not frames:
            return b""
        pcm = (np.concatenate(frames).flatten() * 32767).astype("<i2").tobytes()
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(SAMPLE_RATE)
            wav.writeframes(pcm)
        return buffer.getvalue()

    async def get(self, attempt: int) -> str:
        print("🎙️  Parlez maintenant (le micro s'arrête après un court silence)...")
        wav_bytes = await asyncio.to_thread(self._record)
        return await transcribe(wav_bytes) if wav_bytes else ""


class SimulatedVoiceAnswer:
    """Réponses simulées, pour tester le flux complet SANS micro : chaque réponse
    est synthétisée en voix (TTS) puis retranscrite (STT), donc le chemin de
    reconnaissance vocale est réellement exercé. Aucune fausse voix de personne
    réelle : voix standard du projet."""

    def __init__(self, answers: list[str]) -> None:
        self.answers = answers

    async def get(self, attempt: int) -> str:
        text = self.answers[min(attempt, len(self.answers) - 1)]
        _, wav_bytes = await synthesize(text, stem=f"simulated_answer_{attempt}")
        return await transcribe(wav_bytes)


async def interpret_answer(question: str, transcript: str) -> tuple[str, str]:
    """Fait interpréter la transcription par le LLM : (décision, explication).
    Une transcription vide ou un échec du LLM donnent « unclear »."""
    if not transcript.strip():
        return "unclear", "Aucune réponse comprise."
    try:
        response = await PipelexMTHDSProtocol().execute(
            pipe_code="interpret_user_answer",
            inputs={
                "monitored_action": {
                    "concept": "veilleur_guardian.MonitoredAction",
                    "content": {
                        "source": "réponse vocale de l'utilisateur, transcrite automatiquement (peut contenir des erreurs)",
                        "original_user_intent": question,
                        "content_or_action": transcript,
                    },
                }
            },
        )
        content = response.pipe_output.main_stuff.content
        return content.decision, content.explanation
    except Exception as exc:
        return "unclear", f"Interprétation impossible ({type(exc).__name__}) : traité comme une réponse pas claire."


async def confirm_with_user(
    verdict: RiskVerdict,
    answers: AnswerSource,
    *,
    speak: bool = True,
    play: bool = True,
    voice_id: str = DEFAULT_VOICE_ID_FR,
) -> ConfirmationResult:
    """Pose la question de confirmation pour un verdict `ask_user` et renvoie la
    décision. `speak=False` n'appelle pas la synthèse vocale (affichage texte
    seulement) ; `play=False` synthétise sans jouer le son."""
    question = f"Veilleur a besoin de votre confirmation. {verdict.explanation}{QUESTION_SUFFIX}"
    transcript, decision, explanation = "", "unclear", "Aucune réponse."
    attempts = 0
    for attempt in range(MAX_ATTEMPTS):
        attempts = attempt + 1
        spoken = question if attempt == 0 else RETRY_QUESTION
        print(f"🗣️  Veilleur : {spoken}")
        if speak:
            try:
                audio_path, _ = await synthesize(spoken, stem=f"confirmation_question_{attempt}", voice_id=voice_id)
                if play:
                    play_audio(audio_path)
            except Exception as exc:
                print(f"(voix indisponible ({exc}) — question affichée seulement)")
        transcript = await answers.get(attempt)
        print(f"👤 Réponse transcrite : {transcript!r}")
        decision, explanation = await interpret_answer(question, transcript)
        print(f"   Interprétation : {decision} — {explanation}")
        if decision != "unclear":
            break

    approved = decision == "approve"
    final = "approve" if approved else ("deny" if decision == "deny" else "unclear")
    log_verdict(
        source="confirmation de l'utilisateur (verdict ask_user)",
        original_user_intent=question,
        content_or_action=transcript,
        guardian=GUARDIAN_NAME,
        verdict=RiskVerdict(
            risk_level="nul" if approved else "moyen",
            anomaly_type="aucune",
            explanation=f"Réponse de l'utilisateur interprétée comme « {final} » : {explanation}"
            + ("" if approved or final == "deny" else " Sans réponse claire, l'action est refusée par prudence."),
            recommended_action="allow" if approved else "block",
        ),
    )
    return ConfirmationResult(approved=approved, decision=final, transcript=transcript, explanation=explanation, attempts=attempts)

