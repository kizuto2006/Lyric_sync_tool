import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import requests
from mutagen import File as MutagenFile

USER_AGENT = "LyricSyncTool/2.0 (https://github.com/kizuto/Lyric_sync_tool)"

CJK_CHAR_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")
LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)
INSTRUMENTAL_RE = re.compile(
    r"(?:^|[\s\(\[\-_])(?:instrumental|inst\.?|off[\s\-_]*vocal|karaoke(?:\s*ver\.?)?|no[\s\-_]*vocal|bgm|beat[\s\-_]*only)(?:$|[\s\)\]\-_])|オフボーカル|ガイド無しカラオケ|カラオケ|インストゥルメンタル",
    re.IGNORECASE,
)

# Thứ hạng chất lượng lời bài hát để không bao giờ ghi đè bản tốt bằng bản kém hơn
RANK_NONE = 0
RANK_PLAIN = 1
RANK_LRC_LINE = 2
RANK_ELRC_WORD = 3
RANK_TTML_LINE = 4
RANK_TTML_WORD = 5


def norm_str(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"\(.*?\)|\[.*?\]", "", s.lower())
    return re.sub(r"[^\w]", "", s, flags=re.UNICODE)


def split_artists(artist_str: str) -> list[str]:
    if not artist_str:
        return []
    parts = re.split(r"[,&;/]|\bfeat\.?\b|\bft\.?\b", artist_str, flags=re.IGNORECASE)
    return [norm_str(p) for p in parts if norm_str(p)]


def strip_xml_tags(xml_text: str) -> str:
    try:
        root = ET.fromstring(xml_text)
        return " ".join(t.strip() for t in root.itertext() if t and t.strip())
    except Exception:
        return re.sub(r"<[^>]+>", " ", xml_text)


def strip_lrc_tags(lrc_text: str) -> str:
    text = re.sub(r"\[[a-zA-Z]+:[^\]]*\]", " ", lrc_text)
    text = re.sub(r"<\d{1,2}:\d{2}(?:\.\d{1,3})?>", " ", text)
    text = re.sub(r"\[\d{1,2}:\d{2}(?:\.\d{1,3})?\]", " ", text)
    return text


def detect_script_type(text: str, threshold: float = 0.30) -> str:
    """Phân loại CJK nếu tỷ lệ ký tự Hán/Nhật/Hàn trên tổng số chữ cái >= 30%."""
    if not text:
        return "ABC"
    letters = LETTER_RE.findall(text)
    if not letters:
        return "ABC"
    cjk_count = sum(1 for ch in letters if CJK_CHAR_RE.match(ch))
    return "CJK" if (cjk_count / len(letters)) >= threshold else "ABC"


def classify_lyric_rank(path: Path) -> int:
    """Đánh giá thứ hạng chất lượng của file lời hiện có trên đĩa."""
    if not path.exists():
        return RANK_NONE
    content = path.read_text(encoding="utf-8", errors="ignore").strip()
    if not content:
        return RANK_NONE

    ext = path.suffix.lower()
    if ext == ".ttml":
        if re.search(r"<span\b[^>]*\bbegin=", content):
            return RANK_TTML_WORD
        return RANK_TTML_LINE

    if ext == ".lrc":
        if re.search(r"<\d{1,2}:\d{2}\.\d{2,3}>", content):
            return RANK_ELRC_WORD
        if re.search(r"\[\d{1,2}:\d{2}\.\d{2,3}\]", content):
            return RANK_LRC_LINE
        return RANK_PLAIN

    return RANK_PLAIN


def get_best_existing_rank(audio_path: Path) -> tuple[int, Path | None]:
    best_rank = RANK_NONE
    best_path = None
    for ext in (".ttml", ".lrc"):
        candidate = audio_path.with_suffix(ext)
        r = classify_lyric_rank(candidate)
        if r > best_rank:
            best_rank = r
            best_path = candidate
    return best_rank, best_path


def backup_same_extension(target_file: Path, dry_run: bool = False) -> Path | None:
    """Chỉ backup file CÙNG đuôi sắp ghi và KHÔNG BAO GIỜ xóa bản .bak đã tồn tại."""
    if not target_file.exists():
        return None
    base_bak = target_file.with_name(target_file.name + ".bak")
    if not base_bak.exists():
        bak_path = base_bak
    else:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        bak_path = target_file.with_name(f"{target_file.name}.{stamp}.bak")
    if not dry_run:
        shutil.copy2(str(target_file), str(bak_path))
    return bak_path


def get_audio_metadata(audio_path: Path) -> dict:
    title = audio_path.stem
    artist = ""
    album = ""
    genre = ""
    duration = 0

    try:
        audio = MutagenFile(str(audio_path), easy=True)
        if audio:
            title = audio.get("title", [title])[0].strip() or title
            artist = audio.get("artist", [""])[0].strip()
            album = audio.get("album", [""])[0].strip()
            genre = audio.get("genre", [""])[0].strip()
            if audio.info:
                duration = int(round(audio.info.length))
    except Exception:
        pass

    if not artist and " - " in audio_path.stem:
        parts = audio_path.stem.split(" - ", 1)
        artist = re.sub(r"^\d+\.\s*", "", parts[0]).strip()
        title = parts[1].strip()

    is_inst = bool(
        INSTRUMENTAL_RE.search(title)
        or INSTRUMENTAL_RE.search(audio_path.stem)
        or "instrumental" in genre.lower()
    )

    # Ưu tiên lấy text thuần từ lời cũ (đã bóc thẻ XML/LRC) để phân loại chính xác
    sample_text = ""
    old_ttml = audio_path.with_suffix(".ttml")
    old_lrc = audio_path.with_suffix(".lrc")
    if old_ttml.exists():
        sample_text = strip_xml_tags(old_ttml.read_text(encoding="utf-8", errors="ignore"))
    elif old_lrc.exists():
        sample_text = strip_lrc_tags(old_lrc.read_text(encoding="utf-8", errors="ignore"))

    if not sample_text.strip():
        sample_text = f"{title} {artist}"

    script_type = detect_script_type(sample_text)

    return {
        "path": str(audio_path),
        "title": title,
        "artist": artist,
        "album": album,
        "duration": duration,
        "is_instrumental": is_inst,
        "script_type": script_type,
    }


def _request_with_retry(url: str, params: dict | None = None, timeout: int = 12, max_retries: int = 2) -> requests.Response:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json, text/plain, */*"}
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=timeout)
            if resp.status_code == 429:
                wait = 2.0 * (attempt + 1)
                time.sleep(wait)
                continue
            return resp
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < max_retries:
                time.sleep(1.0 * (attempt + 1))
    if last_exc:
        raise last_exc
    raise RuntimeError("Request failed after retries")


def _score_apple_candidate(meta: dict, item: dict) -> float:
    target_title = norm_str(meta["title"])
    item_title = norm_str(item.get("track_name", ""))
    if not target_title or not item_title:
        return -1.0

    score = 0.0
    if target_title == item_title:
        score += 50.0
    elif len(target_title) >= 3 and (target_title in item_title or item_title in target_title):
        score += 30.0
    else:
        return -1.0

    # Chấm điểm artist
    q_artists = set(split_artists(meta["artist"]))
    i_artists = set(split_artists(item.get("artist_name", "")))
    if q_artists and i_artists:
        if q_artists & i_artists:
            score += 35.0
        else:
            return -1.0  # Sai nghệ sĩ -> loại thẳng

    # Chấm điểm duration (đơn vị chuẩn từ lrc.red là giây)
    q_dur = meta.get("duration", 0)
    i_dur = item.get("duration", 0)
    if q_dur > 0 and i_dur > 0:
        # Đề phòng mirror trả về ms
        if i_dur > 10000:
            i_dur = int(round(i_dur / 1000))
        diff = abs(q_dur - i_dur)
        if diff <= 2:
            score += 25.0
        elif diff <= 5:
            score += 15.0
        elif diff <= 10:
            score += 0.0
        else:
            return -1.0
    else:
        # Thiếu duration -> không xác định, yêu cầu title & artist khớp chính xác tuyệt đối
        if target_title != item_title or not (q_artists & i_artists):
            return -1.0

    # Chấm điểm album
    q_album = norm_str(meta.get("album", ""))
    i_album = norm_str(item.get("album_name", ""))
    if q_album and i_album and (q_album == i_album or q_album in i_album or i_album in q_album):
        score += 10.0

    # Ưu tiên bản word-synced
    if item.get("timing_type") == "word":
        score += 15.0

    return score


def fetch_apple_ttml(meta: dict) -> tuple[str, str, int, str]:
    """
    Trả về: (status: FOUND|NOT_FOUND|ERROR, ttml_content, rank, detail_msg)
    Dùng endpoint chính https://lrc.red/api/v1 và mirror https://lyrics-api.binimum.org/getLyrics
    """
    if not norm_str(meta["title"]) or not split_artists(meta["artist"]):
        return "NOT_FOUND", "", RANK_NONE, "Thiếu metadata title/artist"

    endpoints = [
        "https://lrc.red/api/v1",
        "https://lyrics-api.binimum.org/getLyrics",
    ]
    params = {
        "title": meta["title"],
        "artist": meta["artist"],
        "album": meta["album"],
        "duration": meta["duration"],
    }

    last_error = ""
    for ep in endpoints:
        try:
            resp = _request_with_retry(ep, params=params, timeout=12, max_retries=1)
            if resp.status_code == 404:
                return "NOT_FOUND", "", RANK_NONE, ""
            if resp.status_code != 200:
                last_error = f"HTTP {resp.status_code} từ {ep}"
                continue

            data = resp.json()
            results = data.get("results", [])
            if not results:
                return "NOT_FOUND", "", RANK_NONE, ""

            scored = []
            for item in results:
                s = _score_apple_candidate(meta, item)
                if s >= 70.0 and item.get("lyricsUrl"):
                    scored.append((s, item))

            if not scored:
                return "NOT_FOUND", "", RANK_NONE, ""

            scored.sort(key=lambda x: x[0], reverse=True)
            best_item = scored[0][1]
            ttml_resp = _request_with_retry(best_item["lyricsUrl"], timeout=12, max_retries=1)
            if ttml_resp.status_code == 200 and "<tt" in ttml_resp.text:
                content = ttml_resp.text.strip()
                rank = RANK_TTML_WORD if best_item.get("timing_type") == "word" else RANK_TTML_LINE
                return "FOUND", content, rank, ""
            last_error = f"Lỗi tải lyricsUrl HTTP {ttml_resp.status_code}"
        except Exception as exc:
            last_error = f"{ep}: {exc}"

    return "ERROR", "", RANK_NONE, last_error


def fetch_via_lrxy(meta: dict, provider: str) -> tuple[str, str, int, str]:
    """
    Gọi lrxy trên file audio nhẹ giả lập chứa đúng tag (tránh copy file FLAC gốc nặng qua SSHFS).
    Trả về: (status: FOUND|NOT_FOUND|ERROR, lrc_content, rank, detail_msg)
    """
    lrxy_bin = Path(sys.executable).parent / "lrxy"
    if not lrxy_bin.exists():
        return "ERROR", "", RANK_NONE, "Không tìm thấy binary lrxy trong .venv"

    src_audio = Path(meta["path"])
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            # Copy nguyên file chỉ khi kích thước <= 15MB, ngược lại copy nhanh file gốc
            tmp_audio = Path(tmpdir) / src_audio.name
            shutil.copy2(str(src_audio), str(tmp_audio))

            cmd = [str(lrxy_bin), "-p", provider, "-f", "lrc", "-n", str(tmp_audio)]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
            out_file = tmp_audio.with_suffix(".lrc")

            if out_file.exists():
                content = out_file.read_text(encoding="utf-8", errors="ignore").strip()
                if content:
                    rank = classify_lyric_rank(out_file)
                    return "FOUND", content, rank, ""

            stderr_msg = (proc.stderr or proc.stdout or "").strip()
            if any(code in stderr_msg for code in ("502", "503", "504", "429", "401", "403", "ConnectionError", "Timeout")):
                return "ERROR", "", RANK_NONE, stderr_msg.splitlines()[-1][:160]
            return "NOT_FOUND", "", RANK_NONE, ""
    except subprocess.TimeoutExpired:
        return "ERROR", "", RANK_NONE, f"Timeout khi gọi lrxy ({provider})"
    except Exception as exc:
        return "ERROR", "", RANK_NONE, str(exc)


def fetch_lrclib_lrc(meta: dict) -> tuple[str, str, int, bool, str]:
    """
    Truy vấn LRCLIB (/api/get trước, fallback sang /api/search nếu thiếu album/duration).
    Trả về: (status: FOUND|NOT_FOUND|ERROR, lrc_content, rank, is_instrumental, detail_msg)
    """
    if not norm_str(meta["title"]) or not split_artists(meta["artist"]):
        return "NOT_FOUND", "", RANK_NONE, False, "Thiếu title/artist"

    # 1. Thử /api/get nếu có đủ thông tin
    if meta.get("album") and meta.get("duration", 0) > 0:
        try:
            resp = _request_with_retry(
                "https://lrclib.net/api/get",
                params={
                    "track_name": meta["title"],
                    "artist_name": meta["artist"],
                    "album_name": meta["album"],
                    "duration": meta["duration"],
                },
                timeout=10,
                max_retries=1,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("instrumental"):
                    return "NOT_FOUND", "", RANK_NONE, True, ""
                synced = (data.get("syncedLyrics") or "").strip()
                if synced:
                    return "FOUND", synced, RANK_LRC_LINE, False, ""
            elif resp.status_code not in (400, 404):
                return "ERROR", "", RANK_NONE, False, f"LRCLIB /api/get HTTP {resp.status_code}"
        except Exception as exc:
            return "ERROR", "", RANK_NONE, False, f"LRCLIB /api/get: {exc}"

    # 2. Fallback sang /api/search
    try:
        resp = _request_with_retry(
            "https://lrclib.net/api/search",
            params={"track_name": meta["title"], "artist_name": meta["artist"]},
            timeout=10,
            max_retries=1,
        )
        if resp.status_code != 200:
            return "ERROR", "", RANK_NONE, False, f"LRCLIB /api/search HTTP {resp.status_code}"

        items = resp.json()
        if not isinstance(items, list) or not items:
            return "NOT_FOUND", "", RANK_NONE, False, ""

        q_title = norm_str(meta["title"])
        q_artists = set(split_artists(meta["artist"]))
        q_dur = meta.get("duration", 0)

        best_item = None
        best_score = -1.0
        for item in items:
            i_title = norm_str(item.get("trackName", ""))
            i_artists = set(split_artists(item.get("artistName", "")))
            if not i_title or (q_title != i_title and q_title not in i_title):
                continue
            if q_artists and i_artists and not (q_artists & i_artists):
                continue

            score = 40.0 if q_title == i_title else 25.0
            if q_artists & i_artists:
                score += 35.0
            i_dur = int(round(item.get("duration") or 0))
            if q_dur > 0 and i_dur > 0:
                diff = abs(q_dur - i_dur)
                if diff <= 3:
                    score += 25.0
                elif diff <= 8:
                    score += 10.0
                else:
                    continue
            if score > best_score:
                best_score = score
                best_item = item

        if not best_item:
            return "NOT_FOUND", "", RANK_NONE, False, ""
        if best_item.get("instrumental"):
            return "NOT_FOUND", "", RANK_NONE, True, ""

        synced = (best_item.get("syncedLyrics") or "").strip()
        if synced:
            return "FOUND", synced, RANK_LRC_LINE, False, ""
        return "NOT_FOUND", "", RANK_NONE, False, ""
    except Exception as exc:
        return "ERROR", "", RANK_NONE, False, f"LRCLIB /api/search: {exc}"
def _parse_netease_yrc(yrc_text: str) -> str:
    """Chuyển đổi định dạng từng từ YRC của NetEase ([start,dur]từ(start,dur)) sang Enhanced LRC"""
    out_lines = []
    for raw_line in yrc_text.splitlines():
        line_match = re.match(r"^\[(\d+),(\d+)\](.*)$", raw_line.strip())
        if not line_match:
            continue
        line_start_ms = int(line_match.group(1))
        rest = line_match.group(3)
        tokens = re.findall(r"([^(]+)\((\d+),(\d+)(?:,\d+)?\)", rest)
        if not tokens:
            continue

        mm, ss = divmod(line_start_ms / 1000.0, 60)
        prefix = f"[{int(mm):02d}:{ss:05.2f}]"
        parts = []
        for word, w_start_str, _ in tokens:
            w_ms = int(w_start_str)
            wm, ws = divmod(w_ms / 1000.0, 60)
            parts.append(f"<{int(wm):02d}:{ws:05.2f}>{word}")
        out_lines.append(f"{prefix} {''.join(parts)}")
    return "\n".join(out_lines).strip()


def fetch_netease_lrc(meta: dict) -> tuple[str, str, int, str]:
    """
    Gọi trực tiếp API công khai của NetEase Cloud Music (không qua proxy trung gian).
    Trả về: (status: FOUND|NOT_FOUND|ERROR, lrc_content, rank, detail_msg)
    """
    q_title = norm_str(meta["title"])
    q_artists = set(split_artists(meta["artist"]))
    if not q_title or not q_artists:
        return "NOT_FOUND", "", RANK_NONE, "Thiếu title/artist"

    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
        "Referer": "https://music.163.com/",
        "Cookie": "os=pc; appver=2.9.7",
    }
    query = f"{meta['title']} {meta['artist']}".strip()

    try:
        search_resp = requests.post(
            "https://music.163.com/api/cloudsearch/pc",
            data={"s": query, "type": 1, "offset": 0, "limit": 8},
            headers=headers,
            timeout=12,
        )
        if search_resp.status_code != 200:
            return "ERROR", "", RANK_NONE, f"NetEase search HTTP {search_resp.status_code}"

        payload = search_resp.json()
        if not isinstance(payload, dict):
            return "NOT_FOUND", "", RANK_NONE, ""

        result_obj = payload.get("result")
        if not isinstance(result_obj, dict):
            return "NOT_FOUND", "", RANK_NONE, ""

        songs = result_obj.get("songs")
        if not isinstance(songs, list) or not songs:
            return "NOT_FOUND", "", RANK_NONE, ""

        q_dur = meta.get("duration", 0)
        best_id = None
        best_score = -1.0

        for song in songs:
            if not isinstance(song, dict):
                continue
            s_title = norm_str(song.get("name", ""))
            if not s_title:
                continue
            if q_title != s_title and not (len(q_title) >= 3 and (q_title in s_title or s_title in q_title)):
                continue

            # cloudsearch/pc dùng khóa "ar" (hoặc "artists" ở API cũ)
            raw_artists = song.get("ar") or song.get("artists") or []
            s_artists = {
                norm_str(a.get("name", ""))
                for a in raw_artists
                if isinstance(a, dict) and norm_str(a.get("name", ""))
            }
            if q_artists and s_artists and not (q_artists & s_artists):
                continue

            score = 45.0 if q_title == s_title else 25.0
            if q_artists & s_artists:
                score += 35.0

            # cloudsearch/pc dùng khóa "dt" (ms) hoặc "duration" (ms)
            raw_dur_ms = song.get("dt") or song.get("duration") or 0
            s_dur = int(round(raw_dur_ms / 1000))
            if q_dur > 0 and s_dur > 0:
                diff = abs(q_dur - s_dur)
                if diff <= 3:
                    score += 25.0
                elif diff <= 8:
                    score += 10.0
                else:
                    continue

            if score > best_score:
                best_score = score
                best_id = song.get("id")

        if not best_id:
            return "NOT_FOUND", "", RANK_NONE, ""

        lyric_resp = requests.get(
            "https://music.163.com/api/song/lyric",
            params={"id": best_id, "lv": 1, "yv": 1, "tv": -1},
            headers=headers,
            timeout=12,
        )
        if lyric_resp.status_code != 200:
            return "ERROR", "", RANK_NONE, f"NetEase lyric HTTP {lyric_resp.status_code}"

        ldata = lyric_resp.json()
        if not isinstance(ldata, dict):
            return "NOT_FOUND", "", RANK_NONE, ""

        # Ưu tiên lời từng từ (yrc) nếu bài hát có hỗ trợ
        yrc_obj = ldata.get("yrc")
        if isinstance(yrc_obj, dict):
            yrc_raw = (yrc_obj.get("lyric") or "").strip()
            if yrc_raw:
                elrc = _parse_netease_yrc(yrc_raw)
                if elrc:
                    return "FOUND", elrc, RANK_ELRC_WORD, ""

        # Fallback sang lời từng dòng chuẩn (.lrc)
        lrc_obj = ldata.get("lrc")
        if isinstance(lrc_obj, dict):
            lrc_raw = (lrc_obj.get("lyric") or "").strip()
            if lrc_raw and re.search(r"\[\d{1,2}:\d{2}", lrc_raw):
                return "FOUND", lrc_raw, RANK_LRC_LINE, ""

        return "NOT_FOUND", "", RANK_NONE, ""
    except Exception as exc:
        return "ERROR", "", RANK_NONE, f"NetEase: {exc}"
