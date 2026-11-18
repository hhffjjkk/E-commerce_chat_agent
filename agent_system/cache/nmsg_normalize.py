# agent_system/cache/nmsg_normalize.py
# 归一化消息：把用户输入压缩成“同一写法”的字符串，用作缓存 key 的一部分。
#
# 这是**字符级归一化**，不是语义理解：
#   - 命中条件是「稳定前缀哈希 + 归一化消息哈希」完全相等；
#   - 不做同义词/停用词/繁简/词序处理（见文件末尾“不做什么”）。
# 这样既能吸收全角/空格/大小写/重复标点等“同一句话的不同写法”，
# 又不会把语义不同的问题误判为同一条缓存。

import re
import unicodedata

# 中文/全角标点到半角标点的显式映射。
# NFKC 已能把大部分全角字符转半角，但中文句号「。」、顿号「、」、
# 书名号等不在 NFKC 的兼容映射内，必须手动补齐。
_PUNCT_MAP = str.maketrans(
    {
        "。": ".",
        "、": ",",
        "《": "<",
        "》": ">",
        "「": '"',
        "」": '"',
        "『": '"',
        "』": '"',
        "【": "[",
        "】": "]",
        "～": "~",
        "—": "-",
        "－": "-",
        "…": ".",
    }
)

# 连续重复的“结构标点”折叠为一个：真的吗？？？？ -> 真的吗?
# 只折叠句读类标点，不折叠数字/字母，避免把 "1000" 变成 "10"。
_REPEAT_PUNCT = re.compile(r"([,.?!:;])\1+")

# 纯数字/纯符号消息（订单号、物流单号、日期等）不缓存：
# 这类消息彼此之间没有复用价值，缓存只会浪费内存并增加误命中风险。
_NON_TEXT = re.compile(r"^[\d\s.,?!:;+\-*/()%<>\[\]~]+$")

# 变体选择符（emoji 的肤色/样式修饰）category 是 Mn，单独清理。
_VARIATION_SELECTORS = {"\ufe0e", "\ufe0f"}


def _strip_invisible_and_emoji(text: str) -> str:
    """删除不可见字符与 emoji。

    参数:
        text: 原始文本。

    返回:
        删除后的文本。

    说明:
        Unicode category 以 C 开头的是控制/格式/代理/私用字符；
        So 是 emoji 与杂项符号。两者都不参与缓存 key，
        否则同一句“你好😀”与“你好”会因 emoji 不同而无法命中。
    """
    kept = []
    for char in text:
        category = unicodedata.category(char)
        if category.startswith("C") or category == "So":
            continue
        if char in _VARIATION_SELECTORS:
            continue
        kept.append(char)
    return "".join(kept)


def normalize_message(message: str) -> str:
    """把用户消息归一化为缓存 key 的输入。

    做什么:
      - NFKC 统一全角/半角与兼容字符；
      - 去除所有空白（内部空白对命中影响最大）；
      - 英文与数字转小写；
      - 中文标点转半角；
      - 连续重复句读标点折叠；
      - 去除 emoji 与不可见字符。

    不做什么:
      - 不做同义词替换：\"多少钱\" 与 \"价格\" 仍视为不同消息；
      - 不做停用词剔除：\"请问一下这个商品\" 与 \"这个商品\" 不同；
      - 不做繁简转换：\"退款\" 与 \"退款\" 的繁体写法不会互相命中；
      - 不做词序调整：\"退款申请\" 与 \"申请退款\" 不同。
    这些都是“语义缓存（embedding）”的范畴，本步骤不实现。

    参数:
        message: 用户原始消息。

    返回:
        归一化后的字符串；只含标点/emoji 时返回空串。
    """
    text = unicodedata.normalize("NFKC", message or "")
    text = _strip_invisible_and_emoji(text)
    text = text.translate(_PUNCT_MAP)
    text = text.lower()
    text = re.sub(r"\s+", "", text)
    text = _REPEAT_PUNCT.sub(r"\1", text)
    return text


def is_cacheable(normalized: str, *, min_length: int | None = None) -> bool:
    """判断归一化后的消息是否值得缓存。

    参数:
        normalized: normalize_message 的返回值。
        min_length: 最小长度；None 时读取 NMSG_MIN_LENGTH（默认 4）。

    返回:
        True 表示可缓存。

    说明:
        两条约束：
          1. 空串或长度小于阈值不缓存，避免 \"好\" / \"嗯\" / \"确认\"
             这类短消息互相命中；
          2. 纯数字/纯符号不缓存（订单号、物流单号没有复用价值）。
    """
    if not normalized:
        return False
    if min_length is None:
        # 延迟导入 config，避免归一化模块被单独引用时产生配置依赖环
        from agent_system.config import get_settings

        min_length = get_settings().nmsg_min_length
    if len(normalized) < min_length:
        return False
    if _NON_TEXT.fullmatch(normalized):
        return False
    return True
