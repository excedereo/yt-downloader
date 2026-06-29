"""
YT Downloader — скачивание видео/аудио с YouTube. FastAPI + yt-dlp.

Сервис hub: отдаёт APIRouter, монтируется ядром на префикс /yt.
ffmpeg/deno докачиваются ядром через shared.binaries (объявлены в needs).
"""

import re
import uuid
import shutil
import asyncio
import json
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, StreamingResponse
import yt_dlp

from shared import binaries

router = APIRouter()

BASE = Path(__file__).resolve().parent
DL_DIR = BASE / "downloads"
DL_DIR.mkdir(exist_ok=True)

YT_RE = re.compile(r"(youtube\.com|youtu\.be)", re.I)
MAX_DURATION = 15 * 60   # лимит длительности видео, секунд (15 минут)


@router.get("/", response_class=HTMLResponse)
def index():
    return PAGE


@router.get("/api/info")
async def info(url: str):
    """Метаданные по ссылке: название, длительность, превью."""
    if not YT_RE.search(url or ""):
        return JSONResponse({"error": "Это не похоже на ссылку YouTube"}, status_code=400)

    def _extract():
        with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True, "noplaylist": True,
                               "remote_components": ["ejs:github"]}) as ydl:
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


def build_opts(job: Path, fmt: str, quality: str, progress_hook=None):
    """Собирает опции yt-dlp для скачивания в папку job."""
    opts = {
        "outtmpl": str(job / "%(title)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": False,
        "remote_components": ["ejs:github"],   # EJS-скрипты для YouTube-challenge (Deno)
        "socket_timeout": 60,
        "retries": 10,
        "fragment_retries": 10,
        "http_chunk_size": 10485760,
        "concurrent_fragment_downloads": 4,
    }
    if progress_hook:
        opts["progress_hooks"] = [progress_hook]
    if binaries.FFMPEG_DIR:
        opts["ffmpeg_location"] = binaries.FFMPEG_DIR

    if fmt == "audio":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192",
        }]
    else:
        if quality == "best":
            opts["format"] = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"
        else:
            q = re.sub(r"\D", "", quality) or "720"
            opts["format"] = (
                f"bestvideo[height<={q}][ext=mp4]+bestaudio[ext=m4a]/"
                f"bestvideo[height<={q}]+bestaudio/best[height<={q}]/best"
            )
        opts["merge_output_format"] = "mp4"
        # звук в AAC при склейке (Opus не играет в Windows-плеере)
        opts["postprocessor_args"] = {"merger": ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k"]}
    return opts


def _cleanup(job: Path):
    from starlette.background import BackgroundTask
    return BackgroundTask(lambda: shutil.rmtree(job, ignore_errors=True))


@router.get("/api/prepare")
async def prepare(url: str, fmt: str = "video", quality: str = "720"):
    """SSE-стрим: качает с реальным прогрессом, в конце отдаёт job_id+имя файла."""
    if not YT_RE.search(url or ""):
        async def err():
            yield 'data: ' + json.dumps({"error": "Это не похоже на ссылку YouTube"}) + '\n\n'
        return StreamingResponse(err(), media_type="text/event-stream")

    loop = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue()
    job = DL_DIR / uuid.uuid4().hex

    def hook(d):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            pct = (done / total * 100) if total else 0
            loop.call_soon_threadsafe(queue.put_nowait, {"percent": round(pct, 1)})
        elif d.get("status") == "finished":
            loop.call_soon_threadsafe(queue.put_nowait, {"percent": 100, "stage": "merge"})

    def worker():
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True, "noplaylist": True,
                                   "remote_components": ["ejs:github"]}) as ydl:
                meta = ydl.extract_info(url, download=False)
            dur = meta.get("duration") or 0
            if dur > MAX_DURATION:
                loop.call_soon_threadsafe(queue.put_nowait, {
                    "error": f"Видео длиннее {MAX_DURATION // 60} минут — лимит сервиса"})
                return
            job.mkdir(exist_ok=True)
            with yt_dlp.YoutubeDL(build_opts(job, fmt, quality, hook)) as ydl:
                ydl.extract_info(url, download=True)
            files = [p for p in job.iterdir() if p.is_file()]
            if not files:
                loop.call_soon_threadsafe(queue.put_nowait, {"error": "Файл не создан"})
                return
            f = max(files, key=lambda p: p.stat().st_size)
            loop.call_soon_threadsafe(queue.put_nowait,
                                      {"done": True, "job": job.name, "filename": f.name})
        except Exception as e:
            shutil.rmtree(job, ignore_errors=True)
            loop.call_soon_threadsafe(queue.put_nowait, {"error": str(e)[:300]})
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    asyncio.ensure_future(asyncio.to_thread(worker))

    async def stream():
        while True:
            item = await queue.get()
            if item is None:
                break
            yield 'data: ' + json.dumps(item) + '\n\n'

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/api/file/{job_id}")
async def get_file(job_id: str):
    """Отдаёт готовый файл по job_id и чистит папку после отправки."""
    if not re.fullmatch(r"[0-9a-f]{32}", job_id or ""):
        return JSONResponse({"error": "bad job"}, status_code=400)
    job = DL_DIR / job_id
    files = [p for p in job.iterdir() if p.is_file()] if job.exists() else []
    if not files:
        return JSONResponse({"error": "Файл не найден или уже скачан"}, status_code=404)
    f = max(files, key=lambda p: p.stat().st_size)
    return FileResponse(f, filename=f.name, media_type="application/octet-stream",
                        background=_cleanup(job))


# ── фронт (одна страница, без сборки) ──
PAGE = r"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>YT Downloader</title>
<link rel="preconnect" href="https://cdnjs.cloudflare.com">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.2/css/all.min.css">
<style>
  :root{
    --bg:#0f0f0f;--card:#181818;--soft:#212121;--line:#303030;
    --ink:#f1f1f1;--gray:#aaa;--yt:#fe0032;--yt-dim:#c80028;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:radial-gradient(1200px 600px at 50% -10%,#1a1213 0%,var(--bg) 55%);
    color:var(--ink);font-family:'Segoe UI',system-ui,Roboto,sans-serif;
    min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:18px;
    padding:32px 28px;width:100%;max-width:500px;box-shadow:0 24px 70px rgba(0,0,0,.55)}
  .brand{display:flex;align-items:center;gap:11px;margin-bottom:4px}
  .brand .logo{width:38px;height:38px;border-radius:10px;background:var(--yt);
    display:grid;place-items:center;color:#fff;font-size:18px;box-shadow:0 6px 16px rgba(254,0,50,.35)}
  .brand h1{font-size:21px;font-weight:700;letter-spacing:-.3px}
  .sub{color:var(--gray);font-size:13px;margin:6px 0 22px}

  .field{position:relative}
  .field>i{position:absolute;left:15px;top:50%;transform:translateY(-50%);color:var(--gray);font-size:14px}
  input[type=text]{width:100%;padding:13px 15px 13px 40px;border-radius:12px;
    border:1px solid var(--line);background:var(--soft);color:var(--ink);font-size:15px;
    outline:none;transition:border-color .15s,box-shadow .15s}
  input[type=text]:focus{border-color:var(--yt);box-shadow:0 0 0 3px rgba(254,0,50,.15)}
  input[type=text]:focus + i{color:var(--yt)}

  .row{display:flex;gap:10px;margin-top:14px}
  .seg{display:flex;background:var(--soft);border:1px solid var(--line);
    border-radius:12px;padding:4px;flex:1;gap:4px}
  .seg button{flex:1;padding:9px;border:none;background:none;color:var(--gray);
    cursor:pointer;font-size:14px;font-weight:600;border-radius:9px;
    display:flex;align-items:center;justify-content:center;gap:7px;transition:.15s}
  .seg button.on{background:var(--yt);color:#fff;box-shadow:0 4px 12px rgba(254,0,50,.3)}
  .seg button:not(.on):hover{color:var(--ink)}

  /* кастомный дропдаун */
  .dd{position:relative;min-width:118px}
  .dd-btn{width:100%;height:100%;padding:11px 13px;border-radius:12px;
    border:1px solid var(--line);background:var(--soft);color:var(--ink);
    font-size:14px;font-weight:600;cursor:pointer;display:flex;align-items:center;
    justify-content:space-between;gap:8px;transition:.15s}
  .dd-btn:hover{border-color:#454545}
  .dd-btn .fa-chevron-down{font-size:11px;color:var(--gray);transition:transform .2s}
  .dd.open .dd-btn{border-color:var(--yt)}
  .dd.open .fa-chevron-down{transform:rotate(180deg)}
  .dd-menu{position:absolute;top:calc(100% + 6px);left:0;right:0;background:#1d1d1d;
    border:1px solid var(--line);border-radius:12px;padding:5px;z-index:20;
    box-shadow:0 14px 34px rgba(0,0,0,.5);opacity:0;transform:translateY(-6px);
    pointer-events:none;transition:.16s}
  .dd.open .dd-menu{opacity:1;transform:translateY(0);pointer-events:auto}
  .dd-menu div{padding:9px 11px;border-radius:8px;font-size:14px;cursor:pointer;
    display:flex;align-items:center;justify-content:space-between;color:var(--gray)}
  .dd-menu div:hover{background:var(--soft);color:var(--ink)}
  .dd-menu div.sel{color:var(--ink)}
  .dd-menu div.sel::after{content:"\f00c";font-family:"Font Awesome 6 Free";
    font-weight:900;color:var(--yt);font-size:12px}
  .dd.hide{display:none}

  .meta{margin-top:18px;display:none;gap:13px;align-items:center;background:var(--soft);
    border:1px solid var(--line);border-radius:12px;padding:11px;animation:fade .25s}
  .meta img{width:118px;height:66px;object-fit:cover;border-radius:9px;flex-shrink:0;background:#000}
  .meta .t{font-size:14px;font-weight:600;line-height:1.32;
    display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
  .meta .u{font-size:12px;color:var(--gray);margin-top:4px;display:flex;align-items:center;gap:6px}
  @keyframes fade{from{opacity:0;transform:translateY(4px)}to{opacity:1}}

  .go{margin-top:18px;width:100%;padding:14px;border:none;border-radius:12px;cursor:pointer;
    font-size:15px;font-weight:700;color:#fff;background:var(--yt);
    display:flex;align-items:center;justify-content:center;gap:9px;transition:.15s}
  .go:hover:not(:disabled){background:var(--yt-dim)}
  .go:disabled{opacity:.45;cursor:not-allowed;background:#3a3a3a}

  .pbar{margin-top:16px;display:none;background:var(--soft);border:1px solid var(--line);
    border-radius:11px;overflow:hidden;height:26px;position:relative}
  .pbar-fill{height:100%;width:0%;background:var(--yt);transition:width .3s ease}
  .pbar-txt{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
    font-size:12px;font-weight:700;color:#fff;text-shadow:0 1px 2px rgba(0,0,0,.5)}
  .pbar.merge .pbar-fill{animation:pulse 1.1s ease-in-out infinite}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.55}}

  .status{margin-top:13px;font-size:13px;color:var(--gray);min-height:18px;text-align:center;
    display:flex;align-items:center;justify-content:center;gap:7px}
  .status.err{color:var(--yt)}
  .status.ok{color:#4ec98a}
</style></head><body>
<div class="card">
  <div class="brand">
    <div class="logo"><i class="fa-brands fa-youtube"></i></div>
    <h1>YT Downloader</h1>
  </div>
  <div class="sub">Вставь ссылку на YouTube — скачай видео или аудио. Лимит 15 минут.</div>

  <div class="field">
    <input type="text" id="url" placeholder="https://youtube.com/watch?v=..." autocomplete="off">
    <i class="fa-solid fa-link"></i>
  </div>

  <div class="row">
    <div class="seg" id="fmt">
      <button data-v="video" class="on"><i class="fa-solid fa-film"></i>Видео</button>
      <button data-v="audio"><i class="fa-solid fa-music"></i>Аудио</button>
    </div>
    <div class="dd" id="qdd">
      <button class="dd-btn" type="button">
        <span><i class="fa-solid fa-gauge-high" style="margin-right:7px;color:var(--gray)"></i><span id="qlabel">720p</span></span>
        <i class="fa-solid fa-chevron-down"></i>
      </button>
      <div class="dd-menu">
        <div data-v="360">360p</div>
        <div data-v="720" class="sel">720p</div>
        <div data-v="1080">1080p</div>
        <div data-v="best">Максимум</div>
      </div>
    </div>
  </div>

  <div class="meta" id="meta">
    <img id="thumb" alt="">
    <div><div class="t" id="mtitle"></div>
      <div class="u" id="muploader"></div></div>
  </div>

  <button class="go" id="go" disabled><i class="fa-solid fa-download"></i><span id="golabel">Скачать</span></button>

  <div class="pbar" id="pbar"><div class="pbar-fill" id="pfill"></div><div class="pbar-txt" id="ptxt">0%</div></div>
  <div class="status" id="status"></div>
</div>
<script>
const $=s=>document.querySelector(s);
const MAX_DUR=15*60;
let fmt='video', quality='720';
let infoState='empty';   // empty | loading | ok | error | toolong
let curDur=0, infoTimer, es=null;

// ── переключатель формата ──
document.querySelectorAll('#fmt button').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('#fmt button').forEach(x=>x.classList.remove('on'));
  b.classList.add('on'); fmt=b.dataset.v;
  $('#qdd').classList.toggle('hide', fmt==='audio');
});

// ── кастомный дропдаун качества ──
const qdd=$('#qdd');
qdd.querySelector('.dd-btn').onclick=e=>{e.stopPropagation();qdd.classList.toggle('open');};
qdd.querySelectorAll('.dd-menu div').forEach(d=>d.onclick=()=>{
  quality=d.dataset.v;
  qdd.querySelectorAll('.dd-menu div').forEach(x=>x.classList.remove('sel'));
  d.classList.add('sel');
  $('#qlabel').textContent=d.textContent;
  qdd.classList.remove('open');
});
document.addEventListener('click',()=>qdd.classList.remove('open'));

// ── ввод ссылки: всё сбрасываем, тянем инфу ──
$('#url').addEventListener('input',()=>{
  clearTimeout(infoTimer);
  if(es){es.close();es=null;}
  resetUI();
  const u=$('#url').value.trim();
  if(!u){infoState='empty';refreshBtn();return;}
  infoState='loading';refreshBtn();
  setStatus('Получаю инфо о видео...','load');
  infoTimer=setTimeout(()=>fetchInfo(u),600);
});

async function fetchInfo(u){
  try{
    const r=await fetch('api/info?url='+encodeURIComponent(u));
    const d=await r.json();
    if(d.error){infoState='error';setStatus(d.error,'err');refreshBtn();return;}
    curDur=d.duration||0;
    $('#thumb').src=d.thumbnail||'';
    $('#mtitle').textContent=d.title||'';
    $('#muploader').innerHTML='<i class="fa-solid fa-user"></i>'+(d.uploader||'—')+
      (curDur?' &nbsp;·&nbsp; <i class="fa-solid fa-clock"></i>'+fmtDur(curDur):'');
    $('#meta').style.display='flex';
    if(curDur>MAX_DUR){infoState='toolong';setStatus('Видео длиннее 15 минут — лимит сервиса','err');}
    else{infoState='ok';setStatus('');}
    refreshBtn();
  }catch(e){infoState='error';setStatus('Не удалось получить инфо','err');refreshBtn();}
}

function refreshBtn(){
  const go=$('#go'), lbl=$('#golabel');
  go.disabled = infoState!=='ok';
  if(infoState==='loading'){lbl.textContent='Загрузка инфо...';}
  else if(infoState==='toolong'){lbl.textContent='Слишком длинное';}
  else{lbl.textContent='Скачать';}
}

function resetUI(){
  $('#meta').style.display='none';
  $('#thumb').src='';$('#mtitle').textContent='';$('#muploader').textContent='';
  $('#pbar').style.display='none';$('#pbar').classList.remove('merge');
  $('#pfill').style.width='0%';$('#ptxt').textContent='0%';
  setStatus('');curDur=0;
}

function fmtDur(s){const m=Math.floor(s/60),x=s%60;return m+':'+String(x).padStart(2,'0');}

function setProg(p,stage){
  $('#pbar').style.display='block';
  $('#pbar').classList.toggle('merge',stage==='merge');
  $('#pfill').style.width=p+'%';
  $('#ptxt').textContent=stage==='merge'?'Склейка...':(p.toFixed(0)+'%');
}

function setStatus(t,kind){
  const s=$('#status');
  const ic=kind==='err'?'<i class="fa-solid fa-circle-exclamation"></i>':
           kind==='ok'?'<i class="fa-solid fa-circle-check"></i>':
           kind==='load'?'<i class="fa-solid fa-spinner fa-spin"></i>':'';
  s.innerHTML=t?ic+'<span>'+t+'</span>':'';
  s.className='status'+(kind==='err'?' err':kind==='ok'?' ok':'');
}

// ── скачивание ──
$('#go').onclick=()=>{
  if(infoState!=='ok')return;
  const u=$('#url').value.trim();
  $('#go').disabled=true;$('#golabel').textContent='Качаю...';
  setProg(0);setStatus('Скачиваю...','load');
  es=new EventSource('api/prepare?url='+encodeURIComponent(u)+'&fmt='+fmt+'&quality='+quality);
  es.onmessage=ev=>{
    let d;try{d=JSON.parse(ev.data);}catch(e){return;}
    if(d.error){es.close();es=null;setStatus(d.error,'err');
      $('#pbar').style.display='none';infoState='ok';refreshBtn();return;}
    if(d.percent!=null)setProg(d.percent,d.stage);
    if(d.done){
      es.close();es=null;
      const a=document.createElement('a');
      a.href='api/file/'+d.job;a.download=d.filename||'download';
      document.body.appendChild(a);a.click();a.remove();
      setProg(100);$('#pbar').classList.remove('merge');$('#ptxt').textContent='Готово';
      setStatus('Файл скачивается','ok');
      infoState='ok';refreshBtn();
    }
  };
  es.onerror=()=>{es.close();es=null;setStatus('Ошибка соединения','err');
    $('#pbar').style.display='none';infoState='ok';refreshBtn();};
};
</script>
</body></html>"""
