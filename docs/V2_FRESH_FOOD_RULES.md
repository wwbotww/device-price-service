# V2 生鲜品类规则与脱敏重放说明

> 适用版本：阶段 H 已实现
> 数据范围：阶段 D demo 规则 + 商务部批发价 15 个品种 + 上海政府零售均价 8 个品种
> 当前生产目标：政府网站公开的蔬菜、水果、肉类和蛋类价格

## 1. 已实现规则包

| 叶子品类 | 规则引用 | 计量维度 | 必填可比属性 |
| --- | --- | --- | --- |
| `FRESH_APPLE` | `fresh-apple@1` | `WEIGHT` | 品种、产地、等级、保鲜状态、包装 |
| `FRESH_EGG` | `packaged-egg@1` | `COUNT` | 蛋品类型、产地、等级、保鲜状态、包装 |
| `FRESH_PORK` | `fresh-pork@1` | `WEIGHT` | 明确部位、产地、等级、保鲜状态、包装 |
| `FRESH_MONITORED_COMMODITY` | `government-fresh@1` | `WEIGHT` | 规范品种代码、来源名称、分组、来源规格、报价单位 |

苹果规则额外识别果径；鸡蛋规则识别蛋壳颜色；猪肉规则识别带骨/去骨和带皮/去皮。可选属性一旦存在也进入身份指纹，因此不会把不同果径、骨皮状态或包装的价格误认为同一规格。

阶段 D 的公共市场苹果 fixture 复用 `fresh-apple@1`，并使用旧 `MARKET_AVERAGE` 作为回归基线。阶段 F 的新政府数据改用 `RETAIL_AVERAGE` 或 `WHOLESALE_AVERAGE`，旧 fixture 不冒充生产来源。

政府发布的是标准品种/规格的单位价格，不是电商包装 SKU。`government-fresh@1` 以明确的品种代码、来源名称、来源规格和报价单位形成身份，只接受连接器中的精确白名单，不强制补齐政府文件没有发布的产地、等级和包装信息。上海表的 `元/500克` 规范为 `CNY_PER_500G + 0.500 KG`；商务部表的 `元/公斤` 规范为 `CNY_PER_KG + 1 KG`。报价单位和规范数量必须成对一致，其他单位不猜测。

## 2. 数量和单位

`normalization/measurements.py` 只接受可以精确证明的表达：

| 原单位/示例 | 规范原单位 | 基本单位 | 换算 |
| --- | --- | --- | --- |
| `500g`、`500克` | `G` | `KG` | ÷ 1000 |
| `5kg`、`5公斤`、`5千克` | `KG` | `KG` | 不变 |
| `1斤` | `JIN` | `KG` | × 0.5 |
| `950ml`、`950毫升` | `ML` | `L` | ÷ 1000 |
| `1L`、`1升` | `L` | `L` | 不变 |
| `30枚/个/只` | `PIECE` | `PIECE` | 不变 |
| `2盒×15枚` | `PIECE` | `PIECE` | 总数 30 |
| `400-500g` | `G` 范围 | `KG` 范围 | 0.4～0.5 |

不接受“约 5kg”、磅、模糊大小、缺少单位或计量维度不匹配的文本。这些情况使用 `QUANTITY_UNRESOLVED` 进入待复核，不按经验换算。

身份指纹以基本数量为准，因此同一商品从 `5kg` 改写为 `5000g` 不会生成虚假的新规格版本；原始写法仍保留在 `source_attributes` 和首份证据中。

## 3. 规范属性

当前别名是小而明确的白名单：

- 红富士苹果 → `RED_FUJI`；
- 一级/一等 → `GRADE_1`，A级 → `GRADE_A`；
- 新鲜 → `FRESH`，冷鲜/冷藏 → `CHILLED`，冷冻 → `FROZEN`；
- 箱/盒、袋、托盘、真空、简装分别形成独立包装值；
- 猪五花肉、里脊、梅花肉形成不同部位；
- 鲜鸡蛋和土鸡蛋形成不同蛋品类型。

产地只做空白清理，不把省、市、产区擅自合并。未知别名使用 `IDENTITY_VALUE_UNSUPPORTED` 待复核；新增别名必须有脱敏 fixture 和规则版本评估。

## 4. 质量状态

| 原因码 | 状态 | 含义 |
| --- | --- | --- |
| `IDENTITY_FIELDS_MISSING` | `REVIEW_REQUIRED` | 缺少必填可比属性 |
| `IDENTITY_VALUE_UNSUPPORTED` | `REVIEW_REQUIRED` | 属性存在，但当前规则不能可靠规范化 |
| `QUANTITY_UNRESOLVED` | `REVIEW_REQUIRED` | 数量、单位或计量维度不能精确换算 |
| `QUOTED_UNIT_MISMATCH` | `REVIEW_REQUIRED` | 报价单位与规范数量不一致 |

待复核来源版本会保存证据，但不会成为 `source_listing.current_revision_id`，也不会形成可信当前价格。

价格质量仍由中央 `CatalogPricePolicy` 决定：

- 5kg 苹果 79 元 → 15.800000 元/kg；
- 30 枚鸡蛋 32.80 元 → 1.093333 元/枚；
- 500g 五花肉 28.80 元 → 57.600000 元/kg；
- 公共市场 6.80 元/500g → 13.600000 元/kg；
- 会员苹果价保留为拒绝观察，不覆盖直接售价。

政府数据还必须满足：

- 零售均价和批发均价分别保存；
- 来源数据日期作为价格时点，下载时间只用于采集审计；
- `original_price` 为空；
- “前一日价格”、环比和指数不进入本轮价格观察；
- 没有明确库存含义时 `availability=UNKNOWN`。

## 5. 初始化和 fixture

V2 生鲜品类树通过显式命令初始化：

```bash
uv run device-price db seed-v2-fresh
uv run device-price db seed-v2-government
```

`seed-v2-fresh` 只幂等写入 8 条 V2 品类和规则引用。`seed-v2-government` 同时写入上海和商务部两个来源配置，均默认 `enabled=false`；只有显式增加 `--enable` 才启用。两个命令都不访问网络、不写 V1 表，运行前必须已经升级到包含 V2 影子表的 Alembic head。

脱敏 fixture 位于 `tests/fixtures/catalog_fresh/`：

- `ecommerce_discovery.json`；
- `apple_5kg.json`；
- `egg_30.json`；
- `pork_belly_500g.json`；
- `public_market_discovery.json`；
- `public_market_apple.json`；
- `shanghai_index.html`、`shanghai_article.html`；
- `shanghai_daily_20260819.xls.b64` 与同日修订样本；
- `mofcom_cucumber_20260823.html` 与同日修订样本。

阶段 D fixture 使用保留测试域 `example.test`，卖家、外部 ID 和产区描述均为示例。上海 fixture 为验证域名白名单而保留官方主机名，但路径、文章和工作簿均已重新生成和脱敏。所有 fixture 都不包含真实平台 Cookie、Token、签名、页面全文或公司信息；阶段 D 的电商样本只用于通用规则回归，当前不会据此开发商业平台连接器。

上海 HTML fixture 只保留文章发现、标题日期和附件链接所需结构。XLS fixture 是重新生成的 8 列脱敏工作簿，以 Base64 文本保存，测试时只在内存中解码。商务部 HTML fixture 只保留品种导航、标题、表头和 5 条虚构市场行，其中一个市场用两个来源地区验证多地区价格。它们都不是政府原始文件。真实下载内容仅用于本地 smoke 和运行证据，不提交仓库。

## 6. 新增生鲜品类时

1. 先确认最小可比属性和计量维度；
2. 只增加确定性别名，不用标题模糊匹配替代字段证据；
3. 为叶子品类建立新的 `profile_code + version`；
4. 对等价单位、缺字段、未知别名和范围数量增加单元测试；
5. 政府来源增加零售/批发性质、来源日期、单位和地区 fixture；
6. 在 MySQL 5.7 和 8.x 各重放至少两次；
7. 真实来源执行一次低频只读 smoke；demo 不要求连续运行或调度试验。

上海白名单为青菜/新鲜一级、鸡毛菜/新鲜一级、大白菜/新鲜一级、黄瓜/新鲜一级、胡萝卜/新鲜一级、苹果/红富士一级、鸡蛋/新鲜完整和鲜猪肉/精瘦肉，均为元/500克。

商务部白名单均为元/公斤：

| 规范代码 | commodityid | 来源名称 | 分组 |
| --- | --- | --- | --- |
| `NAPA_CABBAGE` | `170060` | 大白菜 | 蔬菜 |
| `CUCUMBER` | `170130` | 黄瓜 | 蔬菜 |
| `CARROT` | `170260` | 胡萝卜 | 蔬菜 |
| `WHITE_RADISH` | `170070` | 白萝卜 | 蔬菜 |
| `TOMATO` | `170120` | 西红柿 | 蔬菜 |
| `POTATO` | `170080` | 土豆 | 蔬菜 |
| `ONION` | `170090` | 洋葱 | 蔬菜 |
| `GARLIC` | `170100` | 蒜头 | 蔬菜 |
| `GINGER` | `170110` | 生姜 | 蔬菜 |
| `EGGPLANT` | `170140` | 茄子 | 蔬菜 |
| `BROCCOLI` | `170340` | 西兰花 | 蔬菜 |
| `CHICKEN_EGG` | `150010` | 鲜鸡蛋 | 禽蛋 |
| `PORK_HIND` | `130014` | 后臀尖 | 肉类 |
| `BEEF_LEG` | `130025` | 牛腿肉 | 肉类 |
| `DRESSED_CHICKEN` | `280020` | 白条鸡 | 肉禽蛋 |

每个 `commodityid`、页面标题、导航名称和单位必须同时精确匹配。导航项“鲜猪肉”与“鲜牛肉”对应页面标题实际为“白条猪”与“整牛（白条牛）”，当前不把它们当作零售鲜肉，因此选择明确部位的后臀尖和牛腿肉。`PORK_HIND` 独立于上海 `LEAN_PORK`，不会把不同猪肉部位合并。

当前不支持鲜奶、活体、按实际称重结算、混合礼篮和需要人工判断成熟度的商品；只有出现明确政府数据样本后再增加。
