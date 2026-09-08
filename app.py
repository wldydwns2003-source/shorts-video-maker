from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import threading
import uuid
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import yt_dlp
import edge_tts

ROOT = Path(__file__).parent.resolve()
STATIC = ROOT
WORK = ROOT / "work"
WORK.mkdir(exist_ok=True)

app = FastAPI(title="쇼츠 영상 소스 메이커")
jobs: dict[str, dict] = {}


class AnalyzeRequest(BaseModel):
    script: str
    results_per_scene: int = 4


class RenderRequest(BaseModel):
    script: str
    scenes: list[dict]
    voice: str = "ko-KR-SunHiNeural"
    width: int = 1080
    height: int = 1920


def run(cmd: list[str], cwd: Path | None = None) -> str:
    p = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)
    if p.returncode:
        raise RuntimeError((p.stderr or p.stdout)[-4000:])
    return p.stdout


def require_tools() -> None:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RuntimeError("FFmpeg가 설치되어 있지 않습니다. install.bat을 먼저 실행하세요.")


def split_script(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text.strip())
    parts = re.split(r"(?<=[.!?])\s+|(?<=다\.)|(?<=요\.)", text)
    return [p.strip() for p in parts if len(p.strip()) >= 2][:12]


def search_query(sentence: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z가-힣 ]", " ", sentence)
    stop = {"이거", "진짜", "정말", "그냥", "하면", "해서", "있는데", "됩니다", "합니다", "그리고", "때문에", "바로"}
    words = [w for w in cleaned.split() if len(w) > 1 and w not in stop]
    return " ".join(words[:7]) or cleaned.strip()


def yt_search(query: str, limit: int) -> list[dict]:
    opts = {"quiet": True, "skip_download": True, "extract_flat": True, "playlistend": limit, "noplaylist": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
    out = []
    for e in (info or {}).get("entries", []):
        if not e:
            continue
        vid = e.get("id")
        out.append({
            "id": vid,
            "title": e.get("title") or "제목 없음",
            "url": e.get("url") if str(e.get("url", "")).startswith("http") else f"https://www.youtube.com/watch?v={vid}",
            "thumbnail": e.get("thumbnail") or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
            "duration": e.get("duration"),
            "channel": e.get("channel") or e.get("uploader") or "",
        })
    return out


@app.post("/api/analyze")
def analyze(req: AnalyzeRequest):
    scenes = split_script(req.script)
    if not scenes:
        raise HTTPException(400, "대본을 입력하세요.")
    result = []
    for i, sentence in enumerate(scenes):
        q = search_query(sentence)
        try:
            videos = yt_search(q, max(1, min(req.results_per_scene, 6)))
        except Exception as e:
            videos = []
        result.append({"index": i, "sentence": sentence, "query": q, "videos": videos})
    return {"scenes": result}


def probe_duration(path: Path) -> float:
    raw = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nk=1:nw=1", str(path)])
    return float(raw.strip())


def download_video(url: str, target: Path) -> Path:
    if not re.match(r"^https?://(www\.)?(youtube\.com|youtu\.be)/", url):
        raise RuntimeError("현재 버전은 공개 YouTube URL만 지원합니다.")
    opts = {
        "format": "bv*[height<=1080]+ba/b[height<=1080]/best",
        "outtmpl": str(target.with_suffix(".%(ext)s")),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = Path(ydl.prepare_filename(info))
    merged = target.with_suffix(".mp4")
    return merged if merged.exists() else path


async def create_tts(text: str, voice: str, out: Path) -> None:
    await edge_tts.Communicate(text, voice).save(str(out))


def render_job(job_id: str, req: RenderRequest) -> None:
    folder = WORK / job_id
    folder.mkdir(parents=True, exist_ok=True)
    try:
        require_tools()
        jobs[job_id] = {"status": "working", "progress": 3, "message": "TTS 생성 중"}
        audio = folder / "tts.mp3"
        asyncio.run(create_tts(req.script, req.voice, audio))
        total = probe_duration(audio)
        selected = [s for s in req.scenes if s.get("url")]
        if not selected:
            raise RuntimeError("선택된 영상이 없습니다.")
        weights = [max(1, len(s.get("sentence", ""))) for s in selected]
        weight_sum = sum(weights)
        clips = []
        for i, (scene, weight) in enumerate(zip(selected, weights)):
            jobs[job_id] = {"status": "working", "progress": 8 + int(50 * i / len(selected)), "message": f"영상 {i+1}/{len(selected)} 다운로드 중"}
            src = download_video(scene["url"], folder / f"source-{i}")
            clip_len = max(1.5, total * weight / weight_sum)
            source_len = probe_duration(src)
            available = max(0.0, source_len - clip_len)
            start = min(available, available * ((i * 37) % 100) / 100)
            clip = folder / f"clip-{i}.mp4"
            vf = f"scale={req.width}:{req.height}:force_original_aspect_ratio=increase,crop={req.width}:{req.height},fps=30,setsar=1"
            run(["ffmpeg", "-y", "-ss", f"{start:.3f}", "-stream_loop", "-1", "-i", str(src), "-t", f"{clip_len:.3f}", "-an", "-vf", vf,
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", str(clip)])
            clips.append(clip)
        concat = folder / "concat.txt"
        concat.write_text("".join(f"file '{p.as_posix()}'\n" for p in clips), encoding="utf-8")
        silent = folder / "video.mp4"
        jobs[job_id] = {"status": "working", "progress": 78, "message": "자동 타임라인 합치는 중"}
        run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat), "-c", "copy", str(silent)])
        output = folder / "shorts-complete.mp4"
        jobs[job_id] = {"status": "working", "progress": 90, "message": "MP4 마무리 중"}
        run(["ffmpeg", "-y", "-i", str(silent), "-i", str(audio), "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart", str(output)])
        jobs[job_id] = {"status": "done", "progress": 100, "message": "완료", "download": f"/api/download/{job_id}"}
    except Exception as e:
        jobs[job_id] = {"status": "error", "progress": 0, "message": str(e)}


@app.post("/api/render")
def render(req: RenderRequest, tasks: BackgroundTasks):
    job_id = uuid.uuid4().hex
    jobs[job_id] = {"status": "queued", "progress": 0, "message": "준비 중"}
    tasks.add_task(render_job, job_id, req)
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def job(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "작업을 찾을 수 없습니다.")
    return jobs[job_id]


@app.get("/api/download/{job_id}")
def download(job_id: str):
    path = WORK / job_id / "shorts-complete.mp4"
    if not path.exists():
        raise HTTPException(404, "완성 파일이 없습니다.")
    return FileResponse(path, media_type="video/mp4", filename="shorts-complete.mp4")


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
