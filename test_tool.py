import tempfile
from pathlib import Path

from tools import (
    INSTRUMENTAL_RE,
    RANK_ELRC_WORD,
    RANK_LRC_LINE,
    RANK_TTML_WORD,
    backup_same_extension,
    classify_lyric_rank,
    detect_script_type,
    norm_str,
    strip_xml_tags,
)

# 1. Test norm_str
assert norm_str("Cities (Intro)") == "cities"
assert norm_str("Sao Giờ Em Mới Tới [Live]") == "saogiờemmớitới"
assert norm_str("") == ""

# 2. Test INSTRUMENTAL_RE
assert INSTRUMENTAL_RE.search("05. Song Name (Instrumental)")
assert INSTRUMENTAL_RE.search("Track 02 - Inst. Version")
assert INSTRUMENTAL_RE.search("Song [Off Vocal]")
assert not INSTRUMENTAL_RE.search("Institution")

# 3. Test detect_script_type & strip_xml_tags
ttml_jp = '<tt xmlns="http://www.w3.org/ns/ttml"><body><div><p><span>君の名は</span><span>希望</span></p></div></body></tt>'
assert detect_script_type(strip_xml_tags(ttml_jp)) == "CJK"
assert detect_script_type("Xin chào Việt Nam feat. 龍") == "ABC"  # 1 chữ Hán không làm sai cả bài

# 4. Test backup không bao giờ xóa .bak cũ & xếp hạng chất lượng
with tempfile.TemporaryDirectory() as tmp:
    p = Path(tmp)
    lrc = p / "song.lrc"
    lrc.write_text("[00:01.00] Line 1", encoding="utf-8")
    assert classify_lyric_rank(lrc) == RANK_LRC_LINE

    b1 = backup_same_extension(lrc)
    assert b1 and b1.name == "song.lrc.bak"

    lrc.write_text("[00:01.00] <00:01.00> Word <00:01.50> 1", encoding="utf-8")
    assert classify_lyric_rank(lrc) == RANK_ELRC_WORD

    b2 = backup_same_extension(lrc)
    assert b2 and b2.name != "song.lrc.bak" and b2.exists()
    assert b1.exists()  # Bản .bak đầu tiên vẫn nguyên vẹn

    ttml = p / "song.ttml"
    ttml.write_text('<tt><body><div><p><span begin="00:01.00">Hi</span></p></div></body></tt>', encoding="utf-8")
    assert classify_lyric_rank(ttml) == RANK_TTML_WORD

print("✅ Tất cả unit tests đã vượt qua!")
