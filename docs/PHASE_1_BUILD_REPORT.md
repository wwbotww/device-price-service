# 阶段 1 构建报告

> 完成日期：2026-08-05  
> 阶段状态：已完成  
> 验证数据库：Docker MySQL 8.4.11

## 1. 已完成内容

- Python 3.11+ 工程和 `uv.lock` 依赖锁；
- `.env` 配置、JSON 结构化日志和 Typer CLI；
- Dockerfile、Docker Compose、本地开发库与独立测试库；
- SQLAlchemy 2.x 模型，共 10 张业务表；
- Alembic 初始迁移及可逆降级；
- 5 个品牌、6 个品类节点和 5 个官方渠道的幂等种子数据；
- Product、SKU、Offer 的幂等 Repository；
- CrawlRun 创建和结束 Repository；
- 当前价格与价格历史的事务写入；
- 相同状态延长历史区间、价格/库存状态变化新开历史区间；
- 旧观察值覆盖保护和事务回滚；
- 单元测试、MySQL 集成测试、迁移升级/降级测试；
- 阶段 0 合规门禁和生产待办清单。

## 2. 公司 MySQL 的替代测试方案

当前无法访问公司 MySQL，因此使用官方 Docker `mysql:8.4` 镜像：

- `device_price`：本地开发和迁移验证；
- `device_price_test`：自动化集成测试；
- 字符集：`utf8mb4`；
- 排序规则：`utf8mb4_0900_ai_ci`；
- 数据库时区：UTC。

没有使用 SQLite，因为 SQLite 无法可靠验证 MySQL 的 `DATETIME(3)`、JSON、外键索引、检查约束、行锁和非事务 DDL 行为。

真实 MySQL 验证过程中发现并修复：

1. `DATETIME` 与 `CURRENT_TIMESTAMP(3)` 精度不匹配导致迁移失败；
2. Alembic 自动降级先删除外键依赖索引，导致 MySQL 拒绝降级；
3. `autoflush=False` 会话中，种子渠道写入后无法立即查询。

这些问题均不会被只依赖 SQLite 或 mock 的测试可靠发现。

## 3. 验证结果

执行命令：

```bash
uv run ruff check .
uv run mypy src
RUN_MYSQL_INTEGRATION=1 uv run pytest
uv run alembic check
uv run alembic downgrade base
uv run alembic upgrade head
uv run device-price db seed
uv run device-price db check
```

最终要求：

- Ruff：通过；
- mypy strict：通过；
- 单元与 MySQL 集成测试：13/13 通过；
- Alembic：升级、降级、重新升级通过；
- 模型漂移：无新增迁移操作；
- 数据库检查：10 张业务表完整；
- 种子数据：5 个品牌、6 个品类、5 个官方渠道。
- Docker 镜像：`device-price-service:phase1` 构建成功，非 root 用户下 CLI 启动成功。

## 4. 阶段边界

阶段 1 不包含实际商城请求、品牌 adapter、HTML/JSON 解析器、APScheduler 和正式爬取任务；这些属于阶段 2 及之后。

阶段 0 的技术基线已经完成，但生产爬取授权仍需公司法务或数据负责人确认。授权完成前不能开启持续官网采集。

## 5. 切换公司 MySQL 时的检查

恢复公司内网后：

1. 创建专用数据库和最小权限账号；
2. 确认 MySQL 版本不低于项目支持版本；
3. 确认字符集为 `utf8mb4`、时区为 UTC；
4. 使用公司 Secret 注入连接信息；
5. 先执行 `alembic upgrade head`；
6. 执行 `device-price db seed` 和 `device-price db check`；
7. 在空测试库运行完整 integration suite；
8. 不直接复制本地 Docker 数据目录到公司数据库。
