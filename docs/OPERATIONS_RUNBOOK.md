# 运行与故障处理手册

> 适用版本：V1 demo + V2 阶段 H 政府生鲜来源
> 数据库：MySQL 5.7.36 或 MySQL 8.x
> 原则：生产凭据不落盘；集成测试永远不指向公司库；真实采集必须通过门禁

V2 已在公司 `device_price` 部署：商务部 15 个跨地区批发品种为主来源，上海 8 个零售均价为补充。项目只提供手工 smoke 和单次采集，不注册 V2 常驻调度。2026-08-25 的重建、备份、数据量和质量核对见 [阶段 H 公司库验收报告](V2_PHASE_H_COMPANY_ACCEPTANCE_REPORT.md)。

## 1. 首次部署

1. 建立专用空库，字符集 `utf8mb4`、排序规则 `utf8mb4_general_ci`；
2. 通过进程环境或公司密钥系统注入 `MYSQL_*`，不要写入 `.env`、镜像或命令历史；
3. 执行 `uv run alembic upgrade head`；
4. V1 demo 需要参考数据时执行 `uv run device-price db seed`；只运行政府数据时无需写 V1 种子；
5. 执行 `uv run device-price db seed-v2-government`，确认两个来源默认禁用；
6. 执行 `uv run device-price db check`；
7. 执行 `uv run device-price db audit`；
8. 默认保持 `LIVE_CRAWL_ENABLED=false`；真实测试由操作者显式开启，允许使用不含公司或个人信息的 `DevicePriceTestBot/1.0`。

官方镜像已为非 root 用户创建默认 `/app/var/raw`。若通过 `RAW_STORAGE_PATH` 或卷挂载改用其他证据目录，部署方必须预先授予容器用户 `65532:65532` 写权限。

只含 V1 的公司 MySQL 5.7 环境应有 14 个 `trg_*_validate_*` 触发器；完整 V1+V2 head 在专用测试库中应有 40 个。详见 [MySQL 5.7 兼容说明](MYSQL_57_COMPATIBILITY.md)。

## 2. 运行入口

```bash
uv run device-price adapters
uv run device-price crawl --brand APPLE
uv run device-price scheduler
```

调度器每品牌只允许一个实例，进程间由 MySQL 命名锁互斥。建议容器设置自动重启，但不要同时启动多个用途相同的常驻调度容器。

### 2.1 V2 政府来源

先在专用 V2 数据库初始化；默认来源保持禁用：

```bash
uv run alembic upgrade head
uv run device-price db seed-v2-government
uv run device-price catalog sources
```

真实只读 smoke 只访问各连接器批准的政府域名，不连接或写入 MySQL：

```bash
LIVE_CRAWL_ENABLED=true \
  uv run device-price catalog smoke --channel SH_FGW_FRESH_RETAIL

LIVE_CRAWL_ENABLED=true \
  uv run device-price catalog smoke \
  --channel MOFCOM_FRESH_WHOLESALE --commodity ALL
```

确认上海输出 `parsed_count=8`，商务部 15 个数据集均输出来源日期和非零 `price_count` 后，才可在目标数据库显式启用并单次采集：

```bash
uv run device-price db seed-v2-government --enable
LIVE_CRAWL_ENABLED=true \
  uv run device-price catalog crawl --channel SH_FGW_FRESH_RETAIL

LIVE_CRAWL_ENABLED=true \
  uv run device-price catalog crawl \
  --channel MOFCOM_FRESH_WHOLESALE --commodity ALL
```

上海 `catalog crawl` 每批发现目录和文章各一次、下载 XLS 一次；8 条价格共享一个 `v2_crawl_record`。商务部每个品种页独立发现和下载，页面内所有市场价格共享一个 `v2_crawl_record`；省略 `--commodity` 或使用 `ALL` 会按白名单顺序执行 15 个独立批次。关闭对应 `v2_source_channel.enabled` 即可阻止后续写入。V2 demo 不运行 scheduler。

### 2.2 当前公司库状态

- Alembic head：`96524222b3ec`；23 张业务表；MySQL 5.7 下 40 个校验触发器；
- 10 张 V1 兼容表为空，政府数据只写 13 张 `v2_` 表；
- 两个政府来源已显式启用，但没有任何常驻采集进程；
- 最近一次完整采集为商务部 1,354 条、上海 8 条，共 1,362 条当前价格；
- 当前仍暂用 root，密码只通过进程环境或交互输入，不得复制到仓库或命令行参数。

需要更新时先运行只读 smoke，再按上面的 `catalog crawl` 命令手工采集。相同来源日和相同行证据会幂等复用，不能用手工 SQL 直接改 `v2_price_current`。

## 3. 数据保护规则

- HTTP 层对网络错误、429 和可重试的 5xx 最多重试三次；
- 单商品失败只记录失败证据，不覆盖现有价格；
- 超过 `PRICE_CHANGE_CONFIRM_THRESHOLD` 的价格变化必须立即复抓，两个价格状态完全一致才写入；
- 商品连续缺失 `MISSING_CONFIRMATION_RUNS` 次后必须访问详情页确认；只有详情返回 404/410，或详情成功解析出下架状态，才更新下架状态；
- 发现数量低于 `DISCOVERY_COUNT_FLOOR_RATIO` 时批次标记异常并报警，但缺失商品仍须经过详情确认，不直接下架；
- 乱序观测、当前价与开放历史不一致会使事务失败，不允许人工强行覆盖。
- 政府来源日期写入 `v2_price_observation.observed_at`，下载时间写入 `v2_crawl_record.fetched_at`；完整原文件哈希保存在抓取记录，价格观察使用行级证据哈希。同一价格行重采不新增事实，同日变价只为实际变化行形成可追溯修订链。
- 政府 XLS/HTML 出现缺列、日期冲突、未知单位、市场 ID 异常、金额异常或损坏内容时，整份数据集不推进当前价，但保留失败证据和稳定错误码。

## 4. 巡检与报警

```bash
uv run device-price db audit
```

该命令只执行 `SELECT`。输出为 JSON；关键一致性检查非零时退出码为 1。demo 验收时手工执行即可；若未来生产化，再由现有运维平台决定执行频率和报警接入。

`db audit` 仍以 V1 运行指标为主。V2 手工采集必须核对命令退出码、来源日期、`v2_crawl_run` 计数以及 `v2_crawl_record.error_code`；当前不建设 V2 停更或记录数骤降报警，不能把 V1 audit 通过当成 V2 来源健康。

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
4. 核对 Alembic 版本、部署范围对应的 10 或 23 张业务表、种子数量、触发器数量和关键表行数；
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

### 政府数据集结构变化

1. 停止或禁用对应政府来源，保留最近可信当前价；
2. 查看失败 `v2_crawl_record.error_code`：日期冲突、附件损坏、市场身份异常、数据集结构变化和品种映射缺失分别处理；
3. 只在临时目录检查新页面或附件，不把完整真实内容提交 Git；
4. 更新精确映射和脱敏 HTML/XLS fixture，在 MySQL 5.7/8.x 各重放两次；
5. 通过一次真实只读 smoke 后再恢复单次采集。

## 8. 发布与回滚

发布前必须通过 Ruff、mypy、单元测试以及 MySQL 8.4/5.7.36 集成测试。数据库迁移先备份，再执行升级；MySQL DDL 非事务性，不能把失败迁移假设为自动回滚。

应用回滚只回滚镜像，不自动降级数据库。若迁移需要降级，必须先在备份副本演练并获得审批。adapter 规则不得在生产容器中临时修改。
