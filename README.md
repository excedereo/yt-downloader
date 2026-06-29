# YT Downloader

Сервис скачивания видео/аудио с YouTube. FastAPI + yt-dlp.

Это **модуль для [vaeli-hub](https://github.com/excedereo/vaeli-hub)** — сам по
себе не запускается. Hub втягивает этот репозиторий в `services/yt/` через
GitHub Actions и монтирует на префикс `/yt` (и домен `yt.vaelira.su`).

## Состав

```
__init__.py   контракт Service для ядра hub
router.py     роуты (/api/info, /api/prepare, /api/file) + фронт (PAGE)
requirements.txt
```

## Что умеет

- видео (360/720/1080/макс) и аудио (mp3)
- метаданные по ссылке (превью, длительность, автор)
- реальный прогресс скачивания через SSE
- лимит 15 минут на длительность
- звук в AAC (Opus не играет в стандартном Windows-плеере)

## Зависимости

`yt-dlp`, `fastapi`, `uvicorn`. Плюс ffmpeg и deno — их докачивает hub
(объявлены в `Service.needs`).

## Разработка

Правишь здесь → пушишь → GitHub Action в hub втягивает свежую версию в
`services/yt/` и коммитит. Перезагрузка сервера подхватывает обновление.
