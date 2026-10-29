"""Offline ASR with word-level timestamps via Vosk (optional dependency).

Models are downloaded on first use from https://alphacephei.com/vosk/models
into JSUB_MODEL_DIR (default ~/.cache/jsub/models) and cached there.
"""

from __future__ import annotations

import io
import json
import os
import urllib.request
import wave
import zipfile
from dataclasses import dataclass

MODEL_URLS = {
    "eng": "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip",
    "deu": "https://alphacephei.com/vosk/models/vosk-model-small-de-0.15.zip",
    "fra": "https://alphacephei.com/vosk/models/vosk-model-small-fr-0.42.zip",
    "spa": "https://alphacephei.com/vosk/models/vosk-model-small-es-0.42.zip",
    "ita": "https://alphacephei.com/vosk/models/vosk-model-small-it-0.22.zip",
    "por": "https://alphacephei.com/vosk/models/vosk-model-small-pt-0.3.zip",
    "nld": "https://alphacephei.com/vosk/models/vosk-model-small-nl-0.22.zip",
    "pol": "https://alphacephei.com/vosk/models/vosk-model-small-pl-0.22.zip",
    "rus": "https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip",
    "jpn": "https://alphacephei.com/vosk/models/vosk-model-small-ja-0.22.zip",
    "kor": "https://alphacephei.com/vosk/models/vosk-model-small-ko-0.22.zip",
    "zho": "https://alphacephei.com/vosk/models/vosk-model-small-cn-0.22.zip",
}


@dataclass
class Word:
    start: float
    end: float
    text: str

    @classmethod
    def from_vosk(cls, d: dict) -> "Word":
        return cls(start=float(d["start"]), end=float(d["end"]), text=str(d["word"]))


def model_dir(language: str, base: str | None = None) -> str:
    base = base or os.environ.get("JSUB_MODEL_DIR") or os.path.join(
        os.path.expanduser("~"), ".cache", "jsub", "models"
    )
    lang3 = language.lower()
    for key in MODEL_URLS:
        if lang3.startswith(key) or key.startswith(lang3):
            lang3 = key
            break
    return os.path.join(base, f"vosk-{lang3}")


def ensure_model(language: str, base: str | None = None) -> str:
    """Return the local path of the Vosk model for the language, downloading if needed."""
    path = model_dir(language, base)
    if os.path.isdir(path) and os.path.exists(os.path.join(path, "am", "final.mdl")):
        return path
    lang3 = os.path.basename(path).replace("vosk-", "")
    url = MODEL_URLS.get(lang3)
    if not url:
        raise RuntimeError(f"no Vosk model known for language {language!r}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_zip = path + ".zip"
    urllib.request.urlretrieve(url, tmp_zip)
    with zipfile.ZipFile(tmp_zip) as z:
        z.extractall(os.path.dirname(path))
    os.remove(tmp_zip)
    inner = os.path.join(os.path.dirname(path), os.listdir(os.path.dirname(path))[-1])
    if inner != path:
        os.replace(inner, path)
    return path


class Transcriber:
    """Wraps a Vosk model; transcribe 16 kHz mono WAV files to timestamped words."""

    def __init__(self, model_path: str):
        from vosk import Model, KaldiRecognizer, SetLogLevel

        SetLogLevel(-1)
        self._model = Model(model_path)
        self._KaldiRecognizer = KaldiRecognizer

    def transcribe_wav(self, wav_path: str, base_offset_s: float = 0.0) -> list[Word]:
        words: list[Word] = []
        with wave.open(wav_path, "rb") as wf:
            if wf.getnchannels() != 1 or wf.getsampwidth() != 2:
                raise ValueError("expected mono 16-bit PCM WAV")
            rec = self._KaldiRecognizer(self._model, wf.getframerate())
            rec.SetWords(True)
            while True:
                data = wf.readframes(4000)
                if not data:
                    break
                if rec.AcceptWaveform(data):
                    words.extend(
                        Word.from_vosk(d)
                        for d in json.loads(rec.Result()).get("result", [])
                    )
            words.extend(
                Word.from_vosk(d)
                for d in json.loads(rec.FinalResult()).get("result", [])
            )
        for w in words:
            w.start += base_offset_s
            w.end += base_offset_s
        return words

    def transcribe_pcm(self, pcm: bytes, sample_rate: int = 16000, base_offset_s: float = 0.0) -> list[Word]:
        """Transcribe raw s16le mono PCM (used by tests and streaming)."""
        rec = self._KaldiRecognizer(self._model, sample_rate)
        rec.SetWords(True)
        words: list[Word] = []
        for i in range(0, len(pcm), 8000):
            if rec.AcceptWaveform(pcm[i : i + 8000]):
                words.extend(
                    Word.from_vosk(d)
                    for d in json.loads(rec.Result()).get("result", [])
                )
        words.extend(
            Word.from_vosk(d) for d in json.loads(rec.FinalResult()).get("result", [])
        )
        for w in words:
            w.start += base_offset_s
            w.end += base_offset_s
        return words


def transcribe_file(
    wav_path: str, language: str = "eng", base_offset_s: float = 0.0, model_base: str | None = None
) -> list[Word]:
    path = ensure_model(language, model_base)
    return Transcriber(path).transcribe_wav(wav_path, base_offset_s)
