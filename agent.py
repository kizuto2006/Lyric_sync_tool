import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from tqdm import tqdm

from tools import (
    RANK_NONE,
    backup_same_extension,
    fetch_apple_ttml,
    fetch_lrclib_lrc,
    fetch_netease_lrc,
    fetch_via_lrxy,
    get_audio_metadata,
    get_best_existing_rank,
)

# Các định dạng mutagen + lrxy hỗ trợ đầy đủ
AUDIO_EXTS = {".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".aac", ".aiff", ".wma", ".ape", ".wv"}

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "library_index.json"
MISSING_LOG = BASE_DIR / "missing_lyrics.log"
ERROR_LOG = BASE_DIR / "error_lyrics.log"


def load_cached_index() -> dict[str, dict]:
    if not DB_PATH.exists():
        return {}
    try:
        data = json.loads(DB_PATH.read_text(encoding="utf-8"))
        return {t["path"]: t for t in data.get("tracks", []) if "path" in t}
    except Exception:
        return {}


def scan_library(target_dir: Path, force_rescan: bool = False) -> list[dict]:
    print(f"🔎 Đang quét thư viện tại: {target_dir}")
    cached = {} if force_rescan else load_cached_index()

    files = sorted(
        p
        for p in target_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in AUDIO_EXTS and not p.name.startswith("._")
    )

    records = []
    cache_hits = 0
    for f in tqdm(files, desc="Phân tích metadata & chữ viết", unit="bài"):
        stat = f.stat()
        key = str(f)
        prev = cached.get(key)
        if prev and prev.get("mtime") == stat.st_mtime and prev.get("size") == stat.st_size:
            records.append(prev)
            cache_hits += 1
            continue

        meta = get_audio_metadata(f)
        meta["mtime"] = stat.st_mtime
        meta["size"] = stat.st_size
        records.append(meta)

    inst_cnt = sum(1 for r in records if r["is_instrumental"])
    cjk_cnt = sum(1 for r in records if not r["is_instrumental"] and r["script_type"] == "CJK")
    abc_cnt = sum(1 for r in records if not r["is_instrumental"] and r["script_type"] == "ABC")

    db_payload = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "target_dir": str(target_dir),
        "summary": {
            "total": len(records),
            "instrumental": inst_cnt,
            "cjk": cjk_cnt,
            "abc": abc_cnt,
        },
        "tracks": records,
    }
    DB_PATH.write_text(json.dumps(db_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n📊 Kết quả phân loại (Cache hit: {cache_hits}/{len(records)} bài | Đã lưu {DB_PATH.name}):")
    print(f"   ├─ Tổng số bài hát       : {len(records)}")
    print(f"   ├─ Nhạc không lời (Inst.): {inst_cnt} bài")
    print(f"   ├─ Nhóm Hán/Nhật/Hàn(CJK): {cjk_cnt} bài")
    print(f"   └─ Nhóm Alphabet (ABC)   : {abc_cnt} bài\n")
    return records


def process_single_track(meta: dict, mode: str, skip_existing: bool, force: bool, dry_run: bool) -> dict:
    audio_path = Path(meta["path"])
    if meta["is_instrumental"]:
        return {"outcome": "INST_SKIP", "meta": meta}

    existing_rank, existing_path = get_best_existing_rank(audio_path)
    if skip_existing and not force and existing_rank > RANK_NONE:
        return {"outcome": "EXIST_SKIP", "meta": meta, "detail": existing_path.name if existing_path else ""}

    # Nghỉ nhẹ tránh rate-limit
    time.sleep(0.15)

    status = "NOT_FOUND"
    content = ""
    new_rank = RANK_NONE
    chosen_ext = ".ttml"
    error_detail = ""

    if mode == "1":  # Apple Music TTML (lrc.red / BiniLyrics)
        status, content, new_rank, error_detail = fetch_apple_ttml(meta)
        chosen_ext = ".ttml"

    elif mode == "2":  # Musixmatch LRC (lrxy)
        status, content, new_rank, error_detail = fetch_via_lrxy(meta, "musixmatch")
        chosen_ext = ".lrc"

    elif mode == "3":  # QQ Music LRC (lrxy)
        status, content, new_rank, error_detail = fetch_via_lrxy(meta, "qq")
        chosen_ext = ".lrc"

    elif mode == "4":  # LRCLIB LRC
        status, content, new_rank, is_inst_api, error_detail = fetch_lrclib_lrc(meta)
        if is_inst_api:
            return {"outcome": "INST_SKIP", "meta": meta}
        chosen_ext = ".lrc"

    elif mode == "5":  # NetEase Cloud Music LRC/ELRC (Direct API)
        status, content, new_rank, error_detail = fetch_netease_lrc(meta)
        chosen_ext = ".lrc"

    elif mode == "6":  # Auto Smart dựa theo DB CJK / ABC
        # Bước 1: Luôn ưu tiên chuẩn Apple Music TTML trước
        status, content, new_rank, err1 = fetch_apple_ttml(meta)
        if status == "FOUND":
            chosen_ext = ".ttml"
        else:
            # Bước 2: Định tuyến theo CJK (QQ) hoặc ABC (Musixmatch)
            sub_provider = "qq" if meta["script_type"] == "CJK" else "musixmatch"
            status, content, new_rank, err2 = fetch_via_lrxy(meta, sub_provider)
            if status == "FOUND":
                chosen_ext = ".lrc"
            else:
                # Bước 3: Gọi trực tiếp NetEase Cloud Music API (vượt lỗi 502 của paxsenix)
                status, content, new_rank, err3 = fetch_netease_lrc(meta)
                if status == "FOUND":
                    chosen_ext = ".lrc"
                else:
                    # Bước 4: Fallback cuối sang LRCLIB
                    status, content, new_rank, is_inst_api, err4 = fetch_lrclib_lrc(meta)
                    if is_inst_api:
                        return {"outcome": "INST_SKIP", "meta": meta}
                    if status == "FOUND":
                        chosen_ext = ".lrc"
                    else:
                        errors = [e for e in (err1, err2, err3, err4) if e]
                        if errors:
                            status = "ERROR"
                            error_detail = " | ".join(errors)

    if status == "FOUND" and content:
        # Không bao giờ ghi đè bản hiện có bằng bản chất lượng thấp hơn (trừ khi --force)
        if not force and new_rank < existing_rank:
            return {
                "outcome": "DOWNGRADE_SKIP",
                "meta": meta,
                "detail": f"Giữ bản cũ (rank {existing_rank}) > mới (rank {new_rank})",
            }

        target_file = audio_path.with_suffix(chosen_ext)
        bak_path = backup_same_extension(target_file, dry_run=dry_run)
        if not dry_run:
            target_file.write_text(content, encoding="utf-8")

        return {
            "outcome": "SAVED",
            "ext": chosen_ext,
            "meta": meta,
            "bak": bak_path.name if bak_path else None,
        }

    if status == "ERROR":
        return {"outcome": "ERROR", "meta": meta, "detail": error_detail}

    return {"outcome": "NOT_FOUND", "meta": meta}


def append_log_file(log_path: Path, title: str, lines: list[str]):
    if not lines:
        return
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    block = f"\n=== [{stamp}] {title} ({len(lines)} mục) ===\n" + "\n".join(lines) + "\n"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(block)


def main():
    parser = argparse.ArgumentParser(description="Lyric Multi-Source Fetcher (TTML / LRC)")
    parser.add_argument("target", nargs="?", default=".", help="Thư mục cần quét (mặc định: thư mục hiện tại)")
    parser.add_argument("--mode", choices=["1", "2", "3", "4", "5", "6", "7"], help="Chọn chế độ (1-7) không cần nhập tay")
    parser.add_argument("--skip-existing", action="store_true", default=True, help="Bỏ qua bài đã có lời (mặc định bật)")
    parser.add_argument("--overwrite", action="store_true", help="Cho phép nâng cấp/ghi đè nếu tìm thấy lời chất lượng cao hơn")
    parser.add_argument("--force", action="store_true", help="Bắt buộc tải lại và ghi đè kể cả khi bản mới thấp hạng hơn")
    parser.add_argument("--dry-run", action="store_true", help="Chạy thử, chỉ in kết quả dự kiến, không ghi/sửa file")
    parser.add_argument("--workers", type=int, default=3, help="Số luồng tải song song (mặc định: 3)")
    args = parser.parse_args()

    skip_existing = False if (args.overwrite or args.force) else args.skip_existing

    target_dir = Path(args.target).expanduser().resolve()
    if not target_dir.exists() or not target_dir.is_dir():
        print(f"❌ Đường dẫn không hợp lệ: {target_dir}")
        sys.exit(1)

    if args.dry_run:
        print("🧪 CHẾ ĐỘ DRY-RUN: Không ghi, sửa hay xóa bất kỳ file nào trên ổ cứng.\n")

    choice = args.mode
    if not choice:
        print("============================================================")
        print("🎵 LYRIC MULTI-SOURCE FETCHER (TTML / LRC)")
        print("============================================================")
        print("  1. Apple Music TTML (lrc.red / BiniLyrics) [Mặc định]")
        print("  2. Musixmatch LRC (lrxy)")
        print("  3. QQ Music LRC (lrxy - tối ưu nhạc Hán/Nhật/Hàn)")
        print("  4. LRCLIB LRC (/api/get + /api/search)")
        print("  5. NetEase Cloud Music LRC/ELRC (Direct API)")
        print("  6. Auto Smart (Ưu tiên Apple TTML -> Định tuyến CJK/ABC)")
        print("  7. Chỉ quét & cập nhật DB phân loại (library_index.json)")
        print("============================================================")
        choice = input("👉 Chọn chế độ (1-7, nhấn Enter để chọn 1): ").strip() or "1"

    if choice not in {"1", "2", "3", "4", "5", "6", "7"}:
        print("❌ Lựa chọn không hợp lệ.")
        sys.exit(1)

    records = scan_library(target_dir, force_rescan=args.force)
    if choice == "7":
        print("✅ Đã cập nhật xong database phân loại!")
        return

    stats = {"ttml": 0, "lrc": 0, "inst_skip": 0, "exist_skip": 0, "downgrade_skip": 0, "missing": 0, "error": 0}
    missing_lines = []
    error_lines = []

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {
            pool.submit(process_single_track, m, choice, skip_existing, args.force, args.dry_run): m
            for m in records
        }
        pbar = tqdm(as_completed(futures), total=len(futures), desc="Đang xử lý", unit="bài")
        for fut in pbar:
            res = fut.result()
            meta = res["meta"]
            outcome = res["outcome"]

            if outcome == "SAVED":
                if res["ext"] == ".ttml":
                    stats["ttml"] += 1
                else:
                    stats["lrc"] += 1
                if args.dry_run:
                    bak_note = f" (sẽ backup -> {res['bak']})" if res["bak"] else ""
                    tqdm.write(f"  [DRY-RUN] Lưu {res['ext']}: {Path(meta['path']).name}{bak_note}")
            elif outcome == "INST_SKIP":
                stats["inst_skip"] += 1
            elif outcome == "EXIST_SKIP":
                stats["exist_skip"] += 1
            elif outcome == "DOWNGRADE_SKIP":
                stats["downgrade_skip"] += 1
            elif outcome == "ERROR":
                stats["error"] += 1
                error_lines.append(f"{meta['artist']} - {meta['title']} | {meta['path']} -> {res.get('detail', '')}")
            else:
                stats["missing"] += 1
                missing_lines.append(f"{meta['artist']} - {meta['title']} | {meta['path']}")

    if not args.dry_run:
        append_log_file(MISSING_LOG, "Không tìm thấy lời", missing_lines)
        append_log_file(ERROR_LOG, "Lỗi kết nối / API", error_lines)

    print("\n🎉 Hoàn tất phiên xử lý!")
    print(f"   ├─ Đã lưu chuẩn TTML (Apple)  : {stats['ttml']} bài")
    print(f"   ├─ Đã lưu chuẩn LRC           : {stats['lrc']} bài")
    print(f"   ├─ Bỏ qua (đã có lời tốt)     : {stats['exist_skip']} bài")
    print(f"   ├─ Giữ bản cũ (mới kém hơn)   : {stats['downgrade_skip']} bài")
    print(f"   ├─ Bỏ qua Instrumental        : {stats['inst_skip']} bài")
    print(f"   ├─ Không tìm thấy lời         : {stats['missing']} bài" + (f" (xem {MISSING_LOG.name})" if missing_lines else ""))
    print(f"   └─ Lỗi mạng / API             : {stats['error']} bài" + (f" (xem {ERROR_LOG.name})" if error_lines else ""))


if __name__ == "__main__":
    main()
