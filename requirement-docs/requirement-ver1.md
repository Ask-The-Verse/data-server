# Game Data Server Requirements

## 1. 目标与范围

使用 Python FastAPI 创建一个读取本地 SQLite 游戏数据的 HTTP 服务。

- 这是 demo 服务，不配置认证或授权。
- 服务仅支持 Star Citizen `LIVE` 频道，不处理 `PTU`。
- 服务采用单 FastAPI 进程、单 Uvicorn worker、进程内多线程模型。
- 推荐启动方式：

  ```bash
  uvicorn data_server.main:app --workers 1
  ```

- 数据根目录默认为仓库内的 `data/`，允许使用 `DATA_ROOT` 环境变量覆盖。
- 使用 `pyproject.toml` 管理项目，要求 Python `>=3.9`。
- 运行依赖为 `fastapi` 和 `uvicorn`；测试依赖放入可选 `dev` 依赖组。

## 2. 服务代码结构

服务代码使用独立 package，不把 API 逻辑写入 workflow：

```text
data_server/
├── __init__.py
├── main.py          # FastAPI app、路由和异常处理
├── config.py        # 环境变量和路径
├── database.py      # SQLite connection 和写优先读写锁
├── lifecycle.py     # 并行预热和版本选择
├── models.py        # Pydantic 请求/响应模型
└── ship_service.py  # 名称匹配、懒加载、裁剪和 manufacturer 关联
```

## 3. 启动与预热

FastAPI 开始接受请求前必须完成预热。

1. 使用两个线程并行运行：
   - `ErkulWorkflow`，固定使用 `LIVE` branch。
   - `ScmdbWorkflow`，固定使用 `live` channel。
2. workflow 使用现有的非 `force` 语义：
   - 每次启动仍检查远端最新版本。
   - 如果本地该来源和版本已经是 `complete`，跳过完整下载和重建。
3. 下载、校验、解压和 JSON 解析可以并行。
4. SQLite 写操作必须使用共享的应用级写锁，并使用短事务：
   - `source_files` 和运行状态更新后立即提交。
   - 不得在网络请求期间持有 SQLite 写事务。
   - 最终重建来源表时，在写锁内执行完整事务。
5. 一个 workflow 先失败时，仍等待另一个 workflow 正常结束，再抛出汇总异常。
6. 任一 workflow 本次执行失败时，服务不得启动，即使磁盘上存在旧的完整共同版本。
7. 两个 workflow 都成功后再确定当前版本。

## 4. 版本规则

### 4.1 完整版本

只有同一个规范化版本数据库中的以下两条记录都满足
`crawl_runs.status = 'complete'`，该版本才是可用的完整版本：

```text
erkul
scmdb
```

单一来源完成、`running` 或 `failed` 的版本不得通过 API 暴露。

### 4.2 当前版本

- 如果 Erkul 和 SCMDB 本次发现的最新规范化版本相同，使用该版本。
- 如果两个最新版本不同：
  - 记录 warning。
  - 回退到两者同时存在的最新完整版本。
  - 如果不存在完整共同版本，预热失败，服务不得启动。

### 4.3 版本排序

按以下格式解析版本并逐段使用整数比较：

```text
<major>.<minor>.<patch>-live.<build>
```

例如：

```text
4.10.0-live.100 > 4.9.1-live.999999
```

- 不得使用字符串顺序、目录修改时间或抓取时间判定新旧。
- 无法解析的版本仍可视为历史数据存在，但不得参与当前版本候选，并记录 warning。

## 5. 并发与 SQLite 访问

### 5.1 进程和连接

- 仅支持单 FastAPI 进程、进程内多线程。
- 每个 workflow 线程和请求线程必须独立创建、使用并关闭 SQLite connection。
- 不得在线程之间共享 connection 或 cursor。

### 5.2 读写锁

- 数据库访问使用应用级写优先读写锁。
- 当写线程等待时，新的读线程暂停进入，避免写线程饥饿。
- 读取使用读锁，事务性写入使用写锁。

### 5.3 同一飞船的 single-flight

为每个飞船 `ref` 维护一个进程内互斥锁。多个线程同时请求同一艘尚未缓存的飞船时：

1. 在数据库读锁下检查 `erkul_ships`。
2. 未命中时获取该 `ref` 的互斥锁。
3. 再次查询数据库。
4. 仍未命中时，在不持有数据库写锁的情况下下载、校验和解析详情。
5. 获取数据库写锁。
6. 写入前第三次查询 `erkul_ships`。
7. 只有仍未命中时才写入。

该策略必须避免同一飞船的重复下载和重复写入，同时允许不同飞船并行下载。

## 6. API

### 6.1 健康检查

```text
GET /health
```

成功启动后返回 HTTP `200`：

```json
{
  "status": "ok",
  "current_version": "4.9.0-live.12344265"
}
```

### 6.2 当前版本和历史版本

```text
GET /api/v1/versions
```

- 不接收参数。
- 仅返回 Erkul 和 SCMDB 都为 `complete` 的版本。
- `historical_versions` 不包含 `current_version`。
- `historical_versions` 按从新到旧排序。

响应：

```json
{
  "current_version": "4.9.0-live.12344265",
  "historical_versions": [
    "4.8.1-live.987654"
  ]
}
```

### 6.3 飞船详情

```text
GET /api/v1/ships?name=Hammerhead
```

- 当前只接收一个 query 参数：`name`。
- 始终查询启动时确定的 `current_version`。
- 缺少 `name`、去除首尾空白后为空，或长度超过 200 时返回 HTTP `400`。

## 7. 飞船名称匹配

从 `erkul_ship_catalog` 读取以下三个字段：

```text
name
short_name
display_name
```

同一飞船通过 `class_name` 去重。

### 7.1 名称规范化

对输入和 catalogue 名称统一执行：

1. Unicode `NFKC` 规范化。
2. 使用 `casefold()` 忽略大小写。
3. 将连字符、下划线和其他标点统一为空格。
4. 合并连续空白并去除首尾空白。

例如 `F7C-M` 和 `f7c m` 视为相同名称。

### 7.2 匹配顺序

按以下顺序匹配：

1. **精确匹配**
   - 对三个规范化名称字段做相等比较。
   - 唯一命中时继续获取详情。
   - 多艘命中时返回 `multiple_matches`。
2. **子串匹配**
   - 精确匹配失败后，对三个规范化名称字段做子串匹配。
   - 只命中一艘时直接视为唯一匹配。例如 `hammer` 唯一命中
     `Hammerhead`，应返回详情。
   - 命中多艘时返回 `multiple_matches`。
3. **模糊建议**
   - 精确和子串匹配都失败后，使用
     `difflib.SequenceMatcher.ratio()`。
   - 分别比较三个名称字段，取每艘飞船的最高分。
   - 仅保留相似度 `>= 0.6` 的候选。
   - 按相似度从高到低返回。
   - 真正的模糊匹配不得自动选中飞船，即使只有一个高相似候选。

`possible_matches` 不设数量上限，只使用 `0.6` 阈值控制。返回 catalogue
中较完整的 `name` 字段。多重精确或子串匹配返回全部候选，并使用稳定、
可复现的排序。

## 8. 飞船详情懒加载

唯一匹配后：

1. 使用 catalogue 中的 `ref` 查询 `erkul_ships.ref`。
2. 如果记录存在，读取其 `payload_json`。
3. 如果记录不存在：
   - 从 catalogue 获取 `detail_path` 和 `detail_sha256`。
   - 下载对应 Erkul 详情。
   - 使用 `detail_sha256` 校验内容。
   - 解压并解析 JSON。
   - 原子写入 `erkul/raw/<detail_path>`。
   - 原子写入 `erkul/decoded/<detail_path>.json`。
   - 在 `source_files` 中登记或更新该文件。
   - 按第 5.3 节的 double-check/single-flight 流程写入数据库。
   - 只新增一条 `erkul_ships` 记录，不展开写入
     `erkul_ship_slots` 或 `erkul_default_components`。
4. 懒加载不得修改 `crawl_runs.file_count` 或
   `crawl_runs.record_count`。这两个字段只表示 workflow 完成时的运行统计。

## 9. Manufacturer 关联

读取飞船 `payload_json.manufacturer` 后，在
`erkul_family_manufacturers` 中查找完整 manufacturer：

1. 优先使用 `manufacturer.uuid = erkul_family_manufacturers.ref`。
2. 未命中时使用
   `manufacturer.className = erkul_family_manufacturers.class_name`。
3. 命中时，用 manufacturer 的完整 `payload_json` 替换响应中的引用对象。
4. 两种方式都未命中时：
   - 保留原始 manufacturer 引用。
   - 记录 warning。
   - 仍正常返回飞船详情。

## 10. 飞船响应裁剪

成功响应中的 `ship` 严格只包含以下六个顶层字段：

```text
i18n
manufacturer
precomputed
subType
tags
vehicle
```

- 缺失字段必须保留并设为 `null`。
- 从 `vehicle` 的直接子字段中删除：

  ```text
  hardpoints
  parts
  implementationPath
  ```

- 不递归删除其他层级的同名字段。
- 不得返回 `category`、`className`、`ref`、`slots` 等其他顶层 payload
  字段。

## 11. 飞船接口统一响应

飞船接口始终使用以下外层结构：

```json
{
  "status": "found | not_found | multiple_matches | error",
  "message": "English result description.",
  "possible_matches": [],
  "ship": null
}
```

- 所有 `message` 使用英文。
- `found` 时，`ship` 为裁剪后的详情。
- `not_found`、`multiple_matches` 和 `error` 时，`ship` 为 `null`。
- 无候选时，`possible_matches` 为空数组。

建议消息格式：

```text
Ship found with the name: Hammerhead.
Resolved "hammer" to the unique ship match: Hammerhead.
No ship found with the name: hamerhed. Here are the possible matches: [...]
Multiple ships found with the name: hornet. Here are the possible matches: [...]
```

### 11.1 HTTP 状态

- `found`：HTTP `200`
- `not_found`：HTTP `200`
- `multiple_matches`：HTTP `200`
- 参数缺失、纯空白或超过 200：HTTP `400`
- 上游详情下载、解压、JSON 解析或 SHA-256 校验失败：HTTP `502`
- SQLite 读取或写入失败：HTTP `500`

`400/500/502` 也必须使用统一外层，设置 `status: "error"`，提供清晰的英文
`message`，并返回空 `possible_matches` 和 `ship: null`。

名称未命中和存在歧义是有效的 agent tool 结果，因此必须返回 HTTP `200`，
不得使用 `404` 或 `409`。

## 12. 日志

使用 Python 标准库 `logging`：

- 默认级别为 `INFO`。
- 允许通过 `LOG_LEVEL` 环境变量覆盖。
- 输出到 stdout/stderr。
- 不创建或轮转本地日志文件。
- 日志时间使用 UTC。
- 建议格式：

  ```text
  2026-08-08T12:34:56Z INFO data_server.ship_service message
  ```

必须记录：

- 两个 workflow 的开始、结束、耗时、版本和结果。
- 版本不一致及共同版本回退 warning。
- API 路径、查询名称、结果状态和耗时。
- 详情缓存命中/未命中、下载和写入。
- manufacturer 关联失败 warning。
- 完整异常堆栈。

不得把完整飞船 payload 写入日志。

## 13. 自动化测试

使用离线 `pytest` 测试，所有外部网络请求必须 mock。至少覆盖：

- 游戏版本解析、整数排序、共同版本选择和无共同版本失败。
- 两个 workflow 并行成功。
- 任一 workflow 失败时等待另一个结束并阻止服务启动。
- 名称 NFKC、大小写、标点和空白规范化。
- 精确、唯一子串、多重子串和模糊匹配。
- `SequenceMatcher` 的 `0.6` 阈值和无数量上限结果。
- 飞船接口统一响应结构及 `200/400/500/502` 状态。
- manufacturer UUID 优先关联、className 回退和未命中降级。
- 六字段严格白名单及 `vehicle` 字段删除。
- 同一 `ref` 并发懒加载只下载、登记和写入一次。
- 每个线程使用独立 SQLite connection。
- 健康检查和版本接口。
