"""
YT Downloader — простой веб-сервис скачивания видео/аудio с YouTube.
FastAPI + yt-dlp. Рассчитан на запуск в Pterodactyl (LifeHosting): слушает
0.0.0.0:$SERVER_PORT, ffmpeg при отсутствии докачивается статик-бинарником.

Само-обновление: при старте тянет свою свежую версию из репо и перезапускается
на ней. Поэтому достаточно один раз залить файл — дальше пуш в репо + рестарт
сервера и код обновится сам, без git на хостинге.
"""

import os
import re
import sys
import uuid
import shutil
import asyncio
import tarfile
import urllib.request
from pathlib import Path

# ── само-обновление из репо (до тяжёлых импортов, на голом stdlib) ──
RAW_URL = "https://raw.githubusercontent.com/excedereo/yt-downloader/main/main.py"


def self_update():
    """Качает свежий main.py из репо. Если отличается — пишет и перезапускается."""
    if os.environ.get("NO_SELF_UPDATE") == "1" or "--updated" in sys.argv:
        return
    try:
        me = Path(__file__).resolve()
        req = urllib.request.Request(RAW_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            fresh = r.read()
        current = me.read_bytes()
        if fresh and fresh != current and b"YT Downloader" in fresh:
            me.write_bytes(fresh)
            print("[update] main.py обновлён из репо, перезапускаюсь", flush=True)
            os.execv(sys.executable, [sys.executable, str(me), "--updated"])
        else:
            print("[update] уже актуальная версия", flush=True)
    except Exception as e:
        print("[update] пропускаю (нет связи с репо):", e, flush=True)


self_update()

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
import uvicorn
import yt_dlp

BASE = Path(__file__).resolve().parent
DL_DIR = BASE / "downloads"        # сюда качаем
BIN_DIR = BASE / "bin"             # сюда кладём ffmpeg если докачали
DL_DIR.mkdir(exist_ok=True)
BIN_DIR.mkdir(exist_ok=True)

# ── ffmpeg: ищем в системе, иначе докачиваем статический бинарник ──
FFMPEG_DIR = None  # папка с ffmpeg/ffprobe для yt-dlp (--ffmpeg-location)


def ensure_ffmpeg():
    """Возвращает путь к папке с ffmpeg или None, если он есть в PATH."""
    global FFMPEG_DIR
    if shutil.which("ffmpeg"):
        print("[ffmpeg] найден в системе", flush=True)
        return None
    local = BIN_DIR / "ffmpeg"
    if local.exists():
        print("[ffmpeg] уже докачан в bin/", flush=True)
        FFMPEG_DIR = str(BIN_DIR)
        return FFMPEG_DIR
    # докачиваем статическую сборку (johnvansickle, linux x64)
    url = "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"
    print("[ffmpeg] не найден — качаю статик-сборку...", flush=True)
    try:
        tmp = BIN_DIR / "ffmpeg.tar.xz"
        urllib.request.urlretrieve(url, tmp)
        with tarfile.open(tmp) as t:
            for m in t.getmembers():
                name = os.path.basename(m.name)
                if name in ("ffmpeg", "ffprobe"):
                    m.name = name
                    t.extract(m, BIN_DIR)
        os.chmod(local, 0o755)
        ffprobe = BIN_DIR / "ffprobe"
        if ffprobe.exists():
            os.chmod(ffprobe, 0o755)
        tmp.unlink(missing_ok=True)
        FFMPEG_DIR = str(BIN_DIR)
        print("[ffmpeg] докачан в", FFMPEG_DIR, flush=True)
        return FFMPEG_DIR
    except Exception as e:
        print("[ffmpeg] не удалось докачать:", e, flush=True)
        print("[ffmpeg] mp3 и качество >720p будут недоступны", flush=True)
        return None


def ensure_deno():
    """yt-dlp требует JS-runtime для парсинга YouTube (иначе видео без звука).
    Ставим Deno в bin/ и добавляем в PATH, если его нет в системе."""
    if shutil.which("deno"):
        print("[deno] найден в системе", flush=True)
        return
    local = BIN_DIR / "deno"
    if not local.exists():
        # официальный статик-бинарник Deno (linux x64)
        url = "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip"
        print("[deno] не найден — качаю...", flush=True)
        try:
            import zipfile
            tmp = BIN_DIR / "deno.zip"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req) as r, open(tmp, "wb") as f:
                shutil.copyfileobj(r, f)
            with zipfile.ZipFile(tmp) as z:
                z.extract("deno", BIN_DIR)
            os.chmod(local, 0o755)
            tmp.unlink(missing_ok=True)
            print("[deno] установлен в", local, flush=True)
        except Exception as e:
            print("[deno] не удалось установить:", e, flush=True)
            print("[deno] видео может качаться без звука", flush=True)
            return
    # добавляем bin/ в PATH, чтобы yt-dlp нашёл deno
    os.environ["PATH"] = str(BIN_DIR) + os.pathsep + os.environ.get("PATH", "")
    print("[deno] добавлен в PATH", flush=True)


app = FastAPI()

YT_RE = re.compile(r"(youtube\.com|youtu\.be)", re.I)


def safe_name(s: str) -> str:
    s = re.sub(r"[^\w\s.-]", "", s, flags=re.U).strip()
    return (s or "video")[:120]


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE


@app.get("/api/info")
async def info(url: str):
    """Метаданные по ссылке: название, длительность, превью."""
    if not YT_RE.search(url or ""):
        return JSONResponse({"error": "Это не похоже на ссылку YouTube"}, status_code=400)

    def _extract():
        with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True, "noplaylist": True}) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        d = await asyncio.to_thread(_extract)
        return {
            "title": d.get("title"),
            "duration": d.get("duration"),
            "thumbnail": d.get("thumbnail"),
            "uploader": d.get("uploader"),
        }
    except Exception as e:
        return JSONResponse({"error": str(e)[:300]}, status_code=500)


@app.get("/api/download")
async def download(url: str, fmt: str = "video", quality: str = "720"):
    """Скачивает и отдаёт файл. fmt = video|audio; quality = 360|720|1080|best."""
    if not YT_RE.search(url or ""):
        return JSONResponse({"error": "Это не похоже на ссылку YouTube"}, status_code=400)

    job = DL_DIR / uuid.uuid4().hex
    job.mkdir()
    outtmpl = str(job / "%(title)s.%(ext)s")

    opts = {
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": False,
    }
    if FFMPEG_DIR:
        opts["ffmpeg_location"] = FFMPEG_DIR

    if fmt == "audio":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    else:
        if quality == "best":
            opts["format"] = "bestvideo+bestaudio/best"
        else:
            q = re.sub(r"\D", "", quality) or "720"
            opts["format"] = f"bestvideo[height<={q}]+bestaudio/best[height<={q}]/best"
        opts["merge_output_format"] = "mp4"

    def _run():
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return info

    try:
        await asyncio.to_thread(_run)
    except Exception as e:
        shutil.rmtree(job, ignore_errors=True)
        return JSONResponse({"error": str(e)[:300]}, status_code=500)

    files = [p for p in job.iterdir() if p.is_file()]
    if not files:
        shutil.rmtree(job, ignore_errors=True)
        return JSONResponse({"error": "Файл не создан"}, status_code=500)
    f = max(files, key=lambda p: p.stat().st_size)

    # отдаём файл; чистим папку после отправки
    return FileResponse(
        f, filename=f.name, media_type="application/octet-stream",
        background=_cleanup(job),
    )


def _cleanup(job: Path):
    from starlette.background import BackgroundTask
    return BackgroundTask(lambda: shutil.rmtree(job, ignore_errors=True))


# ── фронт (одна страница, без сборки) ──
PAGE = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>YT Downloader</title>
<style>
  :root{--bg:#0f0f12;--card:#1a1a1f;--line:#2a2a31;--ink:#ececed;--gray:#8a8d93;
    --accent:#ff5c5c;--accent2:#ff8a33}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--ink);font-family:system-ui,'Segoe UI',sans-serif;
    min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:16px;
    padding:34px 30px;width:100%;max-width:520px;box-shadow:0 20px 60px rgba(0,0,0,.5)}
  h1{font-size:24px;margin-bottom:4px}
  .sub{color:var(--gray);font-size:13px;margin-bottom:22px}
  input[type=text]{width:100%;padding:13px 15px;border-radius:10px;border:1px solid var(--line);
    background:#101015;color:var(--ink);font-size:15px;outline:none}
  input[type=text]:focus{border-color:var(--accent)}
  .row{display:flex;gap:10px;margin-top:14px;flex-wrap:wrap}
  .seg{display:flex;background:#101015;border:1px solid var(--line);border-radius:10px;overflow:hidden;flex:1}
  .seg button{flex:1;padding:11px;border:none;background:none;color:var(--gray);
    cursor:pointer;font-size:14px;font-weight:600}
  .seg button.on{background:var(--accent);color:#fff}
  select{padding:11px 13px;border-radius:10px;border:1px solid var(--line);background:#101015;
    color:var(--ink);font-size:14px;outline:none;cursor:pointer}
  .go{margin-top:18px;width:100%;padding:14px;border:none;border-radius:10px;cursor:pointer;
    font-size:15px;font-weight:700;color:#fff;background:linear-gradient(90deg,var(--accent),var(--accent2))}
  .go:disabled{opacity:.5;cursor:default}
  .meta{margin-top:18px;display:none;gap:14px;align-items:center;background:#101015;
    border:1px solid var(--line);border-radius:10px;padding:12px}
  .meta img{width:120px;border-radius:8px}
  .meta .t{font-size:14px;font-weight:600;line-height:1.3}
  .meta .u{font-size:12px;color:var(--gray);margin-top:3px}
  .status{margin-top:14px;font-size:13px;color:var(--gray);min-height:18px;text-align:center}
  .err{color:var(--accent)}
</style></head><body>
<div class="card">
  <h1>YT Downloader</h1>
  <div class="sub">Вставь ссылку на YouTube — скачай видео или аудио <span style="opacity:.5">· v2</span></div>
  <input type="text" id="url" placeholder="https://youtube.com/watch?v=...">
  <div class="row">
    <div class="seg" id="fmt">
      <button data-v="video" class="on">Видео</button>
      <button data-v="audio">Аудио (mp3)</button>
    </div>
    <select id="quality">
      <option value="360">360p</option>
      <option value="720" selected>720p</option>
      <option value="1080">1080p</option>
      <option value="best">Макс</option>
    </select>
  </div>
  <div class="meta" id="meta"><img id="thumb"><div><div class="t" id="mtitle"></div><div class="u" id="muploader"></div></div></div>
  <button class="go" id="go">Скачать</button>
  <div class="status" id="status"></div>
</div>
<script>
let fmt='video';
const $=s=>document.querySelector(s);
document.querySelectorAll('#fmt button').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('#fmt button').forEach(x=>x.classList.remove('on'));
  b.classList.add('on');fmt=b.dataset.v;
  $('#quality').style.display=fmt==='audio'?'none':'';
});
let infoTimer;
$('#url').addEventListener('input',()=>{
  clearTimeout(infoTimer);
  const u=$('#url').value.trim();
  $('#meta').style.display='none';
  if(!u)return;
  infoTimer=setTimeout(async()=>{
    try{
      const r=await fetch('/api/info?url='+encodeURIComponent(u));
      const d=await r.json();
      if(d.error)return;
      $('#thumb').src=d.thumbnail||'';
      $('#mtitle').textContent=d.title||'';
      $('#muploader').textContent=(d.uploader||'')+(d.duration?' · '+fmtDur(d.duration):'');
      $('#meta').style.display='flex';
    }catch(e){}
  },600);
});
function fmtDur(s){const m=Math.floor(s/60),x=s%60;return m+':'+String(x).padStart(2,'0');}
$('#go').onclick=async()=>{
  const u=$('#url').value.trim();
  if(!u){setStatus('Вставь ссылку',true);return;}
  $('#go').disabled=true;setStatus('Качаю... это может занять минуту');
  const q=$('#quality').value;
  const link='/api/download?url='+encodeURIComponent(u)+'&fmt='+fmt+'&quality='+q;
  try{
    const r=await fetch(link);
    if(!r.ok){const d=await r.json().catch(()=>({}));setStatus(d.error||'Ошибка',true);$('#go').disabled=false;return;}
    const blob=await r.blob();
    const cd=r.headers.get('content-disposition')||'';
    let name=decodeURIComponent((cd.match(/filename\\*?=(?:UTF-8'')?\"?([^\";]+)/)||[])[1]||'download');
    const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=name;a.click();
    setStatus('Готово ✓');
  }catch(e){setStatus('Ошибка сети',true);}
  $('#go').disabled=false;
};
function setStatus(t,err){const s=$('#status');s.textContent=t;s.className='status'+(err?' err':'');}
</script>
</body></html>"""


if __name__ == "__main__":
    ensure_ffmpeg()
    ensure_deno()
    port = int(os.environ.get("SERVER_PORT") or os.environ.get("PORT") or 25748)
    print(f"[start] слушаю 0.0.0.0:{port}", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
