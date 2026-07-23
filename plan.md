## 设计决策（已确认 + 本次新增）

| 决策点 | 选择 |
|---|---|
| Web 框架 | **FastAPI**（HTTP + WebSocket 原生、async、自动 OpenAPI、契合 MCP） |
| 接口形态 | **统一 `/query` 入口 + action 参数**（新增查询不改路由、不改 MCP） |
| WS 职责 | **流式推送**（progress/chunk/done/error 事件） |
| 配置 | **.env + Pydantic Settings** |
| HTTP 鉴权 | **静态预共享 Bearer token**（配置 token 集合） |
| WS 鉴权 | **`?token=` 查询参数**（与 HTTP 共用同一 token 集合） |
| **🆕 测试环境鉴权开关** | **`AUTH_ENABLED` 配置项**，设为 `false` 时全局关闭鉴权（HTTP + WS 同时关闭），便于测试与本地调试 |

---

## 本次新增：`AUTH_ENABLED` 鉴权开关

### 1. 配置项（`app/config.py`）
```
AUTH_ENABLED=true   # 默认开启；测试/本地调试设 false 关闭鉴权
```
- 默认 `true`（生产安全默认）
- 在 `.env.example` 中显式列出并注释用途
- 类型为 `bool`，Pydantic Settings 自动解析 `true/false/1/0/yes/no`

### 2. 鉴权依赖逻辑（`app/core/security.py` + `app/api/deps.py`）
- **HTTP**：`get_optional_bearer` 依赖先读 `settings.auth_enabled`
  - `auth_enabled=false` → 直接放行（返回固定占位 principal `"anonymous"`），不校验 token
  - `auth_enabled=true` → 走原有 Bearer 校验
- **WS**：`ws_auth` 在 `accept()` 前同样判断
  - `auth_enabled=false` → 跳过 `?token=` 校验，直接 accept
  - `auth_enabled=true` → 校验 `?token=`，失败拒绝
- **单点实现**：只在 `security.py` 的核心校验函数顶部做一次 `if not settings.auth_enabled: return True`，HTTP/WS 两条路径都经过它，避免散落判断

### 3. 对测试的影响（关键收益）
- **API/WS 测试默认关闭鉴权**：`conftest.py` 的 app fixture 通过 `monkeypatch` 或 dependency override 把 `auth_enabled` 置为 `false`，绝大多数功能测试（成功/错误映射/事件顺序等）**无需关心 token**，代码更简洁
- **鉴权专用测试单独开启**：`test_auth.py` 与 WS 鉴权用例**显式开启** `auth_enabled=true`（用独立 fixture 或 `pytest.mark`），专门覆盖：
  - 关闭时：无 token 也放行（HTTP 200、WS 可连）← **新增用例**
  - 开启时：无 token/错误 token 拒绝（401 / WS 关闭）
  - 开启时：正确 token 放行
- 这样鉴权逻辑本身也被双向测试（开/关两条路径都有用例），不会出现"关了鉴权后某条路径失效"的回归

### 4. 安全护栏
- `main.py` 启动时若检测到 `AUTH_ENABLED=false`，**打印醒目警告日志**（`WARNING: 鉴权已关闭，仅限测试/调试环境使用`）
- 文档（README）明确：生产环境**必须**保持 `AUTH_ENABLED=true`
- 日志脱敏中间件不受影响（即使关闭鉴权也不记录 token 原值）

---

## 完整目录结构（含测试）

```
paper-service/
├── app/
│   ├── main.py                      # FastAPI app + 异常处理 + 日志脱敏 + AUTH_ENABLED 警告
│   ├── config.py                    # Pydantic Settings (.env) — 含 AUTH_ENABLED
│   ├── core/
│   │   ├── exceptions.py
│   │   ├── response.py              # 统一 envelope + 错误码→HTTP 状态映射
│   │   ├── registry.py              # ★ @register + BaseQueryHandler
│   │   └── security.py              # ★ Bearer/WS 鉴权 + AUTH_ENABLED 短路
│   ├── middleware/
│   │   └── logging.py               # ★ 日志脱敏（密钥/token/全文）
│   ├── schemas/
│   │   ├── common.py
│   │   ├── query.py                 # ★ QueryRequest(action,params,request_id,stream)
│   │   └── document.py              # DocFullTextParams
│   ├── api/
│   │   ├── deps.py                  # 依赖注入：鉴权(含开关) + registry
│   │   └── v1/
│   │       ├── query.py             # POST /api/v1/query (HTTP)
│   │       ├── ws.py                # WS  /api/v1/ws?token=
│   │       └── health.py            # GET /health
│   ├── services/
│   │   ├── base.py
│   │   └── document/
│   │       └── fulltext_handler.py  # @register("doc_fulltext")
│   └── clients/
│       └── doc_fulltext_client.py   # ★ 迁移现有 client（逻辑不变）
├── tests/
│   ├── conftest.py                  # 公共 fixture：app(默认关鉴权)、mock 上游、TestClient、ws helper
│   ├── unit/
│   │   ├── test_sign_vector.py      # 固定签名向量
│   │   ├── test_chunk_assemble.py   # chunk 排序/缺失/重复/空
│   │   ├── test_request_build.py    # file_id/doc_hash 二选一、splitter 映射、非法页码
│   │   └── test_security.py         # Bearer 比较 + AUTH_ENABLED 开/关两条路径
│   ├── upstream/
│   │   └── test_fulltext_handler.py # 8 种上游场景(成功/pending×N/失败/业务错误/非法JSON/HTTP错误/网络重试/重签名/pending超时)
│   ├── api/
│   │   ├── test_auth.py             # Bearer 鉴权(开/关) + request_id 透传/自动生成
│   │   ├── test_query.py            # 成功 + 全部错误状态映射
│   │   ├── test_health.py           # /health
│   │   └── test_openapi.py          # OpenAPI 模型
│   └── ws/
│       └── test_ws.py               # 事件顺序/失败/重复/并发/断开取消/鉴权(开/关)
├── requirements.txt                 # fastapi, uvicorn, pydantic-settings, requests, pytest, pytest-asyncio, httpx
├── .env.example                     # 含 AUTH_ENABLED
├── .gitignore
└── README.md                        # 启动 + MCP 对接 + AUTH_ENABLED 说明 + 测试说明
```

---

## 测试覆盖矩阵（含本次新增的鉴权开关用例）

### A. 单元测试
- 固定签名向量；chunk 排序/缺失/重复/空；file_id/doc_hash 二选一、splitter 映射、非法页码
- **🆕 `test_security.py`**：`AUTH_ENABLED=false` 时校验函数直接返回放行；`AUTH_ENABLED=true` 时走 `secrets.compare_digest`；正确 token 放行、错误 token 拒绝、空 token 拒绝

### B. 上游模拟测试（mock requests）
8 场景：直接成功 / 多次 pending 后成功 / 解析失败 / 业务错误 / 非法 JSON / HTTP 错误 / 网络重试 / 重新签名 / pending 超时

### C. API 测试
- **🆕 鉴权开关**：`conftest` 默认 `AUTH_ENABLED=false` → 功能测试无 token 通过；`test_auth.py` 显式开启 → 无/错 token 401、正确 token 200、关闭时无 token 200
- 成功 + 全部错误状态映射（401/400/502/503/504）
- request_id 透传/自动生成（`srv_` 前缀）
- /health；OpenAPI 模型存在

### D. WebSocket 测试（FastAPI 官方 `TestClient.websocket_connect`）
- 完整事件顺序（progress×N → done）/ 失败事件 / 同连接重复查询 / 查询进行中再提交（断言 409 error）/ 客户端断开取消（`CancelledError` 清理，caplog 断言）/ 鉴权失败
- **🆕 鉴权开关**：关闭时 `ws.connect("/api/v1/ws")` 无 token 可连；开启时无 token 被拒

### E. 验收标准验证
- HTTP 与 WS 同输入同最终结果（data 完全相等）
- 并发 N 个查询不阻塞事件循环（`asyncio.to_thread` 包装同步 requests，总耗时≈单次）
- 日志/错误响应不泄露密钥、token、全文（`caplog` 断言出现 `[REDACTED]`/`[content omitted]`，无原值）

---

## 关键实现要点
1. **鉴权单点短路**：`security.py` 核心函数顶部 `if not settings.auth_enabled: return True`，HTTP/WS 共用，避免散落
2. **可注入 client 工厂**：handler 通过依赖注入拿 `DocFullTextClient`，测试用 dependency override 注入 mock，零真实网络
3. **错误码集中映射**：`core/response.py` 的 `EXCEPTION_TO_STATUS` 字典
4. **WS 取消可观测**：stream 任务挂在连接生命周期，客户端断开 → `CancelledError` → `[cancelled]` 日志
5. **常量时间比较**：`secrets.compare_digest`，即使关闭鉴权也不影响该函数本身可测
6. **启动警告**：`AUTH_ENABLED=false` 时打印醒目 `WARNING` 日志

---

## 实现顺序
1. 目录骨架 + `requirements.txt` + `.env.example`（含 `AUTH_ENABLED`）+ `.gitignore`
2. 迁移现有 client 到 `app/clients/`
3. `core/`：exceptions、response、registry、security（含 `AUTH_ENABLED` 短路）
4. `middleware/logging.py`（脱敏）
5. `schemas/`：common、query、document
6. `services/`：base + fulltext_handler
7. `api/`：deps（含开关）、health、query、ws
8. `main.py`（含 `AUTH_ENABLED=false` 启动警告）
9. `config.py`（含 `AUTH_ENABLED`）
10. `tests/`：conftest（默认关鉴权）+ 各测试文件
11. `README.md`

---

## 验收清单（全部由测试自动验证）
- [x] 固定签名向量；chunk 排序/缺失/重复/空；file_id/doc_hash、splitter、非法页码
- [x] 上游 8 场景全覆盖
- [x] **🆕 AUTH_ENABLED 关闭时全放行（HTTP + WS）；开启时鉴权生效**
- [x] Bearer 鉴权 + 全部错误状态码映射
- [x] request_id 透传/自动生成；/health；OpenAPI 模型
- [x] WS：事件顺序/失败/重复/进行中再提交/断开取消/鉴权（开/关）
- [x] HTTP 与 WS 同输入同结果；并发不阻塞事件循环；日志不泄露密钥/全文