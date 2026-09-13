# 运行与故障处理手册

适用于政府生鲜和五品牌官方设备的统一 V2 链路。默认手工运行，不自动启动调度；公司数据和未完成项只在[项目状态](PROJECT_STATUS.md)维护。本手册是操作方法，不是对公司写库、导出或数据修复的授权。

## 1. 运行前确认

1. 核对 `MYSQL_HOST/PORT/DATABASE/USER` 和目标权限。公司仅操作明确授权的 `device_price`，不修改数据库版本、全局参数或其他 schema。
2. 公司密码通过交互或进程环境注入，不写入命令参数、日志、Git、镜像或 `.env`；本地示例密码仅用于 Docker 开发库。
3. 确认 Alembic 已升级、必需 V2 表存在；已有数据库升级先按第 5 节备份和演练，不能清库或执行集成测试。
4. 确认 `RAW_STORAGE_PATH` 是实际证据目录：采集可写，重放/文件审计可读。默认 `var/raw` 相对于运行目录，不同 checkout 不一定持有同一批证据。
5. 真实来源必须有明确访问授权，并满足域名白名单、公开访问规则和低频要求。不使用个人 Cookie，不绕过登录、验证码或签名。
6. 设备写入前检查 binlog 格式和事务前置条件，见[MySQL 兼容说明](MYSQL_57_COMPATIBILITY.md#3-设备事务隔离)。

本地初始化和测试见[开发指南](V2_GENERAL_CATALOG_DEVELOPMENT_PLAN.md#3-本地验证)。以下命令可先确认入口和结构：

```bash
uv run device-price catalog sources
uv run device-price db check
uv run device-price db audit
```

`catalog sources` 不联网、不连接数据库，只列出代码注册的 7 个连接器，**不显示数据库来源是否启用**。`db check` 核对连接、版本、UTC 和 13 张 V2 必需表；不依赖 V1，也不等同于完整数据验收。

首次种子初始化：

```bash
uv run device-price db seed-devices
uv run device-price db seed-v2-government
```

设备种子包含 5 品牌、7 分类和 5 来源；政府种子包含 8 生鲜分类和 2 来源，不必再运行 `seed-v2-fresh`。种子不访问网站，新来源默认禁用；重复执行不重复建档，也不会关闭已有启用来源。`--enable` 会启用相应种子的全部来源，但不会启动采集。

## 2. 手工采集

命令始终显式传 `--channel`，避免省略时选中默认上海来源。配置默认 `LIVE_CRAWL_ENABLED=false`；匿名测试标识可使用 `DevicePriceTestBot/1.0`，不要求公司或个人联系方式。

### 设备

可用渠道：`APPLE_CN_WEB`、`HUAWEI_CN_WEB`、`XIAOMI_CN_WEB`、`OPPO_CN_WEB`、`VIVO_CN_WEB`。华为、小米需要 Chromium，先执行 `make browser-install`。

```bash
# 仅访问网站，抽样完整产品/SKU 并执行静态门禁；不连接 MySQL
LIVE_CRAWL_ENABLED=true uv run device-price catalog smoke \
  --channel APPLE_CN_WEB --max-products 1

# 确认目标库和来源授权后启用；会启用五个设备来源，但不自动抓取
uv run device-price db seed-devices --enable
LIVE_CRAWL_ENABLED=true uv run device-price catalog crawl --channel APPLE_CN_WEB
```

每个产品一个事务，各 SKU 共用产品证据；成功产品可保留，失败产品回滚并保存失败证据。`--max-products` 是 smoke 的抽样参数，不是正式 crawl 的限量开关。缺失 SKU 不会被静默当作下架。

### 政府生鲜

```bash
LIVE_CRAWL_ENABLED=true uv run device-price catalog smoke --channel SH_FGW_FRESH_RETAIL
LIVE_CRAWL_ENABLED=true uv run device-price catalog smoke \
  --channel MOFCOM_FRESH_WHOLESALE --commodity CUCUMBER

# 确认 smoke 的日期、单位、地区、质量与数量后启用两个政府来源
uv run device-price db seed-v2-government --enable
LIVE_CRAWL_ENABLED=true uv run device-price catalog crawl --channel SH_FGW_FRESH_RETAIL
LIVE_CRAWL_ENABLED=true uv run device-price catalog crawl \
  --channel MOFCOM_FRESH_WHOLESALE --commodity CUCUMBER
```

商务部 `--commodity ALL` 或省略该参数会依次执行 15 个独立品种批次；每页所有市场行共享一条证据记录。上海每批发现文章并下载一次 XLS，8 行价格共享一条记录。每份文档独立事务，不拼接成虚假的单份大文件。品种代码见[生鲜规则](V2_FRESH_FOOD_RULES.md)。

政府同一来源日、同一价格行重复采集幂等；同日官方修订只新增变化行的修正观察。检查价格的来源日期，不把重新下载当作更新报价，停更不删除旧价。

### 结果判断

| 命令 | 成功 / 退出码 | 需要检查 |
| --- | --- | --- |
| `catalog smoke/crawl` | SUCCEEDED 为 0，PARTIAL/FAILED 为 1，参数或门禁不满足为 2 | 状态、接受/待复核/拒绝/失败计数、日期、错误码和覆盖 |
| `catalog replay` | SUCCEEDED 为 0，其他重放结果为 1，参数错误为 2 | 当前静态解析与历史校验分别查看 |
| `db audit` | 有 critical 为 1；只有 warning 仍为 0 | 不能仅看退出码忽略过期、覆盖或历史身份问题 |

`accepted_count` 是可信候选数（可含可信无价状态），不是新增事实数；幂等采集新增零行也可成功。全待复核或全拒绝不能成功，同规格有明确直售价及额外条件价时不必整批失败。字段含义详见[连接器契约](V2_CONNECTOR_CONTRACT.md)。

## 3. 只读验收

使用目标库和原始证据目录，将 `123` 替换为该库的 V2 抓取记录 ID：

```bash
uv run device-price catalog replay --record-id 123
uv run device-price db audit
uv run device-price db audit --check-artifacts
```

重放不联网、不写库、不创建批次，使用原时点和保存的发现上下文处理完整产品或政府文档。输出区分当前静态结果、原记录校验与历史事实；当前解析成功不代表历史未确认变价已可信。明确 404/410 状态记录只展示原无价事实，不用旧金额补齐。

默认审计检查数据库关系和证据清单；`--check-artifacts` 额外验证文件哈希、大小及压缩完整性。缺文件或上下文无法可靠恢复时明确失败，不补抓、不改证据。批次过期、价格陈旧等只报告 warning，不自动清理。

完整验收还需比较原证据重放出的身份、规格、价格、地区和时点与已存事实，并核对本次写入的隔离范围。`healthy=true` 不能证明全站覆盖、来源及时发布或不存在历史重复身份。

## 4. 运行保护与故障处理

设备默认配置来自 [.env.example](../.env.example)：价差阈值 30%、连续缺失确认 3 次、发现数量下限 50%、过期批次 120 分钟。HTTP 对网络错误、429 和可重试 5xx 有有限重试；不重试绕过访问控制。

| 结果 / 错误码 | 处理方式 |
| --- | --- |
| `PRICE_CHANGE_CONFIRMATION_REQUIRED` / `PRICE_CHANGE_UNCONFIRMED` | 查看同批产品两次证据；必须整产品配置、金额和状态一致才推进。不得手工改当前价绕过确认 |
| `DISCOVERY_COUNT_BELOW_FLOOR` | 本批不写价、不累计缺失；检查发现页改版、限流或访问失败 |
| `SKU_COVERAGE_INCOMPLETE` | 批次降级，已确认 SKU 可写，遗漏 SKU 保留原状态；检查 SKU 枚举是否完整 |
| `PRODUCT_ABSENCE_UNCONFIRMED` | 详情证据不足或跳转；华为/OPPO 的选中 SKU 404 不能证明整产品下架 |
| `OFF_SHELF_CONFIRMED` | 完整范围连续缺失后取得明确整产品 404/410 证据；新事实无金额，旧价格保留历史 |
| `STALE_RUNNING_RECOVERED` | 新设备采集在命名锁下处理同来源/地区/品类的过期 RUNNING；不处理其他范围或政府批次 |
| 政府日期/单位/市场 ID/文件结构错误 | 保留失败证据和旧可信价格；更新精确映射与脱敏 fixture，验证后才重新 smoke/crawl |
| 审计 critical 或身份重放不一致 | 停止受影响写入，保留数据库及证据；先定位根因，在隔离副本验证限定修复，获准后处理 |

缺失只在完整范围、新时点、发现和已见产品均成功的设备采集中累计；403、解析失败、跳转或某个 SKU 未遍历到均不能伪造下架。政府不使用设备价差/缺失机制。旧可信时点可留历史，不回退当前价或当前规格；同点冲突不以最后写入覆盖。

## 5. 部署、备份与回退

### 已有库增量部署

1. 暂停目标写入，核对当前 Alembic 版本、活跃事务、目标表与权限；确认导出范围和批准的安全目录。
2. 备份目标库及关联证据，在隔离同版本库恢复并演练迁移。
3. 凭据通过安全方式注入后执行 `uv run alembic upgrade head`；不改变实例全局配置，不删除旧表。
4. 核对业务行、DDL、触发器定义及 SQL_MODE/definer/字符集，确认其他 schema 未受影响。
5. 执行 check/audit、代表性原证据重放；再按授权执行 smoke 和单次采集。

全新空库也使用 Alembic，不用 `metadata.create_all` 代替迁移。完整历史链和 MySQL 5.7 约束数量见[兼容说明](MYSQL_57_COMPATIBILITY.md#2-运行表与历史迁移)。集成测试绝不指向公司库。

### 备份和恢复

使用与目标兼容的 mysqldump；下面路径是占位符，先替换为已批准、不覆盖旧备份的新文件路径。密码由交互读取，不写在参数中：

```bash
mysqldump --single-transaction --quick --skip-lock-tables --triggers \
  --default-character-set=utf8mb4 --set-gtid-purged=OFF \
  --host="$MYSQL_HOST" --port="$MYSQL_PORT" --user="$MYSQL_USER" \
  --password device_price > /approved/backup/device_price_TIMESTAMP.sql
```

同时备份实际 `RAW_STORAGE_PATH` 中 `raw_path` 和 `artifact_manifest` 关联的全部文件，保持相对路径并核对哈希。数据库只存引用，单有 SQL 或哈希不等于证据齐全；备份不得进入 Git、镜像或公开目录。迁移和限定数据修复前保留恢复点，日常频率由操作者决定，不为 demo 增加备份调度服务。

恢复只能使用隔离临时实例或明确授权的恢复库，不能覆盖公司现有库。比较数据、DDL、迁移版本、触发器定义与属性，再用备份证据目录运行文件审计和重放；只比较行数不够。恢复核验结束前不得运行会清空该副本的集成测试。mysqldump 的已知触发器 SQL_MODE 差异见[兼容说明](MYSQL_57_COMPATIBILITY.md#4-增量部署与回退)。

MySQL DDL 非事务性。应用回退不自动降级数据库；含 PRODUCT/AVAILABILITY_ONLY 事实时迁移拒绝直接降级。保留现场，先在副本演练恢复，不删除事实强行回退。

### 镜像与文件权限

构建与离线 smoke 见[开发指南](V2_GENERAL_CATALOG_DEVELOPMENT_PLAN.md#3-本地验证)。容器默认非 root `65532:65532`，默认证据目录为 `/app/var/raw`。自定义挂载目录须预先授予对应读写权限，不能通过改为 root 绕过目录配置问题。

## 6. 可选设备调度

仅在已授权真实访问、来源已启用且目标库确认后显式执行：

```bash
LIVE_CRAWL_ENABLED=true uv run device-price scheduler \
  --channel APPLE_CN_WEB --channel HUAWEI_CN_WEB
```

渠道必填、可重复选择不同设备来源；拒绝空值、重复、未知、政府或禁用来源，不自动选全部来源。沿用 `FULL_CRAWL_INTERVAL_HOURS`（默认 6 小时），每来源任务不重叠，进程间使用 MySQL 命名锁；每次运行重新检查开关。停止该进程即可结束调度，勿再启动同用途容器。此能力不构成持续运行或监控平台。

旧顶层 `adapters/crawl/smoke/replay` 和 `db seed` 已删除；当前仅使用本文的 `catalog`、V2 种子和显式 scheduler。V1 历史数据的来历见[历史归档](archive/README.md)，不提供恢复旧业务入口的方法。
