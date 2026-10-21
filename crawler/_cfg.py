# -*- coding: utf-8 -*-
"""新时段抓取公共配置与安全入库层。

时段：2026-07-01 ~ 2026-10-10
目标库：news_batch_2026q3`n20260101-20260530-newspaperNews网络新闻补抓 => news_batch_2026h1`nnewspaperNews网络新闻补抓 => news_batch
字段  ：_id, title, content, from, publishtime, raw_publishTime, fetchTime, baseUrl, sourse
        （照 20260101-20260530-newspaperNews网络新闻补抓 原样）
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime
from typing import Any

# --- 让 model / utils 可被导入（不写死绝对路径） ---
_ONLINENEWS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ONLINENEWS_DIR not in sys.path:
    sys.path.insert(0, _ONLINENEWS_DIR)

from bson import Int64  # noqa: E402
from utils.DateChangeUtil import date_change  # noqa: E402

# ==================== 时段配置 ====================
DB_NAME = "news_batch_2026q3"
WINDOW_START_STR = "2026-07-01 00:00:00"
WINDOW_END_STR = "2026-10-10 23:59:59"

# ==================== 控制台编码兜底（全项目生效） ====================
# 每个站脚本都会 import _cfg，所以放在这里最省事。
# 2026-10-15 实测：Windows 控制台默认 GBK，标题里只要有一个 \xa0（不换行空格）
# 或 U+200B（零宽空格），print 就抛 UnicodeEncodeError **把整个站打断**
# （内蒙古新闻网第一次跑就这么崩的）。改成 utf-8 + errors='replace'，
# 宁可个别字符显示成 '?'，也不能让一个字符毁掉整站。
try:
    import sys as _sys
    _sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    _sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass
# ======================================================================

WINDOW_START = int(datetime(2026, 7, 1, 0, 0, 0).timestamp())
WINDOW_END = int(datetime(2026, 10, 10, 23, 59, 59).timestamp())

# 给"按日期翻电子报/按日期区间查接口"的脚本用（它们把旧时段写死在代码里）
WINDOW_START_DT = datetime(2026, 7, 1)
WINDOW_END_DT = datetime(2026, 10, 10)
WINDOW_START_DATE = WINDOW_START_DT.date()
WINDOW_END_DATE = WINDOW_END_DT.date()
WINDOW_START_DAY = '2026-07-01'
WINDOW_END_DAY = '2026-10-10'
WINDOW_START_ISO = '2026-07-01T00:00:00Z'
WINDOW_END_ISO = '2026-10-10T23:59:59Z'

# 每个关键词之间的间隔（秒）。老脚本是 2，压到 1 可省约一半时间。
KEY_SLEEP = float(os.environ.get("H2_KEY_SLEEP", "1.0"))
# 详情页之间的间隔（秒）
DETAIL_SLEEP = float(os.environ.get("H2_DETAIL_SLEEP", "0.8"))
# 试点用：只跑前 N 个关键词（0 = 全量）
KEY_LIMIT = int(os.environ.get("H2_KEY_LIMIT", "0"))
# 每站最多入库条数（0 = 不限），便于试点
MAX_INSERT = int(os.environ.get("H2_MAX_INSERT", "0"))

MIN_CONTENT_LEN = 30


def normalize_raw_time(ts: int) -> str:
    """把 raw_publishTime 统一成规范格式 YYYY-MM-DD HH:MM:SS。

    依据：下游主表 P_NewsNet.20260101-20260630网络新闻合并 抽样 400/400 都是这个格式，
    源库 20260101-20260530 也以它为主。各站页面上的原始写法五花八门
    （2026年10月05日 11:16 / 2026-10-10 20:13 / 2026.10.10 / 2026/10/5 ...），
    统一按已解析出的 publishtime 反推，保证 269 个站格式完全一致。
    """
    if not ts:
        return ''
    return datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')


KW_STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_state_kw')
_KW_T0 = time.time()
# 有 9 个脚本开机就把关键词整表物化一遍（[k.strip() for k in load_keys()]），
# 那会在几毫秒内"读"到最后一个词。所以开机后前 90 秒一律不落盘，
# 只有真正跑了一会儿之后的进度才算数。
_KW_PERSIST_AFTER = 90.0
_KW_PERSIST_GAP = 20.0
_KW_LIVE: list = []


def _kw_station() -> str:
    """当前进程跑的是哪个站 —— 用入口脚本文件名判断。"""
    try:
        return os.path.basename(sys.argv[0]).rsplit('.', 1)[0]
    except Exception:
        return ''


# 把关键词当**过滤器**（而不是待办队列）的写法。这类站必须拿全量关键词：
# 只喂一部分，匹配不到的文章会被静默丢掉，一条报错都没有。
# 注意这必须和 _run.FILTER_MARKS 保持一致 —— 分片会切片、断点会截断，
# 两条路都会把过滤器的关键词表弄残。


def _code_names(src: str) -> set:
    """源码里**真正出现在代码里**的名字/属性名（不含注释与字符串字面量）。

    为什么不能用 `'run_colpage' in 源码全文`：注释和 docstring 里提一句就会命中，
    把"关键词待办队列"误判成"关键词过滤器" —— `load_keys()` 会静默退化成
    不记断点的普通 list（每轮从第 1 个词重跑），`is_shardable()` 也会误判成不可拆片。
    2026-10-15 东方网.py 第一版就是这么栽的。
    """
    import ast
    try:
        tree = ast.parse(src)
    except Exception:
        return set()
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
    return out

_KW_FILTER_MARKS = ('run_colpage', 'run_listapi', 'run_browser_home',
                    'build_matcher', 'keyset')


def _kw_no_resume(station: str) -> bool:
    """哪些站不能用关键词断点/分片。

    栏目翻页型（run_colpage）、列表接口型（run_listapi）、浏览器首页型
    （run_browser_home）都是"抓列表 -> 本地按关键词筛"，关键词是**过滤器**；
    专用脚本自己建 build_matcher/keyset 同理。这几类必须拿全量，
    给了断点会从中间截断、给了分片会只剩一片。
    """
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), station + '.py')
        src = open(p, encoding='utf-8', errors='replace').read()
        # 只看代码里真调用了什么，不看注释/docstring —— 新站 docstring 里提一句
        # run_colpage 就会把"待办队列"误判成"过滤器"，load_keys() 静默退化成
        # 不记断点的普通 list（每轮从第 1 个词重跑）。2026-10-15 东方网第一版就栽了。
        names = _code_names(src)
        if names:
            return any(m in names for m in _KW_FILTER_MARKS)
        return any(m in src for m in _KW_FILTER_MARKS)     # AST 解析失败时退回全文扫描
    except Exception:
        return True


def _kw_browser(station: str) -> bool:
    """浏览器型脚本（DrissionPage）。

    这类站**可以**拆片，但必须每片一个调试端口：`_run.run_station` 拆片时会给
    每片设 H2_BROWSER_PORT，`install_edge_default()` 再把端口和 profile 目录都
    按它分开。少了这个环境变量就退回单片 —— 多个进程抢同一个端口会互相踩，
    而关键词表被切掉几分之几会**静默漏数据**，比跑得慢严重得多。
    `_run.is_shardable` 的规则必须和这里一致。
    """
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), station + '.py')
        return 'DrissionPage' in open(p, encoding='utf-8', errors='replace').read()
    except Exception:
        return True


def _kw_derive_cursor(station: str, shard_i: int, shard_n: int, total: int) -> int:
    """本分片没有断点时，从历史分片/单片断点换算出续跑位置。

    场景：这个站在本机是用 --shards 5 跑的，交到另一台电脑上却用 --shards 4；
    或者反过来。断点文件名对不上，默认就从第 0 个词重跑 —— 全景网已经跑到
    91%、荆楚网 81%，重跑一次等于前功尽弃。这里把历史断点还原成"哪些词跑过
    了"再按当前分片反推游标，实现无缝接力。
    """
    try:
        import importlib.util
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_ckpt.py')
        spec = importlib.util.spec_from_file_location('_h2ckpt', p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cur, note = mod.derive(station, shard_i, shard_n, total, KW_STATE_DIR)
        if cur:
            print('[断点换算] %s%s 没有本片断点，从历史断点接力：%s'
                  % (station, '' if shard_n <= 1 else ' 分片%d/%d' % (shard_i + 1, shard_n), note))
        return cur
    except Exception as e:
        print('[断点换算] %s 失败（按 0 重跑）：%s: %s' % (station, type(e).__name__, e))
        return 0


def _kw_shard():
    """H2_KEY_SHARD='i/N' -> (i, N)：把一个站的关键词横切成 N 片并行跑。

    单个关键词站要顺序跑 11546 次搜索，慢站要 4-8 小时，是整批最大的瓶颈
    （64 个站被 4 小时超时砍断、只跑完 40-60%，就是这么来的）。
    切成 N 片各起一个进程，同样的机器占用下墙钟时间降到 1/N。
    分片用**交错切片** keys[i::N]，片之间不重叠，入库按 _id 去重仍然安全。
    """
    s = os.environ.get('H2_KEY_SHARD', '')
    if not s or '/' not in s:
        return 0, 1
    try:
        a, b = s.split('/', 1)
        i, n = int(a), int(b)
        if n < 1 or not (0 <= i < n):
            return 0, 1
        return i, n
    except Exception:
        return 0, 1


def _kw_load_cursor(station: str) -> int:
    try:
        return int(json.load(open(os.path.join(KW_STATE_DIR, station + '.json'),
                                  encoding='utf-8')).get('next', 0))
    except Exception:
        return 0


def _kw_save_cursor(station: str, nxt: int) -> None:
    try:
        os.makedirs(KW_STATE_DIR, exist_ok=True)
        tmp = os.path.join(KW_STATE_DIR, station + '.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'next': nxt, 'updated': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}, f)
        os.replace(tmp, os.path.join(KW_STATE_DIR, station + '.json'))
    except Exception:
        pass


class _ResumableKeys(list):
    """会自己记进度的关键词列表。

    单站要顺序跑完 5773 个关键词，慢站要 3-4 小时以上，被 4 小时超时砍掉后
    原来下次又从第 1 个词重来 —— 实测 53 个超时站每轮白烧 212 站·小时，
    而且必然再次被砍，永远跑不完。这里把"跑到第几个词"落盘，重跑时接着跑。
    """

    def __init__(self, items, station: str, start: int = 0) -> None:
        super().__init__(items)
        self._station = station
        self._start = start
        self._last = start
        self._saved = 0.0
        _KW_LIVE.append(self)

    def _note(self, i: int) -> None:
        now = time.time()
        if now - _KW_T0 < _KW_PERSIST_AFTER:
            # 开机物化阶段：整表被读一遍不算进度，连内存游标都不能动，
            # 否则退出时的 flush 会把"读到末尾"当成"跑到末尾"落盘。
            return
        abs_i = self._start + (i if i >= 0 else len(self) + i)
        if abs_i <= self._last:
            return
        self._last = abs_i
        if now - self._saved < _KW_PERSIST_GAP:
            return
        self._saved = now
        _kw_save_cursor(self._station, self._last)

    def __getitem__(self, i):
        if isinstance(i, slice):
            # 有些脚本自己写 START_KEYWORD 断点，会切片成 keys[idx:]。
            # 切片不能退化成普通 list，否则这些站就丢了自动断点能力。
            start, _, _ = i.indices(len(self))
            self._note(start)
            return _ResumableKeys(super().__getitem__(i), self._station, self._start + start)
        self._note(i)
        return super().__getitem__(i)

    def __iter__(self):
        # list 的迭代走 C 层，不会经过 __getitem__，必须显式绕一圈
        for i in range(len(self)):
            yield self[i]


def attach_cursor(items: list, tag: str = 'list') -> list:
    """把脚本自己加工出来的列表重新挂上断点。

    有些脚本没法直接迭代 `load_keys()`，得先把它物化成别的形状 —— 证券之星只取
    6 位股票代码并去重（11546 个检索词 -> 1155 个代码）。物化发生在开机 90 秒内，
    被 `_KW_PERSIST_AFTER` 的"开机不落盘"守卫挡掉，于是这个站**永远没有断点**：
    每轮都从第一批代码重跑，跑了两天也只爬到前 16 个代码，剩下 1100 多个代码
    一次都没碰过（日志末尾停在「代码=000017」就是这么来的）。

    这里让脚本把加工后的列表挂回断点。加工后的列表和关键词表**下标空间不同**，
    所以断点文件用独立的 `站名.tag[.sXofN].json` 命名，既不会污染关键词断点，
    也不会被 `_ckpt` 的跨分片换算误当成关键词断点。

    用法：`return _cfg.attach_cursor(codes, 'codes')`
    """
    station = _kw_station()
    if not station or _kw_no_resume(station):
        return list(items)
    shard_i, shard_n = _kw_shard()
    if shard_n > 1:
        cursor_key = '%s.%s.s%dof%d' % (station, tag, shard_i, shard_n)
    else:
        cursor_key = '%s.%s' % (station, tag)
    start = min(_kw_load_cursor(cursor_key), len(items))
    if start:
        print('[断点续跑] %s %s 列表上次跑到第 %d/%d 个，从第 %d 个接着跑'
              % (station, tag, start, len(items), start + 1))
    return _ResumableKeys(list(items)[start:], cursor_key, start)


def load_keys(skip_digits: bool = False) -> list[str]:
    """载入关键词（代码+名称都作为检索词，与原脚本一致）。

    关键词型脚本自动带断点续跑 + 分片并行；栏目翻页/列表接口型始终拿全量。
    skip_digits=True 时只保留名称类关键词（有些站的搜索接口不认 6 位股票代码，
    原脚本就是这么过滤的）—— 关键是**过滤要在切片之前做**，这样断点下标才准。
    """
    from model.KeywordJsonLoader import loadKeysFromKeywordJson

    keys = [str(k).strip() for k in loadKeysFromKeywordJson() if k and str(k).strip()]
    if skip_digits:
        keys = [k for k in keys if not k.isdigit()]
    if KEY_LIMIT > 0:
        keys = keys[:KEY_LIMIT]
        return keys

    station = _kw_station()
    if not station or _kw_no_resume(station):
        return keys

    shard_i, shard_n = _kw_shard()
    if shard_n > 1 and _kw_browser(station) and not os.environ.get('H2_BROWSER_PORT'):
        # 浏览器站拆片的前提是"每片有自己的调试端口"：`_run.run_station` 拆片时
        # 会逐片设 H2_BROWSER_PORT（端口和 profile 目录都由
        # `install_edge_default()` 按这个环境变量分开）。如果有人手写了
        # H2_KEY_SHARD 却没给端口，多进程会抢同一个端口 —— 这时退回单片，
        # 否则关键词表被切掉 4/5 会静默漏数据。
        # （2026-10-15 起浏览器站默认可拆：云南网/钢联资讯 单进程一轮十几小时，
        #   拆 3 片就是 3 倍。）
        shard_i, shard_n = 0, 1
    _kw_total = len(keys)                    # 切片前的全量长度，换算断点要用
    if shard_n > 1:
        # 交错切片：片之间严格不重叠
        keys = keys[shard_i::shard_n]
        # 每片用自己的断点文件，互不干扰
        cursor_key = '%s.s%dof%d' % (station, shard_i, shard_n)
    else:
        cursor_key = station

    if os.environ.get('H2_KEY_RESET') == '1':
        _kw_save_cursor(cursor_key, 0)
        return _ResumableKeys(keys, cursor_key, 0)

    start = min(_kw_load_cursor(cursor_key), len(keys))
    if not start:
        # 本片断点为 0（或文件不存在）：同一站的历史分片/单片断点还能接力，
        # 别白白从第 0 个词重跑。换算结果立刻落盘，让断点自愈。
        start = min(_kw_derive_cursor(station, shard_i, shard_n, _kw_total), len(keys))
        if start:
            _kw_save_cursor(cursor_key, start)
    if start:
        print('[断点续跑] %s%s 上次跑到第 %d/%d 个关键词，从第 %d 个接着跑'
              % (station, ('' if shard_n <= 1 else ' 分片%d/%d' % (shard_i + 1, shard_n)),
                 start, len(keys), start + 1))
    return _ResumableKeys(keys[start:], cursor_key, start)


def _kw_flush_at_exit() -> None:
    """进程退出前把最终进度落盘（正常跑完 / 被超时杀掉时由 runner 兜不住，
    但正常退出和被 KeyboardInterrupt 打断都能保住进度）。"""
    if time.time() - _KW_T0 < _KW_PERSIST_AFTER:
        return                          # 短命进程：开机物化，没有真进度
    for k in _KW_LIVE:
        try:
            if k._last > k._start:
                _kw_save_cursor(k._station, k._last)
        except Exception:
            pass


import atexit  # noqa: E402

atexit.register(_kw_flush_at_exit)


def to_publishtime(raw: str | None) -> int:
    """把各种日期字符串转成秒级时间戳。失败返回 0。

    特别注意"本身就是时间戳"的字段：浙江在线的 issuetime 是
    '1787554485'（= 2026-08-25，在窗口内），蓝鲸的 create_time 是
    秒级整数。这种不能交给 date_change 去做字符串日期解析，否则要么
    解析失败返回 0（窗口判断全失效），要么算成离谱的年份。
    """
    if raw is None:
        return 0
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        ts = int(raw)
        if ts > 10 ** 12:          # 毫秒
            ts //= 1000
        return ts if ts > 0 else 0
    s = str(raw).strip()
    if not s:
        return 0
    if s.isdigit():
        ts = int(s)
        if len(s) >= 13 or ts > 10 ** 12:   # 毫秒
            ts //= 1000
        if 10 ** 9 <= ts <= 4 * 10 ** 9:    # 2001 ~ 2096 年之间，认它是秒级时间戳
            return ts
    try:
        ts = date_change(s)
        return int(ts) if ts else 0
    except Exception:
        return 0


def in_window(ts: int) -> bool:
    """时间戳是否落在目标窗口内。"""
    return ts > 0 and WINDOW_START <= ts <= WINDOW_END


def older_than_window(ts: int) -> bool:
    """早于窗口起点（结果按时间倒序时可用于停止翻页）。"""
    return ts > 0 and ts < WINDOW_START


def validate_doc(doc: dict) -> str | None:
    """校验普通 dict 形式的文档（B 级脚本直接用 dict 入库）。"""
    for f in ("_id", "title", "content", "from", "publishtime",
              "raw_publishTime", "fetchTime", "baseUrl", "sourse"):
        if f not in doc:
            return "缺字段 %s" % f

    if not doc["_id"]:
        return "id 为空"
    title = str(doc["title"] or "").strip()
    content = str(doc["content"] or "").strip()
    if not title:
        return "标题为空"
    if title == content:
        return "标题和正文相同"
    if len(content) < MIN_CONTENT_LEN:
        return "正文太短(%d字)" % len(content)
    if not isinstance(doc["publishtime"], int):
        return "publishtime 不是 int"
    if not in_window(doc["publishtime"]):
        return "publishtime 不在窗口内(%s)" % doc["publishtime"]
    if not str(doc["baseUrl"] or "").startswith("http"):
        return "baseUrl 不合法"
    if "404" in title[:20] or "404" in content[:50]:
        return "疑似 404 页面"
    return None


def validate(entity: Any) -> str | None:
    """校验 9 字段。返回错误原因，通过则返回 None。"""
    doc = entity.to_dict() if hasattr(entity, "to_dict") else dict(entity)
    doc["_id"] = doc.pop("id", None)
    return validate_doc(doc)


def _url_date(url: str | None) -> tuple[int, int, int] | None:
    """从 URL 中提取 (年,月,日)。取不到返回 None。"""
    if not url:
        return None
    m = re.search(r'/(\d{4})[/-](\d{1,2})[-/](\d{1,2})(?:[/-]|$)', str(url))
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    m = re.search(r'/(\d{4})[/-](\d{1,2})(?:[/-]|$)', str(url))
    if m:
        return int(m.group(1)), int(m.group(2)), 1
    return None


def install_window_stop() -> None:
    """把共享翻页工具里的「按年份停止」换成「按目标窗口停止」。

    老逻辑 STOP_YEAR = 2025 只对跨年窗口有效；本期窗口 2026-07-01 ~ 2026-10-10
    整段都在 2026 年内，按年判断永远不会触发，会把窗口之前的数据一起翻出来。
    这里直接改写 PagingStopUtil，110 个站点脚本无需逐个改动内部逻辑。
    """
    from utils import PagingStopUtil as _P

    window_start_ymd = (2026, 7, 1)

    def stop_by_publish_time(raw: str | None) -> bool:
        ts = to_publishtime(raw)
        return ts > 0 and ts < WINDOW_START

    def stop_by_url_year(url: str | None) -> bool:
        # 故意不再按 URL 日期停止翻页。
        # 原因：URL 里的数字段未必是日期（栏目号/ID 都可能形如 2026/0001/01），
        # 一旦误判就会把整站的抓取提前掐断，而且是静默 0 条 —— 这种失败最难发现。
        # 真正的边界判断交给 stop_by_publish_time（详情页真实发布时间），
        # 代价只是每个关键词/栏目多抓 1 篇边界文章。
        return False

    _P.should_stop_by_publish_time = stop_by_publish_time
    _P.should_stop_by_url_year = stop_by_url_year
    _P.PagingStopUtil.shouldStopByPublishTime = staticmethod(stop_by_publish_time)
    _P.PagingStopUtil.shouldStopByUrlYear = staticmethod(stop_by_url_year)


def install_keepalive() -> None:
    """让所有脚本的 requests.get/post 复用 TCP 连接（keep-alive）。

    实测（同一台机、同一个站、连抓 6 次）：
        每次新建连接  0.47 秒/次
        复用连接      0.17 秒/次     -> 快 2.8 倍
    而解析一个 50KB 页面只要 18 毫秒 —— 也就是说这一层里 **96% 的时间是
    握手 + 网络往返**，不是算力。抓取总耗时 = 关键词数 × 每关键词请求数 ×
    单次耗时，所以把单次从 0.47 降到 0.17，等于整体提速近 3 倍。

    实现方式：在 import _cfg 时把 requests.get/post 换成共享 Session 的版本。
    这样**不用改任何站点脚本**（那些脚本几万个 requests.get 调用都自动受益）。
    传 verify / headers / cookies / params 等参数照旧生效。
    """
    import requests
    try:
        import urllib3
        urllib3.disable_warnings()     # 各站大量自签证书，警告会把日志淹掉
    except Exception:
        pass
    if getattr(requests, '_h2_keepalive', False):
        return
    sess = requests.Session()
    try:
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=32, pool_maxsize=32, max_retries=0)
        sess.mount('https://', adapter)
        sess.mount('http://', adapter)
    except Exception:
        pass
    _orig_get = requests.get
    _orig_post = requests.post

    def _get(url, **kw):
        kw.setdefault('verify', False)
        return sess.get(url, **kw)

    def _post(url, **kw):
        kw.setdefault('verify', False)
        return sess.post(url, **kw)

    requests.get = _get
    requests.post = _post
    requests.Session  # noqa: B018  保留原类
    requests._h2_keepalive = True
    requests._h2_session = sess


def install_edge_default(edge_path: str = r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe') -> None:
    """让浏览器型脚本默认走 Edge，并允许用 H2_BROWSER_PORT 覆盖调试端口。

    覆盖端口是必需的：这些脚本各自把 set_local_port() 写死成同一个值，
    并发跑多站时会抢同一个端口。批量跑时由 _run.py 给每个浏览器 worker
    分配不同端口。
    """
    if not os.path.exists(edge_path):
        return
    try:
        from DrissionPage import ChromiumOptions as _CO
    except Exception:
        return

    if getattr(_CO, '_h2_patched', False):
        return

    _orig_init = _CO.__init__
    _orig_set_port = _CO.set_local_port
    _orig_set_udp = _CO.set_user_data_path
    _orig_headless = _CO.headless

    # 默认无头：浏览器型脚本原先会弹出 Edge 窗口，会在用户屏幕上闪，
    # 抓取必须在后台静默跑。需要人工登录的站（脚本里有 input()）可设 H2_HEADLESS=0。
    force_headless = os.environ.get('H2_HEADLESS', '1') not in ('0', 'false', 'False')

    def _init(self, *args, **kwargs):
        _orig_init(self, *args, **kwargs)
        try:
            self.set_browser_path(edge_path)
        except Exception:
            pass
        # 有些脚本压根不调 set_local_port，会全体落到默认端口 9222 上互相抢。
        # 这里在构造时就按环境变量定好端口。
        env_port = os.environ.get('H2_BROWSER_PORT')
        if env_port:
            try:
                _orig_set_port(self, int(env_port))
            except Exception:
                pass
        if force_headless:
            try:
                _orig_headless(self, True)
            except Exception:
                pass
            # 无头模式默认 UA 里带 HeadlessChrome，部分站会据此拦截/改变行为。
            # 伪装成正常 Edge，并关掉自动化特征。
            try:
                self.set_argument('--disable-blink-features', 'AutomationControlled')
                self.set_argument(
                    '--user-agent',
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                    '(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0')
                # 省资源：抓取不需要 GPU 渲染/音频/扩展，关掉可少起好几个子进程
                self.set_argument('--disable-gpu')
                self.set_argument('--disable-software-rasterizer')
                self.set_argument('--disable-dev-shm-usage')
                self.set_argument('--disable-extensions')
                self.set_argument('--mute-audio')
                self.set_argument('--disable-background-networking')
                self.set_argument('--window-size', '1280,900')
            except Exception:
                pass

    def _headless(self, on_off=True):
        # 脚本自己再调 headless(False) 也拦掉，除非显式要求可见
        return _orig_headless(self, True if force_headless else on_off)

    def _set_local_port(self, port=None, *args, **kwargs):
        env_port = os.environ.get('H2_BROWSER_PORT')
        if env_port:
            try:
                port = int(env_port)
            except Exception:
                pass
        return _orig_set_port(self, port, *args, **kwargs)

    def _set_user_data_path(self, path=None, *args, **kwargs):
        # 兜底：脚本里写死的 C:\Users\<别人>\... 在本机建不出来（WinError 5），
        # 一律改到本机临时目录下的同名 profile。
        try:
            cur = (os.environ.get('USERNAME') or os.environ.get('USER') or '').lower()
            m = re.match(r'^[A-Za-z]:[\\/]+Users[\\/]+([^\\/]+)', str(path or ''), re.I)
            if path and m and m.group(1).lower() != cur:
                base = os.path.basename(str(path).rstrip('\\/')) or 'profile'
                path = os.path.join(tempfile.gettempdir(), 'h2run_profiles', base)
        except Exception:
            pass
        if path:
            # 两个站共用同一个 profile 时，第二个 Edge 不会真的起新进程，而是把命令转给
            # 已有的实例，DrissionPage 就连到了另一个站上的浏览器（我的钢铁网/钢联资讯
            # 就共用 mysteel_9228）。按调试端口给 profile 加后缀，做到一站一份。
            env_port = os.environ.get('H2_BROWSER_PORT')
            if env_port and not str(path).endswith('_p' + env_port):
                path = '%s_p%s' % (str(path).rstrip('\\/'), env_port)
            try:
                os.makedirs(path, exist_ok=True)
            except Exception:
                pass
        return _orig_set_udp(self, path, *args, **kwargs)

    _CO.__init__ = _init
    _CO.set_local_port = _set_local_port
    _CO.set_user_data_path = _set_user_data_path
    _CO.headless = _headless
    _CO._h2_patched = True


# 导入 _cfg 就自动生效，避免哪个脚本的引导头漏写 install_edge_default()
# 导致浏览器窗口弹到用户屏幕上（B 级脚本就曾因为漏写而弹窗）。
install_edge_default()

# 同理：HTTP 长连接也在 import 时自动装上，任何站点脚本都不用改。
# 实测每次请求 0.47s -> 0.17s（快 2.8 倍），见 install_keepalive 的说明。
install_keepalive()


class SafeStorage:
    """在 MongoStorage 外面包一层校验，不合格的记录不入库。"""

    def __init__(self, collection: Any) -> None:
        self.collection = collection
        self.saved = 0
        self.skipped_exists = 0
        self.rejected: list[str] = []

    def exist(self, id_: str) -> bool:
        return self.collection.find_one({"_id": id_}, {"_id": 1}) is not None

    def save(self, entity: Any) -> bool:
        doc = entity.to_dict() if hasattr(entity, "to_dict") else dict(entity)
        doc["_id"] = doc.pop("id", None)
        if doc.get("_id") and self.exist(doc["_id"]):
            self.skipped_exists += 1
            return False
        err = validate(entity)
        if err:
            self.rejected.append("%s | %s" % (doc.get("_id"), err))
            return False
        doc["fetchTime"] = Int64(doc["fetchTime"] or int(time.time() * 1000))
        # 统一 raw_publishTime 格式（各站原始写法不一致，必须归一化）
        doc["raw_publishTime"] = normalize_raw_time(doc.get("publishtime") or 0)
        doc["_h2_ok"] = True  # 已校验，让 B 级兼容层不要重复校验
        self.collection.insert_one(doc)
        self.saved += 1
        if MAX_INSERT and self.saved >= MAX_INSERT:
            # SystemExit 不是 Exception 子类，能穿透脚本里的 except Exception，直接收工
            print('[H2] 达到 MAX_INSERT=%d，停止本脚本' % MAX_INSERT)
            raise SystemExit(0)
        return True


_B_PATCHED = False
_B_REJECTS: list[tuple[str, str]] = []


def install_b_compat() -> None:
    """B 级脚本兼容层（根目录 bg_*.py 那一批）。

    这批脚本原本 `from safe_insert_local import safe_insert` —— 那个模块零校验，
    字段名还是上一期的 publishTime/source，个别脚本甚至直接 insert_one 或 delete_many。
    这里不动它们的业务代码，只做三件事把它们纳入同一套闸门：

      1. 用假的 safe_insert_local 顶掉原模块（导入语句一行不改）
      2. patch Collection.insert_one：统一字段名 + 校验 + 日期归一化 + 窗口过滤
      3. patch Collection.delete_many：直接失效（铁律 4 只增不删）
    """
    global _B_PATCHED
    if _B_PATCHED:
        return
    _B_PATCHED = True

    import atexit
    import types

    from pymongo.collection import Collection

    _orig_insert_one = Collection.insert_one

    def _validating_insert_one(self, doc, *args, **kwargs):
        d = dict(doc)
        if d.pop('_h2_ok', None):          # 已由 SafeStorage 校验过
            return _orig_insert_one(self, d, *args, **kwargs)
        # --- 字段名迁移：上一期 -> 本期 ---
        if 'publishTime' in d:
            d['publishtime'] = d.pop('publishTime')
        if 'source' in d:
            d['sourse'] = d.pop('source')
        err = validate_doc(d)
        if err:
            _B_REJECTS.append((str(d.get('_id')), err))
            return types.SimpleNamespace(inserted_id=None, acknowledged=False)
        d['raw_publishTime'] = normalize_raw_time(d.get('publishtime') or 0)
        if not isinstance(d.get('fetchTime'), Int64):
            d['fetchTime'] = Int64(d.get('fetchTime') or int(time.time() * 1000))
        return _orig_insert_one(self, d, *args, **kwargs)

    def _no_delete(self, *args, **kwargs):
        print('[H2] 已拦截 delete_many/delete_one（铁律4：只增不删）')
        return types.SimpleNamespace(deleted_count=0, acknowledged=True)

    Collection.insert_one = _validating_insert_one
    Collection.delete_many = _no_delete
    Collection.delete_one = _no_delete

    shim = types.ModuleType('safe_insert_local')

    def safe_insert(coll, doc):
        try:
            if coll.find_one({'_id': doc.get('_id')}, {'_id': 1}):
                return False
        except Exception:
            pass
        coll.insert_one(doc)          # 走上面的校验闸门
        return True

    shim.safe_insert = safe_insert
    shim.flush_all = lambda *a, **k: None
    shim.import_offline = lambda *a, **k: None
    shim.MONGO_OK = True
    shim.MAX_PER_SCRIPT = 0
    sys.modules['safe_insert_local'] = shim

    def _report():
        if _B_REJECTS:
            print('\n[H2] 校验拒绝 %d 条（未入库），样例：' % len(_B_REJECTS))
            from collections import Counter
            for reason, n in Counter(r for _, r in _B_REJECTS).most_common(8):
                print('     %-28s %d 条' % (reason, n))

    atexit.register(_report)


# ==================== 数据库只读护栏（铁律 6：只增不删不改） ====================
# 2026-10-14 用户明确要求："未经允许，绝不对数据库数据做查询/新增以外的操作"。
# 这个护栏在 **_cfg 被 import 时立刻生效**（所有站脚本都会 import 它），
# 把 pymongo 的破坏性方法全部替换成"抛异常"，而不是静默忽略——
# 真有人（人或 AI）写错一句 update/delete，会立刻炸出来，而不是悄悄改掉几十万条数据。
#
# 放行（只有在明确获得许可、并知道自己在干什么时才用）：
#     set H2_ALLOW_DB_WRITE=1
_DB_GUARD_PATCHED = False
_DB_WRITE_EXTS = ('delete_one', 'delete_many', 'update_one', 'update_many', 'replace_one',
                  'find_one_and_delete', 'find_one_and_update', 'find_one_and_replace',
                  'bulk_write', 'drop', 'rename', 'drop_index', 'drop_indexes')


def install_db_write_guard() -> None:
    """把 pymongo 的写/删/改/删表方法全部封死，只留 insert 与查询。默认开启。"""
    global _DB_GUARD_PATCHED
    if _DB_GUARD_PATCHED:
        return
    _DB_GUARD_PATCHED = True
    if os.environ.get('H2_ALLOW_DB_WRITE') == '1':
        print('[H2][危险] H2_ALLOW_DB_WRITE=1：数据库写护栏已关闭，请自行确认操作')
        return

    from pymongo.collection import Collection
    from pymongo.database import Database
    from pymongo.mongo_client import MongoClient

    def _blocked(name):
        def _f(self, *args, **kwargs):
            raise RuntimeError(
                '[H2][已拦截] 数据库操作 %s 被护栏拦下：本项目铁律 6「只增不删不改」，'
                '只允许 find/aggregate/count 与 insert。确需执行请显式设 H2_ALLOW_DB_WRITE=1 并先取得许可。'
                % name)
        _f.__name__ = 'blocked_%s' % name
        return _f

    for _n in _DB_WRITE_EXTS:
        if hasattr(Collection, _n):
            setattr(Collection, _n, _blocked('Collection.%s' % _n))
    for _n in ('drop_collection', 'drop_database'):
        if hasattr(Database, _n):
            setattr(Database, _n, _blocked('Database.%s' % _n))
    if hasattr(MongoClient, 'drop_database'):
        MongoClient.drop_database = _blocked('MongoClient.drop_database')
    print('[H2] 数据库护栏已生效：delete/update/replace/bulk_write/drop 一律拦截（只增不删不改）')


install_db_write_guard()
