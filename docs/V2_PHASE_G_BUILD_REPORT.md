# V2 阶段 G 构建报告：商务部跨地区批发价格

> 完成日期：2026-08-25
> 状态：实现、脱敏重放、MySQL 5.7/8.x 和真实只读 smoke 均通过
> 生产影响：无；未连接或迁移公司 MySQL，未启用 V2 调度

## 1. 阶段结论

阶段 G 已完成。项目现在具备两个互补的政府生鲜来源：上海市发展改革委提供市级平均零售价，商务部商务预报“百家日报”提供跨地区批发市场当日报价。两种价格分别保存为 `RETAIL_AVERAGE` 和 `WHOLESALE_AVERAGE`，不会互相覆盖。

项目按 demo 定位交付。V2 只提供显式手工 smoke 和单次采集，不建设常驻调度、告警平台、运行监控或 7 日连续观察；质量保障集中在严格来源身份、解析门禁、原始证据、幂等写入和失败不推进当前价。

## 2. 商务部连接器

来源配置：

| 项目 | 值 |
| --- | --- |
| channel | `MOFCOM_FRESH_WHOLESALE` |
| connector | `mofcom-fresh-wholesale-1` |
| 批准域名 | `cif.mofcom.gov.cn` |
| 数据页 | `/cif/seach.fhtml?commdityid=...` |
| 地区 | `PROVINCE`；每行使用明确地区映射 |
| 价格性质 | `WHOLESALE_AVERAGE` |
| 报价单位 | 元/公斤，规范为 `CNY_PER_KG` |
| 原价 | `NULL/NONE` |

连接器每次只处理一个品种页面：先获取页面确认品种、来源日期和单位，再重新获取同一页面作为本批原始证据。若两次获取间来源日期变化，批次失败，不把不同日期的发现和数据拼接。阶段 G 的 `ALL` 顺序执行首批 5 个独立数据集；阶段 H 已将同一精确白名单机制扩展为 15 个，每页仍保留自己的哈希和 `v2_crawl_record`。

## 3. 首批品种与稳定身份

| 规范代码 | commodityid | 来源名称 |
| --- | --- | --- |
| `NAPA_CABBAGE` | `170060` | 大白菜 |
| `CUCUMBER` | `170130` | 黄瓜 |
| `CARROT` | `170260` | 胡萝卜 |
| `CHICKEN_EGG` | `150010` | 鲜鸡蛋 |
| `PORK_HIND` | `130014` | 后臀尖 |

品种 ID、导航名称、页面标题和元/公斤单位必须同时精确匹配。后臀尖使用独立 `PORK_HIND`，不与上海精瘦肉 `LEAN_PORK` 合并。

每个市场使用走势图链接中的公开 `enterid` 作为稳定 merchant 身份，市场名称只作为可更新展示字段。listing 身份由 `commodityid + enterid + 报价单位` 组成，不包含价格或来源日期。同一 `enterid` 若出现在两个来源地区，只建立一个 listing，并保存两个地区价格候选。

省级名称映射为明确行政区代码；来源明确列出的新疆生产建设兵团使用来源范围码 `XJ_CORPS`，不猜测为某个省级行政区代码。

## 4. 质量门禁

连接器要求：

- 页面 URL、主机、路径和 `commdityid` 与白名单一致；
- 标题中的日期、品种和单位与配置一致，来源日期不晚于抓取时间；
- 表格 `id`、六列表头和每行列数保持精确；
- 当日价格为最多两位小数的人民币正数；
- 图表链接中的 `enterid`、`commdityid` 和 `Edate` 与当前行一致；
- 地区必须存在显式映射，市场 ID 与名称不得冲突；
- 同一市场和地区不得在一个数据集中重复。

页面中的前一日价格和环比不参与价格事实，也不被保存为原价。任一结构级错误会保留已下载 HTML 及失败记录，但整份数据集不推进 `price_current`。

每条地区价格使用来源日期、品种、`enterid`、市场名称、地区、报价单位和当日价格生成行级证据哈希；完整 HTML 哈希仍保存在 `v2_crawl_record.raw_hash`。因此同一页面只有一个市场价格被修订时，只新增该市场/地区的修正观察，其他价格不会因整页哈希变化而产生伪历史。

## 5. TLS 兼容

真实验证发现 Python/OpenSSL 默认客户端访问商务部旧站会收到握手失败，实测 TLS 1.2 的 `AES128-GCM-SHA256` 兼容配置可以成功访问。共享 HTTP 抓取器增加显式 `TLS12_COMPAT` 配置，仅由商务部连接器选择：

- 最低和最高协议均为 TLS 1.2；
- 只启用已实测的 AES-GCM 套件；
- 继续校验证书链和主机名；
- 不关闭 HTTPS 验证，不影响上海或其他来源的默认 TLS。

## 6. CLI

```bash
uv run device-price catalog sources

# 不写数据库；省略 commodity 时等同于 ALL
LIVE_CRAWL_ENABLED=true \
  uv run device-price catalog smoke \
  --channel MOFCOM_FRESH_WHOLESALE --commodity ALL

# 专用 V2 数据库中显式启用后，手工采集一个或全部品种
uv run device-price db seed-v2-government --enable
LIVE_CRAWL_ENABLED=true \
  uv run device-price catalog crawl \
  --channel MOFCOM_FRESH_WHOLESALE --commodity CUCUMBER
```

两个政府来源的种子均默认禁用。商务部 smoke 只展示前 10 条价格样本，同时给出完整 `parsed_count`、`price_count`、来源日期和证据哈希。

生产镜像继续使用非 root 用户 `65532`，构建时显式创建并授权默认 `/app/var/raw` 证据目录；修复前镜像只能列出连接器，实际 smoke 会在网络请求前因目录无写权限退出。

## 7. 验证结果

```text
ruff check .
  passed

mypy src
  passed

pytest -m "not integration"
  155 passed

RUN_MYSQL_INTEGRATION=1 pytest -m integration
  34 passed (MySQL 8.4)

RUN_MYSQL_INTEGRATION=1 TEST_MYSQL_PORT=3308 pytest -m integration
  34 passed (MySQL 5.7.36)

docker build -t device-price-service:phase-g-final .
docker run --rm device-price-service:phase-g-final catalog sources
docker run --rm -e LIVE_CRAWL_ENABLED=true device-price-service:phase-g-final \
  catalog smoke --channel MOFCOM_FRESH_WHOLESALE --commodity CUCUMBER
  passed; non-root artifact directory and container TLS verified
```

商务部专项重放覆盖 4 个稳定市场 listing、5 个地区价格、完全重采幂等、同日页面单行修订、同市场多地区、结构失败证据和默认禁用来源。fixture 中只修改北京一行时，观察总数由 5 条增长到 6 条，且只有 1 条修订关系。阶段 G 没有新增业务表或数据库迁移。

## 8. 真实只读 smoke

2026-08-25 对首批 5 个页面各执行一次发现和一次证据获取，10 个请求均为 HTTP 200：

| 品种 | 来源日期 | 解析价格数 |
| --- | --- | ---: |
| 大白菜 | 2026-08-23 | 110 |
| 黄瓜 | 2026-08-23 | 108 |
| 胡萝卜 | 2026-08-23 | 107 |
| 鲜鸡蛋 | 2026-08-23 | 64 |
| 后臀尖 | 2026-08-23 | 40 |

合计 429 条地区—市场价格，所有页面单位均为元/公斤，原价均为空。smoke 没有连接数据库、没有写业务表、没有提交真实页面内容。

## 9. 后续完成情况

阶段 H 已完成：商务部扩展到 15 个品种，公司 `device_price` 在完整备份后重建并写入两来源真实数据，其他 schema 基线保持不变，V2 scheduler 未启动。详见 [V2 阶段 H 公司库验收报告](V2_PHASE_H_COMPANY_ACCEPTANCE_REPORT.md)。
