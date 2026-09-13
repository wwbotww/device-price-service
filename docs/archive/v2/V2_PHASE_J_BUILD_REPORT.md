# V2 设备原生采集：阶段 J 构建报告

> 历史归档：本文件保留当时的方案、结果和限制，不作为当前操作指南；其中旧命令、待办或授权不可直接沿用。当前入口见[文档导航](../../README.md)与[项目状态](../../PROJECT_STATUS.md)。

> 日期：2026-09-11
> 分支：`codex/device-v2-native-collection`
> 范围：Apple 单品牌原生 V2、共用管道及静态入口；K～M 待实施

## 1. 实现结果

Apple 现在直接输出并写入 V2，不读取或搬运 V1 数据。手机产品样本一次抓取生成 3 条 SKU 来源行，共用 1 条 `PRODUCT` 证据，同时建立品牌、标准型号、规格、来源版本、精确匹配和当前/历史价格。MacBook 样本也通过 V2-only 入库与联表核对，完整保留内存、容量、尺寸、处理器及厂商部件号。政府文档使用同一套行准备与入库方法，没有复制第三条持久化流程。

主要改动：

- 原 `AppleAdapter` 已替换为 `AppleCatalogConnector`，删除旧 `normalize` 和 V1 注册；保留已验证的 HTML/bootstrap 来源逻辑，完整保存配置和可证明的厂商部件号，配置容器编号不冒充厂商部件号。
- `CatalogConnector` 改为产品发现/抓取/多 SKU 解析，旧单 listing 接口及调用方全部替换；一个注册表管理 Apple 与两个政府连接器，单独的数据集注册表已删除。
- `catalog_preparation.py` 为正式采集及 smoke 共用的静态门禁，不访问网络或数据库；校验产品/品牌/直营卖家/分类/地区/URL/规格/价格，不能用候选覆盖产品共享时点和哈希。
- 同一产品任一规格身份不可信时，整个产品不推进可信价；单 SKU 有可信直售价与额外条件价时仍可成功。零售原价低于售价只留审计，不成为当前可信价。
- 一产品/一文档一事务。失败后只登记证据，不再为了失败审计初始化或恢复 listing；另一个产品不受影响。规范化、质量计数及写库路径共用，持久化层不包含 Apple 分支。
- 版本选择只比较本次可信时点与现当前规格的可信观察，不使用被拒绝价格也会更新的 `revision.last_observed_at`。较旧/重复证据不覆盖当前 URL、生命周期或缺失计数。
- 设备同时点只允许真正幂等重放；不同价格、规格或原文形成的第二条非幂等可信事实被拒绝，不能留下未关联的可信记录让当前投影重建任意选择。政府同日修订机制不变。

`artifact_manifest` 保存发现 DTO、请求范围、品牌/渠道及主响应路径、哈希、时点。这里完成的是原始证据经连接器和管道再次执行的 fixture 重放与幂等验证，尚未完成阶段 L 的 `catalog replay` 命令。

## 2. 入口与数据边界

| 入口 | 当前行为 |
| --- | --- |
| `catalog sources` | 政府两个来源 + `APPLE_CN_WEB` |
| `catalog smoke --channel APPLE_CN_WEB --max-products 1` | 完整解析抽样产品的 SKU，复用静态校验，不创建数据库连接 |
| `catalog crawl --channel APPLE_CN_WEB` | 直接写 13 张 V2 表；来源须已初始化、显式启用且真实门禁开启 |
| 旧 `crawl/smoke --brand APPLE` | 提示改用 V2，不再写 V1 |
| 其他四品牌旧入口 | 暂保留，阶段 K 迁入 V2 |
| `db check/audit`、旧 `replay/scheduler` | 尚未全面退出 V1 依赖；不能当作 Apple V2 的审计/重放/调度工具 |

批次与 CLI 明确输出状态：全范围可信结果为 `SUCCEEDED`（退出 0），部分可信为 `PARTIAL`、零可信为 `FAILED`（退出 1），参数或来源配置/真实开关门禁不通过退出 2。计数以本次可信候选为准，不把幂等零新增判为失败，也不以 `failed_count=0` 代替质量判定。重复产品发现在抓取前整批拒绝。

没有新增业务表或迁移：代码 head 仍为 `b72c910e4f31`，公司最近确认的 head 仍是 `96524222b3ec`。本轮未连接公司数据库、未访问真实商城或政府来源，也未提交/推送远程。

## 3. 验证

- Ruff、mypy 通过；单元测试 287 项通过。
- 本地 MySQL 8.4 和 5.7.36 各通过 67 项集成验证：原全量 66 项通过，最后新增的 MacBook V2-only 入库测试在两库各补跑 1 项通过；Apple 场景共 22 项。没有在公司库运行测试。
- 核心场景包括：V2-only 13 表建档、一个产品多 SKU 共享证据、匹配幂等、同价新时点、较早重放、规格变化、标题变化、同时点冲突、整产品回滚、空/重复发现、全拒绝/全待复核/部分成功、失效元数据保护及可信时序选择。
- 共存测试比较既有 V1 行和上海政府行的全部字段，确认 Apple 采集没有改动它们；原商务部/上海发布时点、行级幂等和同日修订测试继续通过。
- 镜像 `device-price-service:device-v2-apple` 构建通过；`--network none` 下来源列表、种子帮助、smoke 帮助及旧品牌列表检查通过。来源列表含 Apple，旧品牌列表已不含 Apple；默认门禁下 `catalog crawl --channel APPLE_CN_WEB` 如期退出 2，未建立网络连接。

复验命令（仅本地专用测试库，集成测试会清理该库）：

```bash
make lint test-unit
RUN_MYSQL_INTEGRATION=1 TEST_MYSQL_HOST=127.0.0.1 \
  TEST_MYSQL_PORT=3307 TEST_MYSQL_DATABASE=device_price_test make test-integration
RUN_MYSQL_INTEGRATION=1 TEST_MYSQL_HOST=127.0.0.1 \
  TEST_MYSQL_PORT=3308 TEST_MYSQL_DATABASE=device_price_test make test-integration
docker build -t device-price-service:device-v2-apple .
docker run --rm --network none device-price-service:device-v2-apple catalog sources
docker run --rm --network none device-price-service:device-v2-apple catalog smoke --help
docker run --rm --network none device-price-service:device-v2-apple db seed-devices --help
docker run --rm --network none device-price-service:device-v2-apple adapters
```

## 4. 尚未完成与下一步

阶段 K 继续原生接入华为、小米、OPPO、vivo，并接续价格突变复抓、发现数量保护、产品/SKU 缺失确认与过期批次恢复。本阶段已有异常/取消终态和数据时序保护，不等于这些运行保护已齐全。

阶段 L 才完成通用离线 `catalog replay`、V2 audit/check、可选 V2 调度和全部 V1 写库退出。Apple 旧解析器已删除，因此旧 Apple V1 记录也不再由当前分支的旧 replay 解析；没有为历史 demo 增加兼容层。

阶段 M 在本地验收完备后再做受控真实 smoke、公司库增量迁移和原生重采。本阶段不保证当前真实官网结构仍与 fixture 一致，未解决的 Apple Watch 组合总价问题继续保留；不得为通过验收放宽直接售价或证据规则。

实施依据见[改造计划](V2_DEVICE_NATIVE_COLLECTION_PLAN.md)，命令前置条件见[运行手册](../../OPERATIONS_RUNBOOK.md#2-手工采集)。
