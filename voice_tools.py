import os
from io import BytesIO

from openai import AsyncOpenAI


VOICE_MODEL = os.environ.get(
    "OPENAI_TRANSCRIBE_MODEL",
    "gpt-4o-mini-transcribe",
).strip()


async def transcribe_voice(audio_bytes: bytes) -> str:
    client = AsyncOpenAI()

    audio = BytesIO(audio_bytes)
    audio.name = "voice.ogg"

    result = await client.audio.transcriptions.create(
        model=VOICE_MODEL,
        file=audio,
        language="ru",
        prompt=(
            "Это голосовая команда личному рабочему ассистенту. "
            "В речи могут встречаться названия проектов, задачи, "
            "Google Calendar, Google Tasks и Telegram."
        ),
    )

    return result.text.strip()



async def synthesize_voice(
    text: str,
) -> bytes:
    """
    Превращает текст ответа ассистента
    в OGG/Opus для Telegram voice message.
    """

    import re

    text = (text or "").strip()

    if not text:
        raise ValueError(
            "Пустой текст для озвучивания"
        )

    # Убираем Markdown, чтобы ассистент
    # не пытался произносить звёздочки и backticks.
    speech_text = re.sub(
        r"\*\*(.*?)\*\*",
        r"\1",
        text,
    )

    speech_text = speech_text.replace(
        "`",
        "",
    )

    speech_text = speech_text.replace(
        "✅",
        "",
    ).replace(
        "❌",
        "",
    )

    # Speech API имеет ограничение длины input.
    speech_text = speech_text[:3800]

    client = AsyncOpenAI()

    response = await client.audio.speech.create(
        model="gpt-4o-mini-tts",
        voice="nova",
        input=speech_text,
        instructions=(
            "Говори на естественном современном русском языке. "
            "Голос спокойный, ясный и уверенный. "
            "В рабочих командах оставайся точной и собранной. "
            "Не произноси markdown, эмодзи и технические символы."
        ),
        response_format="opus",
        speed=1.0,
    )

    data = getattr(
        response,
        "content",
        None,
    )

    if data:
        return bytes(data)

    # Запасной вариант для разных версий SDK.
    read_method = getattr(
        response,
        "read",
        None,
    )

    if read_method:
        result = read_method()

        if hasattr(
            result,
            "__await__",
        ):
            result = await result

        return bytes(result)

    raise RuntimeError(
        "Не удалось получить аудио из Speech API"
    )
