"""归一化规则测试：做什么 / 明确不做什么。"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for _key in ("REDIS_URL", "NMSG_MIN_LENGTH"):
    os.environ.pop(_key, None)

from agent_system.cache.nmsg_normalize import is_cacheable, normalize_message  # noqa: E402


def test_fullwidth_punctuation_to_halfwidth():
    """全角标点转半角。"""
    assert normalize_message("，。？！") == ",.?!"


def test_internal_whitespace_removed():
    """内部空白全部去除。"""
    assert normalize_message("你好 世界") == "你好世界"


def test_lowercase_ascii():
    """英文/数字转小写。"""
    assert normalize_message("Hello WORLD") == "helloworld"


def test_repeated_punctuation_collapsed():
    """连续重复句读标点折叠为一个。"""
    assert normalize_message("真的吗？？？？") == "真的吗?"


def test_emoji_and_invisible_removed():
    """emoji 与不可见字符被删除。"""
    assert normalize_message("你好😀") == "你好"
    assert normalize_message("你\u200b好") == "你好"


def test_pure_digits_not_cacheable():
    """纯数字（订单号/物流单号）不缓存。"""
    assert is_cacheable("123456") is False


def test_empty_message_not_cacheable():
    """只含标点或 emoji 的消息归一化为空，不可缓存。"""
    assert normalize_message("？？？😀") == "?"
    assert is_cacheable(normalize_message("。")) is False
    assert is_cacheable("") is False


def test_short_message_not_cacheable():
    """归一化后长度小于 4 的短消息不缓存。"""
    assert is_cacheable("好") is False
    assert is_cacheable("好的") is False


def test_no_synonym_replacement():
    """不做同义词替换：多少钱 与 价格 归一化后不同。"""
    assert normalize_message("多少钱") != normalize_message("价格")


def test_no_stopword_removal():
    """不做停用词剔除：请问一下这个商品 与 这个商品 不同。"""
    assert normalize_message("请问一下这个商品") != normalize_message("这个商品")


def test_normalization_is_stable():
    """同一输入多次调用结果一致。"""
    message = "  这个商品 多少钱？？ "
    assert normalize_message(message) == normalize_message(message)
    assert normalize_message(message) == "这个商品多少钱?"
