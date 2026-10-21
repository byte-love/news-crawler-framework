from __future__ import annotations

from datetime import datetime
import re

# 统一日期解析工具：尽量兼容 Java 版本中的各种日期格式。
# 说明：这里只实现常用且与原逻辑一致的格式匹配，匹配成功后返回秒级时间戳。

_PATTERN_FORMATS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}"), "%Y-%m-%d %H:%M:%S"),
    (re.compile(r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}"), "%Y-%m-%d %H:%M"),
    (re.compile(r"\d{4}-\d{2}-\d{2}\s+\d{1}:\d{2}:\d{2}"), "%Y-%m-%d %H:%M:%S"),
    (re.compile(r"\d{4}-\d{2}-\d{2}\s+\d{1}:\d{2}"), "%Y-%m-%d %H:%M"),
    (re.compile(r"\d{4}年\d{2}月\d{2}日\s+\d{2}:\d{2}:\d{2}"), "%Y年%m月%d日 %H:%M:%S"),
    (re.compile(r"\d{4}年\d{2}月\d{2}日\s+\d{2}:\d{2}"), "%Y年%m月%d日 %H:%M"),
    (re.compile(r"\d{4}年\d{2}月\d{2}日\s+\d{1}:\d{2}:\d{2}"), "%Y年%m月%d日 %H:%M:%S"),
    (re.compile(r"\d{4}年\d{2}月\d{2}日\s+\d{1}:\d{2}"), "%Y年%m月%d日 %H:%M"),
    (re.compile(r"\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2}"), "%Y/%m/%d %H:%M:%S"),
    (re.compile(r"\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}"), "%Y/%m/%d %H:%M"),
    (re.compile(r"\d{4}/\d{2}/\d{2}\s+\d{1}:\d{2}:\d{2}"), "%Y/%m/%d %H:%M:%S"),
    (re.compile(r"\d{4}/\d{2}/\d{2}\s+\d{1}:\d{2}"), "%Y/%m/%d %H:%M"),
    (re.compile(r"\d{4}-\d{2}-\d{2}"), "%Y-%m-%d"),
    (re.compile(r"\d{4}年\d{2}月\d{2}日"), "%Y年%m月%d日"),
    (re.compile(r"\d{4}/\d{2}/\d{2}"), "%Y/%m/%d"),
    (re.compile(r"\d{4}-\d{1}-\d{1}\s+\d{2}:\d{2}:\d{2}"), "%Y-%m-%d %H:%M:%S"),
    (re.compile(r"\d{4}-\d{1}-\d{1}\s+\d{2}:\d{2}"), "%Y-%m-%d %H:%M"),
    (re.compile(r"\d{4}-\d{1}-\d{1}\s+\d{1}:\d{2}:\d{2}"), "%Y-%m-%d %H:%M:%S"),
    (re.compile(r"\d{4}-\d{1}-\d{1}\s+\d{1}:\d{2}"), "%Y-%m-%d %H:%M"),
    (re.compile(r"\d{4}-\d{1}-\d{2}\s+\d{2}:\d{2}:\d{2}"), "%Y-%m-%d %H:%M:%S"),
    (re.compile(r"\d{4}-\d{1}-\d{2}\s+\d{2}:\d{2}"), "%Y-%m-%d %H:%M"),
    (re.compile(r"\d{4}-\d{1}-\d{2}\s+\d{1}:\d{2}:\d{2}"), "%Y-%m-%d %H:%M:%S"),
    (re.compile(r"\d{4}-\d{1}-\d{2}\s+\d{1}:\d{2}"), "%Y-%m-%d %H:%M"),
    (re.compile(r"\d{4}-\d{2}-\d{1}\s+\d{2}:\d{2}:\d{2}"), "%Y-%m-%d %H:%M:%S"),
    (re.compile(r"\d{4}-\d{2}-\d{1}\s+\d{2}:\d{2}"), "%Y-%m-%d %H:%M"),
    (re.compile(r"\d{4}-\d{2}-\d{1}\s+\d{1}:\d{2}:\d{2}"), "%Y-%m-%d %H:%M:%S"),
    (re.compile(r"\d{4}-\d{2}-\d{1}\s+\d{1}:\d{2}"), "%Y-%m-%d %H:%M"),
    (re.compile(r"\d{4}年\d{1}月\d{1}日\s+\d{2}:\d{2}:\d{2}"), "%Y年%m月%d日 %H:%M:%S"),
    (re.compile(r"\d{4}年\d{1}月\d{1}日\s+\d{2}:\d{2}"), "%Y年%m月%d日 %H:%M"),
    (re.compile(r"\d{4}年\d{1}月\d{1}日\s+\d{1}:\d{2}:\d{2}"), "%Y年%m月%d日 %H:%M:%S"),
    (re.compile(r"\d{4}年\d{1}月\d{1}日\s+\d{1}:\d{2}"), "%Y年%m月%d日 %H:%M"),
    (re.compile(r"\d{4}年\d{1}月\d{2}日\s+\d{2}:\d{2}:\d{2}"), "%Y年%m月%d日 %H:%M:%S"),
    (re.compile(r"\d{4}年\d{1}月\d{2}日\s+\d{2}:\d{2}"), "%Y年%m月%d日 %H:%M"),
    (re.compile(r"\d{4}年\d{1}月\d{2}日\s+\d{1}:\d{2}:\d{2}"), "%Y年%m月%d日 %H:%M:%S"),
    (re.compile(r"\d{4}年\d{1}月\d{2}日\s+\d{1}:\d{2}"), "%Y年%m月%d日 %H:%M"),
    (re.compile(r"\d{4}年\d{2}月\d{1}日\s+\d{2}:\d{2}:\d{2}"), "%Y年%m月%d日 %H:%M:%S"),
    (re.compile(r"\d{4}年\d{2}月\d{1}日\s+\d{2}:\d{2}"), "%Y年%m月%d日 %H:%M"),
    (re.compile(r"\d{4}年\d{2}月\d{1}日\s+\d{1}:\d{2}:\d{2}"), "%Y年%m月%d日 %H:%M:%S"),
    (re.compile(r"\d{4}年\d{2}月\d{1}日\s+\d{1}:\d{2}"), "%Y年%m月%d日 %H:%M"),
    (re.compile(r"\d{4}年\d{1}月\d{2}日"), "%Y年%m月%d日"),
    (re.compile(r"\d{4}年\d{2}月\d{1}日"), "%Y年%m月%d日"),
    (re.compile(r"\d{4}年\d{1}月\d{1}日"), "%Y年%m月%d日"),
    (re.compile(r"\d{4}/\d{1}/\d{1}\s+\d{2}:\d{2}:\d{2}"), "%Y/%m/%d %H:%M:%S"),
    (re.compile(r"\d{4}/\d{1}/\d{1}\s+\d{2}:\d{2}"), "%Y/%m/%d %H:%M"),
    (re.compile(r"\d{4}/\d{1}/\d{1}\s+\d{1}:\d{2}:\d{2}"), "%Y/%m/%d %H:%M:%S"),
    (re.compile(r"\d{4}/\d{1}/\d{1}\s+\d{1}:\d{2}"), "%Y/%m/%d %H:%M"),
    (re.compile(r"\d{4}/\d{1}/\d{2}\s+\d{2}:\d{2}:\d{2}"), "%Y/%m/%d %H:%M:%S"),
    (re.compile(r"\d{4}/\d{1}/\d{2}\s+\d{2}:\d{2}"), "%Y/%m/%d %H:%M"),
    (re.compile(r"\d{4}/\d{1}/\d{2}\s+\d{1}:\d{2}:\d{2}"), "%Y/%m/%d %H:%M:%S"),
    (re.compile(r"\d{4}/\d{1}/\d{2}\s+\d{1}:\d{2}"), "%Y/%m/%d %H:%M"),
    (re.compile(r"\d{4}/\d{2}/\d{1}\s+\d{2}:\d{2}:\d{2}"), "%Y/%m/%d %H:%M:%S"),
    (re.compile(r"\d{4}/\d{2}/\d{1}\s+\d{2}:\d{2}"), "%Y/%m/%d %H:%M"),
    (re.compile(r"\d{4}/\d{2}/\d{1}\s+\d{1}:\d{2}:\d{2}"), "%Y/%m/%d %H:%M:%S"),
    (re.compile(r"\d{4}/\d{2}/\d{1}\s+\d{1}:\d{2}"), "%Y/%m/%d %H:%M"),
    (re.compile(r"\d{2}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}"), "%y-%m-%d %H:%M:%S"),
    (re.compile(r"\d{2}-\d{2}-\d{2}\s+\d{2}:\d{2}"), "%y-%m-%d %H:%M"),
    (re.compile(r"\d{2}-\d{2}-\d{2}\s+\d{1}:\d{2}:\d{2}"), "%y-%m-%d %H:%M:%S"),
    (re.compile(r"\d{2}-\d{2}-\d{2}\s+\d{1}:\d{2}"), "%y-%m-%d %H:%M"),
    (re.compile(r"\d{2}-\d{1}-\d{2}\s+\d{2}:\d{2}:\d{2}"), "%y-%m-%d %H:%M:%S"),
    (re.compile(r"\d{2}-\d{1}-\d{2}\s+\d{2}:\d{2}"), "%y-%m-%d %H:%M"),
    (re.compile(r"\d{2}-\d{1}-\d{2}\s+\d{1}:\d{2}:\d{2}"), "%y-%m-%d %H:%M:%S"),
    (re.compile(r"\d{2}-\d{1}-\d{2}\s+\d{1}:\d{2}"), "%y-%m-%d %H:%M"),
    (re.compile(r"\d{2}-\d{2}-\d{1}\s+\d{2}:\d{2}:\d{2}"), "%y-%m-%d %H:%M:%S"),
    (re.compile(r"\d{2}-\d{2}-\d{1}\s+\d{2}:\d{2}"), "%y-%m-%d %H:%M"),
    (re.compile(r"\d{2}-\d{2}-\d{1}\s+\d{1}:\d{2}:\d{2}"), "%y-%m-%d %H:%M:%S"),
    (re.compile(r"\d{2}-\d{2}-\d{1}\s+\d{1}:\d{2}"), "%y-%m-%d %H:%M"),
    (re.compile(r"\d{4}\s+\d{2}/\d{2}\s+\d{2}:\d{2}"), "%Y %m/%d %H:%M"),
    (re.compile(r"\d{4}/\s*\d{2}/\d{2}\s*\d{2}:\d{2}"), "%Y/%m/%d %H:%M"),
    (re.compile(r"\d{4}\s*/\s*\d{2}\s*/\s*\d{2}\s+\d{2}:\d{2}"), "%Y/%m/%d %H:%M"),
    (re.compile(r"\d{4}\.\d{2}\.\d{2}"), "%Y.%m.%d"),
    (re.compile(r"\d{4}\.\d{1}\.\d{2}"), "%Y.%m.%d"),
    (re.compile(r"\d{4}\.\d{2}\.\d{1}"), "%Y.%m.%d"),
    (re.compile(r"\d{4}\.\d{1}\.\d{1}"), "%Y.%m.%d"),
)


def date_change(old_date_str: str | None) -> int:
    """将字符串日期转换为秒级时间戳。"""
    if old_date_str is None:
        return 0

    text = old_date_str.strip()
    if not text or text.lower() == "null":
        return 0

    info = get_format_regex(text)
    if info[0] is None or info[1] is None:
        return 0

    try:
        return int(datetime.strptime(info[1], info[0]).timestamp())
    except ValueError:
        return 0


def get_format_regex(old_date_str: str | None) -> list[str | None]:
    """识别日期字符串的格式，并返回 `[格式, 日期片段]`。"""
    if old_date_str is None:
        return [None, None]

    for pattern, fmt in _PATTERN_FORMATS:
        match = pattern.search(old_date_str)
        if match:
            return [fmt, match.group(0)]

    return [None, None]


def get_feach_time() -> int:
    """获取当前时间的秒级时间戳。"""
    return int(datetime.now().timestamp())


__all__ = ["date_change", "get_format_regex", "get_feach_time"]
