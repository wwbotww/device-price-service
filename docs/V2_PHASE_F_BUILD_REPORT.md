# V2 阶段 F 构建报告：上海政府生鲜零售均价 MVP

> 完成日期：2026-08-25
> 状态：实现、离线重放、MySQL 5.7/8.x 和真实只读 smoke 均通过
> 生产影响：无；未连接或迁移公司 MySQL，未启用 V2 调度

## 1. 阶段结论

阶段 F 已完成。项目现在可以从上海市发展改革委公开目录发现最新“主要主副食品品种价格信息表”，校验文章日期，下载一次旧版 `.xls` 附件，并将 8 个明确品种的平均零售价按来源日期写入现有 13 张 V2 表。

本阶段没有新增业务表、Redis、消息队列、浏览器抓取或通用 ETL 框架。只增加 `xlrd` 作为旧版 XLS 读取依赖，HTML 继续使用现有轻量解析器，批量行继续复用既有品类规则、中央价格门禁和 Repository。

## 2. 实现内容

### 2.1 领域和数据库兼容

- `PriceNature` 增加 `RETAIL_AVERAGE`、`WHOLESALE_AVERAGE`，旧 `MARKET_AVERAGE` 只保留 demo 兼容；
- 价格候选增加可选 `source_observed_at`，缺失时才退回下载时间；
- 只有日期的上海数据按 `Asia/Shanghai 00:00:00` 转为 UTC，并保留 `time_precision=DAY`；
- MySQL 8 原生 `CHECK` 与 MySQL 5.7 兼容触发器均接受两种明确均价性质；
- 同日修订身份不再错误要求相同证据哈希，因为官方替换附件必然产生新哈希。

V2 尚未部署公司库，因此本阶段直接调整未发布的 V2 影子迁移，并用全新专用测试 schema 从 base 升级到 head 验证；没有对公司 V1 迁移历史做重写或执行。

### 2.2 窄范围批量入口

新增 `CatalogDatasetConnector`：

```text
发现目录和文章
  → 下载一份 XLS
  → 保存一个 PUBLIC_PRICE crawl_record 和原始证据
  → 解析 8 个 listing-shaped rows
  → 逐行复用 CategoryRule + CatalogPricePolicy
  → 原子写入 listing_revision / price_observation / price_current
```

同一文件不会按品种重复下载。8 个价格观察可以引用同一个 `v2_crawl_record`；任一结构级错误会回滚整份数据写入，再单独保存失败记录和原始证据，上一条可信当前价不受影响。

### 2.3 上海连接器

来源配置：

| 项目 | 值 |
| --- | --- |
| channel | `SH_FGW_FRESH_RETAIL` |
| connector | `shanghai-fresh-retail-1` |
| 批准域名 | `fgw.sh.gov.cn` |
| 地区 | `CITY/310100` |
| 价格性质 | `RETAIL_AVERAGE` |
| 价格类型 | `PUBLISHED_VALUE` |
| 报价基础 | `UNIT_QUOTED` |
| 原价 | `NULL/NONE` |
| 库存 | `UNKNOWN` |

连接器不读取文章中的环比、前一日价格或文字趋势，只读取 XLS 的“均价”行。真实附件内部均值保留更多计算尾数，但工作簿将价格格式化为两位小数；系统按官方显示精度使用十进制四舍五入，不保存不可见尾数。

### 2.4 首批精确映射

| 规范代码 | 来源品种 | 来源规格 | 单位 |
| --- | --- | --- | --- |
| `QINGCAI` | 青菜 | 新鲜一级 | 元/500克 |
| `JIMAOCAI` | 鸡毛菜 | 新鲜一级 | 元/500克 |
| `NAPA_CABBAGE` | 大白菜 | 新鲜一级 | 元/500克 |
| `CUCUMBER` | 黄瓜 | 新鲜一级 | 元/500克 |
| `CARROT` | 胡萝卜 | 新鲜一级 | 元/500克 |
| `RED_FUJI_APPLE` | 苹果 | 红富士一级 | 元/500克 |
| `CHICKEN_EGG` | 鸡蛋 | 新鲜完整 | 元/500克 |
| `LEAN_PORK` | 鲜猪肉 | 精瘦肉 | 元/500克 |

匹配同时要求类别、品种、规格和单位唯一，不能把其他猪肉部位或未知单位模糊映射进来。8 个品种使用一个 `FRESH_MONITORED_COMMODITY` 叶子品类和一个 `government-fresh@1` 规则，不为每种蔬菜增加表、连接器或规则类。

### 2.5 幂等与修订

- 同一来源日、同一价格行证据哈希、同一解析版本重放：复用原观察，不新增事实；
- 同一来源日官方替换附件：只为内容变化的价格行新增事实，并以 `supersedes_observation_id` 指向上一条同日有效观察；完整附件仍按字节哈希留档；
- 再次重放修订附件：复用修订观察，不增长修订链；
- 较旧来源日或被拒绝观察：保存历史但不倒退 `price_current`；
- 原始观察从不覆盖或删除。

## 3. CLI 与安全门禁

```bash
uv run device-price catalog sources
uv run device-price db seed-v2-government       # 默认 disabled

LIVE_CRAWL_ENABLED=true \
  uv run device-price catalog smoke --channel SH_FGW_FRESH_RETAIL

uv run device-price db seed-v2-government --enable
LIVE_CRAWL_ENABLED=true \
  uv run device-price catalog crawl --channel SH_FGW_FRESH_RETAIL
```

`catalog smoke` 不连接或写入 MySQL。`catalog crawl` 同时要求全局真实采集门禁和数据库中的来源启用状态；来源种子默认禁用。阶段 F 没有把上海来源加入 APScheduler。

## 4. 验证结果

```text
ruff check .
  passed

mypy src
  passed

pytest -m "not integration"
  142 passed

RUN_MYSQL_INTEGRATION=1 pytest -m integration
  31 passed (MySQL 8.4)

RUN_MYSQL_INTEGRATION=1 TEST_MYSQL_PORT=3308 pytest -m integration
  31 passed (MySQL 5.7.36)

docker build -t device-price-service:phase-f .
docker run --rm device-price-service:phase-f catalog sources
  passed; container includes xlrd and SH_FGW_FRESH_RETAIL
```

离线 XLS 重放覆盖：一次文件下载、8 行入库、完全重采幂等、同日附件修订、修订重采幂等、损坏附件、缺少品种列、未知单位和日期不一致。主闭环测试使用实际 Alembic head，而不只使用 SQLAlchemy `create_all`，因此同时验证了 MySQL 8 `CHECK` 和 MySQL 5.7 触发器。

阶段 G 的最终审计进一步把批量文件的价格证据收敛到行级：fixture 中只修改青菜一列时，观察总数由首批 8 条增长到 9 条，且只有 1 条 `supersedes_observation_id`；未变化的 7 个品种保持幂等。

## 5. 真实只读 smoke

2026-08-25 使用匿名测试 User-Agent 对批准域名执行一次低频 smoke：

- 目录、最新文章和 XLS 各请求一次，均为 HTTP 200；
- 发现来源日 `2026-08-24`，价格时点为 UTC `2026-08-23T16:00:00.000`；
- 内容类型为 `application/vnd.ms-excel`；
- 8 个白名单品种全部解析，`original_price` 全为空；
- 附件 SHA-256 为 `247af3656893b07f0fd56c6f38c77615a1197c48086573e546037e6ca56f4501`；
- smoke 未连接公司数据库、未写业务表、未提交真实附件。

真实页面暴露了文章标题由标题 `<span>` 与日期 `<small>` 共同组成的差异；解析器已按明确标题模式修正，脱敏 HTML fixture 同步覆盖。

## 6. 已知边界与下一步

- 只接入上海市级零售均价，不代表全国覆盖，也不等于门店即时售价；
- 只接受当前 8 个品种和元/500克，新增品种需显式映射与 fixture；
- 政府来源没有 SLA，节假日、停更和页面结构调整仍需阶段 G 告警；
- 阶段 F 只提供手工单次运行，不做连续运行结论；
- 公司 `device_price` 仍是 V1 基线，未运行 V2 迁移或种子。

阶段 G 已接入商务部“百家日报”批发价，并保持 `WHOLESALE_AVERAGE` 与上海零售均价隔离。后续范围已按 demo 定位收敛，不再建设保守日调度、停更告警或 7 日连续观察；结果见 [阶段 G 构建报告](V2_PHASE_G_BUILD_REPORT.md)。
