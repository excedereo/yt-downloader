# YT Downloader

Простой веб-сервис скачивания видео/аудио с YouTube. FastAPI + yt-dlp.

## Запуск (LifeHosting / Pterodactyl, Python 3.12+)

В настройках сервера (вкладка «Запуск»):

- **GIT REPO ADDRESS:** `https://github.com/excedereo/yt-downloader`
- **Дополнительные пакеты Python:** `yt-dlp fastapi uvicorn`
- **Образ Docker:** Python 3.12+

Сервер слушает `0.0.0.0:$SERVER_PORT` (порт берётся из окружения панели).
Открывается по `http://IP:ПОРТ`.

## ffmpeg

Нужен для mp3 и качества выше 720p. Если в системе нет — `main.py` сам докачает
статическую сборку в `bin/` при первом старте.

## Локально

```bash
pip install -r requirements.txt
python main.py    # http://localhost:25748
```
