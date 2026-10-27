
from datetime import datetime
import os
import subprocess

import requests
from dotenv import load_dotenv


load_dotenv()

ELEVENLABS_API_URL = "https://api.elevenlabs.io/v1/text-to-speech/JBFqnCBsd6RMkjVDRZzb"


def get_greeting():
    hour = datetime.now().hour

    if 5 <= hour < 12:
        return "Доброе утро."
    elif 12 <= hour < 18:
        return "Добрый день."
    elif 18 <= hour < 23:
        return "Добрый вечер."
    else:
        return "Доброй ночи."


def speak(text):
    print(f"NOVA AI: {text}")
    api_key = os.getenv("ELEVENLABS_API_KEY")
    if not api_key:
        raise RuntimeError("ELEVENLABS_API_KEY не найден в .env")

    response = requests.post(
        ELEVENLABS_API_URL,
        headers={
            "xi-api-key": api_key,
            "Content-Type": "application/json",
        },
        json={
            "text": text,
            "model_id": "eleven_multilingual_v2",
        },
        params={"output_format": "mp3_44100_128"},
        timeout=60,
    )
    response.raise_for_status()

    audio_path = "/tmp/jarvis_response.mp3"
    with open(audio_path, "wb") as audio_file:
        audio_file.write(response.content)

    subprocess.run(["afplay", audio_path], check=True)


def main():
    greeting = get_greeting()

    speak(f"{greeting} Система запущена. NOVA AI готов к работе.")


if __name__ == "__main__":
    main()
