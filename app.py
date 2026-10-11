import json
import re
import urllib.parse
import urllib.request

from flask import Flask, jsonify, render_template, request
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import NoTranscriptFound, TranscriptsDisabled

app = Flask(__name__)
api = YouTubeTranscriptApi()

PREFERRED_LANGS = ["ru", "en"]
PARAGRAPH_GAP = 2.0  # пауза в секундах, с которой начинается новый абзац
PARAGRAPH_MAX = 50.0  # максимальная длина абзаца в секундах
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
NOISE_RE = re.compile(r"\[([^\[\]]{1,30})\]")  # [Music], [Applause] и т.п.
MD_SPECIAL_RE = re.compile(r"([\\`*_{}\[\]<>|~])")


class NoSubtitles(Exception):
    pass


def extract_video_id(raw: str) -> str | None:
    raw = raw.strip()
    if not raw or len(raw) > 500:
        return None
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urllib.parse.urlparse(raw)
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]

    if host == "youtu.be":
        vid = parsed.path.lstrip("/").split("/")[0]
    elif host in ("youtube.com", "m.youtube.com", "music.youtube.com"):
        if parsed.path == "/watch":
            vid = urllib.parse.parse_qs(parsed.query).get("v", [""])[0]
        else:
            parts = [p for p in parsed.path.split("/") if p]
            vid = parts[1] if len(parts) >= 2 and parts[0] in ("shorts", "embed", "live", "v") else ""
    else:
        return None
    return vid if VIDEO_ID_RE.match(vid) else None


def fetch_meta(video_id: str) -> tuple[str, str]:
    watch_url = f"https://www.youtube.com/watch?v={video_id}"
    url = "https://www.youtube.com/oembed?" + urllib.parse.urlencode({"url": watch_url, "format": "json"})
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.load(resp)
        return data.get("title", f"Видео {video_id}"), data.get("author_name", "")
    except Exception:
        return f"Видео {video_id}", ""


def pick_transcript(video_id: str):
    tlist = api.list(video_id)
    for finder in (tlist.find_manually_created_transcript, tlist.find_generated_transcript):
        try:
            return finder(PREFERRED_LANGS)
        except NoTranscriptFound:
            pass
    # Нужного языка нет: берём любой, ручные субтитры приоритетнее автоматических
    available = sorted(tlist, key=lambda t: t.is_generated)
    if available:
        return available[0]
    raise NoSubtitles(video_id)


def escape_md(text: str) -> str:
    return MD_SPECIAL_RE.sub(r"\\\1", text)


def format_text(text: str) -> str:
    # Служебные пометки вроде [Music] делаем курсивом, остальной текст экранируем
    out = []
    pos = 0
    for m in NOISE_RE.finditer(text):
        out.append(escape_md(text[pos:m.start()]))
        out.append("*" + escape_md("[" + m.group(1) + "]") + "*")
        pos = m.end()
    out.append(escape_md(text[pos:]))
    return "".join(out)


def fmt_time(seconds: float) -> str:
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02}:{m:02}:{s:02}" if h else f"{m:02}:{s:02}"


def build_paragraphs(snippets) -> list[dict]:
    paragraphs: list[dict] = []
    cur = None
    for snip in snippets:
        text = " ".join(snip.text.split())
        if not text:
            continue
        end = snip.start + snip.duration
        if cur is not None and (snip.start - cur["last_end"] >= PARAGRAPH_GAP
                                or snip.start - cur["start"] >= PARAGRAPH_MAX):
            paragraphs.append(cur)
            cur = None
        if cur is None:
            cur = {"start": snip.start, "last_end": end, "parts": [text]}
        else:
            cur["parts"].append(text)
            cur["last_end"] = end
    if cur is not None:
        paragraphs.append(cur)
    return paragraphs


def build_markdown(video_id: str, title: str, author: str, fetched, paragraphs: list[dict]) -> str:
    link = f"https://youtu.be/{video_id}"
    lang_label = fetched.language_code
    if fetched.is_generated:
        lang_label += ", автоматические"

    lines = [f"# {escape_md(title)}", ""]
    if author:
        lines.append(f"**Канал:** {escape_md(author)}  ")
    lines.append(f"**Источник:** [{link}]({link})  ")
    lines.append(f"**Язык субтитров:** {lang_label}")
    lines += ["", "---", ""]

    for para in paragraphs:
        ts = fmt_time(para["start"])
        href = f"{link}?t={int(para['start'])}"
        body = format_text(" ".join(para["parts"]))
        lines.append(f"**[{ts}]({href})** {body}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def error(message: str, status: int):
    return jsonify(error=message), status


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/transcript")
def transcript():
    data = request.get_json(silent=True) or {}
    video_id = extract_video_id(str(data.get("url", "")))
    if not video_id:
        return error("Не получилось распознать ссылку на видео YouTube.", 400)

    try:
        fetched = pick_transcript(video_id).fetch()
    except TranscriptsDisabled:
        return error("У этого видео отключены субтитры.", 422)
    except NoSubtitles:
        return error("Для этого видео не найдено ни одной дорожки с субтитрами.", 404)
    except Exception:
        app.logger.exception("Не удалось получить субтитры для %s", video_id)
        return error("Не удалось получить субтитры. Видео может быть недоступно, либо YouTube временно ограничил запросы.", 502)

    title, author = fetch_meta(video_id)
    paragraphs = build_paragraphs(fetched.snippets)
    markdown = build_markdown(video_id, title, author, fetched, paragraphs)
    return jsonify(
        markdown=markdown,
        title=title,
        language_code=fetched.language_code,
        is_generated=fetched.is_generated,
        video_id=video_id,
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
