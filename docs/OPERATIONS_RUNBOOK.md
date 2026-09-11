# 运行与故障处理手册

> 适用版本：V2 政府生鲜 + 五品牌原生设备；I～L 已完成，M 未通过最终验收，待 6 组 iMac 身份限定修复授权
> 数据库：MySQL 5.7.36 或 MySQL 8.x
> 原则：生产凭据不落盘；集成测试永远不指向公司库；真实采集必须通过门禁

公司 `device_price` 保存 V2 政府生鲜：商务部 15 个跨地区批发品种为主来源，上海 8 个零售均价为补充。2026-09-11 已完成设备约束增量迁移和五品牌 V2 复采；严格重放发现 6 组历史 iMac 重复身份，待限定数据修复授权，尚未通过最终验收。所有来源采集已停止，无后台任务。默认手工运行，设备调度仅为显式可选入口，政府不自动调度。历史重建见[阶段 H 报告](V2_PHASE_H_COMPANY_ACCEPTANCE_REPORT.md)，当前状态见[阶段 M 记录](V2_PHASE_M_ACCEPTANCE_REPORT.md)。

## 1. 首次部署

1. 建立专用空库，字符集 `utf8mb4`、排序规则 `utf8mb4_general_ci`；
2. 通过进程环境或公司密钥系统注入 `MYSQL_*`，不要写入 `.env`、镜像或命令历史；
3. 执行 `uv run alembic upgrade head`；
4. 需要采集设备时执行 `uv run device-price db seed-devices`，默认来源禁用且不访问网站；
5. 执行 `uv run device-price db seed-v2-government`，确认两个来源默认禁用；
6. 执行 `uv run device-price db check`；
7. 执行 `uv run device-price db audit`；
8. 默认保持 `LIVE_CRAWL_ENABLED=false`；真实测试由操作者显式开启，允许使用不含公司或个人信息的 `DevicePriceTestBot/1.0`。

官方镜像已为非 root 用户创建默认 `/app/var/raw`。若通过 `RAW_STORAGE_PATH` 或卷挂载改用其他证据目录，部署方必须预先授予容器用户 `65532:65532` 写权限。

只含 V1 的公司 MySQL 5.7 环境应有 14 个 `trg_*_validate_*` 触发器；完整 V1+V2 head 在专用测试库中应有 40 个。详见 [MySQL 5.7 兼容说明](MYSQL_57_COMPATIBILITY.md)。

## 2. 运行入口

```bash
uv run device-price catalog sources
# 先确认来源、目标库与真实访问授权，再显式打开开关
LIVE_CRAWL_ENABLED=true uv run device-price catalog smoke --channel HUAWEI_CN_WEB
LIVE_CRAWL_ENABLED=true uv run device-price catalog crawl --channel HUAWEI_CN_WEB
```

采集只有 `catalog` 一条业务入口，设备与政府共享 V2 流水线。只读重放见第 2.6 节，可选设备调度见第 2.7 节；默认不开启后台任务。

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

上海 `catalog crawl` 每批发现目录和文章各一次、下载 XLS 一次；8 条价格共享一个 `v2_crawl_record`。商务部每个品种页独立发现和下载，页面内所有市场价格共享一个 `v2_crawl_record`；省略 `--commodity` 或使用 `ALL` 会按白名单顺序执行 15 个独立批次。关闭对应 `v2_source_channel.enabled` 即可阻止后续写入。政府来源不注册 scheduler。

### 2.2 公司库状态

2026-09-10 历史基线：

- Alembic head：`96524222b3ec`；23 张业务表；MySQL 5.7 下 40 个校验触发器；
- V1 已恢复五品牌设备 demo：103 个产品、1,114 条 SKU 当前价及历史；政府数据仍只写 13 张 `v2_` 表，电子设备尚未接入 V2；
- 两个政府来源已显式启用，但没有任何常驻采集进程；
- V2 最近一次完整采集仍为 2026-08-25：商务部 1,354 条（来源日 8 月 23 日）、上海 8 条（来源日 8 月 25 日），共 1,362 条当前价格；9 月 10 日的设备重采未刷新这些生鲜价格；
- 当前仍暂用 root，密码只通过进程环境或交互输入，不得复制到仓库或命令行参数。

需要更新时先运行只读 smoke，再按上面的 `catalog crawl` 命令手工采集。相同来源日和相同行证据会幂等复用，不能用手工 SQL 直接改 `v2_price_current`。

设备恢复结果、批次数量、原价和库存状态限制见[V1 重采验收记录](V1_RECOLLECTION_20260910_REPORT.md)。

2026-09-11 阶段 M 已完成获准备份、本地专用 MySQL 5.7 恢复校验及迁移演练，公司库已从 `96524222b3ec` 增量升级至 `b72c910e4f31`；仍为 MySQL 5.7.36-log、23 张业务表和 40 个触发器。迁移仅更换 4 个 V2 校验触发器和 Alembic 版本，既有业务行、表 DDL、其余触发器与其他 schema 结构元数据未变。

修复后的五品牌复采已完成：华为批次 23（49 产品 / 460 条接受观察）、小米 24（17 / 138）、OPPO 26（12 / 110）、vivo 27（10 / 115）均为 `SUCCEEDED`；Apple 批次 25 发现 19 产品、成功解析 16 产品、接受 306 条观察，实际错误汇总为 `APPLEPARSEERROR=3`（三个 Watch 组合页）和 `SKU_COVERAGE_INCOMPLETE=1`（iMac），批次为 `PARTIAL`。这些数字是批次计数，不是当前库总量或全站覆盖承诺。

最终数据库审计全部 critical 为 0，165 份证据引用检查通过，V1/政府全部既有行及其他 schema 的 11 类结构元数据未变；但严格重放记录 49 暴露 6 组 iMac 重复来源身份。不同来源 ID 对应同一真实规格，不能直接改键以规避唯一约束，也不能未经授权合并或删除已入库事实。当前等待仅合并这 6 组身份、保留全部价格时点与证据的限定授权，所有采集已停止；完整结果与拟修复边界见[阶段 M 记录](V2_PHASE_M_ACCEPTANCE_REPORT.md)。

### 2.3 单独恢复或更新 V1 设备 demo

本节原五品牌流程已于 2026-09-10 验证，属于历史基线。当前分支不再提供 V1 设备重采；旧顶层 `crawl/smoke --brand`、`adapters`、`replay` 和 `db seed` 已删除。新采集使用第 2.5 节，可选 scheduler 只调用 V2，不清表、不搬运旧价格。

`db check/audit` 已统一为 V2 工具，不检查或写入 V1 数据；V1 静态内容继续保留，原采集结果见[V1 重采报告](V1_RECOLLECTION_20260910_REPORT.md)。

已写入的 V1 价格可用以下只读查询查看（时间字段为 UTC）：

```sql
SELECT b.code AS brand, p.name AS product, s.name AS sku,
       pc.original_price, pc.original_price_type, pc.current_price,
       pc.currency, oo.availability, pc.observed_at, oo.source_url
FROM price_current pc
JOIN official_offer oo ON oo.id = pc.offer_id
JOIN sku s ON s.id = oo.sku_id
JOIN product p ON p.id = s.product_id
JOIN brand b ON b.id = p.brand_id
ORDER BY b.code, p.id, s.id;
```

### 2.4 设备 V2 基础初始化（阶段 I～L，本地验证）

代码与公司库 head 均为 `b72c910e4f31`：阶段 I～L 完成本地验证，阶段 M 已完成公司增量迁移。此迁移只更新 V2 产品证据和无价状态约束，不新增/删除业务表，也不从 V1 复制价格。以下用于本地开发初始化；先将 `MYSQL_*` 指向本地专用开发库，检查连接目标后执行：

```bash
uv run alembic upgrade head
uv run device-price db seed-devices
uv run device-price catalog sources
```

种子输出应为 `5 brands, 7 categories, 5 sources; enabled=0; no collection started`（全新设备种子时）。重复执行不会产生重复记录，不会关闭已有启用来源，也不会修改政府种子。五个品牌为 Apple、华为、小米、OPPO、vivo，设备分类含两个父节点和手机、平板、笔记本、台式机、手表五个叶节点。

`db seed-devices --enable` 仅显式启用数据库中的设备来源，不访问官网或启动采集。`catalog sources` 显示两个政府连接器和五品牌设备，均可用 `catalog crawl`；不能把旧 `crawl --brand` 当作 V2 写库入口。`db check` 只要求 13 张 V2 表，缺少 V1 不阻止运行；额外表可共存且被忽略。`db audit` 也只检查 V2。

业务运行时元数据只注册 V2；历史 Alembic 链保持不变，完整升级仍创建 V1 10 表和 V2 13 表。自动生成迁移只反射 `v2_` 表，排除旧表和其他业务表，本阶段没有新增删表迁移。正式部署使用 Alembic，不能用 `metadata.create_all` 代替 MySQL 5.7 触发器安装。

双版本验收仅使用本地专用 `device_price_test`：

```bash
RUN_MYSQL_INTEGRATION=1 TEST_MYSQL_HOST=127.0.0.1 \
  TEST_MYSQL_PORT=3307 TEST_MYSQL_DATABASE=device_price_test make test-integration
RUN_MYSQL_INTEGRATION=1 TEST_MYSQL_HOST=127.0.0.1 \
  TEST_MYSQL_PORT=3308 TEST_MYSQL_DATABASE=device_price_test make test-integration
```

这些命令会清空专用测试库，禁止改为公司目标。含 `PRODUCT` 或 `AVAILABILITY_ONLY` 事实时，新迁移会在执行 DDL 前拒绝降级；不得删除证据来强行回退。后续公司部署仍需备份、确认目标并暂停写入；MySQL DDL 不具备事务回滚，不应在采集并发写入时替换约束。重建触发器前记录其 SQL_MODE、definer、字符集和排序规则，迁移连接应保持已有触发器的会话属性，并在迁移后逐项核验；不得用 `SET GLOBAL` 调整实例环境。本轮部署核验见[阶段 M 记录](V2_PHASE_M_ACCEPTANCE_REPORT.md)。

### 2.5 五品牌原生 V2 入口（阶段 J～K）

阶段 I～L 已完成脱敏 fixture 和本地 MySQL 验收；阶段 M 已完成公司增量迁移和五品牌复采，但因历史 iMac 重复身份尚未通过最终验收，当前无后台采集。以下是通用运行入口，不是当前待授权的数据修复命令。获准真实运行时，先确认目标库与开关，再执行（渠道可替换为 `HUAWEI_CN_WEB/XIAOMI_CN_WEB/OPPO_CN_WEB/VIVO_CN_WEB`）：

```bash
# 不连接数据库：发现产品，抽样完整解析 SKU，并执行静态质量校验
LIVE_CRAWL_ENABLED=true uv run device-price catalog smoke --channel APPLE_CN_WEB --max-products 1
# 以下仅用于已确认并升级的目标库；--enable 不会自行开始采集
uv run device-price db seed-devices --enable
LIVE_CRAWL_ENABLED=true uv run device-price catalog crawl --channel APPLE_CN_WEB
```

正式采集每个产品一个事务，所有 SKU 共用一组 `PRODUCT` 证据，直接建立 `v2_catalog_item/item_variant/listing_match` 及价格事实。仅失败的产品回滚，其他成功产品保留；失败证据仍保存。重放旧时点不恢复生命周期或更新当前 URL，未知 SKU 状态不等于有货，Apple Watch 总价缺口仍按严格规则拒绝。

设备单产品写事务单独使用 `READ COMMITTED`，连接归还后恢复默认隔离级别；政府事务与实例全局配置不变。运行前只读确认 `binlog_format` 为 `ROW/MIXED`，若为 `STATEMENT` 则停止设备写入并确认环境方案，不得擅自 `SET GLOBAL`。公司本轮已确认全局/检查会话为 `ROW` 和 `REPEATABLE-READ`。来源/地区命名锁、父 revision 锁及匹配约束继续生效，具体并发修复见[兼容说明](MYSQL_57_COMPATIBILITY.md#22-设备并发写入的事务隔离)。

命令输出包含 `status`：`SUCCEEDED` 才退出 0，`PARTIAL/FAILED` 退出 1，参数、真实开关或数据库来源配置门禁不通过退出 2。除来源日期外应检查 `accepted_count/review_count/rejected_count/failed_count`；不能只看失败请求数或新增行数。V2 重放使用第 2.6 节的 `catalog replay`，不再提供旧顶层 `replay`。

华为/小米依赖 Chromium，先执行 `make browser-install`。设备保护沿用 `.env.example` 中既有配置：价差阈值 30%、缺失确认 3 次、发现产品数量下限 50%、过期批次 120 分钟。无需增加配置或调度服务。

| 结果/错误码 | 含义与处理 |
| --- | --- |
| `PRICE_CHANGE_CONFIRMATION_REQUIRED` | 第一次突变证据待确认；同时检查同批同产品第二条记录，最终成功不代表第一份证据单独可信 |
| `PRICE_CHANGE_UNCONFIRMED` | 两次配置/价格/状态不同、第二次不完整或旧时点；旧可信价保留，检查证据后手工重采 |
| `DISCOVERY_COUNT_BELOW_FLOOR` | 发现产品数骤减；本批不写价、不累计缺失，检查发现页是否改版 |
| `SKU_COVERAGE_INCOMPLETE` | 已有 SKU 未遍历到；批次 PARTIAL，已确认 SKU 可写，遗漏 SKU 不推断下架 |
| `PRODUCT_ABSENCE_UNCONFIRMED` | 证据粒度不足或有跳转；华为/OPPO 的选中 SKU 404 不能代表整产品下架 |
| `OFF_SHELF_CONFIRMED` | 连续完整采集缺失后，整产品端点明确 404/410；新事实全部金额为空，旧价仍在历史中 |
| `STALE_RUNNING_RECOVERED` | 同来源/地区/品类的设备旧 RUNNING 已恢复为 FAILED，不涉及其他范围或政府批次 |

缺失仅在完整默认品类、非查询重采、非重放且新时点的发现与已发现产品采集全部成功时累计；后续缺失确认失败会使最终批次 PARTIAL，但不会伪造状态。已见产品恢复失败、解析失败、403、网络异常不恢复 ACTIVE、不清零缺失次数。政府来源不套用设备价差、下架或新增过期恢复规则。

### 2.6 原始证据只读重放

配置目标库只读访问和原采集的 `RAW_STORAGE_PATH`，将 `123` 替换为 `v2_crawl_record.id`：

```bash
uv run device-price catalog replay --record-id 123
# 等效 Makefile 入口
make catalog-replay RECORD_ID=123
```

不需要打开真实采集开关，不创建抓取器，不联网、不写库，也不创建不存在的证据目录。输出包含 `STATIC_REPLAY`、原始抓取时间、逐 SKU/政府行静态校验、原批次与记录的 `historical_validation` 和历史事实。静态结果用于核验当前解析规则，不代表重新认证历史状态或新的报价时点。

新记录从不可变 manifest 恢复产品/文档及原请求范围，不读取后来变化的 listing 字段猜上下文；已部署的旧商务部/上海记录可根据已保存的来源页面、日期和白名单确定恢复。无法恢复、缺少文件、哈希不符或压缩损坏均退出 1；不补抓、不修改原证据。

共享产品证据输出所有 SKU。待复核变价证据即使当前静态解析成功仍为 `PARTIAL`，退出 1；应查看同批第二条确认记录。已确认 404/410 只核对并展示原无价状态事实，不解析错误页面、不复制旧金额。`SUCCEEDED` 退出 0，其他重放结果退出 1，命令参数错误退出 2。

### 2.7 可选的显式设备调度

默认不启动。仅在已授权访问真实来源、来源已启用且目标库已确认时执行；`--channel` 必填且可重复：

```bash
LIVE_CRAWL_ENABLED=true uv run device-price scheduler \
  --channel APPLE_CN_WEB --channel HUAWEI_CN_WEB
# 等效入口
LIVE_CRAWL_ENABLED=true make scheduler CHANNELS="APPLE_CN_WEB HUAWEI_CN_WEB"
```

启动前校验全部所选来源，拒绝空值、重复、未知、政府或禁用来源；不自动选全部来源。沿用 `FULL_CRAWL_INTERVAL_HOURS`（默认 6 小时），每来源一个不重叠任务，调用同一 V2 流水线，批次标记 `SCHEDULED`，进程间由原 MySQL 命名锁互斥。每次执行重新校验开关；中途禁用来源后不再抓取或建立新批次。

这只是保留已有轻量调度能力，不启动额外服务、不调度政府、不新增监控。人工停止进程即可退出，不应同时启动多个用途相同的调度容器。

## 3. 数据保护规则

- HTTP 层对网络错误、429 和可重试的 5xx 最多重试三次；
- 单商品失败只记录失败证据，不覆盖现有价格；
- 超过 `PRICE_CHANGE_CONFIRM_THRESHOLD` 的价格变化必须立即复抓，两个价格状态完全一致才写入；
- 商品连续缺失 `MISSING_CONFIRMATION_RUNS` 次后必须访问详情页确认；只有详情返回 404/410，或详情成功解析出下架状态，才更新下架状态；
- 设备发现数量低于 `DISCOVERY_COUNT_FLOOR_RATIO` 时明确失败、不写价、不累计缺失；不额外建设报警服务；
- 旧时点可信观察可以保留历史，但不能回退当前规格或价格；同点冲突不以最后写入覆盖，不允许人工强行改当前指针。
- 政府来源日期写入 `v2_price_observation.observed_at`，下载时间写入 `v2_crawl_record.fetched_at`；完整原文件哈希保存在抓取记录，价格观察使用行级证据哈希。同一价格行重采不新增事实，同日变价只为实际变化行形成可追溯修订链。
- 政府 XLS/HTML 出现缺列、日期冲突、未知单位、市场 ID 异常、金额异常或损坏内容时，整份数据集不推进当前价，但保留失败证据和稳定错误码。

## 4. 手工 V2 审计

```bash
uv run device-price db audit
# 可选：同时读取原始证据文件并验证内容哈希
uv run device-price db audit --check-artifacts
```

该命令仅查询 V2 表并读取 schema 元数据，不修复、不写库、不联网。默认只检查数据库关联和证据清单；显式 `--check-artifacts` 才读取证据文件并校验哈希、大小和压缩完整性。JSON 分 `critical` 和 `warnings`，存在 critical 时退出 1；只有 warning 时仍退出 0。缺少必需表会明确报错，V1 表存在或不存在均不影响审计。

关键项包括当前指针与最新可信观察/规格一致、事实与 listing/来源/地区/证据关联、政府同日修订链、设备同点冲突、可信设备匹配与身份、价格/无价状态门禁、批次终态和证据完整性。变价复抓首条待确认记录可保留在最终成功批次中，不误判为整批未完成。

过期 RUNNING、过期当前价、已启用但未完成采集的来源、待确认缺失和未确认变价仅报告 warning；尚未运行的禁用来源不误报。warning 不会自动终结批次或清除价格。仍需结合采集退出码、来源日期、计数和 `v2_crawl_record.error_code` 判断覆盖与时效；审计通过不代表来源发布及时或型号全部覆盖。

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

逻辑备份只包含 `raw_path` 和 `artifact_manifest` 等证据引用，不包含文件正文。必须从实际 `RAW_STORAGE_PATH` 同步备份 `raw_path` 及 manifest 中额外的文件引用，保留相对路径并校验哈希；不能只复制每条记录的主文件，也不能把只有哈希、没有路径的历史上下文当作已归档文件。导出前明确数据范围和存储目录，不将备份提交到 Git。

## 6. 恢复演练

恢复演练只能使用专用临时实例或明确授权的恢复测试库，禁止覆盖生产 `device_price`：

1. 启动与生产大版本相同的空 MySQL；
2. 导入最近备份；
3. 执行 `device-price db check` 和 `device-price db audit`；
4. 核对 Alembic 版本、13 张 V2 必需表（完整历史迁移共 23 张业务表）、种子数量、触发器数量和关键表行数；
5. 使用 fixture 执行一次写入和重放；
6. 删除临时实例，保存演练时间、恢复耗时、校验结果和备份哈希。

恢复核验不能只比较行数，还应核对数据、DDL、触发器定义与属性，并用备份的证据目录检查文件。MySQL 5.7 `mysqldump` 可能在导出的触发器 SQL_MODE 中去掉 `NO_AUTO_CREATE_USER`；本轮原样恢复已记录这一差异，不把它算作生产迁移的允许变更，正式迁移仍保留原触发器属性。具体备份、恢复校验和迁移演练结果见[阶段 M 记录](V2_PHASE_M_ACCEPTANCE_REPORT.md)。

至少每季度演练一次，迁移或备份方式变更后额外演练一次。

## 7. 故障处置

### 批次卡死

1. 运行 `db audit` 确认 `stale_running_crawl`；
2. 检查同渠道容器和 MySQL 命名锁；
3. 保存日志后重启单个调度进程；
4. 下一次设备采集只将同来源、地区、品类且超过 `CRAWL_STALE_AFTER_MINUTES` 的旧批次标为失败；政府批次不套用该恢复规则，审计本身不改状态。

### 大幅价格变化被拒绝

1. 查询同批产品 `v2_crawl_record` 的 `PRICE_CHANGE_CONFIRMATION_REQUIRED/PRICE_CHANGE_UNCONFIRMED` 及关联证据；
2. 人工查看官方页面对应 SKU 总价；
3. 若页面稳定变化，等待下一批再次双观测确认；
4. 若解析规则错误，修复 adapter、增加脱敏 fixture 后发布；
5. 不直接修改 `v2_price_current`。

### 发现数量骤降

1. 查看批次 `DISCOVERY_COUNT_BELOW_FLOOR`；
2. 检查 403、429、验证码、页面结构和品类入口；
3. 核对待确认 `v2_source_listing.consecutive_misses`；
4. 只有详情确认后才接受下架；
5. 修复后使用 fixture 重放，再恢复调度。

### 数据一致性报警

1. 立即停止对应渠道调度，保留数据库和证据；
2. 执行只读查询定位来源 listing、批次和点时事实；
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
