# -*- coding: utf-8 -*-
"""通用抓取器：给"服务端搜索可用"的站共用。

各站只差三件事：搜索接口怎么调、结果怎么解析、翻页怎么翻。
把这些抽成配置，站点脚本就只剩十几行配置。

两条硬底线（与 A/B 级完全一致）：
  1. 入库一律走 _cfg.SafeStorage（时间窗 / title≠content / 正文长度 / 字段类型 全查）
  2. 时间窗用 _cfg.install_window_stop()，按真实发布时间判断边界
"""
from __future__ import annotations

import json
import re
import time
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlparse

import requests
import urllib3
from bs4 import BeautifulSoup

import _cfg

urllib3.disable_warnings()   # 否则日志被 InsecureRequestWarning 淹没，没法排查

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0')
BASE_H = {'User-Agent': UA, 'Accept-Language': 'zh-CN,zh;q=0.9'}

# 详情页正文的候选选择器（按优先级）
CONTENT_SELECTORS = [
    '.article-content', '.articleContent', '.article_content', '.content-content',
    '#article-content', '.TRS_Editor', '.content', '#content', '.detail-content',
    '.news-content', '.article', '.main-content', 'article',
]
# 明显不是正文的容器
CONTENT_BLACKLIST = re.compile(r'nav|menu|footer|header|comment|share|related|breadcrumb|ad[-_]', re.I)
DATE_PAT = [
    re.compile(r'(\d{4})-(\d{1,2})-(\d{1,2})\s+\d{1,2}:\d{2}(?::\d{2})?'),
    re.compile(r'(\d{4})年(\d{1,2})月(\d{1,2})日\s*\d{1,2}[:：]\d{2}'),
    re.compile(r'(\d{4})[/.](\d{1,2})[/.](\d{1,2})\s+\d{1,2}:\d{2}'),
    re.compile(r'(\d{4})年(\d{1,2})月(\d{1,2})日'),
]
URL_DATE = [
    re.compile(r'/(\d{4})[/-](\d{1,2})[/-](\d{1,2})/'),
    re.compile(r'/(\d{4})[-]?(\d{2})[-]?(\d{2})/'),
    # 月日连写的，如 /2026/0925/220637.html（大众财经网就是这种）
    re.compile(r'/(\d{4})/(\d{2})(\d{2})/'),
    # 只有年月的，如 /news/czyh/2021/10/94125265.html（中国金融界网）。
    # 认出来按当月 1 号算，好让老栏目在"页级时间判断"处就被停掉，
    # 而不是白抓一堆 2021 年的详情页再被 publishtime 校验拒掉。
    re.compile(r'/(\d{4})/(\d{1,2})/(?=\d{5,}\.s?html?)'),
    # 年月日 + 长 id 连写在一个文件名里，如东方财富网
    # /a/202610123892621594.html（= 2026-10-12 + 3892621594）
    re.compile(r'/(\d{4})(\d{2})(\d{2})\d{4,}'),
    # 文件名带 t+年月日 前缀，如中安在线 /szxw/202610/t20261015_123456.html。
    # 少了这条，页级"翻过窗口起点就停"永不触发，窗外稿子每篇都要白抓一次详情页。
    re.compile(r'[/_]t(\d{4})(\d{2})(\d{2})[_./]'),
]


def url_date(url: str):
    for p in URL_DATE:
        m = p.search(url or '')
        if m:
            try:
                y, mo = int(m.group(1)), int(m.group(2))
                d = int(m.group(3)) if m.re.groups >= 3 else 1
                if 2000 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
                    return (y, mo, d)
            except Exception:
                pass
    return None


def http(url, *, method='get', data=None, json_body=None, referer=None, timeout=20):
    H = dict(BASE_H)
    if referer:
        H['Referer'] = referer
    if method == 'post' and json_body is not None:
        H['Content-Type'] = 'application/json'
        H['X-Requested-With'] = 'XMLHttpRequest'
        r = requests.post(url, json=json_body, headers=H, timeout=timeout, verify=False)
    elif method == 'post':
        H['Content-Type'] = 'application/x-www-form-urlencoded'
        r = requests.post(url, data=data, headers=H, timeout=timeout, verify=False)
    else:
        r = requests.get(url, headers=H, timeout=timeout, verify=False)
    r.encoding = r.apparent_encoding or 'utf-8'
    return r


# ---------------- 搜索适配器 ----------------

def search_mfullsearch(cfg, kw, page):
    """m_fullsearch 平台（Struts）。返回 [(title, href)]。"""
    base = cfg['base']           # 形如 https://fullsearch.xiancity.cn/m_fullsearch/
    url = base + 'searchurl/mfullsearch!descResult.do'
    r = http(url, method='post', referer=base + 'full_search.jsp',
             data={'keywords': kw, 'channel_id': '0', 'isLockChannel': '0',
                   'pageNoCurrent': str(page), 'orderFlg': ''})
    soup = BeautifulSoup(r.text, 'html.parser')
    out = []
    for td in soup.select('td.searchTitle'):
        a = td.find('a', href=True)
        if not a:
            continue
        href = a['href'].strip()
        title = re.sub(r'\s+', ' ', a.get_text(' ', strip=True)).strip()
        if href.startswith('http') and len(title) >= 4:
            out.append((title, href))
    return out


# 明显不是文章标题的东西（首页/栏目页/功能页）——防止把导航链接当搜索结果抓进来
BAD_TITLE = re.compile(r'首页|广告合作|手机版|网站地图|关于我们|联系方式|版权声明|'
                       r'意见反馈|招聘|订阅|登录|注册|客户服务|广告服务|免责声明|'
                       # 2026-10-15：页脚的许可证/备案链接会被当文章（山西新闻网实测
                       # "广播电视节目制作经营许可证"）。新闻标题里不会出现这些词。
                       r'经营许可证|营业执照|备案号|信息网络传播视听节目|'
                       r'违法和不良信息|举报电话|举报邮箱')
# 纯粹的导航/功能字眼，**整条标题就等于它**时才算导航（不能用"包含"判，
# 否则"登录新老媒体平台…"这种正常标题会被误杀）。
# 2026-10-15：光明网往库里塞了 3 条标题为"全部导航"的脏数据（浏览器模式把导航链接
# 当文章抓了），就是这条漏网。
NAV_TITLES = {'全部导航', '导航', '网站导航', '首页', '更多', '查看更多', '查看全文',
              '阅读全文', '上一页', '下一页', '返回首页', '返回列表', '回到顶部',
              '客户端下载', '下载客户端', '订阅', '关注', '分享', '评论', '打印',
              '关闭', 'English', '无障碍浏览', '长者模式'}
# 文章 URL 的路径不能是站点根或空
_ROOT_PATHS = ('', '/')


def looks_like_article(title: str, content: str) -> bool:
    """入库前的语义兜底：标题像导航、正文太短、路径是首页 -> 不是文章。"""
    if not title or BAD_TITLE.search(title):
        return False
    if title.strip() in NAV_TITLES:
        return False
    if len(content.strip()) < _cfg.MIN_CONTENT_LEN:
        return False
    # 正文里如果堆满了导航词，多半抓的是频道页
    nav_hits = len(BAD_TITLE.findall(content[:200]))
    return nav_hits < 3


def search_httpget(cfg, kw, page):
    """通用 GET 搜索：把关键词塞进 URL 模板，翻页参数可选。"""
    url = cfg['url']
    u = url.replace('{kw}', quote(kw))
    if '{page}' in u:
        u = u.replace('{page}', str(page))
    elif page > 1:
        pn = cfg.get('page_param')
        if not pn:
            return []          # 该站不支持翻页
        u += ('&' if '?' in u else '?') + '%s=%d' % (pn, page)
    r = http(u, referer=cfg.get('referer'))
    soup = BeautifulSoup(r.text, 'html.parser')
    sel = cfg.get('item_selector')
    kw_norm = re.sub(r'\s+', '', kw)
    out = []
    nodes = soup.select(sel) if sel else soup.find_all('a', href=True)
    for a in nodes:
        if a.name != 'a':
            a = a.find('a', href=True)
        if not a:
            continue
        href = a.get('href', '').strip()
        title = re.sub(r'\s+', ' ', a.get_text(' ', strip=True)).strip()
        if not href or len(title) < 8:
            continue
        if href.startswith('/'):
            href = cfg['domain'].rstrip('/') + href
        if not href.startswith('http'):
            continue
        # 排除"导航/功能类"链接：标题像导航、或 URL 就是站点根
        if BAD_TITLE.search(title):
            continue
        from urllib.parse import urlparse as _up
        _path = _up(href).path
        if _path in _ROOT_PATHS or _path.count('/') < 2:
            continue
        # 可选：要求链接文字含关键词（默认关，靠上面的导航过滤 + 入库语义兜底）
        if cfg.get('require_kw_in_title') and kw_norm not in re.sub(r'\s+', '', title):
            continue
        if any(s in href for s in ('javascript:', 'mailto:')):
            continue
        if re.search(r'\.(s?html?|jsp|aspx)(\?|$)', href) or re.search(r'/\d{5,}', href):
            out.append((title, href))
    # 去重保序
    seen, uniq = set(), []
    for t, h in out:
        if h not in seen:
            seen.add(h)
            uniq.append((t, h))
    return uniq


ADAPTERS = {'mfullsearch': search_mfullsearch, 'httpget': search_httpget}


def search_nbd(cfg, kw, page):
    """每日经济新闻/每经网 的站内搜索接口。

    POST /news-search/queryByMatch {"keyword":..,"page":1,"size":N}
    返回 searchResults[]，含 title/url/source/publishTime，且**严格时间倒排**，
    所以一个关键词一次请求（size 给大）就能覆盖整个时间窗，无需翻页。
    """
    r = http(cfg['api'], method='post',
             json_body={'keyword': kw, 'page': page, 'size': int(cfg.get('size', 200))},
             referer=cfg.get('referer'))
    try:
        data = r.json().get('data') or {}
    except Exception:
        return []
    out = []
    all_older = True
    for it in (data.get('searchResults') or []):
        href = str(it.get('url') or '').strip()
        title = re.sub(r'<[^>]+>', '', str(it.get('title') or ''))
        title = re.sub(r'\s+', ' ', title).strip()
        if not (href.startswith('http') and len(title) >= 6):
            continue
        # 2026-10-15：接口返回里带 publishTime（"2026-09-30"），**先在列表层做窗口过滤**。
        # 每经网一个词的 totalHits 能到 1 万，翻页能翻很深；不过滤的话每条都要抓详情页，
        # 请求量直接爆炸。过滤后只对窗口内的抓详情。
        pt = _cfg.to_publishtime(it.get('publishTime'))
        if pt:
            if pt > _cfg.WINDOW_END:
                continue            # 晚于窗口（比如今天发的）
            if pt < _cfg.WINDOW_START:
                continue            # 早于窗口：结果严格时间倒排，后面的更老
            all_older = False
        else:
            all_older = False       # 没时间的条目不敢丢，交给详情页判
        out.append((title, href))
    if not out and all_older:
        return []                   # 整页都比窗口起点老 -> 调用方可以停止翻页
    return out


ADAPTERS['nbd'] = search_nbd


def _first_key(d, keys):
    """按 keys 顺序取第一个非空字符串。支持点号取嵌套，如 'urls.pc'。"""
    for k in keys:
        v = d
        for part in str(k).split('.'):
            if isinstance(v, dict):
                v = v.get(part)
            else:
                v = None
                break
        if isinstance(v, str) and v.strip():
            return v
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return str(v)
    return None


TITLE_KEYS = ['title', 'titleText', 'docTitle', 'headline', 'name', 'newsTitle', 'articleTitle']
URL_KEYS = ['url', 'link', 'href', 'docpuburl', 'articleUrl', 'contentUrl', 'detailUrl', 'webUrl']
TIME_KEYS = ['publishedTime', 'publishTimeStr', 'publishTime', 'pubTime', 'issuetime',
             'create_time', 'publishDate', 'showTime', 'date']


def _extract_items(data, cfg):
    """从任意 JSON 结构里递归找 {标题, 链接, 时间} —— 不依赖具体字段名。

    返回 (标题, 链接, 时间串或 None)。

    为什么要带时间：这些接口的文章 URL 里**没有日期**（中国基金报是
    /article/<uuid>、浙江在线是 t20260824_xxx.shtml），不把接口自带的时间
    取出来，就只能给每一条都去抓详情页才知道在不在窗口内 ——
    1 万多个关键词 × 每页 10 条会变成几百万次请求，根本跑不完。
    """
    out = []
    tkeys = cfg.get('title_keys') or TITLE_KEYS
    ukeys = cfg.get('url_keys') or URL_KEYS
    timekeys = cfg.get('time_keys') or TIME_KEYS
    idkey = cfg.get('id_key')
    url_tpl = cfg.get('item_url_tpl')

    def walk(o):
        if isinstance(o, dict):
            t = _first_key(o, tkeys)
            u = _first_key(o, ukeys)
            if not u and idkey and url_tpl and o.get(idkey) is not None:
                u = url_tpl.replace('{id}', str(o.get(idkey)))
            if t and u and len(t.strip()) >= 6:
                ts = _first_key(o, timekeys)
                out.append((re.sub(r'<[^>]+>', '', t).strip(), u.strip(), ts))
                return
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(data)
    return out


def _page_url(u, cfg, page):
    """把 {page} 或"偏移式翻页"参数填进 URL。

    有些站不是 page=N 而是 start=(N-1)*页大小（浙江在线就是 &start=10）。
    这种靠 cfg['page_offset_param'] + cfg['page_size'] 表达。
    """
    if '{page}' in u:
        return u.replace('{page}', str(page))
    if page > 1:
        if cfg.get('page_offset_param'):
            step = int(cfg.get('page_size', 10))
            return u + ('&' if '?' in u else '?') + '%s=%d' % (
                cfg['page_offset_param'], (page - 1) * step)
        pn = cfg.get('page_param')
        if not pn:
            return None
        return u + ('&' if '?' in u else '?') + '%s=%d' % (pn, page)
    return u


def _abs_url(h, domain):
    """补全相对 / 协议相对（http 页面里常见的 //host/path）链接。"""
    if h.startswith('//'):
        return 'https:' + h
    if h.startswith('/'):
        return domain.rstrip('/') + h
    return h


def search_json(cfg, kw, page):
    """通用 JSON 搜索接口：URL 里换关键词和页码，然后递归抽结果。"""
    u = cfg['url'].replace('{kw}', quote(kw))
    u = _page_url(u, cfg, page)
    if u is None:
        return []
    H = dict(BASE_H)
    if cfg.get('referer'):
        H['Referer'] = cfg['referer']
    if cfg.get('xhr'):
        H['X-Requested-With'] = 'XMLHttpRequest'
    r = requests.get(u, headers=H, timeout=20, verify=False)
    r.encoding = r.apparent_encoding or 'utf-8'
    txt = r.text.strip()
    if cfg.get('jsonp') and '(' in txt:
        # 凤凰财经这类把 JSON 包在 getSoFengDataCallback({...}) 里
        try:
            txt = txt[txt.index('(') + 1: txt.rindex(')')]
        except Exception:
            pass
    try:
        data = json.loads(txt)
    except Exception as e:
        # 别静默返回 []：这里曾经把 NameError（模块忘了 import json）当成
        # "接口没数据"，整站跑完 0 条还看不出来。
        print('  [json 解析失败] %s: %s | 前 120 字符: %s'
              % (type(e).__name__, e, txt[:120].replace('\n', ' ')))
        return []
    items = _extract_items(data, cfg)
    # 补全相对链接
    fixed = []
    for t, h, ts in items:
        h = _abs_url(h, cfg['domain'])
        if h.startswith('http'):
            fixed.append((t, h, ts))
    seen, uniq = set(), []
    for t, h, ts in fixed:
        if h not in seen:
            seen.add(h)
            uniq.append((t, h, ts))
    return uniq


ADAPTERS['json'] = search_json


# ==================== 栏目翻页模式（没有搜索接口的站走这条） ====================

CHANNEL_TEXT = re.compile(r'^[\u4e00-\u9fa5A-Za-z0-9·•・\-—－_、,，/&|+]{1,16}$')
# ↑ 原来只认 2-8 个纯汉字/字母、且不允许任何分隔符 —— 实测（2026-10-13 _diag_columns.py）
#   把"财经·证券""公司-新闻""汽车/行业"这类全挡了：中国金融界网首页 529 个同域链接、
#   盐城新闻网 488 个、比特网 102 个、中国新闻周刊 40 个，**一个都过不了这一关**，
#   于是被 _generic.py:611 判成"没发现任何栏目页，本模式对该站不适用"→ 静默 0 条。
#   放宽后真正的把关交给下面的路径正则（CHANNEL_HINT / ARTICLE_HINT / COL_PATH）
#   与 COL_SKIP，那两处才决定"该不该算栏目"。
# 路径匹配放宽（jsxw / gjyw 这种连写缩写要认出来），
# 只把最容易误伤的 ty/sh/pl/wh/kj 要求整段匹配（否则 ty 会命中 activity）。
CHANNEL_HINT = re.compile(
    r'news|yaowen|cj|finance|caijing|zhengjing|guonei|guoji|shehui|junshi|'
    r'keji|wenhua|pinglun|channel|subject|topic|list|index|yw|xw|zx|gn|gj|js|'
    r'(?:^|/)(?:sh|pl|wh|kj|ty)(?:/|$|\.)', re.I)
# 常见 CMS 的栏目路径形态（这些站光靠上面那条认不出来）：
#   三明网 /folder126/folder131/      华龙网 /web/column/col60900.html
#   河北新闻网 /node_112.htm
COL_PATH = re.compile(r'folder|column|col\d|node_|category|/type/|/lm/|/cat/', re.I)
# "加载更多"这类**每次渲染都变**的查询参数：比对页面是否重复时忽略它们，
# 否则同一个页面会因为参数不同被反复渲染，把 40 页渲染预算烧光。
_CHURN_PARAM = re.compile(
    r'^(?:_|t|ts|time|timestamp|_t|r|rand|random|callback|cb|'
    r'cms_node_post_list|_cms_node_post_list|ajax|format)$', re.I)

ARTICLE_HINT = re.compile(
    r'\.(s?html?|jsp|aspx|php)(\?|$)'          # 常规静态页
    r'|/\d{5,}'                                # 纯数字 id
    # UUID / 短码型文章页：中国基金报 /article/<uuid>、凤凰财经 /c/8ocOPLpuSty。
    # 少了这条，这两个站的栏目页会"一条文章都找不到"（累计页=0），
    # 明明栏目发现是成功的，却整站 0 条。
    r'|/(?:article|content|detail|news|a|c|p|d)/[\w-]{6,}',
    re.I)


# 与上市公司新闻基本无关的栏目：直接跳过，不浪费抓取预算。
# 按"栏目标题"排除最稳（路径缩写容易误判）。
COL_SKIP = re.compile(
    r'体育|娱乐|明星|综艺|影视|电影|音乐|美食|菜谱|旅游|游记|健康|养生|'
    r'天气|彩票|游戏|动漫|宠物|情感|图片|图集|视频|直播|时尚|美妆|育儿|'
    r'家居装饰|收藏|书画|笑话|星座|活动|展馆|展会|会展|商城|招聘|求职|'
    r'论坛|博客|微博|下载|客户端|注册|登录|帮助|广告|合作|联系|简介|'
    r'订阅|投票|问卷|专题|访谈|图库|相册|排行|公示|声明|版权|关于我们|关于|客服|'
    r'网站地图|无障碍|英文|邮箱|RSS|首页')
# 与公司/财经/产业高度相关的栏目：优先抓（超时预算先花在这些上）
COL_PRIORITY = re.compile(
    r'财经|经济|公司|企业|产业|产经|证券|股票|股市|金融|投资|银行|基金|保险|'
    r'信托|要闻|时政|国内|社会|市场|商业|消费|科技|汽车|房产|地产|能源|化工|'
    r'医药|钢铁|有色|环保|电力|宏观|政策|监管|上市|IPO|财讯|财报|龙头|概念')


def col_relevance(name: str, url: str) -> int:
    """栏目相关度：0=跳过，2=优先，1=普通。"""
    blob = (name or '') + ' ' + (url or '')
    if COL_SKIP.search(blob):
        return 0
    if COL_PRIORITY.search(blob):
        return 2
    return 1


def _reg_domain(netloc: str) -> str:
    """取可注册域（去掉 www、只留最后两段）。

    原来一律要求 **host 完全相同**，于是"首页在 www.x.com、栏目在 news.x.com"
    的站会被整片丢掉（比特网 telecom.chinabyte.com 就是这种），首页一个栏目都发现不了。
    """
    h = re.sub(r'^www\.', '', (netloc or '').lower())
    parts = h.split('.')
    return '.'.join(parts[-2:]) if len(parts) >= 2 else h


def discover_columns(domain):
    """从首页找新闻栏目页。返回 [(栏目名, URL)], base。"""
    base = 'https://' + domain
    r = None
    for b in ('https://' + domain, 'http://' + domain):
        try:
            rr = http(b, timeout=15)
            if rr.status_code < 400:
                r, base = rr, b
                break
        except Exception:
            continue
    if r is None:
        return [], base
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(r.text, 'html.parser')
    host = _reg_domain(urlparse(base).netloc)   # 用注册域，跨子域不误杀
    out, seen, seen_paths = [], set(), set()
    for a in soup.find_all('a', href=True):
        txt = re.sub(r'\s+', '', a.get_text(strip=True))
        href = urljoin(base, a['href'])
        if _reg_domain(urlparse(href).netloc) != _reg_domain(base):
            continue
        path = urlparse(href).path
        if not CHANNEL_TEXT.match(txt):
            continue
        is_col = bool(COL_PATH.search(path))
        # 栏目路径优先：/web/column/col60900.html 虽然是 .html，但它是栏目不是文章
        if not is_col:
            if ARTICLE_HINT.search(path) or not CHANNEL_HINT.search(path):
                continue
        if href in seen:
            continue
        # 同一栏目常同时挂 http/https 两个链接，按 path 去重（https 优先）
        if path in seen_paths:
            continue
        seen.add(href)
        seen_paths.add(path)
        out.append((txt, href))
    # 按相关度排序：优先财经/公司类，跳过的直接剔掉，普通类垫后
    scored = [(col_relevance(t, u), t, u) for t, u in out]
    scored = [x for x in scored if x[0] > 0]
    if not scored:
        # 首页没有任何"长得像栏目"的链接 —— 很多站导航是 JS 渲染的，路径也是自定义的。
        # 退回一步：把首页里非文章形态的同域链接逐个抓下来，页面里真的有成排文章才算栏目。
        # 用"页面内容"判定而不是路径猜测，正好也符合"只搜内容相关的栏目"。
        deep = _discover_columns_deep(soup, base, host)
        scored = [(col_relevance(t, u), t, u) for t, u in deep]
        scored = [x for x in scored if x[0] > 0]
    if not scored:
        # 最后一道兜底：有些站首页只有 JS/图片导航（定西网、嘉峪关新闻网、大河财立方），
        # 一条 <a> 栏目都提不出来，但 /news/、/cj/ 这类频道路径是活的。
        # 直接试路径，页里真的排着文章才算栏目 —— 与深度发现同一套内容验证标准。
        probed = _probe_common_channels(base)
        scored = [(col_relevance(t, u), t, u) for t, u in probed]
        scored = [x for x in scored if x[0] > 0]
    scored.sort(key=lambda x: (-x[0], len(x[2])))
    return [(t, u) for _, t, u in scored[:24]], base


DEEP_MAX_TEST = 30      # 最多试抓多少个候选页
DEEP_MIN_ARTS = 5       # 页面里至少要有这么多文章链接才算栏目


def _discover_columns_deep(soup, base, host):
    """内容验证型栏目发现：候选页真的排着文章才收。"""
    cands, seen = [], set()
    for a in soup.find_all('a', href=True):
        txt = re.sub(r'\s+', '', a.get_text(strip=True))
        href = urljoin(base, a['href'])
        u = urlparse(href)
        if _reg_domain(u.netloc) != _reg_domain(base):
            continue
        if not (2 <= len(txt) <= 20) or not re.search(r'[\u4e00-\u9fa5]', txt):
            continue
        if COL_SKIP.search(txt):
            continue
        if re.search(r'/(19|20)\d{2}[-/]\d{1,2}', u.path):
            continue                      # 路径里带年月的基本是文章
        if href.rstrip('/') == base.rstrip('/'):
            continue
        key = u.path or u.query
        if not key or key in seen:
            continue
        seen.add(key)
        cands.append((txt, href))
    # 相关度高的先试，试到够数就停（控制请求量）
    cands.sort(key=lambda x: -col_relevance(x[0], x[1]))
    out = []
    for txt, href in cands[:DEEP_MAX_TEST]:
        try:
            rr = http(href, referer=base, timeout=12)
        except Exception:
            continue
        if rr.status_code >= 400 or len(rr.text) < 2000:
            continue
        arts = col_article_links(rr.text, href)
        if len(arts) >= DEEP_MIN_ARTS:
            out.append((txt, href))
            if len(out) >= 16:
                break
    return out


PROBE_CHANNEL_PATHS = (
    '/news/', '/cj/', '/finance/', '/yaowen/', '/gn/', '/gsxw/', '/szyw/',
    '/society/', '/list/', '/news/index.html', '/index.html',
)


def _probe_common_channels(base):
    """兜底：直接试常见频道路径，页里真的排着文章（>= DEEP_MIN_ARTS 条）才收。

    定西网 / 嘉峪关新闻网 / 大河财立方这类站，首页几乎没有可点的栏目链接
    （JS 渲染或图片导航），靠首页 <a> 一条栏目都发现不了，但频道页是活的。
    只对"首页+深度发现都失败"的站生效，最多多花 len(PROBE_CHANNEL_PATHS) 个请求。
    """
    out = []
    for p in PROBE_CHANNEL_PATHS:
        url = urljoin(base, p)
        try:
            r = http(url, referer=base, timeout=12)
        except Exception:
            continue
        if r.status_code >= 400 or len(r.text) < 2000:
            continue
        if len(col_article_links(r.text, base)) >= DEEP_MIN_ARTS:
            out.append((p.strip('/') or 'index', url))
    return out


def find_next_page(html, base, cur_url):
    """从列表页里找"下一页"链接 —— 比猜 URL 规则可靠得多。"""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, 'html.parser')
    pats = (r'^\s*下一页', r'^\s*下页', r'^\s*next', r'^\s*Next', r'^\s*下ー页', r'>\s*$')
    for a in soup.find_all('a', href=True):
        txt = a.get_text(strip=True)
        for p in pats:
            if re.search(p, txt):
                href = urljoin(base, a['href'])
                if href and href != cur_url and not href.lower().startswith('javascript'):
                    return href
    return None


WAF_MARKS = ('challengeform', '请稍候', '安全防护系统', 'jsl_clearance',
             'cf_app_waf', 'just a moment', 'challenge-platform', '访问验证',
             '网防风控', '拦截')


def looks_like_waf(html: str) -> bool:
    """这一页是不是反爬/WAF 的挑战页。

    2026-10-15 实测：www.wxrb.com（360 磐云）请求一多就返回 **HTTP 200 + 3716 字节
    挑战页**。爬虫看到的是"列表页没有文章" → 静默停住，日志只显示"累计页=1 命中=0"，
    看起来像"这个站没数据"，实际是被挡了。有这个判据，日志才能明确说出来。
    """
    if not html or len(html) > 20000:
        return False
    low = html.lower()
    return any(m in low for m in WAF_MARKS)


def col_page_candidates(col_url, page):
    """翻页 URL 候选（找"下一页"链接失败时的兜底）。

    两个坑都踩过：
      - 扩展名要试 .htm / .html / .shtml 三种（news.youth.cn 用 .htm，只试 .html 会全 404）
      - 国内站的页码常常是 0 基（index_1.htm 就是第 2 页），所以 page-1 和 page 都要试
    """
    if page == 1:
        return [col_url]
    u = col_url.rstrip('/')
    m = re.search(r'\.(s?html?)$', u)
    if m:
        stem, ext = u[:m.start()], m.group(0)
        return ['%s_%d%s' % (stem, page - 1, ext), '%s_%d%s' % (stem, page, ext)]
    out = []
    for e in ('htm', 'html', 'shtml'):
        out.append('%s/index_%d.%s' % (u, page - 1, e))
        out.append('%s/index_%d.%s' % (u, page, e))
    out.append('%s/list_%d.htm' % (u, page - 1))
    out.append('%s/list_%d.html' % (u, page - 1))
    return out


def col_article_links(html, base):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, 'html.parser')
    host = _reg_domain(urlparse(base).netloc)   # 用注册域，跨子域不误杀
    out, seen = [], set()
    for a in soup.find_all('a', href=True):
        txt = re.sub(r'\s+', ' ', a.get_text(' ', strip=True)).strip()
        href = urljoin(base, a['href'])
        if _reg_domain(urlparse(href).netloc) != host:
            continue
        if len(txt) < 8 or not ARTICLE_HINT.search(urlparse(href).path):
            continue
        if href in seen:
            continue
        seen.add(href)
        out.append((txt, href))
    return out


def col_article_links_ex(html, base):
    """同 col_article_links，但额外把"列表里那段摘要"也带出来。

    为什么要摘要：像证券时报网，文章 URL 是 /article/detail/<数字>.html，
    **URL 里没有日期、列表页也不显示日期**，只能抓详情页才知道时间。
    但列表页是有摘要的 —— 先用"标题+摘要"在本地过一遍关键词，命中才去抓详情页，
    能省掉绝大部分请求（东方财富网就是靠这一条才跑得完）。
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, 'html.parser')
    host = _reg_domain(urlparse(base).netloc)   # 用注册域，跨子域不误杀
    out, seen = [], set()
    for a in soup.find_all('a', href=True):
        txt = re.sub(r'\s+', ' ', a.get_text(' ', strip=True)).strip()
        href = urljoin(base, a['href'])
        if _reg_domain(urlparse(href).netloc) != host:
            continue
        if len(txt) < 8 or not ARTICLE_HINT.search(urlparse(href).path):
            continue
        if href in seen:
            continue
        seen.add(href)
        # 往上找几层，取比标题明显更长的那段文本当摘要
        summary = ''
        node = a
        for _ in range(4):
            if node.parent is None:
                break
            node = node.parent
            blob = re.sub(r'\s+', ' ', node.get_text(' ', strip=True))
            if len(blob) > len(txt) + 15:
                summary = blob.replace(txt, ' ', 1)[:400]
                break
        out.append((txt, href, summary))
    return out


def run_colpage(cfg) -> None:
    """栏目翻页：扫频道列表 -> 只抓时间窗内的文章 -> 命中关键词才入库。"""
    station = cfg['station']
    keys = _cfg.load_keys()          # 栏目型必须拿全量关键词当过滤器
    keyset = set(k for k in keys if len(k) >= 2)
    page_tpl = cfg.get('page_url_tpl')
    if cfg.get('columns'):
        # 栏目发现认不出来的站，直接手写栏目清单（证券时报网这种）。
        base = 'https://' + cfg['domain']
        cols = [(c[0], c[1]) if isinstance(c, (list, tuple)) else (c, c)
                for c in cfg['columns']]
    else:
        cols, base = discover_columns(cfg['domain'])

    from pymongo import MongoClient
    from model.NewsEntity import NewsEntity
    from utils.TableUtil import reverse_url
    coll = MongoClient('127.0.0.1').get_database(_cfg.DB_NAME).get_collection(station)
    storage = _cfg.SafeStorage(coll)

    print('库=%s 集合=%s  域名=%s  发现栏目=%d'
          % (_cfg.DB_NAME, station, cfg['domain'], len(cols)))
    if not cols:
        print('  ！！没发现任何栏目页，本模式对该站不适用')
        return
    for t, u in cols:
        print('   栏目 [%s] %-10s %s' % ('优先' if col_relevance(t, u) == 2 else '普通', t, u[:80]))

    max_page = int(cfg.get('max_page', 60))
    stat = {'cols': 0, 'pages': 0, 'in_win': 0, 'kw': 0, 'saved': 0,
            'skip_dup': 0, 'drop_detail': 0, 'drop_kw': 0, 'why': []}
    for cname, curl in cols:
        stat['cols'] += 1
        stop = False
        cur = curl
        for page in range(1, max_page + 1):
            arts = []
            r = None
            # 有些站的翻页既没有"下一页"链接也不是 index_N 规则，而是固定参数：
            # 证券时报网列表页用 ?page_time=N（实测 ?page=2 返回同一页，只有
            # page_time 会真的换内容）。这种用 cfg['page_url_tpl'] 表达。
            if page_tpl and page > 1:
                cands = [page_tpl.replace('{col}', curl).replace('{page}', str(page))]
            elif page > 1:
                # 第 2 页起要把"猜出来的候选"都试一遍，不能只试一个。
                # 原来只试 cur（= 上一轮猜的第一个候选），一旦猜错（比如
                # 无锡新传媒真正有效的是 index_2.shtml，而候选里 index_1.shtml 排前面）
                # 本页就拿不到文章、直接 break —— 整个栏目白跑，日志只显示"累计页=1"。
                cands = [cur] + [c for c in col_page_candidates(curl, page) if c != cur]
            else:
                cands = col_page_candidates(curl, 1)
            waf_hit = False
            for cand in cands:
                try:
                    rr = http(cand, referer=base)
                except Exception:
                    continue
                if rr.status_code >= 400:
                    continue
                got = col_article_links_ex(rr.text, cand)
                if got:
                    arts, r = got, rr
                    break
                if looks_like_waf(rr.text):
                    waf_hit = True
            if not arts:
                if waf_hit:
                    stat['waf'] = int(stat.get('waf', 0)) + 1
                    print('  ！！栏目[%s] 被 WAF 挑战页挡住（HTTP 200 但正文是"请稍候/安全防护"），'
                          '不是没数据 —— 本列提前结束' % cname)
                break
            stat['pages'] += 1

            # 时间窗判断放在**页级**：列表按时间倒排，只要本页还有窗口内的文章就继续翻。
            # （原来在条目级"遇到一个旧日期就整列停止"，会被侧栏里的旧链接误杀。）
            page_dates = [d for _, h, *_ in arts if (d := url_date(h))]
            if page_dates and max(page_dates) < (2026, 7, 1):
                stop = True

            # 下一页优先跟着列表页自己的"下一页"链接走
            nxt = find_next_page(r.text, base, cur)
            if page_tpl:
                nxt = page_tpl.replace('{col}', curl).replace('{page}', str(page + 1))
            elif not nxt:
                # 兜底：按 URL 规则猜 —— 必须**逐个试到"真能出文章"的那一个**，
                # 不能像以前那样盲拿第一个。
                # 2026-10-15 实测（无锡新传媒 www.wxrb.com）：
                #   /cj/index.shtml 的下一页其实是 /cj/index_2.shtml，
                #   而候选里 index_1.shtml 排前面且是 404；
                #   盲拿 index_1 会把 cur 污染掉，于是第 3 页起候选变成
                #   index_1_2.shtml / index_1_3.shtml（全 404）→ 整列提前结束。
                #   日志表现就是"累计页卡在个位数、in_win 极少、命中 0~1"。
                for cand in col_page_candidates(curl, page + 1):
                    if cand == cur:
                        continue
                    try:
                        rr = http(cand, referer=base)
                    except Exception:
                        continue
                    if rr.status_code >= 400:
                        continue
                    if col_article_links_ex(rr.text, cand):
                        nxt = cand
                        break
            prev = cur
            cur = nxt or cur
            if cur == prev:
                _last_ok = False
            else:
                _last_ok = True
            for title, href, summary in arts:
                ud = url_date(href)
                if ud and ud < (2026, 7, 1):
                    continue        # 侧栏/相关阅读里的旧文章，跳过即可，不停止整列
                if ud and ud > (2026, 10, 10):
                    continue
                stat['in_win'] += 1
                # 可选的"用列表摘要先筛关键词"（cfg['kw_prefilter']，默认关）。
                # 只给证券时报网这种"URL 没日期、列表没日期、只能逐条抓详情"的
                # 高产量站开 —— 它能把请求量砍掉一大截。
                # 但它是**有损**的：关键词只出现在正文、列表标题/摘要里没有的文章会被丢掉
                # （贵视网就是这样，开了它 4756 条窗口内文章一条都命中不了）。
                # 所以默认必须关，不能全局开。
                if cfg.get('kw_prefilter') and summary \
                        and not any(k in (title + summary) for k in keyset):
                    continue
                try:
                    id_ = reverse_url(href)
                except Exception:
                    id_ = href
                if storage.exist(id_):
                    stat['skip_dup'] += 1
                    continue
                try:
                    dr = http(href, referer=base)
                    d_title, content, source, raw_time = parse_detail(dr.text, href)
                except Exception as e:
                    stat['drop_detail'] += 1
                    if len(stat['why']) < 3:
                        stat['why'].append('详情失败 %s: %s @ %s'
                                           % (type(e).__name__, str(e)[:50], href[:60]))
                    continue
                # kw_filter=False 的站：栏目里窗口内的稿子直接收，不做关键词过滤。
                # 2026-10-14：北极星电力（窗内 503 命中 0）、金羊网 110/0、济宁新闻网 96/0、
                # 大河财立方 28/0、北方网 2/0 —— 这类能源/地方站的栏目稿标题正文里
                # 一个关键词都不出现，硬过滤 = 整站 0 条。默认 True = 行为与以前完全一致。
                if cfg.get('kw_filter', True) \
                        and not any(k in ((d_title or '') + content) for k in keyset):
                    stat['drop_kw'] += 1
                    continue
                stat['kw'] += 1
                # 详情页解析不出时间时，退回用 URL 里的日期 —— 判断窗口用的就是它，
                # 两处口径必须一致。2026-10-15 中国金融网实测：461 条窗口内文章里
                # 46 条被 "publishtime 不在窗口内(0)" 拒掉，就是因为详情页没给时间
                # 而 URL 里明明有（列表页靠 URL 日期判窗口，所以它们被算作窗口内）。
                _ud = url_date(href)
                _ts = _cfg.to_publishtime(raw_time) or _cfg.to_publishtime(
                    '%04d-%02d-%02d' % _ud if _ud else '')
                news = NewsEntity(id=id_, title=d_title or title, content=content, from_=station,
                                  publishtime=_ts,
                                  raw_publishTime=raw_time or _cfg.normalize_raw_time(_ts),
                                  fetchTime=int(time.time() * 1000), baseUrl=href, sourse=source)
                if storage.save(news):
                    stat['saved'] += 1
                    print('   +1 [%s] %s' % (cname, (d_title or title)[:44]))
                time.sleep(_cfg.DETAIL_SLEEP)
            if stop:
                break
            if not _last_ok:
                break          # 翻页走不动了，别再空转
            time.sleep(_cfg.KEY_SLEEP)
        print('  栏目[%s] 完  累计页=%d 命中=%d' % (cname, stat['pages'], stat['saved']))

    print('\n[%s] %s' % (station, stat))
    print('集合共 %d 条，校验拒绝 %d' % (coll.count_documents({}), len(storage.rejected)))
    for rr in storage.rejected[:6]:
        print('   拒绝: %s' % rr)


# ==================== 通用"列表接口"模式 ====================
# 适用于"栏目/分页是个 JSON 接口"的站：中国基金报、CCTV 等。
# 与 run_colpage 的区别：列表本身就是结构化数据（带标题/时间/摘要），
# 所以能先在本地筛时间窗和关键词，命中才去抓详情页 —— 大站全靠这一点才跑得完。

SUMMARY_KEYS = ['summary', 'brief', 'digest', 'description', 'abstract', 'intro', 'content']


def build_matcher(keys):
    """hit(text) -> bool：text 里是否出现任一关键词。

    用 n-gram 查表，不用 any(k in blob)：11546 个关键词 × 几万条
    逐条 in 判断是几亿次子串比较，这样单条 < 0.1ms。
    """
    codes = {k for k in keys if k.isdigit() and len(k) == 6}
    names = {k for k in keys if not k.isdigit() and len(k) >= 2}
    maxlen = max((len(n) for n in names), default=2)
    zh = re.compile(r'[\u4e00-\u9fa5A-Za-z]+')
    num = re.compile(r'\d{6}')

    def hit(text):
        if not text:
            return False
        if codes:
            for m in num.finditer(text):
                if m.group(0) in codes:
                    return True
        for run in zh.findall(text):
            n = len(run)
            for i in range(n):
                for L in range(2, min(maxlen, n - i) + 1):
                    if run[i:i + L] in names:
                        return True
        return False

    return hit


def _extract_rows(data, cfg):
    """递归抽出 {title, url, time, summary} 四元组的列表。"""
    out = []
    tkeys = cfg.get('title_keys') or TITLE_KEYS
    ukeys = cfg.get('url_keys') or URL_KEYS
    timekeys = cfg.get('time_keys') or TIME_KEYS
    skeys = cfg.get('summary_keys') or SUMMARY_KEYS
    idkey = cfg.get('id_key')
    url_tpl = cfg.get('item_url_tpl')

    def walk(o):
        if isinstance(o, dict):
            t = _first_key(o, tkeys)
            u = _first_key(o, ukeys)
            if not u and idkey and url_tpl and o.get(idkey) is not None:
                u = url_tpl.replace('{id}', str(o.get(idkey)))
            if t and u and len(t.strip()) >= 6:
                out.append({
                    'title': re.sub(r'<[^>]+>', '', t).strip(),
                    'url': u.strip(),
                    'time': _first_key(o, timekeys),
                    'summary': re.sub(r'<[^>]+>', '', (_first_key(o, skeys) or '')).strip(),
                })
                return
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(data)
    return out


def run_listapi(cfg) -> None:
    """cfg: station / domain / url(带 {page}，可选 {col}) / cols / jsonp / max_page ..."""
    station = cfg['station']
    keys = _cfg.load_keys()
    hit = build_matcher(keys)
    domain = cfg['domain'] if cfg['domain'].startswith('http') else 'https://' + cfg['domain']

    from pymongo import MongoClient
    from model.NewsEntity import NewsEntity
    from utils.TableUtil import reverse_url
    coll = MongoClient('127.0.0.1').get_database(_cfg.DB_NAME).get_collection(station)
    storage = _cfg.SafeStorage(coll)

    cols = cfg.get('cols') or [(None, station)]
    cols = [(c[0], c[1]) if isinstance(c, (list, tuple)) else (c, str(c)) for c in cols]
    max_page = int(cfg.get('max_page', 300))
    print('库=%s 集合=%s 关键词=%d 栏目=%d 接口=%s'
          % (_cfg.DB_NAME, station, len(keys), len(cols), cfg['url'][:70]))

    stat = {'pages': 0, 'in_win': 0, 'kw': 0, 'saved': 0}
    for col, cname in cols:
        empty_streak = 0
        for page in range(1, max_page + 1):
            u = cfg['url'].replace('{page}', str(page))
            if '{col}' in u:
                u = u.replace('{col}', str(col or ''))
            try:
                r = http(u, referer=cfg.get('referer', domain), timeout=25)
                r.encoding = r.apparent_encoding or 'utf-8'
                txt = r.text.strip()
                if cfg.get('jsonp') and '(' in txt:
                    txt = txt[txt.index('(') + 1: txt.rindex(')')]
                data = json.loads(txt)
            except Exception as e:
                print('  [%s] page=%d 接口失败 %s: %s' % (cname, page, type(e).__name__, str(e)[:80]))
                break
            rows = _extract_rows(data, cfg)
            if not rows:
                break
            stat['pages'] += 1
            times = [_cfg.to_publishtime(x['time']) for x in rows]
            times = [t for t in times if t]
            # 时间倒排：整页都早于窗口起点就停
            if times and max(times) < _cfg.WINDOW_START:
                print('  [%s] 到窗口起点，停止（第 %d 页）' % (cname, page))
                break
            page_kept = 0
            for x in rows:
                ts = _cfg.to_publishtime(x['time'])
                if ts and not _cfg.in_window(ts):
                    continue
                if cfg.get('require_kw') and not hit(x['title']):
                    continue
                page_kept += 1
                stat['in_win'] += 1
                if not hit(x['title'] + ' ' + x['summary']):
                    continue
                stat['kw'] += 1
                href = _abs_url(x['url'], domain)
                if not href.startswith('http'):
                    continue
                try:
                    id_ = reverse_url(href)
                except Exception:
                    id_ = href
                if storage.exist(id_):
                    continue
                try:
                    dr = http(href, referer=domain)
                    d_title, content, source, raw_time = parse_detail(dr.text, href)
                except Exception:
                    continue
                if not content or len(content) < 30:
                    continue
                news = NewsEntity(id=id_, title=d_title or x['title'], content=content,
                                  from_=station,
                                  publishtime=_cfg.to_publishtime(raw_time) or ts,
                                  raw_publishTime=raw_time or _cfg.normalize_raw_time(ts),
                                  fetchTime=int(time.time() * 1000), baseUrl=href,
                                  sourse=source or station)
                if storage.save(news):
                    stat['saved'] += 1
                    if stat['saved'] <= 5 or stat['saved'] % 25 == 0:
                        print('   +%d [%s] %s' % (stat['saved'], cname, (d_title or x['title'])[:44]))
                time.sleep(_cfg.DETAIL_SLEEP)
            if page_kept == 0:
                empty_streak += 1
                if empty_streak >= 3 and page > 3:
                    break
            else:
                empty_streak = 0
            time.sleep(0.3)
        print('  栏目[%s] 完 %s' % (cname, stat))

    print('\n[%s] %s' % (station, stat))
    print('集合共 %d 条，校验拒绝 %d' % (coll.count_documents({}), len(storage.rejected)))
    for rr in storage.rejected[:6]:
        print('   拒绝: %s' % rr)


def channels_from_html(html, base, limit=10):
    """从（已渲染的）HTML 里抽栏目页候选。

    和 discover_columns 同一套启发式，但输入是浏览器渲染后的 HTML ——
    JS 站点的导航只有渲染后才存在，所以这一步必须放在浏览器里做。
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, 'html.parser')
    host = _reg_domain(urlparse(base).netloc)   # 用注册域，跨子域不误杀
    out, seen = [], set()
    for a in soup.find_all('a', href=True):
        txt = re.sub(r'\s+', '', a.get_text(strip=True))
        if not CHANNEL_TEXT.match(txt):
            continue
        href = urljoin(base, a['href'])
        u = urlparse(href)
        if re.sub(r'^www\.', '', u.netloc) != host:
            continue
        path = u.path
        if not path or path == '/' or path in seen:
            continue
        # 排除明显是文章页的
        if ARTICLE_HINT.search(path) and not COL_PATH.search(path):
            continue
        seen.add(path)
        out.append((txt, href))
    scored = [(col_relevance(t, u), t, u) for t, u in out]
    scored = [x for x in scored if x[0] > 0]
    scored.sort(key=lambda x: (-x[0], len(x[2])))
    return [(t, u) for _, t, u in scored[:limit]]


def run_browser_home(cfg) -> None:
    """无头浏览器渲染页面 -> 抽文章链接 -> 详情页 -> 关键词过滤 -> 入库。

    用途：给"HTML 里根本抽不到文章链接"的 JS 站点做兜底。
    很多站的首页/栏目页是前端渲染的，requests 拿到的是空壳（南方网、北极星电力、
    华尔街见闻、大河财立方、上海热线、牛华网……），栏目发现和栏目翻页都因此 0 条。

    重要：**只抓首页没用**。窗口是 7-01~10-10，而跑的时候往往已经过了几天，
    首页只挂当天的稿子（南方网首页 40 条全是 10-14 的，一条都不在窗口内）。
    所以这里渲染完首页后，还会用渲染后的 HTML 发现栏目页，再逐个渲染栏目
    （cfg['discover']=False 可关掉）。
    """
    station = cfg['station']
    keys = _cfg.load_keys()
    keyset = set(k for k in keys if len(k) >= 2)

    from pymongo import MongoClient
    from model.NewsEntity import NewsEntity
    from utils.TableUtil import reverse_url
    coll = MongoClient('127.0.0.1').get_database(_cfg.DB_NAME).get_collection(station)
    storage = _cfg.SafeStorage(coll)

    host = cfg['domain'].replace('https://', '').replace('http://', '').rstrip('/')
    seeds = cfg.get('urls') or ['https://' + host + '/']
    print('库=%s 集合=%s 关键词=%d 种子页 %d 个'
          % (_cfg.DB_NAME, station, len(keys), len(seeds)))

    from DrissionPage import ChromiumPage, ChromiumOptions
    co = ChromiumOptions()
    page = None
    stat = {'pages': 0, 'links': 0, 'in_win': 0, 'kw': 0, 'saved': 0,
            'skip_dup': 0, 'drop_detail': 0, 'drop_short': 0, 'drop_notarticle': 0,
            'drop_kw': 0, 'drop_outwin': 0, 'why': []}
    todo = list(seeds)
    done_urls = set()
    page_depth = {u: 0 for u in seeds}

    def render_and_collect(u):
        """渲染一个页面，返回 (html, 抽到的文章链接)"""
        try:
            page.get(u, retry=2, timeout=30)
        except Exception as e:
            print('  打开失败 %s: %s' % (u[:58], e))
            return '', []
        time.sleep(2.2)
        try:
            for _ in range(4):
                page.scroll.down(1200)
                time.sleep(0.5)
        except Exception:
            pass
        try:
            html = page.html
        except Exception:
            return '', []
        return html, col_article_links_ex(html, u)

    def key_of(u):
        """去重键 = path + **有意义的**查询串。

        以前只用 path（丢掉整个查询串），本意是防"加载更多"那种每次渲染都变的
        参数把渲染预算(40 页)全烧在同一批页面上（如 `?cms_node_post_list...`）。
        但副作用很致命：**靠查询串区分的栏目页会被全部当成重复** ——
        嘉峪关新闻网 37 个栏目全是 `newslist.html?sid=NNN`，路径完全一样，
        结果只渲染了第 1 个、其余 36 个直接被跳过（实测 pages=1，整站 0 条）。
        现在按参数名过滤：只丢掉下面这几个已知的抖动参数，其余查询串并进 key。
        """
        try:
            p = urlparse(u)
        except Exception:
            return u
        q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if not _CHURN_PARAM.match(k)]
        q.sort()
        return p.path + ('?' + urlencode(q) if q else '')

    try:
        page = ChromiumPage(co)
        while todo and stat['pages'] < int(cfg.get('max_render', 14)):
            u = todo.pop(0)
            k = key_of(u)
            if k in done_urls:
                continue
            done_urls.add(k)
            html, arts = render_and_collect(u)
            if not html:
                continue
            stat['pages'] += 1
            stat['links'] += len(arts)
            print('  [%s] 渲染 #%d 抽出文章 %d 条'
                  % (u[:56], stat['pages'], len(arts)))

            # 从首页发现栏目页，追加到待渲染队列
            if cfg.get('discover', True) and len(done_urls) <= len(seeds):
                chans = channels_from_html(html, u, limit=int(cfg.get('max_channels', 8)))
                if chans:
                    print('     发现栏目 %d 个: %s'
                          % (len(chans), ', '.join(t for t, _ in chans[:8])))
                    for _t, cu in chans:
                        if key_of(cu) not in done_urls:
                            todo.append(cu)

            # 跟着列表页自己的"下一页"往深翻。
            # 这一步是必需的：窗口是 7-01~10-10，而跑的时候已经过了几天，
            # 每个栏目的第 1 页全是当天的稿子（南方网首页 40 条全是 10-14 的），
            # 不往深翻一条窗口内的都够不着。
            depth = page_depth.get(u, 0)
            if depth < int(cfg.get('max_depth', 3)):
                # 只跟页面**自己的**"下一页"链接。
                # 这里不用 col_page_candidates 去猜 URL —— JS 站的翻页是前端路由，
                # 猜出来的 index_1.htm / _1_1.htm 全是 404/空页，
                # 实测会把 40 页的渲染预算烧掉一大半还拿不到东西。
                nxt = find_next_page(html, u, u)
                if nxt and key_of(nxt) not in done_urls:
                    page_depth[nxt] = depth + 1
                    todo.append(nxt)

            for title, href, summary in arts:
                ud = url_date(href)
                if ud and not ((2026, 7, 1) <= ud <= (2026, 10, 10)):
                    continue
                stat['in_win'] += 1
                if cfg.get('kw_prefilter') and summary \
                        and not any(k in (title + summary) for k in keyset):
                    continue
                try:
                    id_ = reverse_url(href)
                except Exception:
                    id_ = href
                if storage.exist(id_):
                    stat['skip_dup'] += 1
                    continue
                try:
                    # 2026-10-14：撞 WAF / JS 挑战的站，**详情也必须走浏览器**。
                    # 北极星电力实测：列表靠无头 Edge 过了阿里云 CF_APP_WAF，详情却用 requests，
                    # 拿回来的是 16,227 字节挑战页（两个不同文章 URL 字节数完全相同、<p> 0 个、
                    # 可见文字 507 字）→ 正文 0 字 → 522 条窗内文章全丢（drop_short 508）。
                    # cfg['detail_via_browser']=True 时复用**已经过完挑战的 page 对象**取详情。
                    if cfg.get('detail_via_browser') and 'page' in dir():
                        page.get(href, retry=1, timeout=30)
                        d_title, content, source, raw_time = parse_detail(page.html, href)
                    else:
                        dr = http(href, referer=u, timeout=20)
                        d_title, content, source, raw_time = parse_detail(dr.text, href)
                except Exception as e:
                    stat['drop_detail'] += 1
                    if len(stat['why']) < 3:
                        stat['why'].append('详情失败 %s: %s @ %s'
                                           % (type(e).__name__, str(e)[:50], href[:60]))
                    continue
                if not content or len(content) < 30:
                    stat['drop_short'] += 1
                    if len(stat['why']) < 3:
                        stat['why'].append('正文只有 %d 字 @ %s' % (len(content or ''), href[:60]))
                    continue
                if not looks_like_article(d_title or title, content):
                    stat['drop_notarticle'] += 1
                    storage.rejected.append('%s | 不像文章页' % href)
                    continue
                # 同上：cfg['kw_filter']=False 时不做关键词过滤（默认 True，行为不变）
                if cfg.get('kw_filter', True) \
                        and not any(k in ((d_title or title) + content) for k in keyset):
                    stat['drop_kw'] += 1
                    continue
                stat['kw'] += 1
                ts = _cfg.to_publishtime(raw_time)
                if ts and not _cfg.in_window(ts):
                    # 2026-10-15 补计数器：这里原来是**静默 continue** ——
                    # 南方网实测 kw=54、skip_dup=24、其余计数器全 0、saved=0，
                    # 30 篇抓完详情就凭空消失。真相是列表页 URL 不带日期、
                    # 真的发布时间在详情页里，而那 30 篇都在窗口外（首页/加载更多
                    # 全是当天稿）。没有这个计数器就只能靠人肉推断。
                    stat['drop_outwin'] += 1
                    if len(stat['why']) < 3:
                        stat['why'].append('详情页时间在窗口外 %s @ %s'
                                           % (raw_time or _cfg.normalize_raw_time(ts), href[:60]))
                    continue
                news = NewsEntity(id=id_, title=d_title or title, content=content,
                                  from_=station, publishtime=ts,
                                  raw_publishTime=raw_time or _cfg.normalize_raw_time(ts),
                                  fetchTime=int(time.time() * 1000), baseUrl=href,
                                  sourse=source or station)
                if storage.save(news):
                    stat['saved'] += 1
                    print('   +%d %s' % (stat['saved'], (d_title or title)[:44]))
                time.sleep(_cfg.DETAIL_SLEEP)
    except Exception as e:
        print('  浏览器异常 %s: %s' % (type(e).__name__, e))
    finally:
        try:
            if page is not None:
                page.quit()
        except Exception:
            pass

    print('\n[%s] %s' % (station, stat))
    print('集合共 %d 条，校验拒绝 %d' % (coll.count_documents({}), len(storage.rejected)))
    for rr in storage.rejected[:4]:
        print('   拒绝: %s' % rr)


# ---------------- 详情页解析 ----------------

def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')

    title = ''
    h1 = soup.select_one('h1')
    if h1:
        t = h1.get_text(' ', strip=True)
        # 不少站把 <h1> 拿去做导航（"XX网首页 | 广告合作 | 手机版"），这种不能用
        if t and not BAD_TITLE.search(t):
            title = t
    if not title:
        tt = soup.find('title')
        title = tt.get_text(strip=True) if tt else ''
        title = re.split(r'[_|\-—]\s*', title)[0].strip()
    title = re.sub(r'\s+', ' ', title).strip()

    # 正文：在所有候选里挑最长的那个块
    best = ''
    for sel in CONTENT_SELECTORS:
        for el in soup.select(sel):
            cls = ' '.join(el.get('class') or []) + ' ' + str(el.get('id') or '')
            if CONTENT_BLACKLIST.search(cls):
                continue
            txt = el.get_text(' ', strip=True)
            if len(txt) > len(best):
                best = txt
    if len(best) < _cfg.MIN_CONTENT_LEN:
        # 退而求其次：找最长的 <p> 集合
        ps = [p.get_text(' ', strip=True) for p in soup.find_all('p')]
        joined = ' '.join(x for x in ps if len(x) > 15)
        if len(joined) > len(best):
            best = joined
    content = re.sub(r'\s+', ' ', best).strip()

    # 来源：优先"来源：xxx"
    source = ''
    m = re.search(r'来源[：:]\s*([^\s|，,。]{2,20})', soup.get_text(' ', strip=True))
    if m:
        source = m.group(1).strip()

    # 发布时间：① <meta> 最可靠 ② 正文前 3000 字 ③ URL 日期兜底
    #
    # 2026-10-14 实测（大众网）：页面明明有
    #   <meta name="pubtime" content="2026-09-05 14:15"> 和 publishdate，
    # 但老代码不看 meta，只在正文里找；而那一页 soup.get_text() 里一个日期串都没有
    # （日期藏在样式/JS 里）-> raw_time 抽空或抽到怪格式 -> to_publishtime 返回 0
    # -> 被判"publishtime 不在窗口内(0)"丢掉（大众网一轮白丢 71 条）。
    raw_time = ''
    for m in soup.find_all('meta'):
        nm = (m.get('name') or m.get('property') or '').lower()
        v = (m.get('content') or '').strip()
        if not v or not re.search(r'time|date|pub', nm):
            continue
        if _cfg.to_publishtime(v):        # 只认能解析成时间戳的
            raw_time = v
            break
    if not raw_time:
        page_text = soup.get_text(' ', strip=True)[:3000]
        for p in DATE_PAT:
            mm = p.search(page_text)
            if mm and _cfg.to_publishtime(mm.group(0)):
                raw_time = mm.group(0).strip()
                break
    if raw_time and not _cfg.to_publishtime(raw_time):
        raw_time = ''                    # 抽到了但解析不出来（格式怪），当没抽到
    if not raw_time:
        ud = url_date(url)
        if ud:
            raw_time = '%04d-%02d-%02d' % ud
    return title, content, source, raw_time


def run(cfg) -> None:
    """cfg: station / domain / search / 以及适配器需要的字段。"""
    station = cfg['station']
    adapter = ADAPTERS[cfg['search']]
    keys = _cfg.load_keys()
    from pymongo import MongoClient
    from model.NewsEntity import NewsEntity
    from utils.TableUtil import reverse_url

    coll = MongoClient('127.0.0.1').get_database(_cfg.DB_NAME).get_collection(station)
    storage = _cfg.SafeStorage(coll)

    max_page = int(cfg.get('max_page', 30))
    print('库=%s 集合=%s 关键词=%d 接口=%s' % (_cfg.DB_NAME, station, len(keys), cfg['search']))

    for ki, kw in enumerate(keys, 1):
        stop = False
        for page in range(1, max_page + 1):
            try:
                items = adapter(cfg, kw, page)
            except Exception as e:
                print('  [%d] 搜索失败 kw=%s page=%d %s: %s' % (ki, kw, page, type(e).__name__, e))
                break
            if not items:
                break
            if cfg.get('require_kw'):
                # 有些站的搜索很"松"：搜"万科"会连"中国男足3:0大胜"一起返回
                # （经济观察网、国际在线都是）。这种站必须本地再过一遍关键词，
                # 否则会进一堆跟上市公司无关的新闻。
                kk = re.sub(r'\s+', '', kw)
                items = [it for it in items
                         if kk in re.sub(r'\s+', '', re.sub(r'<[^>]+>', '', it[0]))]
                if not items:
                    break

            # 先按"接口自带的时间"过一遍，能省掉大量详情页请求
            page_ts = []
            kept = []
            for it in items:
                title, href = it[0], it[1]
                ts_hint = it[2] if len(it) > 2 else None
                ht = _cfg.to_publishtime(ts_hint) if ts_hint else 0
                if ht:
                    page_ts.append(ht)
                    if ht < _cfg.WINDOW_START:
                        continue          # 早于窗口，连详情页都不用抓
                    if ht > _cfg.WINDOW_END:
                        continue
                kept.append((title, href, ht))
            if page_ts and max(page_ts) < _cfg.WINDOW_START:
                stop = True               # 这一页最新的都早于窗口 → 时间倒排，后面更早
                break
            items = kept
            if not items:
                time.sleep(_cfg.KEY_SLEEP)
                continue

            for title, href, ht in items:
                ud = url_date(href)
                if ud and ud < (2026, 7, 1):
                    stop = True
                    break
                try:
                    id_ = reverse_url(href)
                except Exception:
                    id_ = href
                if storage.exist(id_):
                    continue
                if ud and ud > (2026, 10, 10):
                    continue
                try:
                    r = http(href, referer=cfg.get('domain'))
                    d_title, content, source, raw_time = parse_detail(r.text, href)
                except Exception as e:
                    print('  [%d] 详情失败 %s %s' % (ki, href, type(e).__name__))
                    continue
                # 语义兜底：不是文章就丢掉（宁可少，不可脏）
                if not looks_like_article(d_title or title, content):
                    storage.rejected.append('%s | 不像文章页(标题=%s)' % (href, (d_title or title)[:30]))
                    time.sleep(_cfg.DETAIL_SLEEP)
                    continue
                news = NewsEntity(
                    id=id_, title=d_title or title, content=content, from_=station,
                    publishtime=_cfg.to_publishtime(raw_time), raw_publishTime=raw_time,
                    fetchTime=int(time.time() * 1000), baseUrl=href, sourse=source)
                if storage.save(news):
                    print('  [%d/%d] +1 kw=%s | %s' % (ki, len(keys), kw, (d_title or title)[:40]))
                time.sleep(_cfg.DETAIL_SLEEP)
            if stop:
                break
            time.sleep(_cfg.KEY_SLEEP)
        if stop:
            continue
        time.sleep(_cfg.KEY_SLEEP)

    print('\n[%s] 新入库 %d, 已存在 %d, 校验拒绝 %d, 集合共 %d 条'
          % (station, storage.saved, storage.skipped_exists,
             len(storage.rejected), coll.count_documents({})))
    for r in storage.rejected[:8]:
        print('   拒绝: %s' % r)
