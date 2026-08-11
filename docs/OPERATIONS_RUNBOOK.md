# 运行与故障处理手册

> 适用版本：V1
> 数据库：MySQL 5.7.36 或 MySQL 8.x
> 原则：生产凭据不落盘；集成测试永远不指向公司库；真实采集必须通过门禁

## 1. 首次部署

1. 建立专用空库，字符集 `utf8mb4`、排序规则 `utf8mb4_general_ci`；
2. 通过进程环境或公司密钥系统注入 `MYSQL_*`，不要写入 `.env`、镜像或命令历史；
3. 执行 `uv run alembic upgrade head`；
4. 执行 `uv run device-price db seed`；
5. 执行 `uv run device-price db check`；
6. 执行 `uv run device-price db audit`；
7. 默认保持 `LIVE_CRAWL_ENABLED=false`；真实测试由操作者显式开启，允许使用不含公司或个人信息的 `DevicePriceTestBot/1.0`。

公司 MySQL 5.7 环境还应确认 14 个 `trg_*_validate_*` 触发器存在。详见 [MySQL 5.7 兼容说明](MYSQL_57_COMPATIBILITY.md)。

## 2. 运行入口

```bash
uv run device-price adapters
uv run device-price crawl --brand APPLE
uv run device-price scheduler
```

调度器每品牌只允许一个实例，进程间由 MySQL 命名锁互斥。建议容器设置自动重启，但不要同时启动多个用途相同的常驻调度容器。

## 3. 数据保护规则

- HTTP 层对网络错误、429 和可重试的 5xx 最多重试三次；
- 单商品失败只记录失败证据，不覆盖现有价格；
- 超过 `PRICE_CHANGE_CONFIRM_THRESHOLD` 的价格变化必须立即复抓，两个价格状态完全一致才写入；
- 商品连续缺失 `MISSING_CONFIRMATION_RUNS` 次后必须访问详情页确认；只有详情返回 404/410，或详情成功解析出下架状态，才更新下架状态；
- 发现数量低于 `DISCOVERY_COUNT_FLOOR_RATIO` 时批次标记异常并报警，但缺失商品仍须经过详情确认，不直接下架；
- 乱序观测、当前价与开放历史不一致会使事务失败，不允许人工强行覆盖。

## 4. 巡检与报警

```bash
uv run device-price db audit
```

该命令只执行 `SELECT`。输出为 JSON；关键一致性检查非零时退出码为 1。至少每 15 分钟执行一次，并将非零退出码及 JSON 接入公司现有报警系统。

关键项包括：

- 当前价与唯一开放历史的基数和状态一致性；
- 历史区间是否重叠；
- 当前价能否追溯到成功采集证据；
- 渠道是否仍为中国大陆、人民币、官方直营；
- 是否存在超过阈值仍为 `RUNNING` 的批次；
- 是否存在过期价格、待确认下架或最近被拒绝的大幅价格变化。

在 `LIVE_CRAWL_ENABLED=true` 时，从未完成过有效批次的启用渠道也会作为关键故障。

## 5. 备份

备份必须使用公司批准的安全目录和访问控制，文件不得进入 Git、镜像或原始证据目录。推荐每天至少一次逻辑备份，并保留迁移前备份：

```bash
mysqldump \
  --single-transaction \
  --quick \
  --routines \
  --triggers \
  --default-character-set=utf8mb4 \
  --set-gtid-purged=OFF \
  --host="$MYSQL_HOST" \
  --port="$MYSQL_PORT" \
  --user="$MYSQL_USER" \
  --password \
  device_price > /secure/backup/device_price_YYYYMMDD_HHMMSS.sql
```

使用 `--password` 让客户端交互读取密码，不在参数中暴露。备份完成后记录文件大小和 SHA-256，并按公司策略加密和轮换。

## 6. 恢复演练

恢复演练只能使用专用临时实例或明确授权的恢复测试库，禁止覆盖生产 `device_price`：

1. 启动与生产大版本相同的空 MySQL；
2. 导入最近备份；
3. 执行 `device-price db check` 和 `device-price db audit`；
4. 核对 Alembic 版本、10 张业务表、种子数量、触发器数量和关键表行数；
5. 使用 fixture 执行一次写入和重放；
6. 删除临时实例，保存演练时间、恢复耗时、校验结果和备份哈希。

至少每季度演练一次，迁移或备份方式变更后额外演练一次。

## 7. 故障处置

### 批次卡死

1. 运行 `db audit` 确认 `stale_running_crawl`；
2. 检查同渠道容器和 MySQL 命名锁；
3. 保存日志后重启单个调度进程；
4. 下一批次会把超过 `CRAWL_STALE_AFTER_MINUTES` 的旧批次标为失败。

### 大幅价格变化被拒绝

1. 查询 `crawl_record.error_code='LARGE_PRICE_CHANGE_UNCONFIRMED'` 的两份证据；
2. 人工查看官方页面对应 SKU 总价；
3. 若页面稳定变化，等待下一批再次双观测确认；
4. 若解析规则错误，修复 adapter、增加脱敏 fixture 后发布；
5. 不直接修改 `price_current`。

### 发现数量骤降

1. 查看批次 `DISCOVERY_COUNT_GUARD`；
2. 检查 403、429、验证码、页面结构和品类入口；
3. 核对待确认 offer 的 `consecutive_misses`；
4. 只有详情确认后才接受下架；
5. 修复后使用 fixture 重放，再恢复调度。

### 数据一致性报警

1. 立即停止对应渠道调度，保留数据库和证据；
2. 执行只读查询定位 offer、批次和历史记录；
3. 不手工删除历史或重建当前价；
4. 从代码缺陷、失败迁移或越权人工操作中确定原因；
5. 在隔离副本验证修复或恢复方案后再处理生产。

## 8. 发布与回滚

发布前必须通过 Ruff、mypy、单元测试以及 MySQL 8.4/5.7.36 集成测试。数据库迁移先备份，再执行升级；MySQL DDL 非事务性，不能把失败迁移假设为自动回滚。

应用回滚只回滚镜像，不自动降级数据库。若迁移需要降级，必须先在备份副本演练并获得审批。adapter 规则不得在生产容器中临时修改。
