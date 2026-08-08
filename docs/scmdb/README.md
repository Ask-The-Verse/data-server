# SCMDB（scmdb\.net）数据来源与爬取复刻指南

**一句话结论**：scmdb\.net 是一个纯静态的 React/Vite 单页应用，**没有任何私有后端 API**。全部数据都是托管在 `https://scmdb.net/data/` 下、按游戏版本切分的静态 JSON 文件。搜索、详情展示、矿物品质计算全部在浏览器端完成。只要按本文下载对应版本的几个 JSON 文件并复刻少量计算逻辑，即可完整复现网站功能，配套脚本见 `scmdb_client.py`。

## 一、目的与范围

本文说明 scmdb\.net（Star Citizen Missions, Crafting \& Mining Database）的全部数据来源与前端处理逻辑，覆盖任务板（missions）、制造页（`?page=fab`）、资源/矿物页（`?page=mine`）三大板块。目标是：仅凭本文，另一个人即可独立完成对该站点的数据爬取，并用 Python 复刻其数据加载、搜索、详情展示与矿物品质计算功能。

不在范围内的内容：站点的账号/贡献者系统（基于 Supabase 的登录与投票，属于可选增强，与只读数据爬取无关）、社区评分 `mema-cache` 的写入链路。

## 二、前置条件

- 能访问公网 `https://scmdb.net`。站点位于 Cloudflare 之后，普通浏览器 User\-Agent 即可直接拉取 JSON，无需登录、无鉴权头、无 Cookie。

- Python 3\.8\+，仅需标准库（`urllib`、`json`、`math`、`unicodedata`）。

- 基本了解 HTTP GET 与 JSON 解析即可，无需逆向前端 JS（本文已完成逆向）。

## 三、站点架构与数据来源总览

页面入口为 `https://scmdb.net/index.html`，其中只有一个 `<div id="root">` 和一个打包后的 JS 包 `/assets/index-*.js`（约 1MB）。所有内容由该 JS 在运行时拉取 JSON 渲染。数据文件全部位于 `/data/` 目录，按游戏构建版本（version）命名。核心与可选文件如下：

|文件（相对 /data/）|必需|用途|
|---|---|---|
|`game-versions.json`|是|版本索引，列出所有可用构建及其 merged 文件名，新版本在前|
|`site-settings.json`|否|全站开关（是否启用制造、是否启用 PTU、场景覆盖等）|
|`merged-<version>.json`|是|任务板核心数据：contracts（任务）\+ 各类共享池（地点、资源、派系、蓝图等）|
|`crafting_blueprints-<version>.json`|是（制造页）|制造配方：产物、层级、槽位、所需资源/物品与最低品质|
|`crafting_items-<version>.json`|是（制造页）|物品属性库（武器/护甲等的数值、伤害抗性、弹药等）|
|`mining_data-<version>.json`|是（矿物页）|可采元素、矿脉成分、地点分布、精炼厂、品质分布与品质带边界|
|`mining_equipment-<version>.json`|是（矿物页）|采矿激光、模块、道具、全局采矿参数|
|`changelog.json`|否|站点更新日志|
|`mema-cache.json`|否|社区任务用时/收益/评分缓存（按 contract\_id）|
|`deltas-<version>.json` / `overrides-<version>.json` / `cig_data_issues-<version>.json` / `mission-history-<version>.json`|否|可选覆盖层/数据修正，通常不存在时会被 SPA 回落成 index\.html（拉取到 HTML 即表示该文件不存在，忽略即可）|

**Supabase 回落。**当 `/data/game-versions.json` 拉取失败时，前端会回落到 Supabase 存储：`https://gqfsmlwaeklemieayaxg.supabase.co/storage/v1/object/public/game-data/<file>`，并查询其 `game_versions` 表。复刻时优先用 `/data/`，无需依赖 Supabase。

## 四、数据加载流程

前端加载遵循固定顺序，复刻时照做即可。

1. 拉取 `/data/game-versions.json?t=<毫秒时间戳>`（加时间戳绕过 CDN 缓存），得到形如 `[{"version":"4.10.0-ptu.12388491","file":"merged-4.10.0-ptu.12388491.json"}, ...]` 的数组，新版本在前。

2. 按频道（channel）分类版本：判定规则等价于——版本串包含 `-ptu.` 或 `-ptu-` 即为 `ptu`，否则为 `live`。URL 参数 `?channel=ptu` 会选中 PTU，默认选中 LIVE。取该频道下的第一条（即最新）作为活动构建。

3. 用活动构建的 `version` 拼出 `merged-<version>.json`、`crafting_blueprints-<version>.json`、`crafting_items-<version>.json`、`mining_data-<version>.json`、`mining_equipment-<version>.json` 并按需下载。`merged` 文件名优先用 `game-versions.json` 里给出的 `file` 字段。

4. 前端把 `game-versions.json` 结果缓存进 `localStorage`（带时间戳，过期重取）；复刻脚本用本地文件缓存即可。

各版本 JSON 文件体积参考（LIVE 4\.9\.0）：merged ≈ 12MB，crafting\_items ≈ 1\.3MB，crafting\_blueprints ≈ 4\.2MB，mining\_data ≈ 388KB，mining\_equipment ≈ 50KB。建议本地缓存，避免重复下载。

## 五、页面路由

路由通过 URL 查询参数控制，无独立后端路由。核心映射：`?page=fab` → 制造（内部键 `fabricator`），`?page=mine` → 矿物（`mining`），默认/无参 → 任务板（`missions`），另有 `?page=signature`（雷达特征）。矿物详情用 `?page=mine&mine=<元素名>`，PTU 频道追加 `&channel=ptu`。这些参数只影响前端渲染哪块数据，不改变数据文件来源。

## 六、任务板（missions）数据模型与详情解析

任务板读取 `merged-<version>.json`。顶层键包括：`contracts`（任务数组）、`legacyContracts`、`locationPools`（地点池，短 ID → 地点对象）、`resourcePools`（资源池）、`factions`（派系，GUID → 派系对象）、`scopes`（声望范畴）、`blueprintPools`（蓝图奖励池）、`factionRewardsPools`（声望奖励池）、`partialRewardPayoutPools`、`ships`、`regions` 等。

每条 contract 用短 ID / 索引引用共享池，详情展示即是把这些引用解析成可读对象：

|contract 字段|解析方式|
|---|---|
|`title` / `description`|已是英文明文，含 `[LOCATION]`、`[DESTINATION]` 等占位符与 `<EM4>` 富文本标记；`titleKey`/`descriptionKey` 供多语言覆盖|
|`factionGuid`|查 `factions[guid]` 得派系名/logo|
|`locations` / `destinations`|短 ID 数组，逐个查 `locationPools[id]` 得 `{name, type, system, planet, moon}`|
|`rewardUEC` / `buyIn`|直接的报酬/买入金额（UEC）|
|`factionRewardsIndex`|索引进 `factionRewardsPools[idx]`，得 `[{factionGuid, scopeGuid, amount}]`；再查 `factions` 与 `scopes` 得「派系 \+ 声望范畴 \+ 数值」|
|`partialRewardPayoutIndex`|索引进 `partialRewardPayoutPools`（部分完成报酬）|
|`blueprintRewards`|数组 `[{blueprintPool, chance, trigger, poolName}]`；用 `blueprintPool` 查 `blueprintPools[guid].blueprints` 得可获得蓝图列表|
|`missionType` / `category` / `illegal` / `canBeShared` / `timeToComplete`|类型、分类、是否非法、是否可分享、时限等元信息，直接使用|

## 七、制造页（fab / crafting）数据模型

制造页读取 `crafting_blueprints-<version>.json` 与 `crafting_items-<version>.json`，前端拉取路径为 `./data/crafting_blueprints-<version>.json` 与 `./data/crafting_items-<version>.json`。

**crafting\_blueprints** 顶层：`meta`、`dismantle`（拆解规则：效率 0\.5、耗时 15s、黑名单资源）、`properties`（属性字典：名称/单位/分类）、`resources`、`items`、`blueprints`（配方数组，约 1597 条）。单条配方结构：

```json
{
  "guid": "...", "tag": "BP_CRAFT_...", "productEntityClass": "...",
  "gear": "missionitems", "type": null, "subtype": null,
  "productName": "Metamaterial Test #146", "manufacturer": null,
  "tiers": [
    {
      "craftTimeSeconds": 70,
      "slots": [
        { "name": "Substrate",
          "options": [ { "type": "resource", "quantity": 2.0, "minQuality": 900, "resourceName": "Titanium" } ] },
        { "name": "Filament Coating",
          "options": [ { "type": "item", "quantity": 4, "minQuality": 0, "itemName": "Yormandi Eye" } ] }
      ]
    }
  ]
}
```

展示逻辑：按 `productName` 查配方（精确匹配优先，回落子串匹配），遍历 `tiers → slots → options`，每个 option 是「resource 或 item \+ quantity \+ minQuality」。`minQuality` 即前面矿物品质（0–1000）在制造中的最低要求，把矿物页与制造页串联起来。

**crafting\_items** 顶层：`manufacturers`、`items`（约 1590 条物品）及若干共享池（`damageResistancePools`、`ammoPools`、`magazinePools`、`fireModesPools`、`signaturesPools`）。物品通过 `fireModesIndex`/`ammoIndex`/`magazineIndex` 等索引引用这些池，展示时按索引取出拼装（与任务的池\-索引模式一致）。

## 八、资源/矿物页（mine / mining）数据模型

矿物页读取 `mining_data-<version>.json` 与 `mining_equipment-<version>.json`。`mining_data` 顶层键：

- `mineableElements`：GUID → 元素对象，含 `name、rarity、density、instability、resistance、qualityBands、materialName` 等。

- `compositions`：GUID → 矿脉成分，含 `parts[]`，每个 part 有 `elementGuid、elementName、probability、minPercent、maxPercent、curveExponent、qualityScale`。

- `locations`：地点数组，每个含 `groups[] → deposits[]`，deposit 通过 `compositionGuid`（相对概率 `relativeProbability`）引用成分；`groupName` 决定采矿方式。

- `qualityDistribution`：按采矿方式的品质分布（详见第九节）。

- `qualityBandBoundaries`：全局品质带边界，值为 `[0, 400, 600, 700, 800, 900, 950, 999]`。

- `refineries` / `refineryProfiles`：精炼厂及其对各元素的产率偏移（profileId 关联）。

`mining_equipment` 含 `globalParams`（如 ship 的 optimalWindowSize、resistanceCurveFactor 等采矿物理参数）、`lasers`（18 把，含 miningBeam/extractionBeam 的射程与 DPS）、`modules`、`gadgets`、`fpsTools`。

采矿难度标签由 `instability` 与 `resistance` 合成：难度分 `= clamp(max(0, instability)/1000 * 0.6 + max(0, resistance + 0.5)/1.5 * 0.4, 0, 1)`，再按 `<0.25 Easy / <0.5 Moderate / <0.75 Hard / 其余 Extreme` 分级。

## 九、矿物品质计算算法（核心）

游戏内矿物「品质」是 0–1000 的数值，其在给定矿脉元素上的分布是一个**截断正态分布**。前端据此计算「品质区间」与「落入各品质带的概率」。完整算法如下（与打包 JS 的 Px / yl / r1 / b1 / qx 函数一一对应）。

**第 1 步：确定采矿方式（method）。**由 deposit 所属 `groupName` 映射：`SpaceShip_Mineables` 与 `SpaceShip_Mineables_Rare` → `shipmineables`；`FPS_Mineables` → `fpsmineables`；`GroundVehicle_Mineables` → `groundmineables`；`Harvestables` → `harvestables`。

**第 2 步：取基础分布。**从 `qualityDistribution[method]` 取 `{min, max, mean, stddev}`。选择规则（对应 JS 函数 `b1`）：

- `shipmineables`：先按元素 `rarity`（common/uncommon/rare/epic/legendary）取子节点，再看 `locationOverrides`：优先匹配「命名地点/星系」覆盖，其次纯星系匹配，Pyro 星系回落到 `pyro` 覆盖，最后用 `default`。

- `fpsmineables`：若元素名命中 `elementOverrides` 则用之；否则 Pyro 用 pyro 覆盖，再回落 `default`。

- 其余方式：Pyro 用 pyro 覆盖，否则 `default`。

**第 3 步：按 qualityScale 缩放。**取 composition part 的 `qualityScale`（0–1，高品质纯矿脉为 1\.0）。得到有效分布：`min' = round(min×scale)`、`max' = round(max×scale)`、`mean' = round(mean×scale)`、`stddev' = max(1, stddev×scale)`。若无基础分布，则只知上界 `≤ round(scale×1000)`。

**第 4 步：正态分布函数。**概率密度 `Px` 为标准正态 PDF；累积分布 `yl` 使用 Abramowitz \& Stegun 26\.2\.17 有理逼近（注意：站点用此逼近而非 erf，复刻时须一致，否则概率有微小偏差）：

```python
def normal_pdf(x, mu, sigma):
    return 1/(sigma*(2*math.pi)**0.5) * math.exp(-0.5*((x-mu)/sigma)**2)

def normal_cdf(x, mu, sigma):  # Abramowitz-Stegun 26.2.17
    l = (x-mu)/sigma
    c = 1/(1+0.2316419*abs(l))
    d = 0.3989422804014327*math.exp(-0.5*l*l)*c*(0.3193815+c*(-0.3565638
        +c*(1.781478+c*(-1.821256+c*1.3302744))))
    return 1-d if l > 0 else d
```

**第 5 步：各品质带概率（对应 JS 函数 ****`r1`****）。**用截断正态在 `[min', max']` 上归一：总质量 `total = CDF(max') - CDF(min')`；对 `qualityBandBoundaries` 中每个边界 `p`（下一个边界记为 `v`，最后一带上界为 1000），该带概率 `= max(0, (CDF(min(max', v)) - CDF(max(min', p))) / total)`。

**第 6 步：与制造对接。**每个元素还带 `qualityBands`（长度 8 的数组，如 Agricium 为 `[346,588,667,796,852,943,971,1000]`），表示该材料在各品质带对应的实际品质映射值，供制造配方的 `minQuality` 门槛判断使用。

**验证**：以 Agricium\(Ore\)、ship 采矿、scale=1\.0、Stanton 星系为例，基础分布为 `{min 501, max 1000, mean 500, stddev 150}`，脚本算出各带概率为 band2≈49\.3%、band3≈32\.5%、band4≈13\.8%、band5≈3\.8%……与网站显示一致。

## 十、搜索逻辑

搜索完全在客户端进行，无搜索接口。任务搜索（对应 JS 函数 `yN`）流程：输入经 150ms 防抖 → `normalize("NFC").toLowerCase()` → 按空白拆成 token。对每条任务，拼接一个大文本 blob（标题、描述、debugName/debugNames、派系名、解析后的奖励蓝图名/船只名/货物名、以及 `illegal/legal`、`shared/solo`、`unique`、`blueprint` 等标志词），同样 NFC\+lowercase 后，要求**所有** token 都是子串（AND 语义）才命中。制造与矿物页的过滤同理，都是对已加载数据做本地大小写不敏感子串匹配。

## 十一、爬取与复刻标准流程（SOP）

1. **取版本索引。**`GET https://scmdb.net/data/game-versions.json?t=<ts>`，解析为数组。

2. **选频道版本。**按第四节规则挑出 LIVE（或 PTU）最新构建的 `version`。

3. **下载数据文件。**按 `version` 下载 merged、crafting\_blueprints、crafting\_items、mining\_data、mining\_equipment 五个文件（按需）。请求头带常规浏览器 UA；建议落地缓存。

4. **校验。**若某文件返回的是 HTML（`<!DOCTYPE html>` 开头）而非 JSON，说明该（可选）文件不存在，跳过即可；核心五文件均应为 200 且是 JSON。

5. **构建索引。**把 `locationPools`、`factions`、`scopes`、`blueprintPools`、`mineableElements`、`compositions` 读入内存字典。

6. **实现搜索与详情解析。**按第六至第十节复刻。

7. **实现品质计算。**严格按第九节的 6 步与两种正态函数复刻，注意用 A\&S CDF 逼近。

**注意事项**：① 数据按版本切分，版本升级后文件名随之变化，务必先读 `game-versions.json` 再拼文件名，不要硬编码版本；② merged 文件较大（约 12MB），务必缓存并考虑流式解析；③ 站点在 Cloudflare 后，请控制请求频率、加合理 UA，避免触发风控。

## 十二、配套 Python 脚本

随附 `scmdb_client.py`（纯标准库）已完整实现上述全部逻辑，可直接运行 `python3 scmdb_client.py` 查看演示输出。主要能力：

- `list_versions()` / `pick_version(channel)`：版本索引与频道选择（`channel_of` 复刻 JS 的 `pN`）。

- `ScmdbData(channel)`：一次性加载并缓存某构建的全部数据文件，暴露 contracts、pools、mineableElements 等快捷入口。

- `search_missions(query)` \+ `contract_detail(c)`：复刻任务搜索与「短 ID/索引 → 可读详情」解析。

- `find_blueprint(name)`：按产物名查制造配方并展开层级/槽位。

- `normal_pdf / normal_cdf / band_probabilities / quality_range`：矿物品质数学，分别对应 JS 的 `Px / yl / r1 / (b1+qx)`。

脚本内每个函数都标注了其对应的原始 JS 函数名，便于对照校验与后续维护。

## 十三、Versioned SQLite workflow

Run the production crawler from the workspace root:

```bash
python3 crawler/workflows/scmdb_workflow.py
```

It discovers the newest game version for the requested channel, skips versions
already marked complete, downloads all required files plus valid optional
overlays, and writes one table per top-level data type into the shared
per-version database under `crawler/data/`.

See [`DATABASE_SCHEMA.md`](./DATABASE_SCHEMA.md) for the output layout, command
options, generated-table contract, and complete table inventory.
