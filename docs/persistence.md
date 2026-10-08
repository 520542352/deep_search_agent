# SQLite 持久化运维指南

本项目使用两份职责分离的 SQLite 数据库：

- `data/application.db`：会话、运行、消息、事件和产物元数据。
- `data/checkpoints.db`：LangGraph checkpoint 与中间写入。

上传文件位于 `upload/`，生成文件位于 `output/`。完整恢复必须同时保留两份
数据库和这两个文件目录，否则产物元数据可能指向不存在的文件。

数据库路径可分别通过 `DEEP_SEARCH_DATABASE_PATH` 和
`DEEP_SEARCH_CHECKPOINT_PATH` 覆盖。下列脚本默认读取同样的环境变量。

## 健康检查

健康检查不连接 LLM、Tavily、MySQL 或 RAGFlow：

```powershell
uv run python scripts/backup_sqlite.py health
```

脚本对两份数据库执行 `PRAGMA quick_check`，检查必需表，并验证业务库的迁移
版本是否与当前代码一致。返回 JSON；健康时退出码为 `0`，缺库、损坏、缺表或
迁移版本不一致时退出码为 `1`。

使用自定义路径：

```powershell
uv run python scripts/backup_sqlite.py health `
  --application-db D:\deep-search\application.db `
  --checkpoint-db D:\deep-search\checkpoints.db
```

## 创建和校验备份

创建备份包：

```powershell
uv run python scripts/backup_sqlite.py backup --destination D:\backups
```

脚本使用 SQLite Online Backup API 读取数据库，不会直接复制正在使用的 WAL
文件。每份数据库各自是事务一致的快照；由于两份数据库依次备份，它们不是同一
时刻的跨库原子快照。需要严格的跨库时间点一致性时，应先停止 API 服务再备份。

备份目录形如 `deep-search-20261008T010203Z/`，内容包括：

```text
deep-search-20261008T010203Z/
├── databases/
│   ├── application.db
│   └── checkpoints.db
├── files/
│   ├── upload/
│   └── output/
└── manifest.json
```

`manifest.json` 记录每个文件的大小和 SHA-256。创建完成前使用隐藏的
`.incomplete` 目录，只有两份数据库通过完整性检查后才发布最终备份目录。

独立校验已有备份：

```powershell
uv run python scripts/backup_sqlite.py verify `
  D:\backups\deep-search-20261008T010203Z
```

校验包含清单格式、路径边界、文件集合、大小、SHA-256 以及两份数据库的
`PRAGMA quick_check`。应将备份复制到异机或对象存储；不要只保存在项目磁盘。

建议至少每天备份一次，并保留多个时间点。实际频率和保留期应根据任务量、文件
大小及可接受的数据丢失窗口确定。

## 恢复与演练

恢复命令只允许写入不存在或为空的目录，不会覆盖当前运行实例：

```powershell
uv run python scripts/backup_sqlite.py restore `
  D:\backups\deep-search-20261008T010203Z `
  D:\restore-drill
```

恢复后目录包含 `data/`、`upload/`、`output/` 和 `restore-manifest.json`。建议按
以下步骤演练：

1. 对备份执行 `verify`。
2. 恢复到新的空目录。
3. 对恢复出的两个数据库执行 `health`，显式传入恢复路径。
4. 检查 `upload/` 和 `output/` 中的代表性文件。
5. 在不连接真实外部服务的环境中启动只读检查，确认历史查询和产物下载。

真正切换生产数据时，先停止 API 服务并确认没有 Python、SQLite 客户端或运维
脚本仍持有数据库连接。保留当前 `data/`、`upload/` 和 `output/` 作为回滚副本，
再把恢复结果放到配置路径；不要在服务运行时覆盖数据库文件。

## 保留与清理

清理工具仅选择满足全部条件的会话：

- `threads.status = 'archived'`；
- `updated_at` 早于给定保留期；
- 不存在 `queued` 或 `running` run。

默认只预览，不写数据库或删除文件：

```powershell
uv run python scripts/prune_persistence.py --older-than-days 90
```

JSON 输出中的 `thread_ids` 是候选集合。确认候选无误后：

1. 创建并校验最新备份。
2. 停止 API 服务及其他 SQLite 客户端。
3. 再使用 `--apply`：

```powershell
uv run python scripts/prune_persistence.py --older-than-days 90 --apply
```

执行模式会拒绝存在 `-wal` 或 `-shm` 辅助文件的数据库，以降低服务仍在运行时
误删数据的风险。不要手工删除这些辅助文件；应正常停止持有数据库连接的进程。

清理业务库时由外键级联删除 runs、messages、events 和 artifacts；随后删除同一
会话的 checkpoint/writes，以及 `upload/session_<thread_id>` 和
`output/session_<thread_id>`。如果文件清理失败，数据库记录可能已删除，但剩余
目录只是孤立文件，可以在确认备份后人工移除。

当前 API 尚未提供归档会话的写接口，因此工具不会自动清理普通 `active` 会话。
这是有意的安全边界：必须先通过受控管理流程把会话标记为 `archived`，清理预览
才会选中它。

## 故障处理

- `health` 报迁移版本落后：使用当前代码正常启动一次应用，让迁移器升级业务库，
  不要手工伪造 `schema_migrations` 记录。
- `quick_check` 非 `ok`：停止写入，保留原文件和 WAL 辅助文件，从最近一次已校验
  备份恢复；不要继续运行清理。
- 备份校验和不一致：该备份不可用于恢复，重新创建备份并检查存储介质。
- 恢复后产物缺失：确认备份包内同时存在 `files/upload` 与 `files/output`，并检查
  运行实例配置的目录是否指向恢复位置。

## 迁移到 PostgreSQL 的边界

业务代码通过 `ConversationRepository`、`EventRepository` 和
`ArtifactRepository` 访问应用数据，未来可在保持这些接口和 API Schema 不变的
前提下替换存储实现。需要迁移的内容包括：

- `threads`、`runs`、`messages`、`events`、`artifacts` 及约束和索引；
- `one_active_run_per_thread` 部分唯一索引及事务竞争语义；
- SQLite 时间表达式、`BEGIN IMMEDIATE` 和自增事件游标的 PostgreSQL 等价实现；
- LangGraph checkpointer 更换为 PostgreSQL 后端；
- 产物二进制文件继续使用文件/对象存储，数据库只保存元数据和稳定标识。

迁移应采用“双写或停机导出 → 行数及校验和核对 → 切换读取 → 保留回滚窗口”的
独立项目完成。本阶段不引入数据库抽象层或双写逻辑，避免在仍使用 SQLite 时增加
无实际收益的复杂度。
