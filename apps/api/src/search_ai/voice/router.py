"""Spraak-API ``/voice/v1`` (B15). P0: alleen eerlijke status; sessies, STT, TTS en intentrouter volgen in P5.

Er is in P0 **geen** STT-, TTS- of taalmodel-app gekoppeld (optionele apps, standaard uit). Daarom geeft elke
spraakfunctie ``501 capability_unavailable`` en blijft getypte invoer in de Hub-UI de terugvaloptie (B15-001).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from .. import VOICE_PREFIX
from ..errors import capability_unavailable
from ..security.auth import Principal, current_principal, require_user_context
from ..timeutil import iso, utcnow

router = APIRouter(prefix=VOICE_PREFIX, tags=["spraak (B15)"])

CAPABILITIES = {
    "typed_chat": "capability_unavailable",
    "stt": "capability_unavailable",
    "tts": "capability_unavailable",
    "intent_router": "capability_unavailable",
    "local_llm": "capability_unavailable",
}


@router.get("/status", summary="Welke spraakfuncties zijn beschikbaar? (P0: geen; getypte invoer blijft de terugval)")
def status(_p: Principal = Depends(current_principal)) -> dict[str, Any]:
    return {
        "capabilities": CAPABILITIES,
        "optional_apps": {
            "stt": {"app": "stt (faster-whisper)", "status": "niet_geinstalleerd", "standaard": "uit"},
            "tts": {"app": "tts (Piper)", "status": "niet_geinstalleerd", "standaard": "uit"},
            "llm": {"app": "ollama", "status": "niet_geinstalleerd", "standaard": "uit"},
        },
        "privacy": {
            "ruwe_audio_opslaan": False,
            "transcripten_bewaren_standaard": False,
            "achtergrond_luisteren": False,
        },
        "fallback_nl": "Spraak is nog niet beschikbaar. Typ je vraag in de Hub; acties lopen altijd via een voorstel.",
        "observed_at": iso(utcnow()),
    }


@router.post(
    "/sessions",
    summary="Sessie starten (volgt in P5)",
    responses={501: {"description": "capability_unavailable"}},
)
def start_session(_p: Principal = Depends(require_user_context)) -> None:
    raise capability_unavailable(
        "voice.session", "Spraak- en chatsessies volgen in fase P5; er wordt nu niets opgenomen of verwerkt."
    )
