# dy-sync

一个轻量、纯本地运行的 **抖音博主视频订阅与增量下载工具**，基于 `Playwright + aiohttp + SQLite` 实现。

不需要任何第三方解析服务器：所有页面签名、登录态、数据库记录和视频下载全部在你的本机完成。

---

## 核心特性

- **博主订阅管理**：记录抖音博主主页链接（支持标准主页链接、`v.douyin.com` 短链以及整段抖音分享文案），统一持久化到 SQLite。
- **真正意义的增量更新**：
  - 按作品 `aweme_id` 去重，已下载视频自动跳过。
  - **智能早停**：翻页时遇到连续 `N` 条（默认 5 条）已下载的**非置顶**作品后自动停止翻页，避免每次同步都把历史几百个视频全刷一遍。
  - **缺失文件补下**：开启 `redownload_missing: true` 后，如果数据库记录已下载但本地视频被你删掉了，下次同步会自动重新补下。
- **原生页面签名（免维护逆向算法）**：
  - 复用 Playwright 持久化浏览器上下文（`data/browser_profile/`），只需扫码登录一次。
  - 同步时通过真实浏览器打开博主主页并监听抖音网页原生发出的 `/aweme/v1/web/aweme/post/` 接口，自动获取最高码率无水印视频直链（同时支持图文作品）。
- **多层防风控机制**：
  - **随机抖动间隔**：翻页滚动间隔、单视频下载间隔均采用区间随机休眠，避免固定机械节奏。
  - **批次冷却休眠**：每连续下载一定数量作品（默认 15 个），自动进入较长冷却休眠（默认 15~30 秒）。
  - **多博主切换冷却**：批量同步多个博主时，切换博主前自动随机休眠（默认 5~12 秒）。
  - **403/429 指数退避 + 自动熔断**：遇到限流/拒绝响应时自动退避冷却；连续失败达到阈值（默认 5 次）立即触发熔断停止同步，保护你的本机 IP 和账号。
  - **验证码页面检测**：翻页过程中检测到验证码/安全验证标题时自动停止无效滚动。
- **原子落盘**：视频先写入 `.part` 临时文件，下载完成后再原子重命名为 `.mp4`，避免断网或中断留下损坏文件。

---

## 目录结构

```text
dy-sync/
├── config.yml                # 全局配置文件（下载路径、并发、防风控参数等）
├── main.py                   # CLI 入口脚本
├── README.md                 # 使用说明
├── dy_sync/
│   ├── config.py             # 配置加载与校验
│   ├── browser_bridge.py     # Playwright 持久化登录与页面响应监听
│   ├── storage.py            # SQLite (WAL) 博主与作品增量存储
│   ├── downloader.py         # 异步限速下载器与 AntiRiskController 防风控引擎
│   ├── utils.py              # 链接解析、安全文件名与模板格式化
│   └── cli.py                # 命令行交互实现
├── tests/
│   └── test_core.py          # 核心逻辑与防风控单元测试
├── data/                     # 运行时自动生成（数据库 + 浏览器登录态）
└── downloads/                # 默认视频保存目录（按博主昵称分文件夹）
```

---

## 环境要求

- Python 3.10+
- 依赖包：`playwright`、`aiohttp`、`pyyaml`、`rich`

如在新环境安装依赖：

```powershell
pip install playwright aiohttp pyyaml rich
# 如不复用本机 Edge/Chrome，需安装 Playwright Chromium 内核：
playwright install chromium
```

> **提示**：如果你不想额外下载 Playwright Chromium，直接修改 `config.yml` 中的 `browser_channel: "msedge"` 或 `"chrome"` 即可复用你电脑里已有的浏览器。

---

## 快速开始

在终端中进入 `dy-sync` 目录：

```powershell
cd d:\WorkSpaceAi\dy-sync
```

### 1. 首次扫码登录抖音

```powershell
python main.py login
```

执行后会弹出一个浏览器窗口，扫码登录抖音成功后回到终端按 `Enter` 键保存会话。登录态保存在 `data/browser_profile/`，后续同步会自动复用。

### 2. 添加要跟踪的博主

支持标准主页链接、短链，或者直接粘贴抖音 App 复制的整段分享文案：

```powershell
python main.py add "https://www.douyin.com/user/MS4wLjABAAAA..."
python main.py add "https://v.douyin.com/iRNBho6u/" --name "备注名"
```

### 3. 查看已记录的博主

```powershell
python main.py list
```

### 4. 执行同步下载

```powershell
# 增量同步全部已记录博主（日常检查更新推荐）
python main.py sync

# 只同步指定博主（可填 list 中看到的 ID 序号，或 sec_uid / 前缀）
python main.py sync --creator 1

# 首次拉取老视频，或需要忽略增量早停强制向后全量扫描时加 --full
python main.py sync --creator 1 --full
```

### 5. 其他管理命令

```powershell
# 查看整体下载统计（博主数、已下载、待下载、失败数）
python main.py status

# 移除某个博主（仅删除数据库记录，不删除已下载到本地的视频文件）
python main.py remove 1

# 使用自定义配置文件
python main.py -c my_config.yml sync
```

---

## 配置说明（`config.yml`）

| 配置项 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `download_dir` | `./downloads` | 视频保存根目录，内部按博主昵称建子目录 |
| `data_dir` | `./data` | 数据库（`dy_sync.db`）和浏览器登录态存放目录 |
| `concurrency` | `2` | 同时下载的视频数，防风控建议保持 `1~2` |
| `rate_limit` | `1.5` | 全局每秒最大下载请求数上限 |
| `retry_times` | `3` | 单文件下载失败重试次数 |
| `redownload_missing` | `true` | 本地文件被手动删除后，下次同步是否自动补下 |
| `incremental_stop_threshold` | `5` | 连续遇到多少条已下载的**非置顶**作品后早停翻页 |
| `max_scroll_pages` | `50` | 单次同步最大翻页数（`0` 表示不限制） |
| `headless` | `false` | 是否隐藏浏览器窗口；首次使用建议 `false`，方便观察或手动过验证码 |
| `browser_channel` | `""` | 填 `"msedge"` 或 `"chrome"` 复用本机浏览器，留空用内置 Chromium |
| `filename_template` | `{date}_{title}_{id}` | 文件名模板，支持 `{date}`、`{title}`、`{id}` |

### 防风控参数（`anti_risk`）

| 参数 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `download_delay_min` / `max` | `1.5` ~ `3.5` 秒 | 每次视频下载请求前的随机抖动间隔 |
| `scroll_delay_min` / `max` | `2.0` ~ `4.5` 秒 | 主页滚动翻页的随机抖动间隔 |
| `batch_cooldown_every` | `15` | 每连续下载多少个作品触发一次批次冷却（`0` 关闭） |
| `batch_cooldown_min` / `max` | `15.0` ~ `30.0` 秒 | 批次冷却休眠时长区间 |
| `creator_delay_min` / `max` | `5.0` ~ `12.0` 秒 | 多博主同步时，切换下一个博主前的随机冷却 |
| `circuit_breaker_failures` | `5` | 连续失败达到此次数后自动熔断，停止后续下载 |
| `rate_limit_backoff_base` | `10.0` 秒 | 遇到 `HTTP 403/429` 时的指数退避基础冷却秒数 |

---

## 运行测试

```powershell
python -m unittest discover -s tests -v
```

---

## 注意事项与免责声明

1. 本工具仅用于个人学习、数据备份与合法授权的内容归档，请遵守目标平台的用户协议及相关法律法规。
2. 所有网络请求均直接使用你本机的网络环境和登录态，请合理设置并发和防风控间隔，避免高频抓取触发账号或 IP 限制。
