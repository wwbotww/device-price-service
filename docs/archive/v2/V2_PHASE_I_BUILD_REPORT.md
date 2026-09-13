# V2 设备原生采集：阶段 I 构建报告

> 历史归档：本文件保留当时的方案、结果和限制，不作为当前操作指南；其中旧命令、待办或授权不可直接沿用。当前入口见[文档导航](../../README.md)与[项目状态](../../PROJECT_STATUS.md)。

> 日期：2026-09-11
> 分支：`codex/device-v2-native-collection`
> 结论：改造方案已修订确认；阶段 I 基础通过本地验收，设备原生采集 J～M 待实施

## 1. 本轮范围与方案确认

以[设备原生采集计划](V2_DEVICE_NATIVE_COLLECTION_PLAN.md)为实施依据：直接向现有 13 张 V2 表采集新设备价格，不迁移 V1 数据，不新增业务表、Redis 或常驻监控。保留政府生鲜流程，最终退出 V1 写库路径，但分阶段替换过程中不提前删除仍有调用方的实现。

本轮补齐并确认三项设计边界：

- 批次按每个请求目标是否产生可信价格或可信状态判定；全拒绝不能成功，幂等重放新增零行仍可成功。新批次判定与管道改造一起在阶段 J 实施。
- 无金额状态不是普通价格的放宽：只允许有证据的零售 `OFF_SHELF/OUT_OF_STOCK/COMING_SOON`，全部金额为空，历史金额不丢失；状态证据确认与缺失保护在阶段 K 实施。
- 重放依赖原始证据及确定的产品/分类/地区/规格上下文；上下文不足时拒绝，不访问网络补猜。完整重放入口在阶段 L 完成。

当前没有需要扩表或推翻通用结构的阻碍。产品页天然一对多，政府文档也天然一对多，两者应共用来源行及事务；接口及生产调用方将在阶段 J 一起替换，不增加长期适配包装层。

## 2. 已完成的基础

| 内容 | 当前实现与解决的问题 |
| --- | --- |
| 产品多 SKU 数据契约 | 新增 `DiscoveredCatalogProduct/ParsedCatalogProduct`，检查产品和分类归属、重复 listing 和官方 SKU；政府行改名为共用 `ParsedCatalogRow`，旧命名及代码调用已替换 |
| 设备规格与稳定键 | 保存明确颜色、容量、内存、连接方式、尺寸、版本、部件号和完整附加维度；官方 SKU 优先、明确规格指纹兜底；标题/价格不作身份，未知规格进入待复核 |
| V2 设备种子 | 新增 `db seed-devices` / `make seed-v2-devices`；幂等创建 5 个品牌、7 个分类和 5 个默认禁用来源；V1/V2 共用一份官方来源静态配置，不读取 V1 业务数据 |
| 产品证据与无价状态 | `PRODUCT` 证据可供多个 SKU 共享；严格 `AVAILABILITY_ONLY` 领域校验、策略、ORM、迁移和数据库约束保持一致 |
| 测试隔离 | 集成测试仅接受专用库名 `device_price_test`；本轮明确连接本地容器，未使用 SQLite 代替 MySQL 兼容验证 |

`CatalogConnector` 现有单 listing 方法尚未替换，`DeviceCategoryRule` 尚未注册到设备生产链路。本轮是数据契约、初始化与持久化基础，不是五品牌接入完成。

## 3. 数据库变更及保护

追加迁移 `b72c910e4f31`，父版本 `96524222b3ec`；不修改已执行过的迁移。MySQL 8.0.16+ 更新 3 个 CHECK，MySQL 5.7 更新两个 V2 表的 4 个校验触发器，其余校验保留。完整结构仍为 V1 10 张 + V2 13 张业务表；MySQL 5.7 共 40 个触发器。

集成验证覆盖：

- 只有 V2 表时，设备种子 CLI 与标准产品、规格、来源版本、匹配建档均可执行；重复初始化不复制记录，也不重置来源启用开关；
- 可信价格 → 无价状态 → 恢复在售的历史保留与当前投影推进；领域载荷序列化往返后重复写入幂等，较早状态不覆盖较新价格，重建当前投影结果一致；
- 绕过应用直接写入非法状态仍被数据库拒绝；
- 从旧 head 增量升级后，测试中的全部 23 张表既有行内容不变；
- 已有新类型证据时拒绝有损降级，失败前不执行 DDL、不改版本号或事实；无新类型事实时，原迁移往返测试继续通过。

这里的“重放”验证仅指领域载荷/Repository 幂等，不代表设备原始页面或 CLI replay 已完成。真实公司库本轮没有连接、清表、迁移或写入，其最近确认的 head 仍为 `96524222b3ec`。后续公司增量升级应先备份、暂停写入并确认目标；不能运行会清库的集成测试。

## 4. 验证结果

| 验证 | 结果 |
| --- | --- |
| `make lint` | Ruff 和 mypy 通过 |
| `make test-unit` | 211 项通过 |
| 本地 MySQL 8.4 全部集成测试 | 45 项通过（新增设备基础 11 项） |
| 本地 MySQL 5.7.36 全部集成测试 | 45 项通过（新增设备基础 11 项） |
| Docker 镜像构建 | `device-price-service:device-v2-foundation` 构建成功 |
| 镜像离线 CLI smoke | `db seed-devices --help`、`catalog sources`、`adapters` 在 `--network none` 下通过 |

本地验证命令（数据库清理仅限专用测试库）：

```bash
make lint test-unit
RUN_MYSQL_INTEGRATION=1 TEST_MYSQL_HOST=127.0.0.1 \
  TEST_MYSQL_PORT=3307 TEST_MYSQL_DATABASE=device_price_test make test-integration
RUN_MYSQL_INTEGRATION=1 TEST_MYSQL_HOST=127.0.0.1 \
  TEST_MYSQL_PORT=3308 TEST_MYSQL_DATABASE=device_price_test make test-integration
docker build -t device-price-service:device-v2-foundation .
docker run --rm --network none device-price-service:device-v2-foundation db seed-devices --help
docker run --rm --network none device-price-service:device-v2-foundation catalog sources
docker run --rm --network none device-price-service:device-v2-foundation adapters
```

## 5. 下一阶段与已知限制

下一阶段 J：替换商品连接器为产品发现/多 SKU 解析，统一产品和政府文档的逐行处理，补齐批次结果判定、标准商品/规格建档与共享产品证据，再以 Apple fixture 贯通 V2；不能读写 V1 作为中转。

阶段 K 接入其余品牌与价格突变/发现骤降/缺失确认保护；L 完成统一 CLI、证据重放、V2 audit 与旧写库代码退出；M 再进行受控真实采集和公司库验收。

当前设备采集仍走旧 `crawl --brand` 写 V1；`catalog sources` 只注册两个政府来源。设备种子开关不等于连接器可用，`db check/audit` 也尚未退出 V1 依赖。历史 Apple Watch 总价覆盖缺口不会因本轮结构改造而自动消失，后续仍须遵守完整配置直接售价规则。
