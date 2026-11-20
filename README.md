# 电视台字幕归档（HLS / WebVTT → SQLite）

按**播出 UTC 时间**检索直播字幕的归档网页。可切换本地 HLS 演示源的频道、
模拟取片延迟、制造缺片并触发采集，查看哪一段已取、哪一段待补、哪一段缺失。

## 快速启动

```bash
docker compose up --build
# 网页      http://localhost:8000
# 演示源    http://localhost:8001/channel/news/master.m3u8
```

网页顶部的“演示源控制”面板可以推进直播窗口、让某个片段取片失败、回放旧列表、
设置取片延迟。SQLite 文件保存在具名卷 `archive-data`（容器内 `/data/archive.sqlite3`），
`docker compose restart` 或重建 web 容器后继续在同一档案上采集。

不使用 Docker 时：

```bash
python3 -m pip install -r requirements.txt
# 终端 1：演示源
uvicorn origin.server:app --port 8001
# 终端 2：网页（默认配置指向 127.0.0.1:8001 需要覆盖频道表）
ARCHIVE_DB=./data/archive.sqlite3 ORIGIN_BASE_URL=http://127.0.0.1:8001 \
CHANNEL_CONFIG='news|新闻综合|http://127.0.0.1:8001/channel/news/master.m3u8,sports|体育赛事|http://127.0.0.1:8001/channel/sports/master.m3u8' \
uvicorn app.main:app --port 8000
```

## 时间与字幕的对应规则

* 视频列表里每个片段的 `#EXT-X-PROGRAM-DATE-TIME` 是该片**片首的播出 UTC 时间**。
* 字幕文件里的 `X-TIMESTAMP-MAP` 的 `LOCAL` 是**该片起点**在媒体时钟上的值。
  因此文件内某条字幕的时钟 `C` 对应 UTC = `片首UTC + (C - LOCAL)`。
* 因为每个字幕文件都带自己的 `LOCAL` 锚点，同一条字幕若在相邻两个片段的文件里
  重复出现（跨片字幕），两处都会换算到**完全相同的 UTC 起止**。

`app/hls.py` 负责解析与换算，`app/timeutil.py` 统一以 UTC 毫秒整数存储、以
`...Z` 字符串展示。

## 片段状态与“待补 / 缺失”

片段状态是单调的，`app/db.py` 只允许下列方向：

```
pending（列表里见过、还没取到）
   → captured（取到并入档，空字幕文件也算 captured）
   → missing （滑出直播窗口仍未取到）
```

* `missing → captured` 允许发生：有效片段迟到入档时升级为已取。
* 不会回退：`captured` 永远不会变回 `pending` / `missing`。
* 重复采集、以及源返回比已见窗口更旧的**旧列表**，都不会让状态倒退；
  旧列表也不会把片段判成缺失。
* 列表里已见未取的片段标“待补”；滑出窗口后标“缺失”，即使相邻片段的字幕跨过
  这个缺口也照样标出。

已归档字幕的跨片合并**只在同一连续段内**进行：要求两片的序号连续、UTC 首尾相接，
且都处于 `captured`。缺口（待补或缺失）会切断连续段，因此缺口两侧的重复字幕不会
被误合并。迟到补齐一个缺口后，覆盖段与字幕在同一个响应里同时更新（读档在单个
SQLite 事务快照中完成）。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 归档网页 |
| GET | `/api/channels` | 频道列表 |
| POST | `/api/channels/{id}/capture` | 采集一次该频道 |
| GET | `/api/channels/{id}/archive?from=&to=` | 按 UTC 区间查档（ISO-8601，带 `Z`） |
| GET | `/api/channels/{id}/segments` | 全部片段的完整状态 |
| GET/POST | `/api/demo/{path}` | 转发到演示源 `/__control/*` |

演示源控制：`/__control/state`、`advance`、`latency`、`gap`、`freeze`、`stale`、`reset`。

## 配置

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `ARCHIVE_DB` | `./data/archive.sqlite3` | SQLite 路径（Compose 中为 `/data/archive.sqlite3`） |
| `CHANNEL_CONFIG` | 演示源两个频道 | `id\|名称\|master.m3u8地址`，逗号分隔 |
| `ORIGIN_BASE_URL` | `http://origin:8001` | 演示源控制接口地址 |
| `ORIGIN_TIMEOUT_S` | `10` | 拉取源的超时秒数 |

## 测试

```bash
python3 -m pip install -r requirements.txt -r requirements-dev.txt
python3 -m pytest -q
```

测试直接在同一进程内跑演示源与被测网页（ASGI transport），覆盖：UTC 换算、
跨片字幕合并与缺口不合并、空字幕片段、切台、待补→缺失、迟到补齐、重复采集、
旧列表不回退、SQLite 重启续采、按 UTC 区间过滤。
