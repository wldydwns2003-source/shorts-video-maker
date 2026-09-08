from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import threading
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Annotated

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import threading
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Annotated

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import yt_dlp
import edge_tts
import requests
from bs4 import BeautifulSoup
from xml.etree import ElementTree
from urllib.parse import quote_plus, unquote, urlparse, parse_qs

ROOT = Path(__file__).parent.resolve()
STATIC = ROOT
WORK = ROOT / "work"
WORK.mkdir(exist_ok=True)

app = FastAPI(title="쇼츠 영상 소스 메이커")
jobs: dict[str, dict] = {}


class AnalyzeRequest(BaseModel):
    script: str
    results_per_scene: int = 4


class DiscoverRequest(BaseModel):
    script: str
    per_platform: int = 8


class RenderRequest(BaseModel):
    script: str
    scenes: list[dict]
    voice: str = "ko-KR-SunHiNeural"
    width: int = 1080
    height: int = 1920


class UploadedRenderRequest(BaseModel):
    script: str
    video_tokens: list[str]
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


def detect_topic(script: str) -> str:
    rules = [
        (("화장실", "청소"), "화장실 청소"),
        (("욕실", "청소"), "욕실 청소"),
        (("벽지", "페인트"), "벽지 페인트 셀프 시공"),
        (("도배", "페인트"), "벽지 페인트 셀프 시공"),
        (("욕실", "리모델링"), "욕실 셀프 리모델링"),
        (("주방", "정리"), "주방 정리용품"),
        (("곰팡이",), "벽 곰팡이 제거"),
    ]
    for needles, topic in rules:
        if all(n in script for n in needles):
            return topic
    cleaned = re.sub(r"[^0-9A-Za-z가-힣 ]", " ", script)
    stop = {"여러분", "이거", "하나로", "진짜", "정말", "그냥", "저희", "우리", "그런데", "그리고", "하면", "해서", "있는", "있는데", "발견했어요", "더라고요", "없는데", "정보는", "남겨", "주세요"}
    words = [w for w in cleaned.split() if 2 <= len(w) <= 12 and w not in stop and not w.isdigit()]
    ranked = [w for w, _ in Counter(words).most_common(4)]
    return " ".join(ranked[:3]) or "생활용품 사용법"


def search_query(sentence: str, topic: str = "") -> str:
    cleaned = re.sub(r"[^0-9A-Za-z가-힣 ]", " ", sentence)
    stop = {"여러분", "이거", "하나로", "진짜", "정말", "그냥", "하면", "해서", "있는데", "됩니다", "합니다", "그리고", "그런데", "때문에", "바로", "저희", "우리", "발견했어요", "더라고요", "정보는", "남겨", "주세요"}
    words = [w for w in cleaned.split() if 2 <= len(w) <= 12 and w not in stop and not w.isdigit()]
    actions = []
    action_rules = [
        (("얼룩", "누렇게", "도배"), "누런 얼룩 제거 전후"),
        (("바르", "쓱쓱", "섞어"), "바르는 방법 작업 과정"),
        (("마르", "건조"), "페인트 건조 과정"),
        (("냄새", "친환경"), "친환경 페인트 후기"),
        (("4L", "용량", "구매"), "대용량 제품 사용 후기"),
    ]
    for needles, phrase in action_rules:
        if any(n in sentence for n in needles):
            actions.append(phrase)
    extra = " ".join(actions[:1] or words[:3])
    return f"{topic} {extra}".strip()


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


SUBTITLE_WORDS = {"자막", "字幕", "subtitles", "caption", "captions", "lyrics", "가사", "텍스트"}


def likely_clean(title: str, description: str = "") -> bool:
    haystack = f"{title} {description}".lower()
    return not any(word.lower() in haystack for word in SUBTITLE_WORDS)


def unwrap_search_url(href: str) -> str:
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if "duckduckgo.com" in parsed.netloc:
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        return unquote(target) if target else href
    return href


def public_web_search(query: str, limit: int) -> list[dict]:
    headers = {"User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/124 Mobile Safari/537.36"}
    items = []
    engines = [
        ("https://www.bing.com/search", {"q": query, "count": min(50, limit * 4)}, "bing"),
        ("https://html.duckduckgo.com/html/", {"q": query}, "duck"),
        ("https://www.google.com/search", {"q": query, "num": min(20, limit * 2)}, "google"),
    ]
    seen = set()
    # Bing RSS is substantially more reliable than search-result HTML on
    # datacenter hosts such as Render and does not depend on brittle CSS.
    try:
        response = requests.get(
            "https://www.bing.com/search",
            params={"q": query, "format": "rss", "count": min(50, limit * 4)},
            headers=headers,
            timeout=4,
        )
        response.raise_for_status()
        root = ElementTree.fromstring(response.content)
        for row in root.findall(".//item"):
            url = (row.findtext("link") or "").strip()
            title = (row.findtext("title") or "").strip()
            snippet = (row.findtext("description") or "").strip()
            if url and url not in seen and title and likely_clean(title, snippet):
                seen.add(url)
                items.append({"title": title, "url": url, "description": snippet[:300]})
            if len(items) >= limit:
                return items
    except Exception:
        pass
    for endpoint, params, engine in engines:
        try:
            response = requests.get(endpoint, params=params, headers=headers, timeout=4)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            rows = []
            if engine == "bing":
                rows = [(a, a.find_parent("li")) for a in soup.select("li.b_algo h2 a")]
            elif engine == "duck":
                rows = [(a, a.find_parent(class_="result")) for a in soup.select("a.result__a")]
            else:
                rows = [(a, a.find_parent("div")) for a in soup.select("a[href]") if a.get("href", "").startswith("/url?q=")]
            for link, row in rows:
                href = link.get("href", "")
                if engine == "google":
                    href = parse_qs(urlparse(href).query).get("q", [""])[0]
                url = unwrap_search_url(href)
                title = link.get_text(" ", strip=True)
                snippet = row.get_text(" ", strip=True) if row else ""
                if url and url not in seen and title and likely_clean(title, snippet):
                    seen.add(url)
                    items.append({"title": title, "url": url, "description": snippet[:300]})
                if len(items) >= limit:
                    return items
        except Exception:
            continue
    return items


def platform_results(platform: str, topics: list[dict], limit: int) -> list[dict]:
    config = {
        "tiktok": ("site:tiktok.com", "TikTok"),
        "instagram": ("site:instagram.com/reel", "Instagram Reels"),
        "xiaohongshu": ("site:xiaohongshu.com/explore", "샤오홍슈"),
    }
    prefix, label = config[platform]
    required = {"tiktok": "tiktok.com", "instagram": "instagram.com", "xiaohongshu": "xiaohongshu.com"}[platform]
    results = []
    seen = set()
    # Search each language separately. A single giant OR query is commonly
    # ignored by search engines and was the reason non-YouTube results were 0.
    for translated in topics:
        raw = public_web_search(f'{prefix} "{translated["query"]}" short video', max(4, limit))
        for item in raw:
            url = item["url"]
            if required not in url or url in seen:
                continue
            seen.add(url)
            results.append({**item, "platform": platform, "platform_label": label, "duration": None, "thumbnail": None, "clean_score": 80, "language": translated["language"]})
            if len(results) >= limit:
                return results
    return results


LANGUAGES = ["ko", "en", "zh-CN", "ja", "es", "pt", "fr", "de"]


def translate_topic(topic: str, language: str) -> str:
    if language == "ko":
        return topic
    try:
        response = requests.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "auto", "tl": language, "dt": "t", "q": topic},
            timeout=2,
        )
        response.raise_for_status()
        translated = "".join(part[0] for part in response.json()[0] if part and part[0])
        return translated.strip() or topic
    except Exception:
        pass
    offline = {
        "en": {"화장실": "bathroom", "욕실": "bathroom", "벽지": "wallpaper", "페인트": "paint", "셀프": "DIY", "시공": "application", "얼룩": "stain", "제거": "removal", "주방": "kitchen", "정리": "organization", "청소": "cleaning"},
        "zh-CN": {"화장실": "卫生间", "욕실": "浴室", "벽지": "墙纸", "페인트": "翻新漆", "셀프": "自己动手", "시공": "施工", "얼룩": "污渍", "제거": "去除", "주방": "厨房", "정리": "收纳", "청소": "清洁"},
        "ja": {"화장실": "トイレ", "욕실": "浴室", "벽지": "壁紙", "페인트": "ペイント", "셀프": "DIY", "시공": "施工", "얼룩": "汚れ", "제거": "除去", "주방": "キッチン", "정리": "収納", "청소": "掃除"},
        "es": {"벽지": "papel pintado", "페인트": "pintura", "셀프": "bricolaje", "시공": "aplicación", "청소": "limpieza"},
        "pt": {"벽지": "papel de parede", "페인트": "tinta", "셀프": "faça você mesmo", "시공": "aplicação", "청소": "limpeza"},
        "fr": {"벽지": "papier peint", "페인트": "peinture", "셀프": "DIY", "시공": "application", "청소": "nettoyage"},
        "de": {"벽지": "Tapete", "페인트": "Farbe", "셀프": "Heimwerken", "시공": "Anwendung", "청소": "Reinigung"},
    }
    words = [offline.get(language, {}).get(word, word) for word in topic.split()]
    return " ".join(words)


def multilingual_topics(topic: str) -> list[dict]:
    translated = {"ko": topic}
    with ThreadPoolExecutor(max_workers=7) as pool:
        futures = {pool.submit(translate_topic, topic, lang): lang for lang in LANGUAGES[1:]}
        for future in as_completed(futures):
            lang = futures[future]
            try:
                translated[lang] = future.result()
            except Exception:
                translated[lang] = topic
    unique = []
    seen = set()
    for lang in LANGUAGES:
        text = translated.get(lang, topic).strip()
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            unique.append({"language": lang, "query": text})
    return unique


def merge_unique(groups: list[list[dict]], limit: int) -> list[dict]:
    merged = []
    seen = set()
    for group in groups:
        for item in group:
            url = item.get("url", "")
            if not url or url in seen:
                continue
            seen.add(url)
            merged.append(item)
            if len(merged) >= limit:
                return merged
    return merged


def discover_platform(platform: str, translations: list[dict], limit: int) -> list[dict]:
    queries = [item["query"] for item in translations]
    combined = " OR ".join(f'"{query}"' for query in queries)
    if platform == "youtube":
        videos = yt_search(f"({combined}) shorts", limit * 3)
        clean = []
        for video in videos:
            duration = video.get("duration")
            if duration and float(duration) > 60:
                continue
            if likely_clean(video.get("title", "")):
                clean.append({**video, "platform": "youtube", "platform_label": "YouTube Shorts", "clean_score": 85, "language": "GLOBAL"})
            if len(clean) >= limit:
                break
        return clean
    return platform_results(platform, translations, limit)


def build_discovery(req: DiscoverRequest) -> dict:
    if not req.script.strip():
        raise HTTPException(400, "대본을 입력하세요.")
    topic = detect_topic(req.script)
    total_limit = 10
    search_limit = 10
    translations = multilingual_topics(topic)
    platform_groups = {"youtube": [], "tiktok": [], "instagram": [], "xiaohongshu": []}
    errors = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(discover_platform, platform, translations, search_limit): platform for platform in platform_groups}
        for future in as_completed(futures):
            platform = futures[future]
            try:
                platform_groups[platform] = future.result()
            except Exception as e:
                errors[platform] = str(e)
    all_results = []
    order = ("tiktok", "instagram", "xiaohongshu", "youtube")
    for index in range(search_limit):
        for platform in order:
            group = platform_groups[platform]
            if index < len(group):
                all_results.append(group[index])
                if len(all_results) >= total_limit:
                    break
        if len(all_results) >= total_limit:
            break
    return {"topic": topic, "translations": translations, "results": all_results, "errors": errors}


def discovery_job(job_id: str, req: DiscoverRequest) -> None:
    try:
        jobs[job_id] = {"status": "working", "progress": 15, "message": "검색어를 여러 언어로 변환하는 중"}
        data = build_discovery(req)
        jobs[job_id] = {"status": "done", "progress": 100, "message": "검색 완료", "data": data}
    except Exception as e:
        jobs[job_id] = {"status": "error", "progress": 0, "message": str(e)}


@app.post("/api/discover")
def discover(req: DiscoverRequest, tasks: BackgroundTasks):
    job_id = uuid.uuid4().hex
    jobs[job_id] = {"status": "queued", "progress": 3, "message": "전 세계 숏폼 검색 준비 중"}
    tasks.add_task(discovery_job, job_id, req)
    return {"job_id": job_id}


@app.post("/api/analyze")
def analyze(req: AnalyzeRequest):
    scenes = split_script(req.script)
    if not scenes:
        raise HTTPException(400, "대본을 입력하세요.")
    topic = detect_topic(req.script)
    try:
        common_pool = yt_search(topic, max(8, len(scenes) * 2))
    except Exception:
        common_pool = []
    used_ids: set[str] = set()
    result = []
    for i, sentence in enumerate(scenes):
        q = search_query(sentence, topic)
        try:
            videos = yt_search(q, max(3, min(req.results_per_scene + 2, 6)))
        except Exception:
            videos = []
        candidates = videos + common_pool
        unique = []
        seen = set()
        for video in candidates:
            vid = video.get("id")
            if not vid or vid in seen:
                continue
            seen.add(vid)
            unique.append(video)
        fresh = [v for v in unique if v.get("id") not in used_ids]
        chosen = (fresh or unique)[:max(1, min(req.results_per_scene, 4))]
        if chosen:
            used_ids.add(chosen[0]["id"])
        result.append({"index": i, "sentence": sentence, "query": q, "videos": chosen})
    return {"topic": topic, "scenes": result}


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


def uploaded_video_path(token: str) -> Path:
    parts = token.split("/", 1)
    if len(parts) != 2 or not re.fullmatch(r"[0-9a-f]{32}", parts[0]):
        raise RuntimeError("업로드 영상 정보가 올바르지 않습니다.")
    path = (WORK / "uploads" / parts[0] / Path(parts[1]).name).resolve()
    upload_root = (WORK / "uploads").resolve()
    if upload_root not in path.parents or not path.exists():
        raise RuntimeError("업로드한 영상을 찾을 수 없습니다.")
    return path


@app.post("/api/upload")
async def upload_videos(files: Annotated[list[UploadFile], File()]):
    if not files:
        raise HTTPException(400, "영상 파일을 선택하세요.")
    batch = uuid.uuid4().hex
    folder = WORK / "uploads" / batch
    folder.mkdir(parents=True, exist_ok=True)
    saved = []
    allowed = {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv"}
    for i, item in enumerate(files[:20]):
        suffix = Path(item.filename or "").suffix.lower()
        if suffix not in allowed:
            continue
        name = f"video-{i}{suffix}"
        target = folder / name
        with target.open("wb") as out:
            shutil.copyfileobj(item.file, out)
        token = f"{batch}/{name}"
        saved.append({"token": token, "name": item.filename or name, "preview": f"/api/uploads/{token}"})
    if not saved:
        raise HTTPException(400, "지원되는 영상 파일이 없습니다.")
    return {"files": saved}


@app.get("/api/uploads/{batch}/{filename}")
def preview_upload(batch: str, filename: str):
    try:
        path = uploaded_video_path(f"{batch}/{filename}")
    except RuntimeError as e:
        raise HTTPException(404, str(e))
    return FileResponse(path, media_type="video/mp4")


def render_job(job_id: str, req: RenderRequest) -> None:
    folder = WORK / job_id
    folder.mkdir(parents=True, exist_ok=True)
    try:
        require_tools()
        jobs[job_id] = {"status": "working", "progress": 3, "message": "TTS 생성 중"}
        audio = folder / "tts.mp3"
        asyncio.run(create_tts(req.script, req.voice, audio))
        total = probe_duration(audio)
        selected = [s for s in req.scenes if s.get("url") or s.get("token")]
        if not selected:
            raise RuntimeError("선택된 영상이 없습니다.")
        weights = [max(1, len(s.get("sentence", ""))) for s in selected]
        weight_sum = sum(weights)
        clips = []
        for i, (scene, weight) in enumerate(zip(selected, weights)):
            jobs[job_id] = {"status": "working", "progress": 8 + int(50 * i / len(selected)), "message": f"영상 {i+1}/{len(selected)} 다운로드 중"}
            src = uploaded_video_path(scene["token"]) if scene.get("token") else download_video(scene["url"], folder / f"source-{i}")
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


@app.post("/api/render-uploaded")
def render_uploaded(req: UploadedRenderRequest, tasks: BackgroundTasks):
    sentences = split_script(req.script)
    if not sentences:
        raise HTTPException(400, "대본을 입력하세요.")
    if not req.video_tokens:
        raise HTTPException(400, "영상 파일을 선택하세요.")
    scenes = [{"sentence": sentence, "token": req.video_tokens[i % len(req.video_tokens)]} for i, sentence in enumerate(sentences)]
    render_req = RenderRequest(script=req.script, scenes=scenes, voice=req.voice, width=req.width, height=req.height)
    job_id = uuid.uuid4().hex
    jobs[job_id] = {"status": "queued", "progress": 0, "message": "업로드 영상을 분석하는 중"}
    tasks.add_task(render_job, job_id, render_req)
    return {"job_id": job_id, "scene_count": len(scenes)}


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
import yt_dlp
import edge_tts
import requests
from bs4 import BeautifulSoup
from urllib.parse import quote_plus, unquote, urlparse, parse_qs

ROOT = Path(__file__).parent.resolve()
STATIC = ROOT
WORK = ROOT / "work"
WORK.mkdir(exist_ok=True)

app = FastAPI(title="쇼츠 영상 소스 메이커")
jobs: dict[str, dict] = {}


class AnalyzeRequest(BaseModel):
    script: str
    results_per_scene: int = 4


class DiscoverRequest(BaseModel):
    script: str
    per_platform: int = 8


class RenderRequest(BaseModel):
    script: str
    scenes: list[dict]
    voice: str = "ko-KR-SunHiNeural"
    width: int = 1080
    height: int = 1920


class UploadedRenderRequest(BaseModel):
    script: str
    video_tokens: list[str]
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


def detect_topic(script: str) -> str:
    rules = [
        (("벽지", "페인트"), "벽지 페인트 셀프 시공"),
        (("도배", "페인트"), "벽지 페인트 셀프 시공"),
        (("욕실", "리모델링"), "욕실 셀프 리모델링"),
        (("주방", "정리"), "주방 정리용품"),
        (("곰팡이",), "벽 곰팡이 제거"),
    ]
    for needles, topic in rules:
        if all(n in script for n in needles):
            return topic
    cleaned = re.sub(r"[^0-9A-Za-z가-힣 ]", " ", script)
    stop = {"여러분", "이거", "하나로", "진짜", "정말", "그냥", "저희", "우리", "그런데", "그리고", "하면", "해서", "있는", "있는데", "발견했어요", "더라고요", "없는데", "정보는", "남겨", "주세요"}
    words = [w for w in cleaned.split() if 2 <= len(w) <= 12 and w not in stop and not w.isdigit()]
    ranked = [w for w, _ in Counter(words).most_common(4)]
    return " ".join(ranked[:3]) or "생활용품 사용법"


def search_query(sentence: str, topic: str = "") -> str:
    cleaned = re.sub(r"[^0-9A-Za-z가-힣 ]", " ", sentence)
    stop = {"여러분", "이거", "하나로", "진짜", "정말", "그냥", "하면", "해서", "있는데", "됩니다", "합니다", "그리고", "그런데", "때문에", "바로", "저희", "우리", "발견했어요", "더라고요", "정보는", "남겨", "주세요"}
    words = [w for w in cleaned.split() if 2 <= len(w) <= 12 and w not in stop and not w.isdigit()]
    actions = []
    action_rules = [
        (("얼룩", "누렇게", "도배"), "누런 얼룩 제거 전후"),
        (("바르", "쓱쓱", "섞어"), "바르는 방법 작업 과정"),
        (("마르", "건조"), "페인트 건조 과정"),
        (("냄새", "친환경"), "친환경 페인트 후기"),
        (("4L", "용량", "구매"), "대용량 제품 사용 후기"),
    ]
    for needles, phrase in action_rules:
        if any(n in sentence for n in needles):
            actions.append(phrase)
    extra = " ".join(actions[:1] or words[:3])
    return f"{topic} {extra}".strip()


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


SUBTITLE_WORDS = {"자막", "字幕", "subtitles", "caption", "captions", "lyrics", "가사", "텍스트"}


def likely_clean(title: str, description: str = "") -> bool:
    haystack = f"{title} {description}".lower()
    return not any(word.lower() in haystack for word in SUBTITLE_WORDS)


def unwrap_search_url(href: str) -> str:
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if "duckduckgo.com" in parsed.netloc:
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        return unquote(target) if target else href
    return href


def public_web_search(query: str, limit: int) -> list[dict]:
    headers = {"User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/124 Mobile Safari/537.36"}
    items = []
    engines = [
        ("https://www.bing.com/search", {"q": query, "count": min(50, limit * 4)}, "bing"),
        ("https://html.duckduckgo.com/html/", {"q": query}, "duck"),
        ("https://www.google.com/search", {"q": query, "num": min(20, limit * 2)}, "google"),
    ]
    seen = set()
    for endpoint, params, engine in engines:
        try:
            response = requests.get(endpoint, params=params, headers=headers, timeout=7)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            rows = []
            if engine == "bing":
                rows = [(a, a.find_parent("li")) for a in soup.select("li.b_algo h2 a")]
            elif engine == "duck":
                rows = [(a, a.find_parent(class_="result")) for a in soup.select("a.result__a")]
            else:
                rows = [(a, a.find_parent("div")) for a in soup.select("a[href]") if a.get("href", "").startswith("/url?q=")]
            for link, row in rows:
                href = link.get("href", "")
                if engine == "google":
                    href = parse_qs(urlparse(href).query).get("q", [""])[0]
                url = unwrap_search_url(href)
                title = link.get_text(" ", strip=True)
                snippet = row.get_text(" ", strip=True) if row else ""
                if url and url not in seen and title and likely_clean(title, snippet):
                    seen.add(url)
                    items.append({"title": title, "url": url, "description": snippet[:300]})
                if len(items) >= limit:
                    return items
        except Exception:
            continue
    return items


def platform_results(platform: str, topic: str, limit: int) -> list[dict]:
    config = {
        "tiktok": ("site:tiktok.com", "TikTok"),
        "instagram": ("site:instagram.com/reel", "Instagram Reels"),
        "xiaohongshu": ("site:xiaohongshu.com/explore", "샤오홍슈"),
    }
    prefix, label = config[platform]
    raw = public_web_search(f"{prefix} {topic}", limit * 2)
    required = {"tiktok": "tiktok.com", "instagram": "instagram.com", "xiaohongshu": "xiaohongshu.com"}[platform]
    results = []
    for item in raw:
        if required not in item["url"]:
            continue
        results.append({**item, "platform": platform, "platform_label": label, "duration": None, "thumbnail": None, "clean_score": 80})
        if len(results) >= limit:
            break
    return results


LANGUAGES = ["ko", "en", "zh-CN", "ja", "es", "pt", "fr", "de"]


def translate_topic(topic: str, language: str) -> str:
    if language == "ko":
        return topic
    try:
        response = requests.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "auto", "tl": language, "dt": "t", "q": topic},
            timeout=5,
        )
        response.raise_for_status()
        translated = "".join(part[0] for part in response.json()[0] if part and part[0])
        return translated.strip() or topic
    except Exception:
        pass
    try:
        response = requests.get(
            "https://api.mymemory.translated.net/get",
            params={"q": topic, "langpair": f"ko|{language}"},
            timeout=5,
        )
        response.raise_for_status()
        translated = response.json().get("responseData", {}).get("translatedText", "")
        if translated and translated.casefold() != topic.casefold():
            return translated.strip()
    except Exception:
        pass
    offline = {
        "en": {"벽지": "wallpaper", "페인트": "paint", "셀프": "DIY", "시공": "application", "얼룩": "stain", "제거": "removal", "주방": "kitchen", "정리": "organization", "청소": "cleaning"},
        "zh-CN": {"벽지": "墙纸", "페인트": "翻新漆", "셀프": "自己动手", "시공": "施工", "얼룩": "污渍", "제거": "去除", "주방": "厨房", "정리": "收纳", "청소": "清洁"},
        "ja": {"벽지": "壁紙", "페인트": "ペイント", "셀프": "DIY", "시공": "施工", "얼룩": "汚れ", "제거": "除去", "주방": "キッチン", "정리": "収納", "청소": "掃除"},
        "es": {"벽지": "papel pintado", "페인트": "pintura", "셀프": "bricolaje", "시공": "aplicación", "청소": "limpieza"},
        "pt": {"벽지": "papel de parede", "페인트": "tinta", "셀프": "faça você mesmo", "시공": "aplicação", "청소": "limpeza"},
        "fr": {"벽지": "papier peint", "페인트": "peinture", "셀프": "DIY", "시공": "application", "청소": "nettoyage"},
        "de": {"벽지": "Tapete", "페인트": "Farbe", "셀프": "Heimwerken", "시공": "Anwendung", "청소": "Reinigung"},
    }
    words = [offline.get(language, {}).get(word, word) for word in topic.split()]
    return " ".join(words)


def multilingual_topics(topic: str) -> list[dict]:
    translated = {"ko": topic}
    with ThreadPoolExecutor(max_workers=7) as pool:
        futures = {pool.submit(translate_topic, topic, lang): lang for lang in LANGUAGES[1:]}
        for future in as_completed(futures):
            lang = futures[future]
            try:
                translated[lang] = future.result()
            except Exception:
                translated[lang] = topic
    unique = []
    seen = set()
    for lang in LANGUAGES:
        text = translated.get(lang, topic).strip()
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            unique.append({"language": lang, "query": text})
    return unique


def merge_unique(groups: list[list[dict]], limit: int) -> list[dict]:
    merged = []
    seen = set()
    for group in groups:
        for item in group:
            url = item.get("url", "")
            if not url or url in seen:
                continue
            seen.add(url)
            merged.append(item)
            if len(merged) >= limit:
                return merged
    return merged


def discover_platform(platform: str, translations: list[dict], limit: int) -> list[dict]:
    queries = [item["query"] for item in translations]
    combined = " OR ".join(f'"{query}"' for query in queries)
    if platform == "youtube":
        videos = yt_search(f"({combined}) shorts", limit * 3)
        clean = []
        for video in videos:
            duration = video.get("duration")
            if duration and float(duration) > 60:
                continue
            if likely_clean(video.get("title", "")):
                clean.append({**video, "platform": "youtube", "platform_label": "YouTube Shorts", "clean_score": 85, "language": "GLOBAL"})
            if len(clean) >= limit:
                break
        return clean
    found = platform_results(platform, combined, limit)
    for item in found:
        item["language"] = "GLOBAL"
    return found


def build_discovery(req: DiscoverRequest) -> dict:
    if not req.script.strip():
        raise HTTPException(400, "대본을 입력하세요.")
    topic = detect_topic(req.script)
    total_limit = 10
    search_limit = 10
    translations = multilingual_topics(topic)
    platform_groups = {"youtube": [], "tiktok": [], "instagram": [], "xiaohongshu": []}
    errors = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(discover_platform, platform, translations, search_limit): platform for platform in platform_groups}
        for future in as_completed(futures):
            platform = futures[future]
            try:
                platform_groups[platform] = future.result()
            except Exception as e:
                errors[platform] = str(e)
    all_results = []
    order = ("tiktok", "instagram", "xiaohongshu", "youtube")
    for index in range(search_limit):
        for platform in order:
            group = platform_groups[platform]
            if index < len(group):
                all_results.append(group[index])
                if len(all_results) >= total_limit:
                    break
        if len(all_results) >= total_limit:
            break
    return {"topic": topic, "translations": translations, "results": all_results, "errors": errors}


def discovery_job(job_id: str, req: DiscoverRequest) -> None:
    try:
        jobs[job_id] = {"status": "working", "progress": 15, "message": "검색어를 여러 언어로 변환하는 중"}
        data = build_discovery(req)
        jobs[job_id] = {"status": "done", "progress": 100, "message": "검색 완료", "data": data}
    except Exception as e:
        jobs[job_id] = {"status": "error", "progress": 0, "message": str(e)}


@app.post("/api/discover")
def discover(req: DiscoverRequest, tasks: BackgroundTasks):
    job_id = uuid.uuid4().hex
    jobs[job_id] = {"status": "queued", "progress": 3, "message": "전 세계 숏폼 검색 준비 중"}
    tasks.add_task(discovery_job, job_id, req)
    return {"job_id": job_id}


@app.post("/api/analyze")
def analyze(req: AnalyzeRequest):
    scenes = split_script(req.script)
    if not scenes:
        raise HTTPException(400, "대본을 입력하세요.")
    topic = detect_topic(req.script)
    try:
        common_pool = yt_search(topic, max(8, len(scenes) * 2))
    except Exception:
        common_pool = []
    used_ids: set[str] = set()
    result = []
    for i, sentence in enumerate(scenes):
        q = search_query(sentence, topic)
        try:
            videos = yt_search(q, max(3, min(req.results_per_scene + 2, 6)))
        except Exception:
            videos = []
        candidates = videos + common_pool
        unique = []
        seen = set()
        for video in candidates:
            vid = video.get("id")
            if not vid or vid in seen:
                continue
            seen.add(vid)
            unique.append(video)
        fresh = [v for v in unique if v.get("id") not in used_ids]
        chosen = (fresh or unique)[:max(1, min(req.results_per_scene, 4))]
        if chosen:
            used_ids.add(chosen[0]["id"])
        result.append({"index": i, "sentence": sentence, "query": q, "videos": chosen})
    return {"topic": topic, "scenes": result}


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


def uploaded_video_path(token: str) -> Path:
    parts = token.split("/", 1)
    if len(parts) != 2 or not re.fullmatch(r"[0-9a-f]{32}", parts[0]):
        raise RuntimeError("업로드 영상 정보가 올바르지 않습니다.")
    path = (WORK / "uploads" / parts[0] / Path(parts[1]).name).resolve()
    upload_root = (WORK / "uploads").resolve()
    if upload_root not in path.parents or not path.exists():
        raise RuntimeError("업로드한 영상을 찾을 수 없습니다.")
    return path


@app.post("/api/upload")
async def upload_videos(files: Annotated[list[UploadFile], File()]):
    if not files:
        raise HTTPException(400, "영상 파일을 선택하세요.")
    batch = uuid.uuid4().hex
    folder = WORK / "uploads" / batch
    folder.mkdir(parents=True, exist_ok=True)
    saved = []
    allowed = {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv"}
    for i, item in enumerate(files[:20]):
        suffix = Path(item.filename or "").suffix.lower()
        if suffix not in allowed:
            continue
        name = f"video-{i}{suffix}"
        target = folder / name
        with target.open("wb") as out:
            shutil.copyfileobj(item.file, out)
        token = f"{batch}/{name}"
        saved.append({"token": token, "name": item.filename or name, "preview": f"/api/uploads/{token}"})
    if not saved:
        raise HTTPException(400, "지원되는 영상 파일이 없습니다.")
    return {"files": saved}


@app.get("/api/uploads/{batch}/{filename}")
def preview_upload(batch: str, filename: str):
    try:
        path = uploaded_video_path(f"{batch}/{filename}")
    except RuntimeError as e:
        raise HTTPException(404, str(e))
    return FileResponse(path, media_type="video/mp4")


def render_job(job_id: str, req: RenderRequest) -> None:
    folder = WORK / job_id
    folder.mkdir(parents=True, exist_ok=True)
    try:
        require_tools()
        jobs[job_id] = {"status": "working", "progress": 3, "message": "TTS 생성 중"}
        audio = folder / "tts.mp3"
        asyncio.run(create_tts(req.script, req.voice, audio))
        total = probe_duration(audio)
        selected = [s for s in req.scenes if s.get("url") or s.get("token")]
        if not selected:
            raise RuntimeError("선택된 영상이 없습니다.")
        weights = [max(1, len(s.get("sentence", ""))) for s in selected]
        weight_sum = sum(weights)
        clips = []
        for i, (scene, weight) in enumerate(zip(selected, weights)):
            jobs[job_id] = {"status": "working", "progress": 8 + int(50 * i / len(selected)), "message": f"영상 {i+1}/{len(selected)} 다운로드 중"}
            src = uploaded_video_path(scene["token"]) if scene.get("token") else download_video(scene["url"], folder / f"source-{i}")
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


@app.post("/api/render-uploaded")
def render_uploaded(req: UploadedRenderRequest, tasks: BackgroundTasks):
    sentences = split_script(req.script)
    if not sentences:
        raise HTTPException(400, "대본을 입력하세요.")
    if not req.video_tokens:
        raise HTTPException(400, "영상 파일을 선택하세요.")
    scenes = [{"sentence": sentence, "token": req.video_tokens[i % len(req.video_tokens)]} for i, sentence in enumerate(sentences)]
    render_req = RenderRequest(script=req.script, scenes=scenes, voice=req.voice, width=req.width, height=req.height)
    job_id = uuid.uuid4().hex
    jobs[job_id] = {"status": "queued", "progress": 0, "message": "업로드 영상을 분석하는 중"}
    tasks.add_task(render_job, job_id, render_req)
    return {"job_id": job_id, "scene_count": len(scenes)}


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
