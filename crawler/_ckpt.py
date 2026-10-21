# -*- coding: utf-8 -*-
"""跨分片 / 跨机器的关键词断点换算。

问题
----
同一个站在不同批次里用 `--shards 1/3/4/5` 跑过，`_state_kw/` 里因此同时躺着
`站名.json`、`站名.s0of3.json`、`站名.s0of4.json`、`站名.s0of5.json`。
换一台电脑（或改一次 `--shards`）时，脚本按**目标分片**去找自己的断点文件却
找不到，于是从第 0 个词重跑。数据不会重复（入库按 `_id` 去重），但白烧几个
小时 —— 全景网已经跑到 91%、荆楚网 81%，重跑一次等于前功尽弃。

做法
----
把"哪些关键词跑过了"从所有历史断点里还原成一个全量下标的集合（各来源取并
集），再按目标分片反推每片的游标。于是任意分片、任意机器之间都能无缝接力。

游标语义（与 `_cfg._ResumableKeys` 严格对齐）
--------------------------------------------
* 分片 `(i, N)` 的断点 `next=c`：`keys[i::N]` 的前 c 个已经处理过。
* 单片断点 `next=c`：全量表的前 c 个已经处理过。

安全性
------
* 只做**并集**，不会把没跑过的词标成已跑（除非历史断点本身就是错的）。
* 关键词表若在上次跑之后被改过（本期补过 356 个词），下标会整体错位，因此
  **比关键词表还旧的断点一律不采信**。
* `H2_KEY_RESET=1` 仍然可以直接清空游标，从 0 重跑。
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Dict, List, Tuple

Slicing = Tuple[int, int]          # (分片序号, 分片总数)；n=1 表示单片


def _read_cursor(path: str) -> Tuple[int, str]:
    """读一个断点文件 -> (next, updated)。读不到返回 (-1, '')。"""
    try:
        d = json.load(open(path, encoding='utf-8'))
        return int(d.get('next', 0)), str(d.get('updated', ''))
    except Exception:
        return -1, ''


def _kw_mtime(state_dir: str) -> float:
    """关键词表最后修改时间。用来剔除"比关键词表还旧"的断点（下标会错位）。"""
    for rel in (os.path.join('..', 'key', '关键词.json'),
                os.path.join('..', '..', 'key', '关键词.json')):
        p = os.path.normpath(os.path.join(state_dir, rel))
        if os.path.exists(p):
            return os.path.getmtime(p)
    return 0.0


def _parse_updated(s: str) -> float:
    try:
        return time.mktime(time.strptime(s, '%Y-%m-%d %H:%M:%S'))
    except Exception:
        return 0.0


def sources(station: str, state_dir: str) -> Dict[Slicing, Tuple[int, str]]:
    """扫出该站所有历史断点 -> {(i, n): (cursor, 文件名)}。

    `(0, 1)` 表示单片模式（`站名.json`）。比关键词表旧的断点会被丢弃。
    """
    out: Dict[Slicing, Tuple[int, str]] = {}
    if not os.path.isdir(state_dir):
        return out
    kw_t = _kw_mtime(state_dir)
    pat = re.compile(r'^%s\.s(\d+)of(\d+)\.json$' % re.escape(station))
    for fn in os.listdir(state_dir):
        if fn.endswith('.tmp'):
            continue
        m = pat.match(fn)
        key: Slicing | None = (int(m.group(1)), int(m.group(2))) if m else None
        if m is None and fn == station + '.json':
            key = (0, 1)
        if key is None:
            continue
        cur, upd = _read_cursor(os.path.join(state_dir, fn))
        if cur < 0:
            continue
        if kw_t and _parse_updated(upd) and _parse_updated(upd) < kw_t - 60:
            print('[断点换算] 跳过过期断点 %s（早于关键词表修改时间）' % fn)
            continue
        # 同一分片出现多个文件时取进度更靠前的
        if key not in out or cur > out[key][0]:
            out[key] = (cur, fn)
    return out


def done_mask(src: Dict[Slicing, Tuple[int, str]], total: int) -> bytearray:
    """把各历史断点还原成"全量下标里哪些跑过了"的位图。"""
    done = bytearray(total)
    for (i, n), (cur, _fn) in src.items():
        if total <= 0:
            break
        if n <= 1:
            for t in range(min(cur, total)):
                done[t] = 1
            continue
        for t in range(cur):
            idx = i + t * n
            if idx >= total:
                break
            done[idx] = 1
    return done


def derive(station: str, shard_i: int, shard_n: int, total: int,
           state_dir: str) -> Tuple[int, str]:
    """目标分片 `(shard_i, shard_n)` 该从第几个词接着跑。

    返回 `(游标, 说明)`；没有可用历史断点时返回 `(0, '')`。
    """
    if total <= 0:
        return 0, ''
    src = sources(station, state_dir)
    if not src:
        return 0, ''
    exact = src.get((shard_i, shard_n))
    if exact and exact[0]:
        return 0, ''                      # 本片断点已经够用，不需要换算
    done = done_mask(src, total)
    covered = sum(done)
    if not covered:
        return 0, ''
    if shard_n <= 1:
        cur = 0
        while cur < total and done[cur]:
            cur += 1
    else:
        cur = 0
        for idx in range(shard_i, total, shard_n):
            if not done[idx]:
                break
            cur += 1
    origin = ' '.join('%s=%d' % ('单片' if k == (0, 1) else '%d/%d' % k, v[0])
                      for k, v in sorted(src.items()))
    note = ('历史断点 %s，全量已覆盖 %d/%d (%.0f%%)'
            % (origin, covered, total, 100.0 * covered / total))
    return cur, note


def report(station: str, total: int, state_dir: str,
           targets: List[int] | None = None,
           shardable: bool | None = None) -> str:
    """给人看的核对文本：能不能拆片、接着跑会从第几个词开始。

    只报**这个站实际会用到**的配置：浏览器型和"关键词当过滤器"型只跑单片，
    给它们列 --shards 5 的换算位置是误导（那些位置永远不会被用到）。
    """
    if shardable is None:
        shardable, why = classify(station, state_dir)
    else:
        why = classify(station, state_dir)[1]
    targets = targets or ([1, 5] if shardable else [1])

    lines = ['%s（关键词表 %d 个）%s'
             % (station, total, '' if shardable else '  —— 仅单片：' + why)]
    src = sources(station, state_dir)
    if not shardable and why.startswith('非关键词队列'):
        lines.append('    这个站不查关键词表：进度靠入库 _id 去重，'
                     '重跑只会重扫列表页，不会重复插数据')
    elif not src:
        lines.append('    关键词断点: 无 -> 从第 0 个词开始（数据不会重复，只是重搜一遍）')
    else:
        lines.append('    关键词断点: ' + '  '.join(
            '%s=%d' % ('单片' if k == (0, 1) else '%dof%d' % k, v[0])
            for k, v in sorted(src.items())))
        done = done_mask(src, total)
        lines.append('    已覆盖 %d/%d (%.0f%%)'
                     % (sum(done), total, 100.0 * sum(done) / total))
        for n in targets:
            each = [len(range(i, total, n)) for i in range(n)]
            got = [_leading(done, i, n, total) for i in range(n)]
            if n == 1:
                lines.append('    --shards 1  -> 从第 %d 个词开始' % got[0])
            else:
                lines.append('    --shards %d  -> 各片从 %s 开始（片长 %s）'
                             % (n, '/'.join(map(str, got)), '/'.join(map(str, each))))

    # 「物化列表」断点：站名.<tag>[.sXofN].json（如证券之星的股票代码列表）
    tagged: Dict[Tuple[str, int, int], int] = {}
    if os.path.isdir(state_dir):
        exact = re.compile(r'^%s\.([^.]+)\.json$' % re.escape(station))
        shardd = re.compile(r'^%s\.([^.]+)\.s(\d+)of(\d+)\.json$' % re.escape(station))
        for fn in os.listdir(state_dir):
            m = exact.match(fn)
            key = (m.group(1), 0, 1) if m else None
            if key is None:
                m = shardd.match(fn)
                key = (m.group(1), int(m.group(2)), int(m.group(3))) if m else None
            if key is None or re.match(r'^s\d+of\d+$', key[0]):
                continue
            c, _u = _read_cursor(os.path.join(state_dir, fn))
            if c >= 0:
                tagged[key] = max(tagged.get(key, 0), c)
    for (tag, i, n), c in sorted(tagged.items()):
        lines.append('    物化列表断点[%s]%s = %d%s'
                     % (tag, '' if n == 1 else ' %d/%d' % (i + 1, n), c,
                        '（>0 说明这个列表也接着跑）' if c else ''))
    try:
        if 'attach_cursor' in open(os.path.join(os.path.dirname(state_dir), station + '.py'),
                                   encoding='utf-8', errors='replace').read():
            lines.append('    注意：这个站的真实进度记在「物化列表断点」上，'
                         '上面的关键词断点对它没有意义')
    except Exception:
        pass
    return '\n'.join(lines)


def classify(station: str, state_dir: str) -> Tuple[bool, str]:
    """这个站能不能拆关键词分片 -> (能不能, 人话原因)。

    规则必须和 `_run.is_shardable` / `_cfg._kw_no_resume` 一致：
    把关键词当**过滤器**的（栏目翻页/列表接口/浏览器首页/自建 matcher）不能拆，
    拆了会静默漏数据；浏览器型不能拆，多进程会抢调试端口。
    """
    p = os.path.join(os.path.dirname(state_dir), station + '.py')
    try:
        txt = open(p, encoding='utf-8', errors='replace').read()
    except Exception:
        return False, '看不到脚本文件'
    if 'DrissionPage' in txt:
        return False, '浏览器型（多进程会抢调试端口）'
    hit = [m for m in ('run_colpage', 'run_listapi', 'run_browser_home',
                       'build_matcher', 'keyset') if m in txt]
    if hit:
        return False, '关键词当过滤器用（%s），必须拿全量' % hit[0]
    if any(m in txt for m in ('load_keys', '_generic.run(')):
        return True, '关键词工作队列'
    return False, '非关键词队列（页面/接口翻页）'


def _leading(done: bytearray, i: int, n: int, total: int) -> int:
    """全量位图里，分片 keys[i::n] 从头连续跑过的个数。"""
    c = 0
    for idx in range(i, total, n):
        if not done[idx]:
            break
        c += 1
    return c
