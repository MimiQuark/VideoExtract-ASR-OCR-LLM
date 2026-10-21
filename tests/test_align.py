import extract


def test_extract_core_does_not_swallow_single_token_screen():
    assert extract._extract_core("整屏幻灯片正文和硬字幕文字", "硬字幕") == "硬字幕"


def test_extract_core_rejects_oversized_candidate():
    screen = "这是一整页非常长的幻灯片正文" * 20
    assert extract._extract_core(screen, "硬字幕") == ""


def test_splice_correction_rejects_large_insertion():
    speech = "硬字幕内容"
    core = "硬字幕内容" + "额外插入的整屏文字" * 10
    assert extract._splice_correction(speech, core) == speech


def test_norm_key_and_similarity_are_stable():
    assert extract._norm_key("PM2.5，测试！") == "pm25测试"
    assert extract._sim("abc", "abc") == 1.0
