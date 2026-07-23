# paper-service

基于 FastAPI 的**文档查询统一服务**，同时提供 **HTTP** 与 **WebSocket** 接口，供上游（如 MCP 服务）封装为函数供模型调用。

本服务**不实现 MCP 服务本身**——MCP 由其他服务提供，只需调用本服务暴露的接口即可。

---

## 特性

- **统一查询入口**：`POST /api/v1/navigator/query`（HTTP）与 `WS /api/v1/navigator/ws`（流式），通过 `stream` 字段选择路由：`false`（默认）HTTP 同步、`true` 改用 WebSocket
- **可扩展架构**：新增查询类型只需写一个 handler + `@register`，**无需改路由、无需改 MCP**
- **HTTP 与 WebSocket 结果一致**：同一输入两条路径产出相同结果
- **Bearer 鉴权**：静态预共享 token，支持 `AUTH_ENABLED` 开关（测试环境可关闭）
- **流式推送**：WebSocket 推送 `progress` / `done` / `error` 事件，适合长耗时文档解析
- **异常分类 + 统一错误码**：错误响应遵循统一 envelope，HTTP 状态码集中映射
- **日志脱敏**：日志与错误响应不泄露密钥、token、文档全文
- **高并发**：同步下游调用包装到 `asyncio.to_thread`，不阻塞事件循环
- **uv 管理依赖**：依赖声明统一在 `pyproject.toml`，`uv.lock` 锁定可复现版本
- **完整测试覆盖**：109 个测试（单元 / 上游模拟 / API / WebSocket / 验收）

---

## 快速开始

本项目使用 [**uv**](https://docs.astral.sh/uv/) 管理依赖与虚拟环境，依赖声明统一在 `pyproject.toml`。

### 0. 安装 uv（如尚未安装）

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
# Windows (PowerShell)
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

### 1. 安装依赖

```bash
uv sync                 # 创建 .venv 并安装运行期依赖（同时生成/更新 uv.lock）
uv sync --extra dev     # 额外安装测试依赖（pytest / pytest-asyncio / httpx）
```

> `uv.lock` 已纳入版本管理，保证依赖版本可复现。新增/升级依赖时编辑 `pyproject.toml` 后重新 `uv sync` 即可。

### 2. 配置

```bash
cp .env.example .env
# 编辑 .env，填入真实的下游地址、密钥、对外 token
```

关键配置项：

| 配置项 | 说明 |
|---|---|
| `AUTH_ENABLED` | 对外鉴权开关。**生产必须为 `true`**；测试/调试可设 `false` 关闭 |
| `API_BEARER_TOKENS` | 允许的 Bearer token 集合（逗号分隔） |
| `DOC_SERVICE_HOST/PORT/SCHEME` | 下游文档全文服务地址 |
| `DOC_SERVICE_AUTH_KEY` | 下游 HMAC 鉴权密钥（服务方分配） |
| `DEFAULT_DEVICE_*` | 默认设备信息（请求级可覆盖） |

### 3. 启动

```bash
# 方式一：通过 uv 运行（自动使用 .venv）
uv run uvicorn app.main:app --host 0.0.0.0 --port 12135 --reload

# 方式二：激活虚拟环境后直接运行
source .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 12135 --reload
```

启动后：
- 交互式文档：http://localhost:12135/docs
- 健康检查：http://localhost:12135/health

#### 一键启动脚本（WSL Linux 内运行）

`start_service.py` 一键完成依赖检查、端口冲突处理、后台启动、日志归档：

```bash
# 在 WSL 内、项目根目录执行
python3 start_service.py
# 自定义端口
python3 start_service.py --port 12135
```

脚本行为：
- 自动把 `~/.local/bin` 加入 PATH（uv 常见安装位），校验 `.venv` 与关键包，缺失则自动 `uv sync --extra dev`
- 若端口已被占用，先优雅关闭旧服务（SIGTERM→SIGKILL）再启动
- 后台启动（setsid 脱离进程组，脚本退出后服务继续运行），日志按启动时间戳归档到 `logs/service_YYYYMMDD_HHMMSS.log`
- 启动后轮询 `/health` 确认服务就绪；失败则打印日志末尾便于排查
- 再次运行同一脚本即"先关旧服务再启新服务"

> 若 `AUTH_ENABLED=false`，启动时会打印醒目警告。

---

## 接口说明

### HTTP：`POST /api/v1/navigator/query`

统一查询入口，请求体：

```json
{
  "query": "1db8f80c0b854613aa68d2c977891353.docx",
  "options": {
    "doc_hash": null,
    "splitter": 1,
    "pages": [],
    "with_rect": false
  },
  "stream": false
}
```

默认只需传 `query`（文件 ID），`options` 可全部省略，内部使用默认值。

成功响应：

```json
{
  "code": 200,
  "message": "success",
  "request_id": "srv_1714000000_abcdef12",
  "data": {
    "results": "按 chunk_id 排序拼接的完整全文",
    "chunk_count": 50,
    "doc_hash": "adb13b77..."
  }
}
```

字段说明：
- `query`：文件 ID（**唯一必传**），即原 `file_id`
- `options`：可选覆盖项，全部有默认值：
  - `splitter`：`0` = 大块（CHAPTER，15000 长），`1` = 小块（SMALL_CHUNK，500 长 20 overlap），默认 `1`
  - `pages`：页码范围，空数组表示全部，默认 `[]`
  - `with_rect`：是否返回 rect，默认 `false`
  - `doc_hash`：文档 hash（与 `query` 同时存在时以 `query` 为准），默认 `null`
  - `device`：设备信息覆盖，默认 `null`（用配置默认值）
  - `request_id`：可选请求标识，不传则服务端生成 `srv_` 前缀
- `stream`：路由选择——`false`（默认）HTTP 同步返回；`true` 时 HTTP 主动拒绝并引导到 WebSocket 端点

#### 鉴权

开启鉴权时（`AUTH_ENABLED=true`），需携带：

```
Authorization: Bearer <API_BEARER_TOKENS 中的某个 token>
```

#### 错误响应

统一错误 envelope，HTTP 状态码集中映射：

| HTTP 状态 | code | 场景 |
|---|---|---|
| 400 | `VALIDATION_ERROR` | 参数校验失败 / `stream=true` 未走 WebSocket |
| 401 | `AUTH_REQUIRED` / `AUTH_INVALID` | 鉴权缺失/失败 |
| 502 | `UPSTREAM_BUSINESS_ERROR` | 上游业务错误 / 解析失败 |
| 502 | `UPSTREAM_PARSE_ERROR` | 上游响应非法 JSON |
| 503 | `UPSTREAM_UNAVAILABLE` | 上游网络重试耗尽 |
| 504 | `PENDING_TIMEOUT` | 文档解析持续 pending |
| 409 | `BUSY` | WebSocket 当前连接已有查询进行中 |

### WebSocket：`WS /api/v1/navigator/ws?token=<token>`

流式查询，发送与 HTTP 相同的 query 消息（JSON），服务端推送事件：

```jsonc
// 客户端发送（与 HTTP 同格式）
{"query": "1db8...docx", "options": {}, "stream": true}
// 1. 开始
{"type": "progress", "message": "started", "request_id": "..."}
// 2. 解析中（多次）
{"type": "progress", "attempt": 1, "message": "pending", "request_id": "..."}
// 3a. 完成
{"type": "done", "data": {"results": "...", "chunk_count": 50, "doc_hash": "..."}, "request_id": "..."}
// 3b. 或错误
{"type": "error", "code": "UPSTREAM_BUSINESS_ERROR", "message": "...", "request_id": "..."}
```

- 鉴权：开启时通过 `?token=` 查询参数携带（与 HTTP 共用同一 token 集合），缺失/错误连接被拒
- 并发：同一连接同时只允许一个查询，进行中再提交返回 `BUSY`
- 取消：客户端断开时，正在进行的查询任务被取消并清理

---

## 架构与扩展

### 目录结构

```
app/
├── main.py                # FastAPI 应用入口（路由挂载、异常处理、启动警告）
├── config.py              # Pydantic Settings（.env 加载，含 AUTH_ENABLED）
├── core/
│   ├── exceptions.py      # 异常体系（code + http_status）
│   ├── response.py        # 统一 envelope
│   ├── registry.py        # ★ action→handler 注册机制（扩展核心）
│   └── security.py        # Bearer 鉴权 + AUTH_ENABLED 短路
├── middleware/logging.py  # 日志脱敏
├── schemas/               # Pydantic 模型（query / document / common）
├── api/
│   ├── deps.py            # 依赖注入（鉴权、client 工厂）
│   └── v1/                # query(HTTP) / ws / health 路由
├── services/
│   └── document/fulltext_handler.py  # @register("doc_fulltext")
└── clients/
    └── doc_fulltext_client.py        # 下游客户端（鉴权、轮询、拼接）
tests/                     # 109 个测试
```

### 新增一种查询（扩展示例）

当前对外信封 `{query, options, stream}` 固定路由到 `doc_fulltext` handler。如需新增其他查询类型，扩展点仍在 handler 层（`@register` + `params_schema`），并在 HTTP/WS 入口的转译处按需分流即可。

例如新增"根据 url 查文档元数据"：

1. 写 `app/schemas/metadata.py` 定义参数模型；
2. 写 `app/services/document/metadata_handler.py`：

```python
from app.core.registry import BaseQueryHandler, register

@register("doc_metadata")
class DocMetadataHandler(BaseQueryHandler):
    params_schema = DocMetadataParams
    async def execute(self, params, ctx):
        ...
        return {...}
```

3. 在 `app/main.py` 导入该模块（触发 `@register`）。
4. 在 HTTP/WS 入口的转译逻辑中按 `options` 内的类型标识分流到对应 handler。

**业务逻辑层无需改动**——下游 client、轮询、拼接、鉴权、脱敏等全部复用。

### 与 MCP 对接

MCP 服务把本服务封装为一个函数工具：

```python
# MCP 侧（示意）
def query(file_id: str, options: dict | None = None, stream: bool = False) -> dict:
    """文档全文查询。stream=False 走 HTTP，stream=True 走 WebSocket。"""
    resp = requests.post(
        "http://paper-service:12135/api/v1/navigator/query",
        json={"query": file_id, "options": options or {}, "stream": stream},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    return resp.json()
```

---

## 测试

```bash
uv run pytest          # 运行全部 109 个测试（需先 uv sync --extra dev）
uv run pytest -q       # 简洁输出
```

> 测试依赖在 `pyproject.toml` 的 `[project.optional-dependencies].dev` 中声明，`uv sync --extra dev` 会一并安装。pytest 配置位于 `pyproject.toml` 的 `[tool.pytest.ini_options]`。

测试分层（零真实网络，全部 mock）：

| 目录 | 覆盖 |
|---|---|
| `tests/unit/` | 固定签名向量、chunk 排序/缺失/重复、file_id/doc_hash、splitter 映射、非法页码、鉴权开/关 |
| `tests/upstream/` | 下游 8 场景：成功 / 多次 pending / 解析失败 / 业务错误 / 非法 JSON / HTTP 错误 / 网络重试 / 重签名 / pending 超时 |
| `tests/api/` | Bearer 鉴权、错误状态映射、request_id 透传/生成、健康检查、OpenAPI 模型 |
| `tests/ws/` | 事件顺序、失败、同连接重复查询、进行中再提交 BUSY、断开取消、鉴权开/关 |
| `tests/test_acceptance.py` | HTTP/WS 同结果、并发不阻塞事件循环、日志不泄露密钥/全文 |

---

## 技术说明

- **鉴权**：对外用静态预共享 Bearer token（`secrets.compare_digest` 常量时间比较）；对下游用 HMAC-SHA256 + Base64（签名串 `POST&/copilot_for_docs/doc_fulltext&deviceId={id}&timestamp={ts}`，每次请求重新签名）
- **全文拼接**：按 `metadata.chunk_id` 升序稳定排序后拼接，缺失 chunk_id 排末尾
- **pending 处理**：文档解析中（`status=pending`）自动轮询，HTTP 一次性等待，WS 增量推送 progress 事件
- **日志脱敏**：`RedactingFormatter` 兜底替换已知敏感子串；`sanitize_mapping` 按字段名脱敏（authorization/token/content 等）
