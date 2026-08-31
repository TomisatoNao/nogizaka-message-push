"""
src/furigana.py — 振假名 (Furigana / 振仮名) 智能生成引擎

基于 pykakasi 实现纯 Python、零 C++ 编译依赖的极速注音转换，
支持 HTML DOM 原位保持、送假名 (Okurigana) 精准对齐、
以及坂道三团成员姓名与偶像特有词汇的自定义词典补正。
"""

import logging
import re
from typing import Any
from bs4 import BeautifulSoup

log = logging.getLogger("collink")

try:
    import pykakasi
    _kks = pykakasi.kakasi()
except Exception as _e:
    _kks = None
    log.warning("[furigana] pykakasi 未安装或加载失败: %s", _e)

# 坂道三团特有专有名词与成员生僻读音字典（优先级最高，先行精准替换）
CUSTOM_DICT: dict[str, str] = {
    # 团体名
    "乃木坂46": "<ruby>乃木坂<rt>のぎざか</rt></ruby>46",
    "櫻坂46": "<ruby>櫻坂<rt>さくらざか</rt></ruby>46",
    "日向坂46": "<ruby>日向坂<rt>ひなたざか</rt></ruby>46",
    "乃木坂": "<ruby>乃木坂<rt>のぎざか</rt></ruby>",
    "櫻坂": "<ruby>櫻坂<rt>さくらざか</rt></ruby>",
    "日向坂": "<ruby>日向坂<rt>ひなたざか</rt></ruby>",
    "欅坂46": "<ruby>欅坂<rt>けやきざか</rt></ruby>46",
    "欅坂": "<ruby>欅坂<rt>けやきざか</rt></ruby>",
    # 常用口语 / 习惯读音
    "今日は": "<ruby>今日<rt>きょう</rt></ruby>は",
    "明日は": "<ruby>明日<rt>あした</rt></ruby>は",
    "昨日は": "<ruby>昨日<rt>きのう</rt></ruby>は",
    # 乃木坂46 成员
    "冨里奈央": "<ruby>冨里<rt>とみさと</rt></ruby><ruby>奈央<rt>なお</rt></ruby>",
    "冨里": "<ruby>冨里<rt>とみさと</rt></ruby>",
    "五百城茉央": "<ruby>五百城<rt>いおき</rt></ruby><ruby>茉央<rt>まお</rt></ruby>",
    "五百城": "<ruby>五百城<rt>いおき</rt></ruby>",
    "池田瑛紗": "<ruby>池田<rt>いけだ</rt></ruby><ruby>瑛紗<rt>てれさ</rt></ruby>",
    "瑛紗": "<ruby>瑛紗<rt>てれさ</rt></ruby>",
    "一ノ瀬美空": "<ruby>一ノ瀬<rt>いちのせ</rt></ruby><ruby>美空<rt>みく</rt></ruby>",
    "一ノ瀬": "<ruby>一ノ瀬<rt>いちのせ</rt></ruby>",
    "井上和": "<ruby>井上<rt>いのうえ</rt></ruby><ruby>和<rt>なぎ</rt></ruby>",
    "小川彩": "<ruby>小川<rt>おがわ</rt></ruby><ruby>彩<rt>あや</rt></ruby>",
    "奥田いろは": "<ruby>奥田<rt>おくだ</rt></ruby>いろは",
    "川﨑桜": "<ruby>川﨑<rt>かわさき</rt></ruby><ruby>桜<rt>さくら</rt></ruby>",
    "川崎桜": "<ruby>川崎<rt>かわさき</rt></ruby><ruby>桜<rt>さくら</rt></ruby>",
    "菅原咲月": "<ruby>菅原<rt>すがわら</rt></ruby><ruby>咲月<rt>さつき</rt></ruby>",
    "中西アルノ": "<ruby>中西<rt>なかにし</rt></ruby>アルノ",
    "遠藤さくら": "<ruby>遠藤<rt>えんどう</rt></ruby>さくら",
    "賀喜遥香": "<ruby>賀喜<rt>かき</rt></ruby><ruby>遥香<rt>はるか</rt></ruby>",
    "田村真佑": "<ruby>田村<rt>たむら</rt></ruby><ruby>真佑<rt>まゆ</rt></ruby>",
    "筒井あやめ": "<ruby>筒井<rt>つつい</rt></ruby>あやめ",
    "弓木奈於": "<ruby>弓木<rt>ゆみき</rt></ruby><ruby>奈於<rt>なお</rt></ruby>",
    "金川紗耶": "<ruby>金川<rt>かながわ</rt></ruby><ruby>紗耶<rt>さや</rt></ruby>",
    "柴田柚菜": "<ruby>柴田<rt>しばた</rt></ruby><ruby>柚菜<rt>ゆな</rt></ruby>",
    "清宮レイ": "<ruby>清宮<rt>せいみや</rt></ruby>レイ",
    "早川圣来": "<ruby>早川<rt>はやかわ</rt></ruby><ruby>せいら<rt>せいら</rt></ruby>",
    "早川聖来": "<ruby>早川<rt>はやかわ</rt></ruby><ruby>せいら<rt>せいら</rt></ruby>",
    "山下美月": "<ruby>山下<rt>やました</rt></ruby><ruby>美月<rt>みづき</rt></ruby>",
    "与田祐希": "<ruby>与田<rt>よだ</rt></ruby><ruby>祐希<rt>ゆうき</rt></ruby>",
    "久保史緒里": "<ruby>久保<rt>くぼ</rt></ruby><ruby>史緒里<rt>しおり</rt></ruby>",
    "梅澤美波": "<ruby>梅澤<rt>うめざわ</rt></ruby><ruby>美波<rt>みなみ</rt></ruby>",
    "岩本蓮加": "<ruby>岩本<rt>いわもと</rt></ruby><ruby>蓮加<rt>れんか</rt></ruby>",
    "阪口珠美": "<ruby>阪口<rt>さかぐち</rt></ruby><ruby>珠美<rt>たまみ</rt></ruby>",
    "佐藤楓": "<ruby>佐藤<rt>さとう</rt></ruby><ruby>楓<rt>かえで</rt></ruby>",
    "中村麗乃": "<ruby>中村<rt>なかむら</rt></ruby><ruby>麗乃<rt>れの</rt></ruby>",
    "向井葉月": "<ruby>向井<rt>むかい</rt></ruby><ruby>葉月<rt>はづき</rt></ruby>",
    "吉田綾乃クリスティー": "<ruby>吉田<rt>よしだ</rt></ruby><ruby>綾乃<rt>あやの</rt></ruby>クリスティー",
    "齋藤飞鸟": "<ruby>齋藤<rt>さいとう</rt></ruby><ruby>飞鸟<rt>あすか</rt></ruby>",
    "齋藤飛鳥": "<ruby>齋藤<rt>さいとう</rt></ruby><ruby>飛鳥<rt>あすか</rt></ruby>",
    "西野七瀬": "<ruby>西野<rt>にしの</rt></ruby><ruby>七瀬<rt>ななせ</rt></ruby>",
    "白石麻衣": "<ruby>白石<rt>しらいし</rt></ruby><ruby>麻衣<rt>まい</rt></ruby>",
    "生田絵梨花": "<ruby>生田<rt>いくた</rt></ruby><ruby>絵梨花<rt>えりか</rt></ruby>",
    "橋本奈々未": "<ruby>橋本<rt>はしもと</rt></ruby><ruby>奈々未<rt>ななみ</rt></ruby>",
    "秋元真夏": "<ruby>秋元<rt>あきもと</rt></ruby><ruby>真夏<rt>まなつ</rt></ruby>",
    "生駒里奈": "<ruby>生駒<rt>いこま</rt></ruby><ruby>里奈<rt>りな</rt></ruby>",
    "桜井玲香": "<ruby>桜井<rt>さくらい</rt></ruby><ruby>玲香<rt>れいか</rt></ruby>",
    "高山一実": "<ruby>高山<rt>たかやま</rt></ruby><ruby>一実<rt>かずみ</rt></ruby>",
    "星野みなみ": "<ruby>星野<rt>ほしの</rt></ruby>みなみ",
    "松村沙友理": "<ruby>松村<rt>まつむら</rt></ruby><ruby>沙友理<rt>さゆり</rt></ruby>",
    "若月佑美": "<ruby>若月<rt>わかつき</rt></ruby><ruby>佑美<rt>ゆみ</rt></ruby>",
    # 櫻坂46 成员
    "的野美青": "<ruby>的野<rt>まとの</rt></ruby><ruby>美青<rt>みお</rt></ruby>",
    "山下瞳月": "<ruby>山下<rt>やました</rt></ruby><ruby>瞳月<rt>しづき</rt></ruby>",
    "谷口愛季": "<ruby>谷口<rt>たにぐち</rt></ruby><ruby>愛季<rt>あいり</rt></ruby>",
    "村井優": "<ruby>村井<rt>むらい</rt></ruby><ruby>優<rt>ゆう</rt></ruby>",
    "村山美羽": "<ruby>村山<rt>むらやま</rt></ruby><ruby>美羽<rt>みう</rt></ruby>",
    "中嶋優月": "<ruby>中嶋<rt>なかしま</rt></ruby><ruby>優月<rt>ゆづき</rt></ruby>",
    "小島凪紗": "<ruby>小島<rt>こじま</rt></ruby><ruby>凪紗<rt>なぎさ</rt></ruby>",
    "小田倉麗奈": "<ruby>小田倉<rt>おだくら</rt></ruby><ruby>麗奈<rt>れいな</rt></ruby>",
    "石森璃花": "<ruby>石森<rt>いしもり</rt></ruby><ruby>璃花<rt>りか</rt></ruby>",
    "遠藤理子": "<ruby>遠藤<rt>えんどう</rt></ruby><ruby>理子<rt>りこ</rt></ruby>",
    "向井純葉": "<ruby>向井<rt>むかい</rt></ruby><ruby>純葉<rt>いとは</rt></ruby>",
    "森田ひかる": "<ruby>森田<rt>もりた</rt></ruby>ひかる",
    "田村保乃": "<ruby>田村<rt>たむら</rt></ruby><ruby>保乃<rt>ほの</rt></ruby>",
    "守屋麗奈": "<ruby>守屋<rt>もりや</rt></ruby><ruby>麗奈<rt>れな</rt></ruby>",
    "藤吉夏鈴": "<ruby>藤吉<rt>ふじよし</rt></ruby><ruby>夏鈴<rt>かりん</rt></ruby>",
    "山﨑天": "<ruby>山﨑<rt>やまさき</rt></ruby><ruby>天<rt>てん</rt></ruby>",
    "武元唯衣": "<ruby>武元<rt>たけもと</rt></ruby><ruby>唯衣<rt>ゆい</rt></ruby>",
    "大園玲": "<ruby>大園<rt>おおぞの</rt></ruby><ruby>玲<rt>れい</rt></ruby>",
    "増本綺良": "<ruby>増本<rt>ますもと</rt></ruby><ruby>綺良<rt>きら</rt></ruby>",
    "幸阪茉里乃": "<ruby>幸阪<rt>こうさか</rt></ruby><ruby>茉里乃<rt>まりの</rt></ruby>",
    "井上梨名": "<ruby>井上<rt>いのうえ</rt></ruby><ruby>梨名<rt>りな</rt></ruby>",
    "大沼晶保": "<ruby>大沼<rt>おおぬま</rt></ruby><ruby>晶保<rt>あきほ</rt></ruby>",
    "小林由依": "<ruby>小林<rt>こばやし</rt></ruby><ruby>由依<rt>ゆい</rt></ruby>",
    "菅井友香": "<ruby>菅井<rt>すがい</rt></ruby><ruby>友香<rt>ゆうか</rt></ruby>",
    "渡邉理佐": "<ruby>渡邉<rt>わたなべ</rt></ruby><ruby>理佐<rt>りさ</rt></ruby>",
    "土生瑞穂": "<ruby>土生<rt>はぶ</rt></ruby><ruby>瑞穂<rt>みづほ</rt></ruby>",
    # 日向坂46 成员
    "正源司陽子": "<ruby>正源司<rt>しょうげんじ</rt></ruby><ruby>陽子<rt>ようこ</rt></ruby>",
    "正源司": "<ruby>正源司<rt>しょうげんじ</rt></ruby>",
    "藤嶌果歩": "<ruby>藤嶌<rt>ふじしま</rt></ruby><ruby>果歩<rt>かほ</rt></ruby>",
    "宮地すみれ": "<ruby>宮地<rt>みやち</rt></ruby>すみれ",
    "渡辺莉奈": "<ruby>渡辺<rt>わたなべ</rt></ruby><ruby>莉奈<rt>りな</rt></ruby>",
    "清水理央": "<ruby>清水<rt>しみず</rt></ruby><ruby>理央<rt>りお</rt></ruby>",
    "石塚瑶季": "<ruby>石塚<rt>いしづか</rt></ruby><ruby>瑶季<rt>たまき</rt></ruby>",
    "小西夏菜実": "<ruby>小西<rt>こにし</rt></ruby><ruby>夏菜実<rt>ななみ</rt></ruby>",
    "竹内希来里": "<ruby>竹内<rt>たけうち</rt></ruby><ruby>希来里<rt>きらり</rt></ruby>",
    "平尾帆夏": "<ruby>平尾<rt>ひらお</rt></ruby><ruby>帆夏<rt>ほのか</rt></ruby>",
    "平岡海月": "<ruby>平岡<rt>ひらおか</rt></ruby><ruby>海月<rt>みつき</rt></ruby>",
    "山下葉留花": "<ruby>山下<rt>やました</rt></ruby><ruby>葉留花<rt>はるか</rt></ruby>",
    "金村美玖": "<ruby>金村<rt>かねむら</rt></ruby><ruby>美玖<rt>みく</rt></ruby>",
    "小坂菜緒": "<ruby>小坂<rt>こさか</rt></ruby><ruby>菜緒<rt>なお</rt></ruby>",
    "丹生明里": "<ruby>丹生<rt>にぶ</rt></ruby><ruby>明里<rt>あかり</rt></ruby>",
    "松田好花": "<ruby>松田<rt>まつだ</rt></ruby><ruby>好花<rt>このか</rt></ruby>",
    "河田陽菜": "<ruby>河田<rt>かわた</rt></ruby><ruby>陽菜<rt>ひな</rt></ruby>",
    "富田鈴花": "<ruby>富田<rt>とみた</rt></ruby><ruby>鈴花<rt>すずか</rt></ruby>",
    "濱岸ひより": "<ruby>濱岸<rt>はまぎし</rt></ruby>ひより",
    "上村ひなの": "<ruby>上村<rt>かみむら</rt></ruby>ひなの",
    "髙桥未来虹": "<ruby>髙桥<rt>たかはし</rt></ruby><ruby>未来虹<rt>みくに</rt></ruby>",
    "森本茉莉": "<ruby>森本<rt>もりもと</rt></ruby><ruby>茉莉<rt>まりぃ</rt></ruby>",
    "山口陽世": "<ruby>山口<rt>やまぐち</rt></ruby><ruby>陽世<rt>はるよ</rt></ruby>",
    "佐々木美玲": "<ruby>佐々木<rt>ささき</rt></ruby><ruby>美玲<rt>みれい</rt></ruby>",
    "佐々木久美": "<ruby>佐々木<rt>ささき</rt></ruby><ruby>久美<rt>くみ</rt></ruby>",
    "加藤史帆": "<ruby>加藤<rt>かとう</rt></ruby><ruby>史帆<rt>しほ</rt></ruby>",
    "東村芽依": "<ruby>東村<rt>ひがしむら</rt></ruby><ruby>芽依<rt>めい</rt></ruby>",
    "高本彩花": "<ruby>高本<rt>たかもと</rt></ruby><ruby>彩花<rt>あやか</rt></ruby>",
    "齊藤京子": "<ruby>齊藤<rt>さいとう</rt></ruby><ruby>京子<rt>きょうこ</rt></ruby>",
    "影山優佳": "<ruby>影山<rt>かげやま</rt></ruby><ruby>優佳<rt>ゆうか</rt></ruby>",
}

# 预编译自定义字典正则（按字符串长度倒序，优先匹配长词）
_CUSTOM_PATTERN = re.compile(
    "|".join(re.escape(k) for k in sorted(CUSTOM_DICT.keys(), key=len, reverse=True))
) if CUSTOM_DICT else None


def _to_ruby(orig: str, hira: str) -> str:
    """将单组词汇精细对齐送假名并生成 ruby 标签。"""
    # 无汉字或已相同则原样返回
    if not re.search(r"[\u4e00-\u9faf\u3400-\u4dbf]", orig) or orig == hira:
        return orig

    # 1. 剥离前导平假名/片假名/符号（如 「お話し」 -> 前缀 「お」）
    p_len = 0
    while (
        p_len < len(orig)
        and p_len < len(hira)
        and orig[p_len] == hira[p_len]
        and not re.search(r"[\u4e00-\u9faf\u3400-\u4dbf]", orig[p_len])
    ):
        p_len += 1

    # 2. 剥离尾随送假名（如 「行き」 -> 后缀 「き」）
    s_len = 0
    while (
        s_len < (len(orig) - p_len)
        and s_len < (len(hira) - p_len)
        and orig[-1 - s_len] == hira[-1 - s_len]
        and not re.search(r"[\u4e00-\u9faf\u3400-\u4dbf]", orig[-1 - s_len])
    ):
        s_len += 1

    prefix = orig[:p_len] if p_len > 0 else ""
    suffix = orig[len(orig) - s_len:] if s_len > 0 else ""
    kanji = orig[p_len:len(orig) - s_len] if s_len > 0 else orig[p_len:]
    reading = hira[p_len:len(hira) - s_len] if s_len > 0 else hira[p_len:]

    if kanji:
        return f"{prefix}<ruby>{kanji}<rt>{reading}</rt></ruby>{suffix}"
    return orig


def add_furigana_to_text(text: str) -> str:
    """对纯日文文本增加振假名注音 HTML。"""
    if not text or not _kks:
        return text

    # 先用自定义词典做精确保护与替换
    if _CUSTOM_PATTERN:
        markers: list[str] = []

        def _repl(m: re.Match) -> str:
            markers.append(CUSTOM_DICT[m.group(0)])
            return f"__FURIGANA_MARKER_{len(markers)-1}__"

        protected_text = _CUSTOM_PATTERN.sub(_repl, text)
    else:
        protected_text = text
        markers = []

    # 调用 pykakasi 分词转换
    result = _kks.convert(protected_text)
    out: list[str] = []
    for r in result:
        orig, hira = r["orig"], r["hira"]
        # 还原 marker
        if "__FURIGANA_MARKER_" in orig:
            for idx, mark in enumerate(markers):
                marker_str = f"__FURIGANA_MARKER_{idx}__"
                if marker_str in orig:
                    orig = orig.replace(marker_str, mark)
            out.append(orig)
        else:
            out.append(_to_ruby(orig, hira))

    return "".join(out)


def add_furigana_to_html(html_str: str) -> str:
    """遍历 HTML DOM 的文本节点，安全注入 ruby 振假名标签，完整保留 HTML 标签结构与图片。"""
    if not html_str or not _kks:
        return html_str

    soup = BeautifulSoup(html_str, "html.parser")
    # 查找所有文本节点
    for text_node in list(soup.find_all(string=True)):
        # 忽略 script, style, 已有 ruby, 属性等节点
        if text_node.parent and text_node.parent.name in [
            "script", "style", "ruby", "rt", "head", "title"
        ]:
            continue
        text = str(text_node)
        # 仅在文本中包含汉字时进行转换，极大提升性能
        if not re.search(r"[\u4e00-\u9faf\u3400-\u4dbf]", text):
            continue

        annotated = add_furigana_to_text(text)
        if annotated != text:
            frag_soup = BeautifulSoup(annotated, "html.parser")
            text_node.replace_with(frag_soup)

    return str(soup)


def add_furigana_to_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """对结构化博客块列表中的日文段落 (jp) 添加振假名，返回全新的 blocks 列表。"""
    if not blocks or not _kks:
        return blocks

    new_blocks = []
    for b in blocks:
        nb = dict(b)
        if "jp" in nb and isinstance(nb["jp"], str):
            nb["jp"] = add_furigana_to_html(nb["jp"])
        new_blocks.append(nb)
    return new_blocks
