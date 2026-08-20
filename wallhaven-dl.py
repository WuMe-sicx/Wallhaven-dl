"""
wallhaven-dl.py：按可叠加的筛选轴批量下载 wallhaven 壁纸，并按预设分目录存盘。

模型见 docs/adr/0001-filter-axes-replace-exclusive-modes.md，词汇见 CONTEXT.md。

@author  Elvis Juan (xiaoxuyax@gmail.com)
@created 2016-06-26
"""

# PEP 723 内联依赖声明，供 `uv run wallhaven-dl.py` 使用。
# requires-python 卡在 3.11+ 是为了避开 macOS 自带的 3.9——它链 LibreSSL 2.8.3，
# urllib3 v2 不再支持，每次运行都会刷 NotOpenSSLWarning。对 pip 安装路径无影响。
# /// script
# requires-python = ">=3.11"
# dependencies = ["requests==2.32.5"]
# ///

import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import urllib.parse

import requests

def load_dotenv(path='.env'):
    """把 .env 的键值填进 os.environ，已存在的不覆盖——真 export 的优先。

    只认最朴素的 KEY=value：跳过空行与 # 注释，剥掉值两端引号。不做 $VAR 展开——
    那是 shell 的职责，装成支持会让人以为整套 shell 语法都可用（见 docs/adr/0005）。
    """
    if not os.path.exists(path):
        return
    with open(path, encoding='utf-8') as config:
        for line in config:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, value = line.partition('=')
            os.environ.setdefault(key.strip(), value.strip().strip('\'"'))


def normalize_lsky_url(raw):
    """把用户填的站点地址归一成 API 根地址；未配置返回空串。

    用户可能填尾随斜杠，也可能已经带上 /api/v2，两种都得认。
    """
    url = (raw or '').strip().rstrip('/')
    if not url:
        return ''
    return url if url.endswith('/api/v2') else url + '/api/v2'


load_dotenv()

API_ROOT = 'https://wallhaven.cc/api/v1/search'
DOWNLOAD_DIR = 'Wallhaven'
UNGROUPED_DIR = '未分组'
HTTP_TIMEOUT = 30

# apikey 不落源码：本文件受 git 跟踪，历史上已误提交过一次真实 key（见 e4f2a78）
APIKEY = os.environ.get('WALLHAVEN_API_KEY', '')

# wallhaven 限流窗口为每分钟 45 次请求，撞上后按整窗退避最省事
RATE_LIMIT_PAUSE = 60

# 兰空图床（仅新版 /api/v2 接口）。三项都空则不会出现「上传到图床」这一问
LSKY_URL = normalize_lsky_url(os.environ.get('LSKY_URL'))
LSKY_TOKEN = os.environ.get('LSKY_TOKEN', '').strip()
LSKY_PAGE_SIZE = 100
# 分页安全上限。兰空若不认 page 参数会每页都返回满页，没有这个就是死循环
LSKY_MAX_PAGES = 200

# 单张图失败重试次数。一次网络抖动不该让整张图静默丢掉
DOWNLOAD_RETRIES = 2
CHUNK_SIZE = 65536
BAR_WIDTH = 20

# 并发下载路数。图片走 w.wallhaven.cc，与 API 不同域，45/分钟的限流管不着它
WORKER_MENU = [('4', '稳妥'), ('8', '较快'), ('16', '最快，注意别把带宽占满')]
DEFAULT_WORKERS = 4

# 纯度按位或合并，因此多选即可表达任意组合；旧版的 ws/wn/sn 组合码已无存在必要
PURITY_MENU = [('sfw', '安全'), ('sketchy', '轻度'), ('nsfw', '成人（需 API key）')]
PURITY_BITS = {'sfw': '100', 'sketchy': '010', 'nsfw': '001'}

# 「最新」为空 dict：wallhaven 的默认排序就是 date_added，不必显式传。
# 「月榜」同理不传 topRange——sorting=toplist 默认即 1M（实测两者结果一致）。
SORTINGS = {
    '最新': {},
    '月榜': {'sorting': 'toplist'},
    '年榜': {'sorting': 'toplist', 'topRange': '1y'},
    '最多收藏': {'sorting': 'favorites'},
    '最多浏览': {'sorting': 'views'},
}

# 内容预设 = 拨动某条筛选轴的具名快捷方式。加一行即加一个预设，同轴冲突检测自动生效。
# 「风景」用关键词而非精确标签 id:711，理由见 docs/adr/0002。
PRESETS = {
    '动漫': {'categories': '010'},
    '风景': {'q': 'landscape'},
}

# 尺寸单选。portrait 与 landscape 的并集等于不筛选，单选让这种退化组合无从产生，
# 因此不需要任何退化检测代码——非法状态在结构上不可表达。
RATIOS = {'不限': {}, '手机端': {'ratios': 'portrait'}, '电脑端': {'ratios': 'landscape'}}
RATIO_MENU = [('手机端', '竖向'), ('电脑端', '横向')]

AXIS_LABELS = {'categories': '内容类别', 'q': '关键词', 'purity': '纯度',
               'ratios': '比例', 'sorting': '排序'}


def _merge_and(left, right):
    return left + ' ' + right


def _merge_or_bits(left, right):
    return ''.join('1' if a == '1' or b == '1' else '0' for a, b in zip(left, right))


def _merge_or_list(left, right):
    return left + ',' + right


# 未列出的轴（如 sorting）视为互斥，重复写入直接报错——wallhaven 遇到这种情况会静默取其一
AXIS_MERGE = {
    'q': _merge_and,
    'categories': _merge_or_bits,
    'purity': _merge_or_bits,
    'ratios': _merge_or_list,
}

PRESET_NAMES = list(PRESETS)
SORTING_NAMES = list(SORTINGS)



def _write_axis(params, axis, value, source):
    """把一个取值写进 params：同轴已有值就按算子合并，互斥轴重复写入抛 ValueError。

    所有轴写入都必须过这里。早先预设走合并、而尺寸/纯度/排序直接赋值覆盖，
    一旦有人往 PRESETS 加一个写 ratios 的预设，就会被静默盖掉——正是 wallhaven
    「静默取其一」那个坑的翻版，而 CLAUDE.md 恰恰承诺了「加预设别处不用改」。
    """
    if axis not in params:
        params[axis] = value
    elif axis in AXIS_MERGE:
        params[axis] = AXIS_MERGE[axis](params[axis], value)
    else:
        raise ValueError('「%s」与已选项都设定了 %s，该轴只能取一个值' % (source, axis))


def merge_presets(names):
    """把选中的预设合并成一组查询参数。"""
    params = {}
    for name in names:
        for axis, value in PRESETS[name].items():
            _write_axis(params, axis, value, name)
    return params


def purity_bits(names):
    """把纯度名字列表并成三位标志串。空列表取 sfw。"""
    bits = '000'
    for name in names or ['sfw']:
        bits = _merge_or_bits(bits, PURITY_BITS[name])
    return bits


def build_query(preset_names, ratio_name, keyword, purity_names, sorting_name):
    """把预设、自由关键词、纯度、排序合成一组查询参数。

    关键词与「风景」这类预设写同一条 q 轴，因此走 q 的合并算子（空格拼接，AND 语义），
    而不是互相覆盖。
    """
    params = merge_presets(preset_names)
    sources = [(ratio_name, RATIOS[ratio_name]),
               ('纯度', {'purity': purity_bits(purity_names)}),
               (sorting_name, SORTINGS[sorting_name])]
    if keyword:
        sources.insert(0, ('关键词', {'q': keyword}))
    for source, values in sources:
        for axis, value in values.items():
            _write_axis(params, axis, value, source)
    return params


def group_dir(preset_names, ratio_name):
    """落盘目录名：内容预设按菜单序拼接，尺寸接在末尾；全都没选时归入未分组。

    调用方保证 preset_names 已按菜单序排列，因此选 1,2 与选 2,1 落到同一个目录。
    """
    parts = list(preset_names) + ([ratio_name] if RATIOS[ratio_name] else [])
    return '+'.join(parts) if parts else UNGROUPED_DIR



def _human_size(size):
    for unit in ('B', 'KB', 'MB'):
        if size < 1024:
            return '%.1f %s' % (size, unit)
        size /= 1024
    return '%.1f GB' % size


def _human_time(seconds):
    return '%.1fs' % seconds if seconds < 60 else '%d分%02d秒' % divmod(int(seconds), 60)


class Progress:
    """并发下载的整体进度：百分比按已完成文件数算，速度按累计字节算。

    多个下载线程同时写它，因此所有更新都在锁内；刷新至多 10 次/秒，
    否则几路并发一起刷会把终端刷爆。非终端（管道、重定向）下完全静默。
    """

    def __init__(self, planned, workers):
        self.planned = max(planned, 1)
        self.workers = workers
        self.started = time.time()
        self._lock = threading.Lock()
        self._files = 0
        self._bytes = 0
        self._last_draw = 0.0

    def add_bytes(self, count):
        with self._lock:
            self._bytes += count
            self._draw()

    def finish_file(self):
        with self._lock:
            self._files += 1
            self._draw(force=True)

    @property
    def total_bytes(self):
        return self._bytes

    def clear(self):
        """把进度行擦干净，好让别的输出（失败提示、汇总）从干净的一行开始。"""
        if sys.stdout.isatty():
            sys.stdout.write('\r' + ' ' * 78 + '\r')
            sys.stdout.flush()

    def _draw(self, force=False):
        now = time.time()
        if not sys.stdout.isatty() or (not force and now - self._last_draw < 0.1):
            return
        self._last_draw = now
        percent = 100 * self._files // self.planned
        filled = int(BAR_WIDTH * self._files / self.planned)
        speed = _human_size(self._bytes / max(now - self.started, 1e-6)) + '/s'
        sys.stdout.write('\r[%d/%d] %s %3d%%  %s  %s  %d 路并发' % (
            self._files, self.planned, '█' * filled + '░' * (BAR_WIDTH - filled),
            percent, _human_size(self._bytes), speed, self.workers))
        sys.stdout.flush()


def _display_width(text):
    """中日韩与全角字符占两列。用于确认页对齐，阈值取近似值即可。"""
    return sum(2 if ord(char) > 0x2E7F else 1 for char in text)


def _pad(text, width):
    return text + ' ' * max(0, width - _display_width(text))


def ask_until_valid(prompt, valid, default=None):
    """反复提示直到输入落在 valid 内。default 非空时直接回车即取它。"""
    while True:
        answer = input(prompt).strip().lower()
        if default is not None and not answer:
            return default
        if answer in valid:
            return answer
        print('无效输入，可选：' + '、'.join(valid))


def ask_menu_indexes(prompt, entries, multiple, hint):
    """编号菜单提问，返回选中项的下标列表，已去重并按菜单序排序。

    entries 是 (名字, 说明) 对；说明为空串则该行只显示名字。
    空输入返回空列表——默认值由调用方决定，hint 只负责把它说清楚。
    """
    width = max(_display_width(name) for name, _ in entries)
    menu = '\n'.join('  %d %s' % (i + 1, _pad(name, width) + '  ' + note if note else name)
                     for i, (name, note) in enumerate(entries))
    while True:
        raw = input('%s（%s）：\n%s\n> ' % (prompt, hint, menu)).strip()
        if not raw:
            return []
        picks = [p.strip() for p in raw.split(',') if p.strip()] if multiple else [raw]
        if picks and all(p.isdigit() and 1 <= int(p) <= len(entries) for p in picks):
            return sorted({int(p) - 1 for p in picks})
        print('请输入 1-%d 之间的编号' % len(entries))


def ask_presets():
    """多选内容预设，返回按菜单序排列的名字列表。同轴冲突当场重问。"""
    entries = [(name, '') for name in PRESET_NAMES]
    while True:
        names = [PRESET_NAMES[i] for i in
                 ask_menu_indexes('选择内容', entries, True, '可多选，逗号分隔，回车跳过')]
        try:
            merge_presets(names)
        except ValueError as error:
            print('  ⚠ %s' % error)
            continue
        return names


def ask_ratio():
    picked = ask_menu_indexes('下载尺寸', RATIO_MENU, False, '回车=不限')
    return RATIO_MENU[picked[0]][0] if picked else '不限'


def ask_keyword():
    return input('额外关键词（回车跳过）: ').strip()


def ask_purity():
    """多选纯度，返回名字列表。无 API key 时挡住 nsfw。"""
    while True:
        names = [PURITY_MENU[i][0] for i in
                 ask_menu_indexes('纯度', PURITY_MENU, True, '可多选，逗号分隔，回车=sfw')]
        # 无 key 请求 nsfw 会静默返回 0 张，用户无从判断是没权限还是真没图；sketchy 无需 key
        if 'nsfw' in names and not APIKEY:
            print('  ⚠ nsfw 需要 API key，当前未设置 WALLHAVEN_API_KEY。设好再选，或改选其他。')
            continue
        return names or ['sfw']


def ask_sorting():
    entries = [(name, '') for name in SORTING_NAMES]
    picked = ask_menu_indexes('排序', entries, False, '回车=最新')
    return SORTING_NAMES[picked[0]] if picked else '最新'


def ask_workers():
    picked = ask_menu_indexes('并发数', WORKER_MENU, False, '回车=%d' % DEFAULT_WORKERS)
    return int(WORKER_MENU[picked[0]][0]) if picked else DEFAULT_WORKERS


def ask_upload():
    """问要不要传图床；要传则解析出储存 id。返回 None 表示不传。

    未配置 LSKY_URL / LSKY_TOKEN 时压根不问——与无 API key 时挡掉 nsfw 同一个道理，
    让用户对着一个注定失败的选项发呆没有意义。
    """
    if not (LSKY_URL and LSKY_TOKEN):
        return None
    if ask_until_valid('上传到图床（回车=否）[y/N]: ', ['y', 'n'], default='n') != 'y':
        return None
    configured = os.environ.get('LSKY_STORAGE_ID', '').strip()
    if configured.isdigit():
        return int(configured)
    storages = lsky_request('GET', '/group')['storages']
    if not storages:
        raise RuntimeError('图床账号下没有可用储存，先去后台配置一个')
    if len(storages) == 1:
        return storages[0]['id']
    # 多个储存时必须明确选：CONTEXT.md 的「储存」词条写明不能替用户默认挑，
    # 挑错会把整批图传到不想要的后端，而这一步事后无法从本地看出来
    entries = [(item['name'], item.get('provider', '')) for item in storages]
    while True:
        picked = ask_menu_indexes('储存', entries, False, '必选，无默认值')
        if picked:
            return storages[picked[0]]['id']
        print('  ⚠ 账号下有多个储存，必须明确选一个')


def ask_page_count(last_page):
    """问页数，上限取实际可下页数，因此不可能输出越界值。"""
    while True:
        raw = input('页数（1-%d，回车=1）: ' % last_page).strip()
        if not raw:
            return 1
        if raw.isdigit() and 1 <= int(raw) <= last_page:
            return int(raw)
        print('请输入 1-%d 之间的整数' % last_page)


ASKERS = {'presets': ask_presets, 'ratio': ask_ratio, 'keyword': ask_keyword,
          'purity': ask_purity, 'sorting': ask_sorting, 'workers': ask_workers,
          'upload': ask_upload}
ASK_ORDER = ['presets', 'ratio', 'keyword', 'purity', 'sorting', 'workers', 'upload']
STEP_LABELS = {'presets': '内容', 'ratio': '尺寸', 'keyword': '关键词', 'purity': '纯度',
               'sorting': '排序', 'workers': '并发数', 'upload': '图床', 'pages': '页数'}
# 少数答案直接 str() 出来没有意义，这里给它们各自的说法
STEP_DESCRIBERS = {'upload': lambda v: '不上传' if v is None else '上传（储存 #%d）' % v}
AMEND_KEYS = ASK_ORDER + ['pages']


def describe_answer(key, value):
    if key in STEP_DESCRIBERS:
        return STEP_DESCRIBERS[key](value)
    if not value:
        return '（无）'
    return '、'.join(value) if isinstance(value, list) else str(value)


def amend(answers):
    """让用户挑一项重问；返回 False 表示放弃。

    改动筛选相关项会让命中数与页数上限失效，故页数置空由调用方在取到新上限后重问。
    """
    entries = [(STEP_LABELS[key], describe_answer(key, answers[key])) for key in AMEND_KEYS]
    picked = ask_menu_indexes('要改哪一项', entries, False, '回车=放弃')
    if not picked:
        return False
    key = AMEND_KEYS[picked[0]]
    answers[key] = ASKERS[key]() if key in ASKERS else None
    return True


def confirmation_rows(answers, params):
    """确认页的行：轴中文名、人类可读取值、实际 API 参数。"""
    rows = []
    for name in answers['presets']:
        for axis, value in PRESETS[name].items():
            rows.append((AXIS_LABELS[axis], name, '%s=%s' % (axis, value)))
    if RATIOS[answers['ratio']]:
        rows.append((AXIS_LABELS['ratios'], answers['ratio'], 'ratios=' + params['ratios']))
    if answers['keyword']:
        rows.append((AXIS_LABELS['q'], answers['keyword'], 'q=' + params['q']))
    rows.append((AXIS_LABELS['purity'], '、'.join(answers['purity']), 'purity=' + params['purity']))
    sorting = SORTINGS[answers['sorting']]
    rows.append((AXIS_LABELS['sorting'], answers['sorting'],
                 ' '.join('%s=%s' % pair for pair in sorting.items()) or '（默认排序）'))
    rows.append(('并发数', '%d 路' % answers['workers'], ''))
    if answers['upload'] is not None:
        rows.append(('图床', '上传至相册「%s」'
                     % group_dir(answers['presets'], answers['ratio']), ''))
    return rows


def print_confirmation(answers, params, total, last_page):
    rows = confirmation_rows(answers, params)
    rows.append(('存至', os.path.join(DOWNLOAD_DIR, group_dir(answers['presets'], answers['ratio'])) + '/', ''))
    rows.append(('命中', '共 %d 张，%d 页可下' % (total, last_page), ''))
    label_width = max(_display_width(row[0]) for row in rows)
    # 只按带参数的行算宽度，否则末尾两行的长文案会把参数列推得很靠右
    value_width = max(_display_width(row[1]) for row in rows if row[2])

    print('\n───────── 确认 ─────────')
    for label, value, raw in rows:
        line = ' %s  %s' % (_pad(label, label_width), _pad(value, value_width) if raw else value)
        print((line + '  ' + raw).rstrip())
    print('────────────────────────')



def search_url(params, page):
    """把查询参数拼成第 page 页的搜索 URL。

    apikey 为空时照常请求：sfw 与 sketchy 都不需要 key，只有 nsfw 会拿不到结果。
    """
    query = dict(params, page=page)
    if APIKEY:
        query['apikey'] = APIKEY
    return API_ROOT + '?' + urllib.parse.urlencode(query)


def fetch_page(params, page):
    """取第 page 页搜索结果，返回 (原图 URL 列表, meta)。

    meta 带 total 与 last_page，确认流程据此报命中数；第 1 页会留给下载阶段复用，
    因此确认不额外消耗限流配额。

    撞上 429 时退避重试一次；其余 HTTP 失败与非 JSON 响应一律翻译成 RuntimeError，
    避免把 wallhaven 的错误页喂进 json 解析后炸成与病因无关的栈回溯。
    """
    url = search_url(params, page)
    response = requests.get(url, timeout=HTTP_TIMEOUT)

    if response.status_code == 429:
        print('触发 wallhaven 限流，%d 秒后重试第 %d 页' % (RATE_LIMIT_PAUSE, page))
        time.sleep(RATE_LIMIT_PAUSE)
        response = requests.get(url, timeout=HTTP_TIMEOUT)

    if response.status_code == 401:
        raise RuntimeError('apikey 无效或缺失：请设置环境变量 WALLHAVEN_API_KEY')
    if response.status_code != 200:
        raise RuntimeError('wallhaven 返回 HTTP %d（第 %d 页）' % (response.status_code, page))

    try:
        payload = json.loads(response.text)
        return [image['path'] for image in payload['data']], payload['meta']
    except (ValueError, KeyError, TypeError):
        raise RuntimeError('wallhaven 返回了非预期内容（第 %d 页）：%s'
                           % (page, response.text[:200]))


def _get_image(url):
    """取图片响应；网络异常或 5xx 时重试。返回响应，或 None 表示全部失败。

    4xx 不重试——图确实没了，重试只是白等。
    """
    response = None
    for attempt in range(DOWNLOAD_RETRIES):
        try:
            response = requests.get(url, stream=True, timeout=HTTP_TIMEOUT)
        except requests.RequestException:
            response = None
        if response is not None and response.status_code < 500:
            return response
        if attempt + 1 < DOWNLOAD_RETRIES:
            time.sleep(1)
    return response


def _lsky_headers():
    return {'Accept': 'application/json', 'Authorization': 'Bearer ' + LSKY_TOKEN}


def _lsky_pages(path, params):
    """逐页取兰空的分页接口，产出全部条目。

    以「本页条目数少于 per_page」判定结束，而不是去读 last_page：分页信封的嵌套层数
    官方文档没有钉死（meta 可能与 data 同级，也可能包着 data），读错位置不会报错，
    只会让翻页在第一页就停——那是静默截断，会让相册重复创建、去重失效。
    """
    for page in range(1, LSKY_MAX_PAGES + 1):
        payload = lsky_request('GET', path,
                               params=dict(params, page=page, per_page=LSKY_PAGE_SIZE))
        items = payload if isinstance(payload, list) else payload.get('data', [])
        for item in items:
            yield item
        if len(items) < LSKY_PAGE_SIZE:
            return
    print('警告：%s 翻页超过 %d 页仍未结束，可能是分页参数未生效' % (path, LSKY_MAX_PAGES))


def _lsky_send(method, path, **kwargs):
    """发一次请求。连不上就翻译成带地址的 RuntimeError——地址填错是最常见的配置事故。"""
    try:
        return requests.request(method, LSKY_URL + path, headers=_lsky_headers(),
                                timeout=HTTP_TIMEOUT, **kwargs)
    except requests.RequestException as error:
        raise RuntimeError('连不上图床 %s：%s' % (LSKY_URL, error.__class__.__name__))


def _lsky_check(response, what):
    """把兰空的失败响应翻译成 RuntimeError。上传与普通请求共用，免得两处各写一份。"""
    if response.status_code == 401:
        raise RuntimeError('LSKY_TOKEN 无效或已过期')
    if response.status_code >= 400:
        raise RuntimeError('图床返回 HTTP %d（%s）' % (response.status_code, what))


def lsky_request(method, path, **kwargs):
    """调兰空 API 并返回响应里的 data。429 退避重试一次，其余失败翻译成 RuntimeError。"""
    response = _lsky_send(method, path, **kwargs)
    if response.status_code == 429:
        print('图床限流，%d 秒后重试' % RATE_LIMIT_PAUSE)
        time.sleep(RATE_LIMIT_PAUSE)
        response = _lsky_send(method, path, **kwargs)
    _lsky_check(response, '%s %s' % (method, path))
    try:
        return json.loads(response.text).get('data')
    except ValueError:
        raise RuntimeError('图床返回了非预期内容（%s %s）：%s' % (method, path, response.text[:200]))


def lsky_album_id(name):
    """取同名相册的 id，没有就创建。

    拉全量在客户端精确比对 name：q 参数是精确还是模糊匹配文档没写，
    依赖它可能把「动漫」错认成「动漫+手机端」而挂错相册（见 docs/adr/0003）。
    """
    for album in _lsky_pages('/user/albums', {}):
        if album['name'] == name:
            return album['id']
    return lsky_request('POST', '/user/albums', json={'name': name, 'is_public': '0'})['id']


def lsky_album_filenames(album_id):
    """列出相册里已有的文件名。wallhaven 的文件名本身就是唯一 ID，够用来去重。"""
    return {item['filename'] for item in _lsky_pages('/user/photos', {'album_id': album_id})}


def lsky_upload(path, storage_id, album_id):
    """上传一张图，返回图片 id。

    只在 429 时重试——那是请求被拒绝、根本没被处理。其余失败一律不重试：
    POST /upload 不幂等，而漏传的下次运行会被文件名去重发现并补上（见 docs/adr/0004）。
    """
    fields = {'storage_id': str(storage_id), 'album_id': str(album_id), 'is_public': '0'}
    for attempt in range(2):
        with open(path, 'rb') as image:
            response = _lsky_send('POST', '/upload', data=fields,
                                  files={'file': (os.path.basename(path), image)})
        if response.status_code != 429 or attempt:
            break
        time.sleep(RATE_LIMIT_PAUSE)
    _lsky_check(response, '上传 ' + os.path.basename(path))
    try:
        return json.loads(response.text)['data']['id']
    except (ValueError, KeyError, TypeError):
        # 必须翻译成 RuntimeError：调用方只捕获它，别的异常会穿透 pool.map 让整批中断，
        # 而 docs/adr/0004 说好了「单次运行结束时可能有若干张显示为失败，这是正常的」
        raise RuntimeError('上传响应非预期：%s' % response.text[:120])


def upload_directory(directory, album_name, storage_id, workers):
    """把 directory 里还没传过的图上传到同名相册，返回 (已传, 跳过, 失败)。

    上传范围是目录里的全部文件，不只本次新下的——这样以前没开上传时下的、
    以及上次传失败的，都会在这里被补上。
    """
    album_id = lsky_album_id(album_name)
    existing = lsky_album_filenames(album_id)
    local = sorted(name for name in os.listdir(directory) if not name.endswith('.part'))
    pending = [name for name in local if name not in existing]
    print('图床相册「%s」：已有 %d 张，本地 %d 张，待传 %d 张'
          % (album_name, len(existing), len(local), len(pending)))
    if not pending:
        return 0, len(local), 0

    progress = Progress(len(pending), workers)
    failures = []

    def push(name):
        path = os.path.join(directory, name)
        try:
            lsky_upload(path, storage_id, album_id)
        except RuntimeError as error:
            progress.finish_file()
            return name, str(error)
        progress.add_bytes(os.path.getsize(path))
        progress.finish_file()
        return name, ''

    results = _run_concurrently(pending, push, workers, progress)
    progress.clear()
    for name, detail in results:
        if detail:
            failures.append('%s（%s）' % (name, detail))
            print('上传失败：%s（%s）' % (name, detail))
    return len(results) - len(failures), len(local) - len(pending), len(failures)


def save(url, directory, on_bytes=None):
    """下载一张图到 directory，返回 (状态, 字节数, 说明)。

    状态取 downloaded / exists / failed；说明只在失败时非空。on_bytes 每写入一块被调用
    一次，供调用方累计进度——本函数自己不打印任何东西，因为并发下多个线程一起 print
    会把进度行冲烂。

    先写 .part 再 os.replace：早先直接以追加模式写目标文件，中断留下的半张图
    会被下次运行的「已存在」判断当成完成品，永远补不全。
    """
    path = os.path.join(directory, os.path.basename(url))
    if os.path.exists(path):
        return 'exists', 0, ''

    response = _get_image(url)
    if response is None or response.status_code != 200:
        return 'failed', 0, '连接失败' if response is None else 'HTTP %d' % response.status_code

    partial = path + '.part'
    done = 0
    try:
        with open(partial, 'wb') as image_file:
            for chunk in response.iter_content(CHUNK_SIZE):
                image_file.write(chunk)
                done += len(chunk)
                if on_bytes:
                    on_bytes(len(chunk))
        os.replace(partial, path)
    except BaseException:
        # Ctrl-C 或写盘失败都不该留下垃圾 .part——它不会被「已存在」判断挡住，
        # 但会永远躺在目录里，除非恰好重下同一张图
        if os.path.exists(partial):
            os.remove(partial)
        raise
    return 'downloaded', done, ''


def _run_concurrently(items, worker, workers, progress):
    """并发跑 worker(item) 并按原顺序返回结果。Ctrl-C 时取消余下任务并擦掉进度行。"""
    with ThreadPoolExecutor(max_workers=workers) as pool:
        try:
            return list(pool.map(worker, items))
        except KeyboardInterrupt:
            pool.shutdown(wait=False, cancel_futures=True)
            progress.clear()
            raise


def download(params, directory, pages, first_page, planned, workers):
    """下载前 pages 页，每页内 workers 路并发。

    first_page 是确认阶段已取到的第 1 页，直接复用不再请求。
    按页分批：一页下完再取下一页，省得为大页数把全部 URL 先攒在内存里，
    也让翻页请求分散开，不至于一口气撞上 API 的 45/分钟限流。
    """
    os.makedirs(directory, exist_ok=True)
    progress = Progress(planned, workers)
    tally = {'downloaded': 0, 'exists': 0, 'failed': 0}
    failures = []

    def fetch_one(url):
        status, size, detail = save(url, directory, progress.add_bytes)
        progress.finish_file()
        return status, os.path.basename(url), detail

    for page in range(1, pages + 1):
        page_urls = first_page if page == 1 else fetch_page(params, page)[0]
        results = _run_concurrently(page_urls, fetch_one, workers, progress)
        for status, name, detail in results:
            tally[status] += 1
            if status == 'failed':
                failures.append('%s（%s）' % (name, detail))

    progress.clear()
    elapsed = time.time() - progress.started
    total = sum(tally.values())
    speed = _human_size(progress.total_bytes / max(elapsed, 1e-6)) + '/s'
    for failure in failures:
        print('下载失败：%s' % failure)
    print('完成：共 %d 张，新增 %d、已存在 %d、失败 %d' %
          (total, tally['downloaded'], tally['exists'], tally['failed']))
    print('      %s，用时 %s，平均 %s（%d 路并发）' %
          (_human_size(progress.total_bytes), _human_time(elapsed), speed, workers))
    print('      保存于 %s/' % directory)


def main():
    answers = {'pages': None}
    for key in ASK_ORDER:
        answers[key] = ASKERS[key]()

    while True:
        params = build_query(answers['presets'], answers['ratio'], answers['keyword'],
                             answers['purity'], answers['sorting'])
        first_page, meta = fetch_page(params, 1)
        total, last_page = meta['total'], meta['last_page']

        # total 为 0 时 last_page 仍是 1，照问会得到「页数（1-1）」这种没有意义的提示
        if total == 0:
            print('\n  命中 0 张 —— 当前筛选没有匹配结果')
        else:
            print('\n  命中 %d 张，最多 %d 页' % (total, last_page))
            if answers['pages'] is None:
                answers['pages'] = ask_page_count(last_page)
            elif answers['pages'] > last_page:
                print('  原定 %d 页超出上限，收紧为 %d 页' % (answers['pages'], last_page))
                answers['pages'] = last_page

            print_confirmation(answers, params, total, last_page)
            if ask_until_valid('开始下载？[Y/n]: ', ['y', 'n'], default='y') == 'y':
                break

        if not amend(answers):
            raise SystemExit('已取消')

    album_name = group_dir(answers['presets'], answers['ratio'])
    directory = os.path.join(DOWNLOAD_DIR, album_name)
    download(params, directory, answers['pages'], first_page,
             min(total, answers['pages'] * meta['per_page']), answers['workers'])

    if answers['upload'] is not None:
        uploaded, skipped, failed = upload_directory(directory, album_name,
                                                     answers['upload'], answers['workers'])
        print('图床：新增 %d、已存在 %d、失败 %d' % (uploaded, skipped, failed))


if __name__ == '__main__':
    try:
        main()
    except RuntimeError as error:
        raise SystemExit(error)
    except (KeyboardInterrupt, EOFError):
        # Ctrl-C 与 Ctrl-D 都是正常的退出方式，不该甩栈回溯
        raise SystemExit('\n已取消')
