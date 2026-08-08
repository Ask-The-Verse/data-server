# Erkul `#DPSCalculator` 查询逻辑逆向分析

> 目标站点：`https://erkul.games/calculator`
> 数据源（CDN）：`https://cdn.erkul.games`
> 分析对象：前端 Angular 打包产物（`main-*.js` + 若干 `chunk-*.js`）
> 本文所有结论均已通过 [`erkul_fetch.py`](./erkul_fetch.py) 对线上真实数据端到端复现验证（见文末“验证结果”）。

本目录还保存了：
- [`erkul_fetch.py`](./erkul_fetch.py)：可重复执行的下载 + 解码 + 搜索 + 组件提取脚本
- `data/`：脚本运行时落盘的解码后 JSON（`catalog.bin.json`、`index.*.bin.json`、`ships_*.json`、`weapons/shields/powerplants.*.json` 等）
- `beauty-chunk-*.js`：关键 chunk 的美化（可读）版本

---

## 0. 结论速览（TL;DR）

| 关注点 | 结论 |
|---|---|
| 配置来源 | `main-*.js` 内联 `environment`：`catalogBaseUrl / pricesBaseUrl = https://cdn.erkul.games`，`apiBaseUrl = https://api.erkul.games`，默认 `branch = "LIVE"` |
| `.bin` 编码 | **UTF-8 JSON → raw DEFLATE**（无 gzip/zlib 头）。浏览器用 `DecompressionStream("deflate-raw")`，Python 用 `zlib.decompress(data, wbits=-zlib.MAX_WBITS)` |
| URL 规则 | 分支内资源：`{baseUrl}/{branch}/{path}`；全局资源（`status.bin`）：`{baseUrl}/{path}` |
| 加载链路 | `status.bin → catalog.bin(manifest) → index.<hash>.bin(轻量舰船清单) →（搜索）→ ships.group.<hash>.bin(组索引) → ships/<class>.<hash>.bin(完整舰船)` |
| 搜索补全逻辑 | **纯子串 OR 匹配**（非模糊/非打分）：`query` 小写去空格后，对每艘船的 `displayName / name / className / manufacturerName / role / career` 做 `includes` |
| 完整性校验 | manifest / 组索引里每个条目都带 `sha256`（64 hex），文件名 8 位 hash = SHA-256 前 8 位；哈希随数据更新变化，**不可硬编码** |
| 组件属性 | 舰船 blob 的 `slots` 树里是“默认装配”的完整组件对象；可换装的全量组件属性来自 `families`（`weapons/shields/powerplants/coolers/...`）family 文件 |
| Power Management | 每个组件的功耗（segments）来自其 `resource.states[].flows[].consumes[]`（`resource === "Power"`）；舰船总预算 + 分配由 `powerPools` 与前端 `ce()` 计算器综合得出 |

---

## 1. 应用启动与运行时配置

`calculator.html` 是一个 Angular SPA 外壳，`<erkul-root>` 由 `main-OLN2ALGO.js`（ESBuild 打包）驱动，其余功能按路由懒加载为 `chunk-*.js`。

运行时配置直接内联在主包里（`main-*.js`）：

```js
environment = {
  production: true,
  catalogBaseUrl: "https://cdn.erkul.games",
  pricesBaseUrl:  "https://cdn.erkul.games",
  branch: "LIVE",
  apiBaseUrl: "https://api.erkul.games",
  siteBaseUrl: "https://erkul.games",
  ...
}
```

通过 DI token `CATALOG_CONFIG` 注入到各服务（`beauty-chunk-NTT4BOSV.js`）：
- `DataVersionService` 管理当前分支 `branch ∈ {LIVE, PTU}`，默认 `LIVE`，可持久化到 `localStorage["erkul.dataVersion"]`；移动端强制 `LIVE`。
- 所有分支内资源 URL 由 `CatalogService.url()` 拼装：

```js
url(path, branch = this.dataVersion.branch()) {
  return `${this.config.baseUrl}/${branch}/${path}`;   // 例: https://cdn.erkul.games/LIVE/catalog.bin
}
```

---

## 2. `.bin` 的真实格式与解码

`.bin` **不是**加密数据，也不是 Protobuf/MessagePack，而是：

```
UTF-8 JSON  ->  raw DEFLATE（无 gzip / zlib 文件头）
```

因为是 raw DEFLATE，`file` 命令通常只识别为普通 `data`。

**浏览器端实现**（`beauty-chunk-ZQK62K4V.js`）：

```js
function jd(bytes) { return Iu(bytes, new DecompressionStream("deflate-raw")); }
async function yf(bytes) { return new TextDecoder().decode(await jd(bytes)); }
// 使用： JSON.parse(await yf(uint8array))
```

同一 chunk 还导出了对称的压缩函数（`CompressionStream("deflate-raw")`，用于分享构建时上传）。此外该 chunk 内置了整套 **Zod** 校验库——**每一份下载的数据在解码后都会被对应 Zod schema 严格 `parse` 校验**。

**Python 等价实现**（见 `erkul_fetch.py`）：

```python
import zlib, json
def decode_bin(raw: bytes):
    text = zlib.decompress(raw, wbits=-zlib.MAX_WBITS).decode("utf-8")
    return json.loads(text)
```

---

## 3. 数据加载链路（逐步）

核心服务 `CatalogService`（`beauty-chunk-7LWXX5WT.js` 中的 `class xt`）。其 `fetchDecoded()` 封装了“取字节 → raw-inflate → JSON.parse → Zod 校验”：

```js
async fetchDecoded(path, branch = this.dataVersion.branch()) {
  let url = this.url(path, branch);
  let res = await fetch(url);
  if (!res.ok) res = await fetch(url, { cache: "reload" });   // 失败重试(catalog 可能已更新)
  if (!res.ok) throw new Error("Catalogue fetch failed ... Reloading the page should fix it.");
  let bytes = new Uint8Array(await res.arrayBuffer());
  return JSON.parse(await je(bytes));   // je = 上文 yf，raw-deflate 解码
}
```

### 3.1 `status.bin`（全局，不带分支）
`BranchStatusService`（`beauty-chunk-OJVIGUSE.js`）每 60s 拉一次 `GET {baseUrl}/status.bin`：

```
{ schemaVersion:1, LIVE:{status:"open"|"closed", message?}, PTU:{...}, updatedAt }
```
用于判断 LIVE/PTU 是否在维护（`closed` 时禁止加载该分支的构建）。

### 3.2 `catalog.bin`（manifest / 资源清单）
`CatalogService.manifest()` → `GET {baseUrl}/{branch}/catalog.bin`，Zod schema（`yt`）结构：

```jsonc
{
  "schemaVersion": 8,                    // 7 或 8
  "branch": "LIVE",
  "dataVersion": "4.9.0-LIVE.12344265",  // 游戏数据版本(patch)
  "generatedAt": "...",
  "assemblerVersion": "...",
  "source": { "manifestSha256": "<64hex>", "parserManifest": {...} },
  "groups":   [ { "kind":"ships"|"groundvehicles", "count", "totalBytes",
                  "indexPath":"ships.group.<hash>.bin", "indexSha256":"<64hex>" }, ... ],
  "families": [ { "kind":"weapons"|"shields"|"powerplants"|..., "count", "totalBytes",
                  "path":"weapons.<hash>.bin", "sha256":"<64hex>" }, ... ],
  "singles":  [ { "kind":"index"|"integrity"|"changelog", "path":"index.<hash>.bin",
                  "sha256":"<64hex>", "bytes" }, ... ],
  "patches":  [ ... ]
}
```

三类资源：
- **singles**：其中 `kind:"index"` 是全站**轻量舰船清单**（搜索/列表用）；`kind:"integrity"` 是校验清单。
- **groups**：`ships` / `groundvehicles`，每组有自己的“组索引”文件（`indexPath`）。
- **families**：23 个可换装组件族（`weapons, shields, powerplants, coolers, radars, quantumdrives, mounts, turrets, missiles, missileracks, bombs, mininglasers, salvageheads, tractorbeams, qeds, emps, jumpdrives, rocketpods, blades, modules, paints, utilities, manufacturers`）。

### 3.3 `index.<hash>.bin`（轻量舰船清单，搜索基础）
`CatalogService.loadIndex()`：

```js
let single = (await this.manifest()).singles.find(s => s.kind === "index");
return ft.parse(await this.fetchDecoded(single.path));   // ft = 舰船索引 schema
```

返回 `{ branch, dataVersion, ships: LightShip[], reverseIndex }`。每条 `LightShip`（schema `rn`）是一份精简元数据，**足够渲染列表与搜索，但不含插槽/装配**：

```
ref, className, category("AssembledShip"|"AssembledGroundVehicle"),
name, shortName?, displayName?, role, career, focus,
crewSize, size, manufacturerName, massFixedKg, hp, cargo, storage,
shield?, crossSection?, dimensions?, armor*, shield*, flight?, fuel?, dps?, quantum? ...
```

`ShipIndexService`（`beauty-chunk-POBC5U3X.js`）在分支变化时调用 `loadIndex()`，把 `ships` 灌入一个 signal（`this.ships`），供 UI 消费。

### 3.4 完整舰船数据 `ships/<class>.<hash>.bin`
选定某船后 `CatalogService.loadVehicle(className, category)`：

```js
async loadVehicle(className, category) {
  let manifest = await this.manifest();
  let kind  = category === "AssembledGroundVehicle" ? "groundvehicles" : "ships";
  let group = manifest.groups.find(g => g.kind === kind);
  // 第一跳：取“组索引”，其 blobs[] 把 className -> 具体 blob 路径映射
  let blob  = bt.parse(await this.fetchDecoded(group.indexPath))
                .blobs.find(b => b.id === className);
  // 第二跳：取完整舰船 blob
  let data  = await this.fetchDecoded(blob.path);
  return kind === "groundvehicles" ? he.parse(data) : ge.parse(data);
}
```

组索引条目（schema `pn`）形如：

```json
{ "id":"aegs_hammerhead_gs",
  "path":"ships/aegs_hammerhead_gs.f0d123ba.bin",
  "sha256":"f0d123ba...(64 hex)", "bytes":26841 }
```

> **哈希说明**：文件名里的 8 位 hash（`f0d123ba`）就是完整 `sha256` 的前 8 位。数据一更新哈希就变，因此**不能长期硬编码 hashed URL**——必须每次从 `catalog.bin` → 组索引里现取。`erkul_fetch.py` 对每一跳都做了 `sha256` 校验，全部通过。

### 3.5 组件族文件 `families`
可换装组件（用于替换默认件、以及做对比/计算）按需整族加载，带内存缓存：

```js
loadWeapons()     { return this.loadFamily("weapons", G); }      // AssembledWeapon[]
loadShields()     { return this.loadFamily("shields", Ce); }
loadPowerPlants() { return this.loadFamily("powerplants", ve); }
loadCoolers()     { return this.loadFamily("coolers", Se); }
// fetchFamily: 取 families[kind].path -> 解码 -> Zod 数组校验 -> Map(className -> item)
```

---

## 4. 飞船搜索与自动补全逻辑

搜索并不是模糊/打分算法，而是**大小写无关的子串 OR 过滤**。两个入口的实现完全一致：

**① 舰队目录抽屉**（`beauty-chunk-YAOUP3I2.js`，`rows` 计算）——最贴近“搜索飞船”的场景：

```js
rows = computed(() => {
  let list = this.ships() ?? [];              // 来自 ShipIndex 的轻量清单
  let q = this.query().trim().toLowerCase();  // 去空格 + 小写
  let out = [];
  for (let m of list) {
    // 先按 kind(ships/groundvehicles) 和 size 过滤
    if (kind==="ships" && m.category!=="AssembledShip") continue;
    if (sizes.size>0 && !sizes.has(m.size)) continue;
    let label = m.displayName ?? m.name;
    // ★ 核心补全匹配：任一字段包含 q 即命中
    if (q &&
        !label.toLowerCase().includes(q) &&
        !m.name.toLowerCase().includes(q) &&
        !m.className.toLowerCase().includes(q) &&
        !m.manufacturerName.toLowerCase().includes(q) &&
        !m.role.toLowerCase().includes(q) &&
        !m.career.toLowerCase().includes(q)) continue;
    out.push({ ship: m, label, ... /* 拼接商店价格等 */ });
  }
  // 排序：name(A-Z) / cheapest / priciest，其余按 label.localeCompare
  return out.sort(...);
});
```

**② 统计/对比表**（`beauty-chunk-4SB3VKZH.js`）对同一清单做类似过滤，命中字段为 `name / sub(=manufacturerName) / id(=className)`，并叠加 size / grade / class / maker 等下拉筛选。

**匹配字段汇总**：`displayName`、`name`、`className`、`manufacturerName`、`role`、`career`（对比表用 `name / manufacturerName / className`）。没有拼写纠错、没有编辑距离、没有权重排序——就是 `String.prototype.includes`。

**选中即加载**：点击结果 → `LoadoutService.select(lightShip)` → 内部调用 `catalog.loadVehicle(className, category)`（即 §3.4 两跳），随后 `recompute()`：

```js
async select(e) {
  let ship = await this.catalog.loadVehicle(e.className, e.category);  // 完整数据
  this.vehicle.set(ship);
  this.ensureInitialAllocation(ship, this.shipMode());
  this.recompute();   // 计算 DPS / 电力 / 冷却等
}
```

---

## 5. 从舰船数据中提取 武器 / 护盾 / 电源 等组件

完整舰船 blob（schema `AssembledShip` = `ge`）关键字段：

```
category:"AssembledShip", className, ref, size, grade, type, subType, tags, i18n, manufacturer,
vehicle:  { vehicleDisplayName, crewSize, totalMass, hardpoints[], parts[], powerPools, ... },
slots:    Slot[]         // ★ 装配树（默认已装好的组件都在这里）
precomputed: { hp, dps, flight, thrust, fuel, cargo, storage, quantum, signatureAtRest, ... },
shield?: "Bubble" | "Quadrant"
```

### 5.1 `slots` 装配树 —— 默认装配的组件
`slots` 是递归树（schema `D`），每个节点：

```jsonc
{ "kind": "fixed" | "swappable",
  "portName": "hardpoint_weapon_left",
  "portPath": ["...","..."],          // 端口路径，做 key 用
  "hardpoint": { minSize, maxSize, accepts:[{type,subTypes}], ... },
  "item": { category:"Weapon"|"Shield"|"PowerPlant"|..., className, ... },  // 默认装的完整组件对象
  "default": { uuid, className },     // swappable 端口的出厂默认
  "children": [ ... ]                 // 子端口（如挂架下的武器、炮塔下的枪）
}
```

**提取方式 = 深度遍历 `slots`，读每个节点的 `item.category`**。前端用 `Mo()` 把 `category` 映射到“电力族”（`beauty-chunk-727TWVJF.js`）：

```
Weapon/AssembledWeapon -> "weapon"     Shield -> "shield"
PowerPlant -> "powerplant"             Cooler -> "coolers"
Radar -> "radar"                       QuantumDrive -> "qdrive"
QED -> "qed"   EMP -> "emp"   LifeSupport -> "lifeSupport"
MiningLaser -> "miningLaser"   SalvageHead -> "salvage"
TractorBeam -> "tractorBeam"/"towingbeam"
Controller(Flight/Wheeled) / AssembledBlade -> "engine"
```

`erkul_fetch.py` 用同样的遍历，从 Hammerhead 的 `slots` 中提取到：`weapon×24, shield×2, powerplant×2, cooler×2, radar×1, qdrive×1`。

各类组件的**详细属性 schema**（都在 `beauty-chunk-7LWXX5WT.js` 定义）：
- **武器 `Weapon`（V）**：`weapon.fireActions[]`（`single/rapid/burst/charged/beam`，含 `fireRate, damageMultiplier, pelletCount, spread...`）、`weapon.ammo`、`weapon.regen`、`weaponType {class, form}`、`resource`（功耗/热/信号）。
- **护盾 `Shield`（ct）**：`shield.{maxShieldHealth, maxShieldRegen, downedRegenDelay, damagedRegenDelay, resistance, absorption, ...}` + `resource`。
- **电源 `PowerPlant`（nt）**：主要就是 `resource`（见下）+ `distortion`。
- **冷却 `Cooler`（Ue）**：`resource` + `distortion`。

### 5.2 可换装全量组件
`slots` 里只有“默认件”。要在 UI 里换装/对比，需 §3.5 的 family 文件（如 `weapons.<hash>.bin` 含 135 件武器、`shields` 66 件、`powerplants` 75 件），按 `className` 建 Map 供选择。

---

## 6. Power Management（电力管理）信息

Power Management 是**前端计算结果**，不是某个字段直接给出，输入有两处：

### 6.1 每个组件的功耗（来自 `resource` 模型）
组件的 `resource`（schema `n`）描述其在不同状态下的资源流：

```jsonc
"resource": {
  "states": [
    { "name": "Online",
      "flows": [ { "kind":"consume",
                   "consumes":[ { "resource":"Power", "units": 3, "unitKind":"powerSegment"|"standard", ... } ],
                   "minimumFraction": 0.x } ],
      "powerRanges": [ { "band":"off"|"low"|"medium"|"high"|"overclock", "start", "modifier", "registerRange" } ],
      "signature": { "em":{nominal,decayRate?}, "ir":{...} } } ]
}
```

前端 `w()`（`beauty-chunk-2I5GNAI3.js`）据此算“Power draw（seg）”：找 `states` 里 `Online` 的 `flows`，取 `consumes` 中 `resource==="Power"` 的 `units`；若 `unitKind==="powerSegment"` 则按 `minimumFraction` 折算出可变下限，得到“最小～额定”功耗（单位 `seg`），并顺带给出 EM/IR 信号。

### 6.2 舰船电力预算与分配（`powerPools` + `ce()` 计算器）
舰船 `vehicle.powerPools` 定义了各类池子（示例 Hammerhead）：

```json
{ "maxDefaultDistribution": 0,
  "pools": [ { "kind":"Fixed",  "itemType":"WeaponGun", "poolSize": 10 },
             { "kind":"Dynamic","itemType":"Shield",    "maxItemCount": 2 },
             { "kind":"Dynamic","itemType":"Flight...", ... } ] }
```

`ce()`（`beauty-chunk-727TWVJF.js`）把电力预算按“段（segments）”在各族之间分配，产出 Power Management 面板的数据结构：

```js
{
  totalAvailable,     // 该船可用总段数(e.power.totalAvailableSegments)
  remaining,          // 剩余 = 总数 - 各列已用
  shipMode: "SCM"|"NAV",
  columns: [          // 每个电力族一列
    { id:"weapon",  family:"weapon",  isWeaponPool:true, totalSegments, powered, blocks:[...] },
    { id:"shield",  family:"shield",  totalSegments, powered, locked, blocks:[...] },
    { id:"engine" }, { id:"qdrive" }, { id:"radar" }, { id:"lifeSupport" },
    { id:"qed" }, { id:"emp" }, { id:"coolers:<port>", isCooler:true }, ...
  ]
}
```

要点：
- **武器池**：`powerPools` 里 `kind:"Fixed", itemType:"WeaponGun"` 的 `poolSize`（Hammerhead = 10）就是武器共享段数；`xe()` 把各武器打包成 blocks，用户可分配段数。
- **护盾**：动态池 `itemType:"Shield"` 的 `maxItemCount` 决定可上线的护盾数（`ue()`，默认下限 2）；`So()` 处理护盾上/下线与段位排序；`SCM/NAV` 模式会 lock/清零部分族（如护盾在 NAV、qdrive 在 SCM）。
- **各族段数**：`bo()` 依据 `overrides`（用户手动改的 `powerStates: Online/Offline`、`segments`、`emp on/off`、`cooler` 段数等）与 `perPort` 缺省值计算。
- **持久化**：用户的电力覆盖存于 `overridesByMode.{SCM,NAV}`（schema `Le`：`weaponPoolSegments / engine / shield / cooler / radar / quantum / qed / lifeSupport / emp / powerStates / shieldPromotions`），可随分享链接（`le` schema）恢复。

---

## 7. 完整调用序列（含真实 URL）

```
GET https://cdn.erkul.games/status.bin                              # 分支开/关
GET https://cdn.erkul.games/LIVE/catalog.bin                        # manifest
GET https://cdn.erkul.games/LIVE/index.54ab12ab.bin                 # 218 艘轻量舰船(搜索)
      └─(本地 substring 过滤) hammerhead -> className=aegs_hammerhead_gs
GET https://cdn.erkul.games/LIVE/ships.group.fe98ea02.bin           # ships 组索引(className->blob)
GET https://cdn.erkul.games/LIVE/ships/aegs_hammerhead_gs.f0d123ba.bin   # 完整舰船(slots/precomputed/powerPools)
# 换装/计算按需：
GET https://cdn.erkul.games/LIVE/weapons.3408d52a.bin               # 135 武器
GET https://cdn.erkul.games/LIVE/shields.b39b6ddc.bin               # 66 护盾
GET https://cdn.erkul.games/LIVE/powerplants.0d80c252.bin           # 75 电源
GET https://cdn.erkul.games/LIVE/coolers.<hash>.bin                 # 冷却器 ...
```

（价格来自 `prices.bin`：`GET {pricesBaseUrl}/{...}`，与 `api.erkul.games` 一起用于商店/分享/排行等社区功能，非核心计算所需。）

---

## 8. 复现方式与验证结果

```bash
cd erkul-analysis
python3 erkul_fetch.py hammerhead      # 参数=搜索关键字，默认 hammerhead
```

真实运行输出（节选，均已通过 sha256 校验）：

```
[1] status.bin            LIVE=open  PTU=open
[2] catalog.bin           schemaVersion=8  dataVersion=4.9.0-LIVE.12344265
    groups=['groundvehicles','ships']   singles=['index','integrity']
    families=[blades,bombs,coolers,emps,jumpdrives,manufacturers,mininglasers,
              missileracks,missiles,modules,mounts,paints,powerplants,qeds,
              quantumdrives,radars,rocketpods,salvageheads,shields,tractorbeams,
              turrets,utilities,weapons]
[3] index.54ab12ab.bin    sha256 OK   ship count = 218
[4] search 'hammerhead'   -> Hammerhead  className=aegs_hammerhead_gs (Aegis Dynamics)
[5] ships.group.fe98ea02.bin  sha256 OK  -> ships/aegs_hammerhead_gs.f0d123ba.bin (26841B)
[6] ship blob             sha256 OK   vehicle=Aegis Hammerhead crew=8
    powerPools: Fixed WeaponGun poolSize=10, Dynamic Shield maxItemCount=2, ...
[7] slot tree ->  weapon:24  shield:2  powerplant:2  cooler:2  radar:1  qdrive:1
[8] families ->  weapons:135  shields:66(maxShieldHealth=194400)  powerplants:75(powerRanges: low/medium/high)
```

结论：本文档描述的“配置 → 编码格式 → URL 规则 → 加载链路 → 搜索逻辑 → 组件/电力提取”全部与线上实际数据一致。

---

## 9. Versioned SQLite workflow

Run the production crawler from the workspace root:

```bash
python3 crawler/workflows/erkul_workflow.py
```

It discovers the current game version, skips versions already marked complete,
downloads and verifies all manifest families, stores the complete lightweight
ship catalogue plus only the Hammerhead full detail, and writes to the shared
per-version database under `crawler/data/`.

See [`DATABASE_SCHEMA.md`](./DATABASE_SCHEMA.md) for the output layout, command
options, and every Erkul table schema.
