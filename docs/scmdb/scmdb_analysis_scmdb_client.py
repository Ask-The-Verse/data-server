#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scmdb_client.py
================
A self-contained example client that reproduces the data-access and core
computation logic of https://scmdb.net/ (SCMDB — Star Citizen Missions,
Crafting & Mining Database).

The website is a static React/Vite single-page application. It has **no private
backend API**: every piece of data is a static JSON file served from
``https://scmdb.net/data/``. This script shows how to:

  1. Discover the available game builds (versions) and pick the LIVE / PTU one.
  2. Download & cache all data files for a version.
  3. Reproduce the mission (task board) search and detail resolution.
  4. Reproduce the crafting (fab) blueprint lookup.
  5. Reproduce the mining (mine) resource model AND the mineral-quality maths
     (truncated-normal distribution + per-band probabilities), which is a
     faithful port of the minified JS functions Px / yl / r1 / b1 / qx.

Only the Python standard library is required.

Run:  python3 scmdb_client.py
"""

from __future__ import annotations

import json
import math
import os
import time
import unicodedata
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

BASE = "https://scmdb.net"
DATA_BASE = f"{BASE}/data"
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".scmdb_cache")

# ---------------------------------------------------------------------------
# 0. Low-level fetch + on-disk cache
# ---------------------------------------------------------------------------

def _http_get(url: str) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            # scmdb.net sits behind Cloudflare; a normal browser UA is enough.
            "User-Agent": "Mozilla/5.0 (compatible; scmdb-client/1.0)",
            "Accept": "application/json,*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def fetch_json(path: str, cache: bool = True, bust: bool = False) -> Any:
    """Fetch a JSON file from /data (or an absolute URL) with optional caching.

    `path` may be a bare file name ("game-versions.json") which is resolved
    under DATA_BASE, or a full "https://..." URL.
    """
    if path.startswith("http"):
        url = path
        fname = path.rsplit("/", 1)[-1].split("?")[0]
    else:
        url = f"{DATA_BASE}/{path}"
        fname = path
    # The site appends ?t=<ts> to bypass CDN caches for a few "live" files.
    if bust:
        url = f"{url}?t={int(time.time()*1000)}"

    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(CACHE_DIR, fname)
    if cache and not bust and os.path.exists(cache_path):
        with open(cache_path, "r", encoding="utf-8") as f:
            return json.load(f)

    raw = _http_get(url)
    text = raw.decode("utf-8")
    data = json.loads(text)  # raises if the SPA returned index.html (optional files)
    if cache:
        with open(cache_path, "w", encoding="utf-8") as f:
            f.write(text)
    return data


# ---------------------------------------------------------------------------
# 1. Version / channel discovery
#    JS: EM4/EM() reads /data/game-versions.json; pN() classifies the channel.
# ---------------------------------------------------------------------------

def channel_of(version: str) -> str:
    """Port of JS `pN`: a build is PTU if its version contains '-ptu.'/'-ptu-'."""
    if version and ("-ptu." in version or "-ptu-" in version):
        return "ptu"
    return "live"


def list_versions() -> List[Dict[str, str]]:
    """Return the version index: [{'version':..., 'file':...}, ...] (newest first)."""
    return fetch_json("game-versions.json", bust=True)


def pick_version(channel: str = "live") -> Dict[str, str]:
    """Pick the newest build for the requested channel ('live' or 'ptu').

    game-versions.json is ordered newest-first, so the first entry whose
    version matches the channel is the active build (same logic the SPA uses
    when you toggle ?channel=ptu).
    """
    for entry in list_versions():
        if channel_of(entry["version"]) == channel:
            return entry
    raise RuntimeError(f"No {channel} build found in game-versions.json")


# ---------------------------------------------------------------------------
# 2. The SCMDB dataset (one object per version) -> all pages read from these.
# ---------------------------------------------------------------------------

class ScmdbData:
    """Loads & holds every data file for a single game build.

    File-name pattern (all under /data/):
        merged-<version>.json              -> missions / contracts + shared pools
        crafting_blueprints-<version>.json -> fab: recipes
        crafting_items-<version>.json      -> fab: item stats
        mining_data-<version>.json         -> mine: elements/compositions/locations
        mining_equipment-<version>.json    -> mine: lasers/modules/gadgets/params
    Optional overlays (may 404 -> served as the SPA's index.html):
        deltas-<version>.json, overrides-<version>.json,
        cig_data_issues-<version>.json, mission-history-<version>.json
    """

    def __init__(self, channel: str = "live"):
        self.channel = channel
        self.version_entry = pick_version(channel)
        self.version = self.version_entry["version"]
        v = self.version

        # merged.json's `file` field is authoritative; fall back to convention.
        merged_file = self.version_entry.get("file") or f"merged-{v}.json"
        self.merged = fetch_json(merged_file)
        self.crafting_bp = fetch_json(f"crafting_blueprints-{v}.json")
        self.crafting_items = fetch_json(f"crafting_items-{v}.json")
        self.mining = fetch_json(f"mining_data-{v}.json")
        self.mining_equipment = fetch_json(f"mining_equipment-{v}.json")

        # Convenience shortcuts into the merged model.
        self.contracts: List[dict] = self.merged.get("contracts", [])
        self.location_pools: Dict[str, dict] = self.merged.get("locationPools", {})
        self.resource_pools: Dict[str, dict] = self.merged.get("resourcePools", {})
        self.factions: Dict[str, dict] = self.merged.get("factions", {})
        self.scopes: Dict[str, dict] = self.merged.get("scopes", {})
        self.blueprint_pools: Dict[str, dict] = self.merged.get("blueprintPools", {})
        self.faction_rewards_pools: List[list] = self.merged.get("factionRewardsPools", [])

    # -- 2a. missions / task board ------------------------------------------

    def resolve_location(self, short_id: str) -> Optional[dict]:
        return self.location_pools.get(short_id)

    def resolve_faction(self, guid: str) -> Optional[dict]:
        return self.factions.get(guid)

    def contract_detail(self, contract: dict) -> dict:
        """Resolve a contract's id references into human-readable detail,
        mirroring what the mission detail panel renders."""
        faction = self.resolve_faction(contract.get("factionGuid", "")) or {}

        locs = [self.resolve_location(x) for x in contract.get("locations", [])]
        dests = [self.resolve_location(x) for x in contract.get("destinations", [])]

        # Reputation rewards: factionRewardsIndex -> factionRewardsPools[idx]
        rep_rewards = []
        idx = contract.get("factionRewardsIndex")
        if idx is not None and 0 <= idx < len(self.faction_rewards_pools):
            for r in self.faction_rewards_pools[idx]:
                fac = self.resolve_faction(r.get("factionGuid", "")) or {}
                scope = self.scopes.get(r.get("scopeGuid", "")) or {}
                rep_rewards.append({
                    "faction": fac.get("name"),
                    "scope": scope.get("displayName") or scope.get("scopeName"),
                    "amount": r.get("amount"),
                })

        # Blueprint (crafting recipe) rewards -> resolve pool -> blueprint names
        bp_rewards = []
        for br in contract.get("blueprintRewards", []) or []:
            pool = self.blueprint_pools.get(br.get("blueprintPool", "")) or {}
            bp_rewards.append({
                "poolName": br.get("poolName") or pool.get("name"),
                "chance": br.get("chance"),
                "trigger": br.get("trigger"),
                "blueprints": [b.get("name") for b in pool.get("blueprints", [])],
            })

        return {
            "id": contract.get("id"),
            "title": contract.get("title"),
            "description": contract.get("description"),
            "category": contract.get("category"),
            "missionType": contract.get("missionType"),
            "faction": faction.get("name"),
            "illegal": contract.get("illegal"),
            "rewardUEC": contract.get("rewardUEC"),
            "buyIn": contract.get("buyIn"),
            "timeToComplete": contract.get("timeToComplete"),
            "locations": [l.get("name") for l in locs if l],
            "destinations": [d.get("name") for d in dests if d],
            "reputationRewards": rep_rewards,
            "blueprintRewards": bp_rewards,
        }

    def search_missions(self, query: str, limit: int = 20) -> List[dict]:
        """Port of the JS mission search (`yN`): case-insensitive, NFC-normalised,
        whitespace-tokenised AND search over a big concatenated text blob built
        from title/description/debugName/faction and resolved reward names."""
        tokens = _norm(query).split()
        if not tokens:
            return self.contracts[:limit]

        out = []
        for c in self.contracts:
            faction = self.resolve_faction(c.get("factionGuid", "")) or {}
            flags = " ".join([
                "shared shareable" if c.get("canBeShared") else "solo",
                "illegal" if c.get("illegal") else "legal",
                c.get("missionType", ""),
            ])
            blob = _norm(" ".join(str(x) for x in [
                c.get("title", ""),
                c.get("description", ""),
                c.get("debugName", ""),
                " ".join(c.get("debugNames", []) or []),
                faction.get("name", ""),
                flags,
            ]))
            if all(t in blob for t in tokens):
                out.append(c)
                if len(out) >= limit:
                    break
        return out

    # -- 2b. crafting / fab -------------------------------------------------

    def blueprints(self) -> List[dict]:
        return self.crafting_bp.get("blueprints", [])

    def find_blueprint(self, product_name: str) -> Optional[dict]:
        pn = product_name.strip().lower()
        for bp in self.blueprints():
            if (bp.get("productName") or "").lower() == pn:
                return bp
        # fall back to substring match
        for bp in self.blueprints():
            if pn in (bp.get("productName") or "").lower():
                return bp
        return None

    # -- 2c. mining / mine --------------------------------------------------

    def mineable_elements(self) -> Dict[str, dict]:
        return self.mining.get("mineableElements", {})

    def compositions(self) -> Dict[str, dict]:
        return self.mining.get("compositions", {})

    def mining_locations(self) -> List[dict]:
        return self.mining.get("locations", [])

    def element_by_name(self, name: str) -> Optional[Tuple[str, dict]]:
        for guid, el in self.mineable_elements().items():
            if el.get("name", "").lower() == name.lower():
                return guid, el
        return None


# ---------------------------------------------------------------------------
# 3. Text normalisation used by the search (JS: str.normalize('NFC').toLowerCase())
# ---------------------------------------------------------------------------

def _norm(s: str) -> str:
    return unicodedata.normalize("NFC", s or "").lower()


# ===========================================================================
# 4. MINERAL QUALITY MATHS  (faithful port of the minified JS)
# ===========================================================================
# In-game mining quality is a value on a 0..1000 scale. Its statistical
# distribution for a given deposit element is a *truncated normal* whose
# parameters come from `qualityDistribution` (scaled by the composition part's
# `qualityScale`). The functions below reproduce SCMDB exactly.

# JS group name -> qualityDistribution key (`Bx`)
GROUP_TO_METHOD = {
    "SpaceShip_Mineables": "shipmineables",
    "SpaceShip_Mineables_Rare": "shipmineables",
    "FPS_Mineables": "fpsmineables",
    "GroundVehicle_Mineables": "groundmineables",
    "Harvestables": "harvestables",
}
PYRO = "pyro"


def normal_pdf(x: float, mu: float, sigma: float) -> float:
    """JS `Px`: standard normal probability density."""
    return 1.0 / (sigma * math.sqrt(2 * math.pi)) * math.exp(-0.5 * ((x - mu) / sigma) ** 2)


def normal_cdf(x: float, mu: float, sigma: float) -> float:
    """JS `yl`: Abramowitz & Stegun 26.2.17 normal-CDF approximation.
    (SCMDB uses this exact rational approximation, not math.erf.)"""
    l = (x - mu) / sigma
    c = 1.0 / (1.0 + 0.2316419 * abs(l))
    d = (0.3989422804014327 * math.exp(-0.5 * l * l) * c *
         (0.3193815 + c * (-0.3565638 + c * (1.781478 + c * (-1.821256 + c * 1.3302744)))))
    return 1.0 - d if l > 0 else d


def band_probabilities(dist: Dict[str, float], boundaries: List[float]) -> List[float]:
    """JS `r1`: probability mass of the truncated-normal distribution inside
    each quality band defined by `boundaries` (e.g. [0,400,600,700,800,900,950,999]).

    Returns one probability per boundary; each band i spans
    [boundaries[i], boundaries[i+1]) with the last band ending at 1000.
    """
    if not boundaries or not dist.get("stddev") or dist["max"] <= dist["min"]:
        return []
    lo, hi, mean, sd = dist["min"], dist["max"], dist["mean"], dist["stddev"]
    base = normal_cdf(lo, mean, sd)
    total = normal_cdf(hi, mean, sd) - base
    if total <= 0:
        return []
    probs = []
    for i, p in enumerate(boundaries):
        v = boundaries[i + 1] if i < len(boundaries) - 1 else 1000
        x = normal_cdf(max(lo, p), mean, sd)
        j = normal_cdf(min(hi, v), mean, sd)
        probs.append(max(0.0, (j - x) / total))
    return probs


def _resolve_distribution(quality_dist: dict, method: str, location_name: str,
                          element: Optional[dict], location_system: Optional[str]) -> Optional[dict]:
    """JS `b1`: pick the right base distribution {min,max,mean,stddev} taking
    into account mining method, element rarity (ship mining), and location /
    Pyro overrides."""
    if not quality_dist or not method:
        return None
    node = quality_dist.get(method)
    if not node:
        return None
    is_pyro = location_system == "Pyro"

    if method == "shipmineables":
        rarity = (element or {}).get("rarity") or "common"
        m = node.get(rarity)
        if not m:
            return (node.get("common") or {}).get("default")
        overrides = m.get("locationOverrides")
        if overrides:
            loc_lower = (location_name or "").lower()
            # named (non-pyro) location match
            for key, arr in overrides.items():
                if key != "pyro" and loc_lower and key in loc_lower:
                    for x in arr:
                        locs = [j.lower() for j in x.get("locations", [])]
                        if (location_system and location_system.lower() in locs) or \
                           (is_pyro and PYRO in locs):
                            return x["distribution"]
                    break
            # explicit system match
            if location_system:
                for key, arr in overrides.items():
                    if key != "pyro":
                        for x in arr:
                            if location_system.lower() in [j.lower() for j in x.get("locations", [])]:
                                return x["distribution"]
            # pyro fallback
            if is_pyro and overrides.get("pyro"):
                for y in overrides["pyro"]:
                    if PYRO in [j.lower() for j in y.get("locations", [])]:
                        return y["distribution"]
        return m.get("default")

    if method == "fpsmineables" and element and node.get("elementOverrides"):
        name_lower = (element.get("name") or "").lower()
        for key, val in node["elementOverrides"].items():
            if key in name_lower:
                return val

    if is_pyro and node.get("locationOverrides", {}).get("pyro"):
        for h in node["locationOverrides"]["pyro"]:
            if PYRO in [j.lower() for j in h.get("locations", [])]:
                return h["distribution"]
    return node.get("default")


def quality_range(quality_scale: float, quality_dist: dict, method: str,
                  location_name: str = "", element: Optional[dict] = None,
                  location_system: Optional[str] = None) -> Optional[dict]:
    """JS `qx`: final quality result for a composition part.
    Scales the base distribution by `quality_scale` (0..1)."""
    if quality_scale is None:
        return None
    base = _resolve_distribution(quality_dist, method, location_name, element, location_system)
    if base:
        lo = round(base["min"] * quality_scale)
        hi = round(base["max"] * quality_scale)
        return {
            "label": f"{lo}-{hi}",
            "min": lo, "max": hi,
            "mean": round((base.get("mean") or 0) * quality_scale),
            "stddev": max(1.0, (base.get("stddev") or 50) * quality_scale),
            "scale": quality_scale,
            "base": {"min": base["min"], "max": base["max"]},
        }
    # No distribution -> only an upper bound is known.
    return {"label": f"<={round(quality_scale * 1000)}", "max": round(quality_scale * 1000)}


# ===========================================================================
# 5. Demonstration
# ===========================================================================

def demo() -> None:
    print("=" * 70)
    print("Loading SCMDB dataset (LIVE channel)…")
    data = ScmdbData(channel="live")
    print(f"  version : {data.version}  (channel={data.channel})")
    print(f"  missions: {len(data.contracts)}")
    print(f"  recipes : {len(data.blueprints())}")
    print(f"  elements: {len(data.mineable_elements())}")

    # --- Missions -------------------------------------------------------
    print("\n" + "=" * 70)
    print("MISSION SEARCH  query='delivery'  (task board)")
    hits = data.search_missions("delivery", limit=3)
    for c in hits:
        d = data.contract_detail(c)
        print(f"  • {d['title']}  [{d['missionType']}]  faction={d['faction']}  "
              f"UEC={d['rewardUEC']}  illegal={d['illegal']}")
    if hits:
        print("\nMISSION DETAIL (first hit, ids resolved):")
        print(json.dumps(data.contract_detail(hits[0]), ensure_ascii=False, indent=2)[:900])

    # --- Crafting -------------------------------------------------------
    print("\n" + "=" * 70)
    print("CRAFTING BLUEPRINT lookup")
    bp = data.blueprints()[0]
    print(f"  product : {bp['productName']}  (gear={bp.get('gear')})")
    for tier in bp.get("tiers", []):
        print(f"    tier craftTime={tier.get('craftTimeSeconds')}s, slots:")
        for slot in tier.get("slots", []):
            opts = ", ".join(
                f"{o.get('resourceName') or o.get('itemName')} x{o.get('quantity')} "
                f"(minQ={o.get('minQuality')})" for o in slot.get("options", []))
            print(f"      - {slot['name']}: {opts}")

    # --- Mining quality -------------------------------------------------
    print("\n" + "=" * 70)
    print("MINING QUALITY CALCULATION")
    qd = data.mining.get("qualityDistribution", {})
    boundaries = data.mining.get("qualityBandBoundaries", [])
    print(f"  qualityBandBoundaries = {boundaries}")

    found = data.element_by_name("Agricium (Ore)")
    if found:
        guid, el = found
        print(f"\n  Element: {el['name']}  rarity={el.get('rarity')}")
        # ship mining, qualityScale = 1.0 (pure high-quality vein), Stanton
        qr = quality_range(1.0, qd, "shipmineables", location_name="",
                           element=el, location_system="Stanton")
        print(f"  Ship-mining quality distribution (scale 1.0): {qr}")
        probs = band_probabilities(qr, boundaries)
        print("  Probability of landing in each quality band:")
        for i, p in enumerate(probs):
            lo = boundaries[i]
            hi = boundaries[i + 1] if i + 1 < len(boundaries) else 1000
            print(f"    band {i+1} [{lo:>4}-{hi:<4}) : {p*100:5.1f}%")
        print(f"  per-element crafting qualityBands: {el.get('qualityBands')}")

    print("\nDone. Raw files are cached under .scmdb_cache/")


if __name__ == "__main__":
    demo()
