from __future__ import annotations

import re

from whisper_server import WhisperServer


class Transcriber:
    def __init__(self, server: WhisperServer, smart_cleanup: bool = True) -> None:
        self.server = server
        self.smart_cleanup = smart_cleanup

    def transcribe(self, audio_path) -> str:
        text = self.server.transcribe(audio_path)
        return self._clean(text) if self.smart_cleanup else text

    def _clean(self, text: str) -> str:
        text = text.strip()
        text = re.sub(r"\s+", " ", text)
        text = re.sub(r"\s+([,.!?;:%])", r"\1", text)
        text = re.sub(r"([¿¡])\s+", r"\1", text)
        text = re.sub(r"\s+([»”])", r"\1", text)
        text = re.sub(r"([«“])\s+", r"\1", text)
        return text
