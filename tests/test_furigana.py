from src.furigana import (
    add_furigana_to_text,
    add_furigana_to_html,
    add_furigana_to_blocks,
    _to_ruby,
)


def test_empty_and_no_kanji():
    assert add_furigana_to_text("") == ""
    assert add_furigana_to_text("abc 123 !? ひらがな カタカナ") == "abc 123 !? ひらがな カタカナ"
    assert add_furigana_to_html("<p>hello 123</p>") == "<p>hello 123</p>"


def test_okurigana_trimming():
    # 行き -> 行(い)き
    ruby1 = _to_ruby("行き", "いき")
    assert "<ruby>行<rt>い</rt></ruby>き" in ruby1

    # お話し -> お話(はな)し
    ruby2 = _to_ruby("お話し", "おはなし")
    assert "お<ruby>話<rt>はな</rt></ruby>し" in ruby2

    # 乃木坂 -> 乃木坂(のぎざか)
    ruby3 = _to_ruby("乃木坂", "のぎざか")
    assert "<ruby>乃木坂<rt>のぎざか</rt></ruby>" in ruby3


def test_idol_custom_dictionary():
    # 冨里奈央
    res_tomisato = add_furigana_to_text("冨里奈央です！")
    assert "<ruby>冨里<rt>とみさと</rt></ruby><ruby>奈央<rt>なお</rt></ruby>" in res_tomisato

    # 五百城茉央
    res_ioki = add_furigana_to_text("五百城茉央です")
    assert "<ruby>五百城<rt>いおき</rt></ruby><ruby>茉央<rt>まお</rt></ruby>" in res_ioki

    # 正源司陽子
    res_shogenji = add_furigana_to_text("正源司陽子です")
    assert "<ruby>正源司<rt>しょうげんじ</rt></ruby><ruby>陽子<rt>ようこ</rt></ruby>" in res_shogenji

    # 乃木坂46
    res_nogi = add_furigana_to_text("乃木坂46")
    assert "<ruby>乃木坂<rt>のぎざか</rt></ruby>46" in res_nogi

    # 今日は (custom override for reading as きょう)
    res_today = add_furigana_to_text("今日は良い天気ですね。")
    assert "<ruby>今日<rt>きょう</rt></ruby>は" in res_today


def test_html_dom_preservation():
    html_input = (
        '<div class="blog-body">'
        '<p>こんにちは！<b>冨里奈央</b>です。<br>'
        '<img src="https://example.com/pic.jpg" alt="test">'
        '<a href="https://nogizaka46.com">乃木坂46公式</a>'
        '</p>'
        '</div>'
    )
    result = add_furigana_to_html(html_input)

    # 验证 HTML 标签与属性完整保留
    assert '<div class="blog-body">' in result
    assert 'src="https://example.com/pic.jpg"' in result
    assert '<a href="https://nogizaka46.com">' in result
    # 验证汉字已安全包裹 ruby 标签
    assert "<ruby>冨里<rt>とみさと</rt></ruby>" in result
    assert "<ruby>奈央<rt>なお</rt></ruby>" in result
    assert "<ruby>乃木坂<rt>のぎざか</rt></ruby>" in result
    assert "<ruby>公式<rt>こうしき</rt></ruby>" in result


def test_structured_blocks():
    blocks = [
        {"type": "p", "jp": "冨里奈央です！", "zh": "我是冨里奈央！"},
        {"type": "img", "src": "/images/01.jpg"},
        {"type": "p", "jp": "今日は握手会でした。", "zh": "今天是握手会。"},
    ]
    f_blocks = add_furigana_to_blocks(blocks)
    assert len(f_blocks) == 3
    assert "<ruby>冨里<rt>とみさと</rt></ruby>" in f_blocks[0]["jp"]
    assert f_blocks[0]["zh"] == "我是冨里奈央！"
    assert f_blocks[1]["type"] == "img"
    assert f_blocks[1]["src"] == "/images/01.jpg"
    assert "<ruby>握手会<rt>あくしゅかい</rt></ruby>" in f_blocks[2]["jp"]
