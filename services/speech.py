import logging
import subprocess
import warnings

import imageio_ffmpeg
import speech_recognition as sr

with warnings.catch_warnings():
    warnings.filterwarnings("ignore", message="Couldn't find ffmpeg or avconv.*")
    from pydub import AudioSegment

AudioSegment.converter = imageio_ffmpeg.get_ffmpeg_exe()
logger = logging.getLogger(__name__)


def recognize_audio(path: str) -> str:
    wav_path = f"{path}.wav"
    subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-y",
            "-i",
            path,
            "-ac",
            "1",
            "-ar",
            "16000",
            "-sample_fmt",
            "s16",
            wav_path,
        ],
        check=True,
        capture_output=True,
        # A malformed file must not hang the worker
        timeout=60,
    )
    audio_segment = AudioSegment.from_wav(wav_path)
    logger.info(
        "Audio loaded: duration=%d ms, channels=%d, rate=%d Hz, rms=%d",
        len(audio_segment),
        audio_segment.channels,
        audio_segment.frame_rate,
        audio_segment.rms,
    )
    if len(audio_segment) < 300:
        raise ValueError("Аудиосообщение слишком короткое")
    if audio_segment.rms == 0:
        raise ValueError("Аудиосообщение не содержит звука")

    recognizer = sr.Recognizer()
    with sr.AudioFile(wav_path) as source:
        recognizer.adjust_for_ambient_noise(source, duration=0.3)
        audio = recognizer.record(source)
    try:
        return recognizer.recognize_google(audio, language="ru-RU")
    except sr.UnknownValueError as error:
        raise ValueError("Не удалось разобрать речь в аудиофайле") from error
    except sr.RequestError as error:
        raise RuntimeError("Сервис распознавания речи временно недоступен") from error
