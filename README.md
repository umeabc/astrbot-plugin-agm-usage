# astrbot_plugin_agm_usage

仓库：https://github.com/umeabc/astrbot-plugin-agm-usage

AstrBot 插件：通过 `/agm` 查询 Antigravity Tools (AGM) 管理 API 的 Token 消耗
与各账号额度状态；`/agmdiag` 输出诊断信息。只读 GET，绝不修改 AGM 状态。

## 安装

### 面板上传 zip

1. 在 GitHub Releases（或自行打包本目录）下载 `astrbot_plugin_agm_usage.zip`；
2. AstrBot WebUI → 插件管理 → 上传插件 zip → 重载；
3. 在插件配置里填写 `base_url` 与 `admin_password`。

### 手动放置

把整个 `astrbot_plugin_agm_usage` 目录放进 AstrBot 的 `data/plugins/` 下重载即可。
生产依赖仅 `aiohttp>=3.9,<4` 与 `tzdata>=2024.1`。

## 配置（`_conf_schema.json`）

| key | 说明 |
|---|---|
| `base_url` | AGM 管理 API 源站（如 `http://127.0.0.1:8045`） |
| `admin_password` | 管理密码，`Authorization: Bearer <password>`；仅后台填写 |
| `instance_name` | 实例名（默认 AGM） |
| `timeout_seconds` | 每请求总超时（1..60，默认 10） |
| `default_timezone` | 统计日/周边界时区（默认 Asia/Shanghai） |
| `allowed_users` / `allowed_groups` | `/agm` 白名单；空 = 拒绝非管理员 |
| `allow_admin` / `allow_private` | 管理员豁免 / 私聊开关 |
| `cooldown_seconds` | 每发送者冷却（0..3600，默认 10）；`/agmdiag` 也受限 |
| `max_lines_per_message` / `max_message_chars` | 分页阈值（默认 30 行 / 1800 字符） |
| `max_response_bytes` | 响应体上限（默认 5MB） |
| `account_label` | 账号标识方式（默认 `short_id`）：<br/>- `short_id`：UUID 前 8 字符（如 `a2e409cf`）<br/>- `masked_email`：邮箱打码用 `…`，如 `s…e@….com`（避免 Markdown 渲染问题）<br/>- `email`：完整邮箱（明确暴露地址，谨慎使用） |
| `show_model_breakdown` | 是否输出「模型消耗（近 24 小时 / 近一周）」小节（默认 true） |
| `enable_diag` | 是否允许触发 `/agmdiag`；关闭后只回复固定提示，不联网（默认 true） |

## 命令

- `/agm` — 消耗报表。输出固定模板：当日 / 本周 / 累计 Token（千分位）、
  全部账号（含禁用/封禁/校验阻塞/quota=null）的 Gemini/Claude 等分组额度百分比
  与重置倒计时、模型「近 24 小时 / 近一周」双窗口消耗。账号默认用 UUID 前 8 位标识
  （`account_label` 可改为打码邮箱或明文邮箱），输出不含 Markdown 触发符。
  白名单受限；超长才分页并加 `[AGM i/N] ` 前缀（单页不加），不丢字符。
- `/agmdiag` — 诊断。**不受白名单限制**，但受冷却限制；可通过 `enable_diag` 关闭
  （关闭后只回复固定提示，不联网）。输出配置有效性、base_url、
  认证结果、health/version、每个请求的状态与耗时、H/HW、daily 交叉核验差值。
  永不打印密码、Authorization 头、完整邮箱或白名单成员。

## 统计口径（重要）

- **当日** = `GET /api/stats/daily?days=2` 中 `period == 当日日期`（default_timezone 下）
  的自然日桶 `total_tokens`。桶缺失（通常表示当日 0 用量）→ 回退滚动
  `summary(hours=H)`，`/agmdiag` 会标记 fallback。
- **本周** = `GET /api/stats/weekly?weeks=1` 中 `period == "YYYY-Www"`（AGM 用
  `strftime('%Y-W%W', localtime)`，**周一为周首**）的自然周桶。桶缺失 → 回退滚动
  `summary(hours=HW)` + 诊断标记。
- **累计** = `summary(hours=0)`（全部时间）。
- **模型消耗（近 24 小时 / 近一周）**：模型维度没有自然日接口，固定展示
  `GET /api/stats/models?hours=24` 与 `hours=168` 两个滚动窗口；两列都是 0 的
  模型不显示；其合计**不等于**「当日已消耗 Token」/「本周已消耗 Token」
  （差值 = 滚动窗口跨过 0 点/周界的那段）。不要把各模型 Token 相加去对账总量。
- 滚动窗口 `H = max(1, ceil((now - 当日00:00)/3600))` 会把昨天 00:00 之后的
  尾巴算进来（实测 00:26 时 hours=1 → 虚高 ~26%），这正是当日/本周改用
  自然桶的原因；滚动值仍保留在 `/agmdiag` 的交叉核验差值里。
- `H` 向上取整、跨午夜请求共用发起时的 H 与日期。

## 无状态说明

插件无持久化、无后台任务、不写任何数据目录；冷却表仅存内存（monotonic）。

## 测试（完全离线）

```
.venv 的 python -m pytest -q          # 本地 aiohttp stub，随机端口
python -m compileall -q .
```

## 联调（只读）

```
set AGM_ADMIN_PASSWORD=<真实密码>     # 仅环境变量，绝不写进文件
python tools/probe_agm.py --base-url http://127.0.0.1:8045            # 摘要
python tools/probe_agm.py --base-url http://127.0.0.1:8045 --render   # + 渲染 /agm 文本
```

退出码：0 正常；2 网络不可达；3 认证失败；4 响应/schema 异常；5 其他。

## 故障排查

| 回复文案 | 含义 |
|---|---|
| `AGM 查询配置无效…` | base_url / 密码未通过校验 |
| `AGM 管理 API 认证失败…` | 401/403，密码错误 |
| `AGM 请求超时…` / `AGM 服务异常…` / `AGM 暂不可达…` | 网络 / 5xx / 连接失败 |
| `AGM 返回非 JSON…` / `AGM 响应格式不兼容…` | 地址错误或 AGM 版本不兼容 |
| `AGM 响应过大…` / `AGM 请求受限…` | 超过字节上限 / 429 |
| `查询过于频繁…` | 本地冷却 |
| `无权查询 AGM 用量。` | 不在白名单 |

## 安全提示

Bearer 密码经 HTTP 明文传输，仅限可信局域网使用；跨网请自建 HTTPS / 隧道。
