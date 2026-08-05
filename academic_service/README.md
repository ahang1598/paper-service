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
- **pip + requirements.txt 管理依赖**：单一 `requirements.txt`（运行期 + 测试），标准 `venv` 虚拟环境
- **完整测试覆盖**：156 个测试（单元 / 上游模拟 / API / WebSocket / 验收 / 配置加载 / 客户端默认值 / type 路由）

---

## 快速开始

本项目使用 **pip + requirements.txt** 管理依赖，虚拟环境用标准 `venv`。

### 0. 创建虚拟环境并安装依赖

```bash
python3 -m venv .venv                                  # 创建虚拟环境
.venv/bin/pip install -r requirements.txt              # 安装依赖（运行期 + 测试）
```

> 升级依赖时编辑 `requirements.txt` 后重新 `pip install -r` 即可。

### 2. 配置

配置分两层：**YAML（结构性默认，提交到版本库）** + **环境变量 / `.env`（密钥与环境覆盖）**。

```bash
cp .env.example .env
# 编辑 .env，仅填入密钥（包括 SILICONFLOW_API_KEY）与 APP_ENV
```

#### YAML 配置（`configs/`）

| 文件 | 作用 |
|---|---|
| `configs/config.yaml` | 基础默认，**生产安全**（`auth.enabled: true`）；地址/端口/超时/轮询/设备信息/接口路径均在此 |
| `configs/config.prod.yaml` | 生产叠加（`APP_ENV=prod`，默认） |
| `configs/config.dev.yaml` | 开发叠加（`APP_ENV=dev`）：关闭鉴权、缩短轮询，便于本地调试 |

YAML 采用嵌套结构，由 `app/config.py` 的 `_YAML_PATHS` 映射表展平为 Settings 字段。**密钥字段（`auth_key`、`bearer_tokens`）在 YAML 中留空**，仅由环境变量注入，绝不进版本库。

#### 环境变量

| 变量 | 说明 |
|---|---|
| `APP_ENV` | 选择叠加文件：`prod`（默认）/ `dev` |
| `APP_CONFIG_FILE` | 显式指定一个或多个（逗号分隔）YAML 文件，覆盖 `APP_ENV` 叠加逻辑 |
| `AUTH_ENABLED` | 对外鉴权开关。**生产必须为 `true`** |
| `API_BEARER_TOKENS` | 允许的 Bearer token 集合（逗号分隔）—— **密钥** |
| `DOC_SERVICE_HOST/PORT/SCHEME` | 下游地址（默认在 YAML，可在此覆盖） |
| `DOC_SERVICE_AUTH_KEY` | 下游 HMAC 鉴权密钥 —— **密钥** |
| `DOCID_SEARCH_URL` | docid 搜索服务 URL（默认在 YAML，可在此覆盖） |
| `DOCID_SEARCH_AUTH_KEY` | docid 搜索服务 HMAC 鉴权密钥 —— **密钥** |
| `RERANKER_PROVIDER` | relevant 检索供应商：`internal`（默认）/ `siliconflow` |
| `SILICONFLOW_API_KEY` | SiliconFlow rerank API key —— **密钥** |
| `INTERNAL_RERANK_SIGN_KEY` | 内网 GTE reranker HMAC 密钥 —— **密钥** |
| `DEBUG_LOG_PAPER_PROCESSING` | 论文处理详细日志开关，默认 `false`；开启后包含全文、章节、chunk 与 reranker 业务入出参 |
| `DEFAULT_DEVICE_*` | 默认设备信息（请求级可覆盖，默认在 YAML） |

> 任意 YAML 字段都可被同名环境变量覆盖（字段名不区分大小写）。

论文处理日志使用独立开关，不要求把全局 `LOG_LEVEL` 改为 `DEBUG`：

```bash
DEBUG_LOG_PAPER_PROCESSING=true
```

开启后日志按 `stage=fulltext.input/parse.output/structure.output/chunk.output/`
`reranker.input/reranker.output/merge.output` 标识各阶段，并额外记录实际 reranker
provider 的请求 payload 和响应 body。日志不包含 API Key、签名或 Authorization 头，
但会包含论文全文和问题，仅应在访问受控的调试环境短时开启。

#### 优先级（低 → 高）

```
类默认 < configs/config.yaml < configs/config.<APP_ENV>.yaml < .env < 真实环境变量 < 显式 kwargs
```

密钥与环境覆盖始终高于 YAML；测试通过显式构造 Settings（kwargs）注入，不受 YAML 影响。

### 3. 启动

```bash
# 方式一（推荐）：一键启动脚本（自动创建 .venv、pip 装依赖、后台启动、健康检查）
python3 scripts/start_service.py

# 方式二：激活虚拟环境后用 uvicorn 运行
# 注：代码以 academic_service.app.* 为包根导入，需把项目父目录加入 PYTHONPATH
source .venv/bin/activate
PYTHONPATH="$(dirname "$(pwd)")" uvicorn academic_service.app.main:app --host 0.0.0.0 --port 12135 --reload
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
- 校验 `.venv` 与关键包，缺失则自动 `python -m venv` 创建并 `pip install -r requirements.txt`
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

#### 查询类型路由（`type`）

通过顶层 `type` 字段选择下游流程（`query` 与 `queries` 至少传一个）：

| `type` | 含义 | 入参 | 下游 |
|---|---|---|---|
| `fileid`（默认） | 按文件 ID 查全文（原行为） | `query`（单个文件ID）；不支持 `queries` | doc_fulltext |
| `docid` | 按 docid 列表查搜索服务 | `queries`（docid 列表）或单个 `query` | /search |

`docid` 模式示例：

```json
{
  "queries": ["4309586360676299249", "7962161433555592055"],
  "type": "docid"
}
```

`docid` 支持两种论文数据意图，均通过 `options` 传递：

| `options.intent` | 行为 | 其它入参 |
|---|---|---|
| `fulltext`（默认） | 保持原 chunks 全文拼接，同时返回结构化 papers | 无 |
| `relevant` | 章节解析 → chunk → 每篇独立 rerank Top 8 → 邻居 ±1 → 合并去重 | `options.question` 必填 |

相关片段请求示例：

```json
{
  "queries": ["4309586360676299249", "7962161433555592055"],
  "type": "docid",
  "options": {
    "intent": "relevant",
    "question": "这些论文采用了哪些强化学习方法？"
  }
}
```

`docid` 成功响应保留兼容 `results`，并新增 `papers` 与 `processing`：

```json
{
  "code": 200,
  "message": "success",
  "request_id": "srv_...",
  "data": {
    "results": "[1]title:论文A|||content:第一段\n第二段",
    "papers": [
      {
        "docid": "4309586360676299249",
        "title": "论文A",
        "status": "ok",
        "metadata": {},
        "content": "第一段\n第二段",
        "warnings": []
      }
    ],
    "processing": {"intent": "fulltext", "chunk_schema_version": "v1"}
  }
}
```

`relevant` 时 `papers[].content` 替换为 `papers[].segments`；每个 segment
提供章节路径、规范化全文字符区间、来源 chunk IDs、seed IDs 和相关分数。
任一 reranker 批次失败时，整次请求统一降级 BM25，并在
`processing.reranker.degraded` 与论文 warnings 中明确标记。

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
academic_service/
├── configs/               # ★ YAML 配置（base + 环境叠加，提交到版本库；密钥除外）
│   ├── config.yaml        # 基础默认（生产安全：鉴权开启）
│   ├── config.prod.yaml   # 生产叠加（APP_ENV=prod，默认）
│   └── config.dev.yaml    # 开发叠加（APP_ENV=dev，关闭鉴权）
├── app/
│   ├── main.py            # FastAPI 应用入口（路由挂载、异常处理、启动警告）
│   ├── config.py          # Pydantic Settings（YAML + .env + 环境变量分层加载）
│   ├── core/
│   │   ├── exceptions.py      # 异常体系（code + http_status）
│   │   ├── response.py        # 统一 envelope
│   │   ├── registry.py        # ★ action→handler 注册机制（扩展核心）
│   │   └── security.py        # Bearer 鉴权 + AUTH_ENABLED 短路
│   ├── middleware/logging.py  # 日志脱敏
│   ├── schemas/               # Pydantic 模型（query / document / common）
│   ├── api/
│   │   ├── deps.py            # 依赖注入（鉴权、client 工厂）
│   │   └── v1/                # query(HTTP) / ws / health 路由
│   ├── services/
│   │   └── document/fulltext_handler.py  # @register("doc_fulltext")
│   └── clients/
│       └── doc_fulltext_client.py        # 下游客户端（鉴权、轮询、拼接）
└── tests/                  # 单元 / 上游模拟 / API / WebSocket / 验收测试
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
# 代码以 academic_service.app.* 为包根导入，需把项目父目录加入 PYTHONPATH
PYTHONPATH="$(dirname "$(pwd)")" .venv/bin/pytest          # 默认测试（不访问 SiliconFlow）
PYTHONPATH="$(dirname "$(pwd)")" .venv/bin/pytest -q       # 简洁输出

# 显式运行真实 SiliconFlow reranker 测试（需 .env 中配置 API key）
RUN_SILICONFLOW_TESTS=1 PYTHONPATH="$(dirname "$(pwd)")" \
  .venv/bin/pytest -q -m siliconflow
```

> 测试依赖已合入 `requirements.txt`，`.venv/bin/pip install -r requirements.txt` 会一并安装。pytest 配置位于 `pytest.ini`。

默认测试分层（零真实网络，外部测试显式启用）：

| 目录 | 覆盖 |
|---|---|
| `tests/unit/` | 固定签名、配置、全文规范化、7 类论文片段、章节/chunk 不变量、reranker、BM25、邻居合并去重 |
| `tests/upstream/` | 下游 8 场景：成功 / 多次 pending / 解析失败 / 业务错误 / 非法 JSON / HTTP 错误 / 网络重试 / 重签名 / pending 超时 |
| `tests/api/` | 鉴权、错误状态、docid fulltext/relevant、结构化 papers、部分成功 |
| `tests/ws/` | 事件顺序、relevant 处理阶段、HTTP/WS 结果一致性、BUSY、断开取消 |
| `tests/integration/` | 显式启用的真实 SiliconFlow 短片段 rerank 测试 |
| `tests/test_acceptance.py` | HTTP/WS 同结果、并发不阻塞事件循环、日志不泄露密钥/全文 |

---

## 技术说明

- **鉴权**：对外用静态预共享 Bearer token（`secrets.compare_digest` 常量时间比较）；对下游用 HMAC-SHA256 + Base64（签名串 `POST&/copilot_for_docs/doc_fulltext&deviceId={id}&timestamp={ts}`，每次请求重新签名）
- **全文拼接**：按 `metadata.chunk_id` 升序稳定排序后拼接，缺失 chunk_id 排末尾
- **pending 处理**：文档解析中（`status=pending`）自动轮询，HTTP 一次性等待，WS 增量推送 progress 事件
- **日志脱敏**：`RedactingFormatter` 兜底替换已知敏感子串；`sanitize_mapping` 按字段名脱敏（authorization/token/content 等）
