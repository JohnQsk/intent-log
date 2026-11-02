# intent-log

一个本地小服务：**选一个任务 → 20 分钟 → 到点填一句反馈 → 5 分钟休息 → 再来一段**，每段自动留一行记录，最后看到一张表。

它是 `task-quick-web`（dstask 任务页）和 `rest.reminder`（服务端周期计时 + 反馈网页）的合并版，只用一个 Python 进程、一个端口、一个网页，零第三方依赖（标准库）。

## 为什么是这张表

关心的不是“坐了多久”，而是：**这段时间做了什么、有没有推进、卡在哪、下一步呢**。
所以每行记录的核心是任务名 + 起止时间 + 实际时长 + 一句自我反馈，而不是效率分。

## 启动

```bat
cd path\to\intent-log
python app.py
```

默认打开 `http://127.0.0.1:8770`。常用参数：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--port` | 8770 | 本地端口（taskweb 用 8765，两者可同时跑） |
| `--work-seconds` | 1200 | 一段工作多久 |
| `--break-seconds` | 300 | 一段休息多久 |
| `--data-dir` | `~/.intent-log` | 状态文件和记录文件放哪 |
| `--no-browser` | 关 | 不自动开浏览器 |

短周期自测：`python app.py --work-seconds 20 --break-seconds 5 --data-dir .tmp-data --no-browser`

## 数据

- `~/.intent-log/state.json` —— 当前周期状态（刷新页面、重启服务都不丢待反馈）
- `~/.intent-log/sessions.jsonl` —— 每段一行，append-only，旧记录不重写

一行长这样：

```json
{"schema_version": 1, "session_id": "...", "task_id": "<dstask uuid>",
 "task_title": "implement LRU cache", "task_project": "algorithms",
 "started_at": "2026-11-02T09:05:00+08:00", "ended_at": "2026-11-02T09:25:00+08:00",
 "planned_seconds": 1200, "work_seconds": 1200,
 "feedback_submitted_at": "2026-11-02T09:26:10+08:00",
 "status": "progress", "content": "主体跑通，卡在边界条件的测试",
 "next_action": "same_task"}
```

`status`：`progress` / `task_done` / `blocked` / `interrupted` / `off_task`
`next_action`：`same_task` / `choose_task` / `finish`

任务本身仍然存在 dstask 里。只有反馈里明确选“任务完成”才会 `dstask done`；20 分钟到点**不会**自动完成任务，也不会动 dstask 的计时。

## 给 Cow / Agent 用的 HTTP 接口

只绑 `127.0.0.1`。所有 POST 需要请求头 `X-Intent-Log: 1`（防跨站），JSON 请求体。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/state` | 当前阶段、任务、剩余秒数、今天汇总 |
| GET | `/api/tasks` | dstask 未完成任务 |
| GET | `/api/tasks/completed` | 最近完成的任务 |
| GET | `/api/records?date=YYYY-MM-DD` | 某天的记录 + 有记录的日期列表 |
| POST | `/api/cycle/start` | `{"task_id": "<uuid>"}`，也可不带任务 |
| POST | `/api/cycle/pause` | 暂停 / 继续（同一个接口切换） |
| POST | `/api/cycle/end` | 提前结束本段，进入反馈 |
| POST | `/api/cycle/cancel` | 放弃本段，不写记录 |
| POST | `/api/cycle/feedback` | `{"status","content","next_action"}` |
| POST | `/api/cycle/skip-break` | 跳过休息 |
| POST | `/api/tasks` | `{"description","project"}` 新建任务 |
| POST | `/api/sync` | `dstask sync` |

例子：

```bash
curl -s http://127.0.0.1:8770/api/state
curl -s -X POST http://127.0.0.1:8770/api/cycle/start -H "X-Intent-Log: 1" -H "Content-Type: application/json" -d "{}"
```

## 当前版本的边界

- 不自动安排一天，也不评价效率；不推断“你这 20 分钟值不值”。
- 不监控应用窗口或浏览内容。
- 不做跨设备同步、CSV 导出。
- 服务停着的时间不算工作时长（重启后从上次记录恢复，不补算离线时间）。
- 原来的 20-20-20 护眼提醒暂未移植（`rest.reminder` 里仍有，可作为可选项再加）。

## 测试

```bat
python -m unittest discover -s tests
python tests\smoke_http.py
```

- `tests/test_cycle.py`：状态机 14 个用例（暂停不计时、提前结束、重启恢复、写盘失败保留反馈、一周期一行等）。
- `tests/smoke_http.py`：真起服务跑完整链路；刻意用“不带任务”的周期，不动真实 dstask 任务。

## License

MIT。代码思路与部分实现来自作者本人的 `task-quick-web` 与 `rest.reminder`（均为 MIT）。
