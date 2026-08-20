"""
test_wallhaven_dl.py：wallhaven-dl 自检。

覆盖轴合并、退化组合识别、目录分组、输入校验、URL 拼装、API 错误翻译与原子落盘。
脚本文件名带连字符不能直接 import，故用 importlib 按路径加载。

@author  Elvis Juan (xiaoxuyax@gmail.com)
@created 2026-08-20
"""

# PEP 723 内联依赖声明，供 `uv run test_wallhaven_dl.py` 使用。理由见 wallhaven-dl.py 同处注释。
# /// script
# requires-python = ">=3.11"
# dependencies = ["requests==2.32.5"]
# ///

import builtins
import importlib.util
import json
import os
import tempfile

_spec = importlib.util.spec_from_file_location(
    'wallhaven_dl', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'wallhaven-dl.py'))
wd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wd)


PAGE_JSON = '{"data":[{"path":"http://x/a.jpg"}],"meta":{"total":1,"last_page":1}}'


class FakeResponse:
    def __init__(self, status_code=200, text='', body=b''):
        self.status_code = status_code
        self.text = text
        self._body = body
        self.headers = {'Content-Length': str(len(body))} if body else {}

    def iter_content(self, size):
        for i in range(0, len(self._body), size):
            yield self._body[i:i + size]


def fake_get(responses):
    """按调用顺序吐出预设响应，并记录收到的 URL。"""
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return responses[len(calls) - 1]

    get.calls = calls
    return get


def fake_input(answers):
    it = iter(answers)
    return lambda _prompt: next(it)


def with_input(answers, call):
    original = builtins.input
    builtins.input = fake_input(answers)
    try:
        return call()
    finally:
        builtins.input = original


# ───────────────────────── 轴合并 ─────────────────────────

def test_presets_on_different_axes_stack():
    assert wd.merge_presets(['动漫', '风景']) == {'categories': '010', 'q': 'landscape'}


def test_q_axis_merges_with_space():
    """风景预设与自由关键词写同一条 q 轴，按 AND 拼接而非互相覆盖（ADR-0002）。"""
    params = wd.build_query(['风景'], '不限', 'sunset', ['sfw'], '最新')
    assert params['q'] == 'landscape sunset', params


def test_keyword_alone_sets_q():
    assert wd.build_query([], '不限', 'sunset', ['sfw'], '最新')['q'] == 'sunset'


def test_categories_axis_merges_by_bitwise_or():
    """位或语义有实测依据：anime 162645 + people 124354 = 011 的 286999。"""
    assert wd._merge_bitmask('010', '001') == '011'
    assert wd._merge_bitmask('110', '011') == '111'


def test_ratios_axis_merges_by_comma():
    assert wd._merge_comma('16x9', '16x10') == '16x9,16x10'


def test_exclusive_axis_raises_instead_of_overwriting():
    """未声明合并算子的轴重复写入必须报错——wallhaven 遇到这种情况会静默取其一。"""
    original = dict(wd.PRESETS)
    try:
        wd.PRESETS['月榜'] = {'sorting': 'toplist'}
        wd.PRESETS['年榜'] = {'sorting': 'favorites'}
        assert_raises_containing(lambda: wd.merge_presets(['月榜', '年榜']), 'sorting')
    finally:
        wd.PRESETS.clear()
        wd.PRESETS.update(original)


def test_sorting_is_not_a_mergeable_axis():
    """sorting 不在算子表里，这正是它互斥的原因。"""
    assert 'sorting' not in wd.AXIS_MERGE


# ───────────────────────── 尺寸轴 ─────────────────────────

def test_ratio_is_single_select_so_the_degenerate_union_cannot_occur():
    """portrait+landscape 的并集等于不筛选；尺寸单选让这个组合在结构上无法表达。"""
    assert wd.RATIO_MENU == [('手机端', '竖向'), ('电脑端', '横向')]
    assert not hasattr(wd, 'degenerate_axis'), '尺寸单选后不该再留退化检测代码'
    assert with_input(['1'], wd.ask_ratio) == '手机端'
    assert with_input(['2'], wd.ask_ratio) == '电脑端'


def test_ratio_defaults_to_unrestricted():
    assert with_input([''], wd.ask_ratio) == '不限'
    assert wd.RATIOS['不限'] == {}


def test_ratio_is_independent_of_content():
    """不选内容仍能只按尺寸筛——这是「总是问尺寸」而非级联的直接结果。"""
    assert wd.build_query([], '手机端', '', ['sfw'], '最新')['ratios'] == 'portrait'
    assert wd.group_dir([], '手机端') == '手机端'


# ───────────────────────── 分组目录 ─────────────────────────

def test_group_dir_is_order_independent():
    """选 1,2 与选 2,1 必须落到同一个目录，否则会长出重复目录。"""
    assert with_input(['1,2'], wd.ask_presets) == with_input(['2,1'], wd.ask_presets)
    assert wd.group_dir(['动漫'], '手机端') == '动漫+手机端'


def test_group_dir_dedupes_repeated_picks():
    assert with_input(['1,1,2'], wd.ask_presets) == ['动漫', '风景']


def test_group_dir_omits_unrestricted_ratio():
    assert wd.group_dir(['动漫'], '不限') == '动漫'
    assert wd.group_dir([], '不限') == wd.UNGROUPED_DIR


# ───────────────────────── 提问 ─────────────────────────

def test_ask_menu_rejects_out_of_range_then_accepts():
    entries = [(n, '') for n in wd.SORTING_NAMES]
    assert with_input(['9', 'abc', '2'], lambda: wd.ask_menu_indexes('x', entries, False, 'h')) == [1]


def test_empty_input_skips_menu():
    assert with_input([''], wd.ask_presets) == []
    assert with_input([''], wd.ask_sorting) == '最新'


def test_ask_sorting_maps_to_api_params():
    """「最新」不传 sorting——wallhaven 默认就是 date_added。"""
    assert wd.SORTINGS[with_input(['1'], wd.ask_sorting)] == {}
    assert wd.SORTINGS[with_input(['2'], wd.ask_sorting)] == {'sorting': 'toplist'}
    assert wd.SORTINGS[with_input(['3'], wd.ask_sorting)] == {'sorting': 'toplist', 'topRange': '1y'}


def test_ask_purity_defaults_to_sfw():
    assert with_input([''], wd.ask_purity) == ['sfw']
    assert wd.purity_bits(with_input([''], wd.ask_purity)) == '100'


def test_purity_multi_select_replaces_combo_codes():
    """旧的 ws = sfw+sketchy，现在靠多选表达，位或得到同一个 110。"""
    assert wd.purity_bits(with_input(['1,2'], wd.ask_purity)) == '110'
    assert wd.purity_bits(['sfw', 'sketchy', 'nsfw']) == '111'


def test_retired_combo_codes_are_gone():
    assert 'ws' not in wd.PURITY_BITS and 'wn' not in wd.PURITY_BITS


def test_ask_purity_blocks_nsfw_without_key():
    """无 key 请求 nsfw 会静默返回 0 张，必须在发请求前拦住。"""
    wd.APIKEY = ''
    assert with_input(['3', '2'], wd.ask_purity) == ['sketchy'], 'sketchy 无 key 可用，不该被拦'
    wd.APIKEY = 'SECRET'
    assert with_input(['3'], wd.ask_purity) == ['nsfw']
    wd.APIKEY = ''


def test_ask_page_count_rejects_non_numeric_and_out_of_range():
    assert with_input(['abc', '0', '-3', '99', '2'], lambda: wd.ask_page_count(5)) == 2


def test_ask_page_count_defaults_to_one():
    assert with_input([''], lambda: wd.ask_page_count(5)) == 1


# ───────────────────────── URL 与 API ─────────────────────────

def test_search_url():
    wd.APIKEY = ''
    url = wd.search_url({'q': 'hello world'}, 2)
    assert 'q=hello+world' in url, url
    assert 'page=2' in url, url
    assert 'apikey' not in url, 'apikey 为空时不应出现在 URL 里'

    wd.APIKEY = 'SECRET'
    assert 'apikey=SECRET' in wd.search_url({'q': 'x'}, 1)
    wd.APIKEY = ''


def test_full_query_reaches_url():
    wd.APIKEY = ''
    params = wd.build_query(['动漫'], '手机端', 'sunset', ['sfw'], '月榜')
    url = wd.search_url(params, 1)
    for fragment in ['categories=010', 'ratios=portrait', 'q=sunset', 'purity=100', 'sorting=toplist']:
        assert fragment in url, '%s 不在 %s' % (fragment, url)


def test_fetch_page_translates_failures():
    original = wd.requests.get
    try:
        wd.requests.get = fake_get([FakeResponse(401)])
        assert_raises_containing(lambda: wd.fetch_page({}, 1), 'WALLHAVEN_API_KEY')

        wd.requests.get = fake_get([FakeResponse(503)])
        assert_raises_containing(lambda: wd.fetch_page({}, 1), 'HTTP 503')

        wd.requests.get = fake_get([FakeResponse(200, text='<html>error</html>')])
        assert_raises_containing(lambda: wd.fetch_page({}, 1), '非预期内容')

        wd.requests.get = fake_get([FakeResponse(200, text='{"foo": 1}')])
        assert_raises_containing(lambda: wd.fetch_page({}, 1), '非预期内容')

        wd.requests.get = fake_get([FakeResponse(200, text=PAGE_JSON)])
        urls, meta = wd.fetch_page({}, 1)
        assert urls == ['http://x/a.jpg']
        assert (meta['total'], meta['last_page']) == (1, 1)
    finally:
        wd.requests.get = original


def test_fetch_page_retries_on_rate_limit():
    original_get, original_sleep = wd.requests.get, wd.time.sleep
    slept = []
    try:
        wd.time.sleep = slept.append
        wd.requests.get = fake_get([
            FakeResponse(429),
            FakeResponse(200, text=PAGE_JSON),
        ])
        assert wd.fetch_page({}, 1)[0] == ['http://x/a.jpg']
        assert slept == [wd.RATE_LIMIT_PAUSE], slept
    finally:
        wd.requests.get, wd.time.sleep = original_get, original_sleep


# ───────────────────────── 落盘 ─────────────────────────

def test_save_is_atomic_and_recovers_from_partial():
    original = wd.requests.get
    try:
        with tempfile.TemporaryDirectory() as directory:
            wd.requests.get = fake_get([FakeResponse(200, body=b'IMAGE-BYTES')])
            assert wd.save('http://x/a.jpg', directory) == ('downloaded', 11, '')
            target = os.path.join(directory, 'a.jpg')
            assert open(target, 'rb').read() == b'IMAGE-BYTES'
            assert not os.path.exists(target + '.part'), '成功后不应留下 .part'

            # 已存在则跳过，不发请求
            wd.requests.get = fake_get([])
            assert wd.save('http://x/a.jpg', directory) == ('exists', 0, '')

            # 上次中断留下的半张图不得阻塞重下
            with open(os.path.join(directory, 'b.jpg.part'), 'wb') as f:
                f.write(b'HALF')
            wd.requests.get = fake_get([FakeResponse(200, body=b'FULL-BYTES')])
            assert wd.save('http://x/b.jpg', directory)[0] == 'downloaded'
            assert open(os.path.join(directory, 'b.jpg'), 'rb').read() == b'FULL-BYTES'

            # 下载失败不得留下空壳文件
            wd.requests.get = fake_get([FakeResponse(404)])
            assert wd.save('http://x/c.jpg', directory) == ('failed', 0, 'HTTP 404')
            assert not os.path.exists(os.path.join(directory, 'c.jpg'))
    finally:
        wd.requests.get = original


def test_save_is_scoped_to_its_group_dir():
    """跨分组不去重：同一张图在另一个分组目录下要重新落盘（ADR-0001）。"""
    original = wd.requests.get
    try:
        with tempfile.TemporaryDirectory() as root:
            one, two = os.path.join(root, '动漫'), os.path.join(root, '动漫+手机端')
            os.makedirs(one)
            os.makedirs(two)
            wd.requests.get = fake_get([FakeResponse(200, body=b'X'), FakeResponse(200, body=b'X')])
            assert wd.save('http://x/a.jpg', one)[0] == 'downloaded'
            assert wd.save('http://x/a.jpg', two)[0] == 'downloaded'
    finally:
        wd.requests.get = original


def test_interrupt_removes_the_partial_file():
    """Ctrl-C 落在写盘中途时，.part 不该留下来当垃圾——它不会被「已存在」挡住。"""
    original = wd.requests.get

    class Interrupting(FakeResponse):
        def iter_content(self, size):
            yield b'HALF'
            raise KeyboardInterrupt

    try:
        with tempfile.TemporaryDirectory() as directory:
            wd.requests.get = fake_get([Interrupting(200, body=b'IGNORED')])
            try:
                wd.save('http://x/a.jpg', directory)
                raise AssertionError('KeyboardInterrupt 应当继续上抛')
            except KeyboardInterrupt:
                pass
            assert os.listdir(directory) == [], os.listdir(directory)
    finally:
        wd.requests.get = original


def test_transient_failure_is_retried():
    """5xx 与连接异常各重试一次；4xx 不重试——图确实没了，白等。"""
    original_get, original_sleep = wd.requests.get, wd.time.sleep
    try:
        wd.time.sleep = lambda _s: None

        getter = fake_get([FakeResponse(500), FakeResponse(200, body=b'OK')])
        wd.requests.get = getter
        with tempfile.TemporaryDirectory() as directory:
            assert wd.save('http://x/a.jpg', directory)[0] == 'downloaded'
        assert len(getter.calls) == 2, '5xx 应当重试'

        getter = fake_get([FakeResponse(404), FakeResponse(200, body=b'OK')])
        wd.requests.get = getter
        with tempfile.TemporaryDirectory() as directory:
            assert wd.save('http://x/a.jpg', directory)[0] == 'failed'
        assert len(getter.calls) == 1, '4xx 不该重试'
    finally:
        wd.requests.get, wd.time.sleep = original_get, original_sleep


def test_ask_workers_offers_4_8_16():
    assert [name for name, _ in wd.WORKER_MENU] == ['4', '8', '16']
    assert with_input([''], wd.ask_workers) == 4
    assert with_input(['2'], wd.ask_workers) == 8
    assert with_input(['3'], wd.ask_workers) == 16


def test_save_reports_bytes_through_the_callback():
    """并发下 save 不打印，只回调字节数——多线程一起 print 会把进度行冲烂。"""
    original = wd.requests.get
    chunks = []
    try:
        with tempfile.TemporaryDirectory() as directory:
            wd.requests.get = fake_get([FakeResponse(200, body=b'A' * 200000)])
            status, size, _ = wd.save('http://x/a.jpg', directory, chunks.append)
            assert (status, size) == ('downloaded', 200000)
            assert sum(chunks) == 200000, chunks
            assert len(chunks) > 1, '大文件应当分多块回调，否则进度是跳变的'
    finally:
        wd.requests.get = original


def test_progress_is_thread_safe_and_silent_off_tty():
    """多线程并发累加不能丢计数；非终端下不该往 stdout 写任何东西。"""
    import io, threading, contextlib
    progress = wd.Progress(planned=100, workers=8)
    captured = io.StringIO()

    def worker():
        for _ in range(500):
            progress.add_bytes(10)
        progress.finish_file()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    with contextlib.redirect_stdout(captured):
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert progress.total_bytes == 8 * 500 * 10, progress.total_bytes
    assert captured.getvalue() == '', '非 tty 下不该有进度输出'


def test_human_size_and_time():
    assert wd._human_size(512) == '512.0 B'
    assert wd._human_size(1536) == '1.5 KB'
    assert wd._human_size(3 * 1024 ** 2) == '3.0 MB'
    assert wd._human_time(4.25) == '4.2s'
    assert wd._human_time(125) == '2分05秒'


# ───────────────────────── 确认与修改 ─────────────────────────

def sample_answers():
    return {'presets': ['动漫'], 'ratio': '手机端', 'keyword': 'sunset', 'purity': ['sfw'],
            'sorting': '月榜', 'workers': 4, 'upload': None, 'pages': 2}


def test_amend_reasks_only_the_chosen_step():
    """选 4（排序）应只改排序，其余四项原样保留。"""
    answers = sample_answers()
    with_input(['5', '1'], lambda: wd.amend(answers))
    assert answers['sorting'] == '最新'
    assert answers['presets'] == ['动漫']
    assert answers['ratio'] == '手机端'
    assert answers['keyword'] == 'sunset'
    assert answers['pages'] == 2


def test_amend_clears_pages_so_the_new_ceiling_applies():
    """选「页数」置空，由 main 取到新上限后再问，避免沿用越界的旧值。"""
    answers = sample_answers()
    with_input(['8'], lambda: wd.amend(answers))
    assert answers['pages'] is None


def test_amend_keeps_pages_when_filters_change():
    """改筛选项不清空页数——超出新上限时由 main 收紧，不多问一次。"""
    answers = sample_answers()
    with_input(['1', '2'], lambda: wd.amend(answers))
    assert answers['presets'] == ['风景']
    assert answers['pages'] == 2


def test_amend_returns_false_on_empty_input():
    assert with_input([''], lambda: wd.amend(sample_answers())) is False


def test_confirmation_shows_preset_and_raw_param():
    """确认页要同时给出人类可读取值与实际 API 参数，第 5 项摩擦就是看不到后者。"""
    answers = sample_answers()
    params = wd.build_query(answers['presets'], answers['ratio'], answers['keyword'],
                            answers['purity'], answers['sorting'])
    rows = wd.confirmation_rows(answers, params)
    assert ('内容类别', '动漫', 'categories=010') in rows, rows
    assert ('比例', '手机端', 'ratios=portrait') in rows, rows
    assert ('排序', '月榜', 'sorting=toplist') in rows, rows


def test_confirmation_shows_merged_q_for_landscape_plus_keyword():
    answers = dict(sample_answers(), presets=['风景'], ratio='不限',
                   keyword='sunset', sorting='最新')
    params = wd.build_query(answers['presets'], answers['ratio'], answers['keyword'],
                            answers['purity'], answers['sorting'])
    rows = wd.confirmation_rows(answers, params)
    # 两行都在：预设贡献的 q，以及合并后的 q。摊开来正好显示合并是怎么发生的
    assert ('关键词', '风景', 'q=landscape') in rows, rows
    assert ('关键词', 'sunset', 'q=landscape sunset') in rows, rows
    assert ('排序', '最新', '（默认排序）') in rows, rows


# ───────────────────────── 配置（.env） ─────────────────────────

def test_dotenv_parsing_and_precedence():
    """已 export 的真环境变量优先；注释、空行、引号都要处理（ADR-0005）。"""
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, '.env')
        with open(path, 'w', encoding='utf-8') as config:
            config.write('# 注释\n\n'
                         'T_PLAIN=abc\n'
                         'T_QUOTED="有引号"\n'
                         "T_SINGLE='单引号'\n"
                         'T_SPACED =  两侧空格  \n'
                         'T_ALREADY_SET=来自文件\n'
                         '没有等号的行\n')
        os.environ['T_ALREADY_SET'] = '来自 export'
        for key in ('T_PLAIN', 'T_QUOTED', 'T_SINGLE', 'T_SPACED'):
            os.environ.pop(key, None)
        try:
            wd.load_dotenv(path)
            assert os.environ['T_PLAIN'] == 'abc'
            assert os.environ['T_QUOTED'] == '有引号'
            assert os.environ['T_SINGLE'] == '单引号'
            assert os.environ['T_SPACED'] == '两侧空格'
            assert os.environ['T_ALREADY_SET'] == '来自 export', 'export 必须压过 .env'
        finally:
            for key in ('T_PLAIN', 'T_QUOTED', 'T_SINGLE', 'T_SPACED', 'T_ALREADY_SET'):
                os.environ.pop(key, None)


def test_dotenv_missing_file_is_fine():
    wd.load_dotenv('/nonexistent/.env')


def test_normalize_lsky_url():
    assert wd.normalize_lsky_url('https://img.example.com') == 'https://img.example.com/api/v2'
    assert wd.normalize_lsky_url('https://img.example.com/') == 'https://img.example.com/api/v2'
    assert wd.normalize_lsky_url('https://img.example.com/api/v2') == 'https://img.example.com/api/v2'
    assert wd.normalize_lsky_url('  https://img.example.com/api/v2/  ') == 'https://img.example.com/api/v2'
    assert wd.normalize_lsky_url('') == ''
    assert wd.normalize_lsky_url(None) == ''


# ───────────────────────── 图床 ─────────────────────────

class FakeLsky:
    """按 (method, path) 记录调用并吐出预设响应。"""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def request(self, method, url, **kwargs):
        path = url.split('/api/v2', 1)[1]
        self.calls.append((method, path, kwargs.get('params') or kwargs.get('json')))
        status, body = self.routes[(method, path.split('?')[0])]
        return FakeResponse(status, text=json.dumps(body))


def with_lsky(routes, call):
    original_request, original_url, original_token = wd.requests.request, wd.LSKY_URL, wd.LSKY_TOKEN
    fake = FakeLsky(routes)
    wd.requests.request = fake.request
    wd.LSKY_URL, wd.LSKY_TOKEN = 'https://img.example.com/api/v2', 'TOK'
    try:
        return call(), fake
    finally:
        wd.requests.request, wd.LSKY_URL, wd.LSKY_TOKEN = original_request, original_url, original_token


def test_album_is_reused_not_recreated():
    """同名相册已存在就复用；文档没说 name 唯一，重复建会长出重名相册。"""
    routes = {('GET', '/user/albums'): (200, {'data': {
        'data': [{'id': 7, 'name': '动漫+手机端'}, {'id': 8, 'name': '风景'}],
        'meta': {'last_page': 1}}})}
    album_id, fake = with_lsky(routes, lambda: wd.lsky_album_id('动漫+手机端'))
    assert album_id == 7
    assert not any(m == 'POST' for m, _, _ in fake.calls), '已存在时不该再建'


def test_album_is_created_when_absent():
    routes = {
        ('GET', '/user/albums'): (200, {'data': {'data': [{'id': 8, 'name': '风景'}],
                                                 'meta': {'last_page': 1}}}),
        ('POST', '/user/albums'): (200, {'data': {'id': 9}}),
    }
    album_id, fake = with_lsky(routes, lambda: wd.lsky_album_id('动漫+手机端'))
    assert album_id == 9
    created = [payload for method, _, payload in fake.calls if method == 'POST'][0]
    assert created['name'] == '动漫+手机端'


def test_album_name_match_is_exact_not_substring():
    """「动漫」不能被「动漫+手机端」顶替——q 的匹配语义没文档化，只能客户端精确比。"""
    routes = {
        ('GET', '/user/albums'): (200, {'data': {'data': [{'id': 7, 'name': '动漫+手机端'}],
                                                 'meta': {'last_page': 1}}}),
        ('POST', '/user/albums'): (200, {'data': {'id': 11}}),
    }
    album_id, _ = with_lsky(routes, lambda: wd.lsky_album_id('动漫'))
    assert album_id == 11, '应当新建「动漫」，而不是复用「动漫+手机端」'


def test_existing_filenames_are_collected():
    routes = {('GET', '/user/photos'): (200, {'data': {
        'data': [{'filename': 'wallhaven-a.jpg'}, {'filename': 'wallhaven-b.jpg'}],
        'meta': {'last_page': 1}}})}
    names, _ = with_lsky(routes, lambda: wd.lsky_album_filenames(7))
    assert names == {'wallhaven-a.jpg', 'wallhaven-b.jpg'}


def test_paged_accepts_both_nestings():
    """兰空分页响应的嵌套层数文档没钉死，两种都得认。"""
    assert wd._paged([{'id': 1}]) == ([{'id': 1}], {})
    assert wd._paged({'data': [{'id': 1}], 'meta': {'last_page': 2}}) == ([{'id': 1}], {'last_page': 2})


def test_lsky_translates_auth_and_http_failures():
    for status, fragment in [(401, 'LSKY_TOKEN'), (500, 'HTTP 500')]:
        routes = {('GET', '/group'): (status, {})}
        try:
            with_lsky(routes, lambda: wd.lsky_request('GET', '/group'))
            raise AssertionError('期望抛出 RuntimeError')
        except RuntimeError as error:
            assert fragment in str(error), (status, str(error))


def test_unreachable_host_is_translated():
    """地址填错是最常见的配置事故，不该甩 ConnectionError 栈回溯。"""
    original_request, original_url, original_token = wd.requests.request, wd.LSKY_URL, wd.LSKY_TOKEN

    def boom(*args, **kwargs):
        raise wd.requests.ConnectionError('nope')

    try:
        wd.requests.request = boom
        wd.LSKY_URL, wd.LSKY_TOKEN = 'https://img.example.com/api/v2', 'TOK'
        assert_raises_containing(lambda: wd.lsky_request('GET', '/group'), '连不上图床')
    finally:
        wd.requests.request, wd.LSKY_URL, wd.LSKY_TOKEN = original_request, original_url, original_token


def test_upload_prompt_is_skipped_without_config():
    """未配置图床时压根不问——与无 API key 时挡掉 nsfw 同一个道理。"""
    original_url, original_token = wd.LSKY_URL, wd.LSKY_TOKEN
    try:
        wd.LSKY_URL, wd.LSKY_TOKEN = '', ''
        assert with_input([], wd.ask_upload) is None, '不该读取任何输入'
    finally:
        wd.LSKY_URL, wd.LSKY_TOKEN = original_url, original_token


def test_configured_storage_id_skips_the_prompt():
    original_url, original_token = wd.LSKY_URL, wd.LSKY_TOKEN
    try:
        wd.LSKY_URL, wd.LSKY_TOKEN = 'https://img.example.com/api/v2', 'TOK'
        os.environ['LSKY_STORAGE_ID'] = '2'
        assert with_input(['y'], wd.ask_upload) == 2, '设了就不该再弹储存单选'
    finally:
        os.environ.pop('LSKY_STORAGE_ID', None)
        wd.LSKY_URL, wd.LSKY_TOKEN = original_url, original_token


def test_confirmation_shows_target_album():
    answers = dict(sample_answers(), upload=2)
    params = wd.build_query(answers['presets'], answers['ratio'], answers['keyword'],
                            answers['purity'], answers['sorting'])
    rows = wd.confirmation_rows(answers, params)
    assert ('图床', '上传至相册「动漫+手机端」', '') in rows, rows
    assert wd.describe_answer('upload', None) == '不上传'
    assert wd.describe_answer('upload', 2) == '上传（储存 #2）'


def assert_raises_containing(call, fragment):
    try:
        call()
    except (RuntimeError, ValueError) as error:
        assert fragment in str(error), '期望包含 %r，实际 %r' % (fragment, str(error))
        return
    raise AssertionError('期望抛出异常（含 %r），但没有' % fragment)


if __name__ == '__main__':
    passed = 0
    for name, test in sorted(globals().items()):
        if name.startswith('test_'):
            test()
            passed += 1
            print('ok  ' + name)
    print('全部通过（%d 项）' % passed)
