"""
YT Downloader — модуль для vaeli-hub. Экспортирует контракт `service` для ядра.

Этот репозиторий — самостоятельный сервис, который vaeli-hub втягивает в
services/yt/ через GitHub Actions (см. manifest.toml в hub). Сам по себе не
запускается: исполняется внутри hub, где есть пакет `registry`.
"""

from registry import Service
from .router import router

service = Service(
    name="yt",
    title="YT Downloader",
    description="Скачать видео или аудио с YouTube по ссылке",
    icon="fa-brands fa-youtube",
    router=router,
    prefix="/yt",
    host="yt.vaelira.su",
    needs=["ffmpeg", "deno"],
)
