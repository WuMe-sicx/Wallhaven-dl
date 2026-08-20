# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 运行

```bash
cp .env.example .env             # 填 wallhaven key 与兰空图床地址/token（.env 已被 gitignore）
uv run wallhaven-dl.py           # 全交互式，无命令行参数
uv run test_wallhaven_dl.py      # 自检，纯 assert 无框架
```

跑单个测试直接调函数名。无 lint 配置。

**别用 macOS 自带的 `/usr/bin/python3`**：它是 3.9 且链 LibreSSL 2.8.3，而 urllib3 v2 只支持 OpenSSL 1.1.1+，每次运行都会刷 `NotOpenSSLWarning`。功能不受影响，但那是 urllib3 明确不再支持的 TLS 后端。两个脚本都带 PEP 723 内联依赖声明（`requires-python = ">=3.11"`），`uv run <脚本>` 会据此自动取一个链 OpenSSL 3.x 的解释器并装好 requests，警告随之消失。**改依赖要同时改 `requirements.txt` 和两个脚本头部的 PEP 723 块**，否则两条安装路径会漂移。

没有 uv 时用任何链 OpenSSL 的解释器均可，例如 `brew install python@3.14` 后 `python3.14 -m pip install -r requirements.txt`。不要靠 `warnings.filterwarnings` 屏蔽，也不要为此把 `urllib3` 钉回 1.x——那是拿仓库迁就单台机器的环境。

Docker：`docker build -t wallhaven-dl . && docker run -v $PWD/Wallhaven:/Wallhaven-dl/Wallhaven -it wallhaven-dl`

## 架构

单文件脚本 `wallhaven-dl.py`。核心模型是**可叠加的筛选轴**（见 `CONTEXT.md` 与 `docs/adr/0001`），不是互斥模式：

0. **读配置** —— `load_dotenv()` 必须在所有模块级常量之前跑，否则 `APIKEY` / `LSKY_URL` 读不到。已 export 的真环境变量优先（见 `docs/adr/0005`）。
1. **问** —— `ASK_ORDER` 里的七个 asker 依次执行（内容 → 尺寸 → 关键词 → 纯度 → 排序 → 并发数 → 图床），各自返回**名字**（预设名、纯度名、排序名），不返回 API 参数，也不写全局。编号菜单统一走 `ask_menu_indexes()`。
2. **合成** —— `build_query()` 把名字解析成查询参数。同轴多写按 `AXIS_MERGE` 里声明的算子合并。
3. **探路** —— 取第 1 页拿到 `meta.total` 与 `meta.last_page`，据此报命中数、限定页数上限。**这一页会留给下载复用**，确认流程不额外消耗限流配额。
4. **确认** —— `print_confirmation()` 三列展示：轴中文名、人类可读取值、实际 API 参数。选 `n` 进 `amend()` 单项重问，改完回到第 2 步重算。
5. **下载** —— `download()` 复用第 1 页，其余页现取；**按页分批，页内用 `ThreadPoolExecutor` 并发**。`save()` 返回 `(状态, 字节数, 说明)`，状态取 `downloaded` / `exists` / `failed`；它**不打印任何东西**，进度经 `on_bytes` 回调给 `Progress`，失败原因随返回值带出由 `download()` 统一打印——并发下多个线程一起 `print` 会把进度行冲烂。

加内容预设：往 `PRESETS` 加一行 `{名字: {轴: 值}}`，同轴冲突检测自动覆盖它，别处不用改。

`PRESETS` 只放内容类的轴。尺寸、纯度、排序各是一个独立提问（`RATIOS` / `PURITY_MENU` / `SORTINGS`），因为它们的取值互斥，做成单选后非法组合在结构上就无法表达——这也是代码里没有退化检测的原因。

加轴：若该轴可合并，在 `AXIS_MERGE` 里声明算子；**不声明即视为互斥**，重复写入会报错——这是刻意的默认值。

6. **上传** —— 选了图床才走：`lsky_album_id()` 先查后建同名相册 → `lsky_album_filenames()` 拿已有文件名 → `upload_directory()` 只传目录里缺的那些，并发数与下载共用。

加提问步骤：往 `ASKERS` 加函数、`ASK_ORDER` 与 `STEP_LABELS` 各加一项，`amend()` 的菜单会自动带上它。答案不能直接 `str()` 的（如图床的 `None`/储存 id）再往 `STEP_DESCRIBERS` 加一条。

加配置项：`.env.example` **必须同步更新**——`.env` 被忽略后，它是使用者唯一的发现路径。

## 需要知道的坑

- **唯一依赖是 `requests`**：代码早已从 HTML 抓取改成调 API，beautifulsoup4 / lxml / tqdm 已在 2026-08-20 清理掉，别再照着旧 README 装回来。
- **APIKEY 只从环境变量 `WALLHAVEN_API_KEY` 读**：不要为了方便改回硬编码——这个仓库因此泄漏过一次真实 key（`e4f2a78`）。
- **wallhaven 大量参数错误是静默的**，这是本脚本多处防御代码的由来，删之前先读 `CONTEXT.md` 的「退化组合」：
  - `ratios=zzz`、`sorting=zzz` 不报错，悄悄不过滤 / 悄悄换排序
  - `ratios=portrait,landscape` 的结果与不加筛选一字不差
  - 无 API key 请求 nsfw 返回 0 张而非 401
- **sketchy 不需要 API key**（实测 264 张），只有 nsfw 需要。README 早先写错过，别照抄。
- **「最新」不传 `sorting`**，wallhaven 默认就是 `date_added`；`sorting=toplist` 默认即 `topRange=1M`，显式传是冗余。
- **每页数量不做假设**：实际下载张数来自 `len(fetch_page(...))`，早先写死的 24 已移除。唯一用到 `meta['per_page']` 的地方是进度条的分母估算，估错只影响百分比显示，不影响下载。
- **`total=0` 时 `last_page` 仍是 1**，不是 0。判断有无结果只能看 `total`，照 `last_page` 问页数会得到「页数（1-1）」这种荒谬提示。
- **超出 `last_page` 的页返回 HTTP 200 + 空 data**，不报错。页数上限在提问处就卡死，就是为了不白发这种请求去占 45/分钟的配额。
- **纯度只认 sfw / sketchy / nsfw**：旧的 `ws`/`wn`/`sn` 组合码已随多选一起移除，别再加回来——位或合并后它们表达不了任何新东西。
- **落盘先写 `.part` 再 `os.replace()`**：保证「文件存在」等价于「文件完整」。写盘中途抛异常（含 Ctrl-C）会删掉 `.part`——它不会被「已存在」挡住，但会永远留在目录里当垃圾。
- **进度条只在 `sys.stdout.isatty()` 时刷**：管道或重定向下静默，否则 `\r` 会把日志刷成一坨。这也意味着自动化测试看不到它，改进度显示要用 pty 复核。
- **`Progress` 是被多个下载线程共写的**：所有更新必须在它自己的锁内，且刷新限流在 10 次/秒——几路并发一起刷会把终端刷爆。
- **并发只在页内**：一页下完再取下一页。既省得为大页数把全部 URL 先攒进内存，也让翻页请求分散开，不至于一口气撞上 API 的 45/分钟限流。
- **图片走 `w.wallhaven.cc`，与 API 的 `wallhaven.cc` 不同域**：45/分钟的限流是 API 的，图片下载不受它约束——这正是并发下载安全的前提，所以 `save()` 里没有节流。
- **实测提速**：同一批 24 张 83.8 MB，串行 2分04秒（687 KB/s），8 路并发 14.2 秒（5.9 MB/s）。
- **图片下载 4xx 不重试**（图确实没了），5xx 与连接异常重试一次。见 `_get_image()`。
- **跨分组不去重**：同一张图会在多个预设目录下各存一份，这是 ADR-0001 的有意决定，不是 bug。
- **上传只走新版 `/api/v2`**：`llms.txt` 里标着「旧版本接口」的那组 `/api/v1` 端点已废弃，别混用。
- **上传失败不重试**，与下载相反，理由见 `docs/adr/0004`——`POST /upload` 不幂等，而按文件名去重本身就是补传机制。
- **相册名比对必须精确**：`GET /user/albums` 的 `q` 是精确还是模糊匹配文档没写，依赖它会把「动漫」错认成「动漫+手机端」。
- **兰空分页靠「本页不满 per_page」判定结束**，不读 `last_page`。分页信封的嵌套层数官方文档没钉死，读错位置不报错、只会在第一页就停——那是静默截断，会让相册重复创建、去重失效。`_lsky_pages()` 另有 `LSKY_MAX_PAGES` 上限，防止对方不认 `page` 参数时无限翻页。
- **`.dockerignore` 必须排除 `.env`**：Dockerfile 用的是 `COPY . .`，没有它凭据会被烤进镜像层，`docs/adr/0005` 把凭据挪出源码的意义就没了。
- **`is_public` 显式传 `'0'`**：不依赖远端默认值——万一对方改了默认，你的壁纸就进了公开广场。
- **判断交互观感必须用 pty，别用管道**：管道喂 stdin 时用户的回车不回显换行，多个提问会在捕获的输出里粘成一行，看起来像排版缺陷，真实终端里却是正常的。照着这种假象「修排版」会让真实终端多出空行。验证脚手架见下。

## 交互验证脚手架

改动提问流程后，用 pty 复核实际观感（`python3 test_wallhaven_dl.py` 只覆盖取值逻辑，覆盖不到排版）：

```python
import os, pty, select, sys, time
pid, fd = pty.fork()
if pid == 0:
    os.execv(sys.executable, [sys.executable, 'wallhaven-dl.py'])
keys, out, deadline = ['\n', '\n', '\n', '\n', '0\n'], b'', time.time() + 12
while time.time() < deadline:
    r, _, _ = select.select([fd], [], [], 0.4)
    if r:
        try: out += os.read(fd, 4096)
        except OSError: break
    elif keys:
        os.write(fd, keys.pop(0).encode())
os.kill(pid, 9)
print(out.decode(errors='replace'))
```

读 pty 要用 `select` 带超时——直接 `os.read` 到 EOF 会在子进程等输入时挂死。

## Agent skills

### Issue tracker

Issues 记在 GitHub Issues（`WuMe-sicx/Wallhaven-dl`），用 `gh` CLI 读写。见 `docs/agents/issue-tracker.md`。

### Triage labels

沿用默认五个标签，标签字符串与角色名一致。见 `docs/agents/triage-labels.md`。

### Domain docs

单 context 布局：根目录 `CONTEXT.md` + `docs/adr/`（均已建立）。见 `docs/agents/domain.md`。
