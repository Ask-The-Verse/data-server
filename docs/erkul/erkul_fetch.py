#!/usr/bin/env python3
"""
Erkul.games #DPSCalculator 数据抓取与解析脚本。

复现前端的完整数据加载链路：
  status.bin -> catalog.bin -> index.<hash>.bin -> ships.group.index -> ships/<class>.<hash>.bin
                                             \-> families (weapons/shields/powerplants/...)

所有 .bin 文件的编码格式为:  UTF-8 JSON  ->  raw DEFLATE (无 zlib/gzip 头)
因此解码时必须使用负数 wbits: zlib.decompress(data, wbits=-zlib.MAX_WBITS)
"""
import json
import sys
import zlib
import hashlib
import urllib.request

BASE = "https://cdn.erkul.games"          # environment.catalogBaseUrl
BRANCH = "LIVE"                            # environment.branch (LIVE | PTU)
OUT = "data"

import os
os.makedirs(OUT, exist_ok=True)


def http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "erkul-analyzer/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def decode_bin(raw: bytes):
    """raw DEFLATE -> UTF-8 JSON -> python object"""
    text = zlib.decompress(raw, wbits=-zlib.MAX_WBITS).decode("utf-8")
    return json.loads(text)


def fetch_decoded(path: str, branch: bool = True, verify_sha=None):
    url = f"{BASE}/{BRANCH}/{path}" if branch else f"{BASE}/{path}"
    raw = http_get(url)
    if verify_sha:
        got = hashlib.sha256(raw).hexdigest()
        ok = got == verify_sha
        print(f"    sha256 {'OK' if ok else 'MISMATCH'} ({got[:8]}...)")
    obj = decode_bin(raw)
    # 存盘方便查看
    fname = os.path.join(OUT, path.replace("/", "_") + ".json")
    with open(fname, "w") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    print(f"    {url}  ({len(raw)} bytes deflate) -> {fname}")
    return obj


def main():
    search_term = sys.argv[1].lower() if len(sys.argv) > 1 else "hammerhead"

    # 1) 分支状态
    print("[1] status.bin")
    status = decode_bin(http_get(f"{BASE}/status.bin"))
    print(f"    LIVE={status['LIVE']['status']}  PTU={status['PTU']['status']}")

    # 2) catalog.bin (manifest)
    print("[2] catalog.bin (manifest)")
    manifest = fetch_decoded("catalog.bin")
    print(f"    schemaVersion={manifest['schemaVersion']} branch={manifest['branch']} "
          f"dataVersion={manifest['dataVersion']}")
    print(f"    groups={[g['kind'] for g in manifest['groups']]}")
    print(f"    families={[fm['kind'] for fm in manifest['families']]}")
    print(f"    singles={[s['kind'] for s in manifest['singles']]}")

    # 3) index single -> 轻量舰船清单 (搜索用)
    print("[3] index single (lightweight ship list)")
    idx_single = next(s for s in manifest["singles"] if s["kind"] == "index")
    index = fetch_decoded(idx_single["path"], verify_sha=idx_single["sha256"])
    ships = index["ships"]
    print(f"    ship count = {len(ships)}")
    sample = ships[0]
    print(f"    sample keys = {sorted(sample.keys())}")

    # 4) 搜索（复现前端 substring OR 过滤）
    print(f"[4] search '{search_term}'")
    def label(s):
        return s.get("displayName") or s.get("name") or s["className"]
    hits = [s for s in ships if any(
        search_term in str(s.get(k, "")).lower()
        for k in ("displayName", "name", "className", "manufacturerName", "role", "career")
    )]
    for s in hits[:10]:
        print(f"    - {label(s):32s} className={s['className']:28s} "
              f"cat={s['category']} maker={s.get('manufacturerName')}")
    if not hits:
        print("    (no match)"); return
    target = hits[0]

    # 5) group index -> 定位完整舰船 blob
    print(f"[5] locate full ship blob for '{target['className']}'")
    grp_kind = "ships" if target["category"] == "AssembledShip" else "groundvehicles"
    group = next(g for g in manifest["groups"] if g["kind"] == grp_kind)
    grp_index = fetch_decoded(group["indexPath"], verify_sha=group["indexSha256"])
    blob = next(b for b in grp_index["blobs"] if b["id"] == target["className"])
    print(f"    blob path = {blob['path']}  bytes={blob['bytes']}")

    # 6) 完整舰船数据
    print("[6] full ship data")
    ship = fetch_decoded(blob["path"], verify_sha=blob["sha256"])
    print(f"    top-level keys = {sorted(ship.keys())}")
    veh = ship["vehicle"]
    print(f"    vehicle: {veh['vehicleDisplayName']} crew={veh['crewSize']} mass={veh['totalMass']}")
    print(f"    powerPools = {json.dumps(veh.get('powerPools'))[:200]}")
    print(f"    precomputed keys = {sorted(ship['precomputed'].keys())}")
    print(f"    slots (top) = {len(ship['slots'])}")

    # 7) 从 slots 树中提取默认装配的 武器 / 护盾 / 电源 类别
    print("[7] walk slot tree, collect component families from default items")
    cat_family = {
        "Weapon": "weapon", "AssembledWeapon": "weapon",
        "Shield": "shield", "PowerPlant": "powerplant",
        "Cooler": "cooler", "Radar": "radar", "QuantumDrive": "qdrive",
    }
    found = {}
    def walk(nodes):
        for nd in nodes or []:
            item = nd.get("item")
            if isinstance(item, dict):
                cat = item.get("category")
                fam = cat_family.get(cat)
                if fam:
                    found.setdefault(fam, []).append(item.get("className"))
            walk(nd.get("children"))
    walk(ship["slots"])
    for fam, items in found.items():
        print(f"    {fam:10s}: {len(items)} -> {items[:4]}")

    # 8) 组件族文件（可换装的完整组件属性，供 Power Management/DPS 计算）
    print("[8] component family files (swappable component stats)")
    for kind in ("weapons", "shields", "powerplants", "coolers"):
        fam = next((f for f in manifest["families"] if f["kind"] == kind), None)
        if not fam:
            continue
        arr = fetch_decoded(fam["path"], verify_sha=fam["sha256"])
        print(f"    {kind}: {len(arr)} entries; sample resource/shield keys:")
        s0 = arr[0]
        if kind == "shields":
            print(f"      shield.maxShieldHealth={s0['shield']['maxShieldHealth']} "
                  f"resource.states={[st['name'] for st in s0['resource']['states']]}")
        elif kind in ("powerplants", "coolers"):
            st = s0["resource"]["states"][0]
            print(f"      {s0['className']} state0 name={st['name']} "
                  f"powerRanges={st.get('powerRanges')}")
        elif kind == "weapons":
            print(f"      {s0['className']} fireActions="
                  f"{[fa.get('kind') for fa in s0['weapon']['fireActions']]}")


if __name__ == "__main__":
    main()
