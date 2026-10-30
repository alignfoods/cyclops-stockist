#!/usr/bin/env python3
"""Cyclops stockist data job.

Builds data/stockists.json from:
  * New World and PAK'nSAVE (Foodstuffs) public guest API: one decorateProducts
    request per store for every known Cyclops product ID.
  * A Google Sheet published as CSV (Woolworths, FreshChoice, independents).

Standard library only. Env vars:
  SHEET_CSV_URL       published-CSV URL (or a local file path) for the sheet. Optional.
  REQUEST_DELAY       seconds between Foodstuffs requests (default 2.5, clamped to 2..3).
  DATA_DIR            output folder (default: ../data next to this script).
  BANNERS             comma list to limit sources, e.g. "newworld" (default: newworld,paknsave).
  MAX_STORES          testing only: limit stores per banner.
"""
import csv, io, json, os, random, re, sys, time, urllib.error, urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR") or os.path.join(HERE, "..", "data")
CONFIG = json.load(open(os.path.join(HERE, "varieties.json"), encoding="utf-8"))
DELAY = min(3.0, max(2.0, float(os.environ.get("REQUEST_DELAY", "2.5"))))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/130.0 Safari/537.36 CyclopsStockistBot/1.0 (+https://cyclops.nz/stockists/)")
EMPTY_RUNS_BEFORE_DROP = 3
MAX_ERROR_RATIO = 0.3  # more store errors than this => treat whole banner as failed

BANNERS = {
    "newworld": {"label": "New World", "web": "https://www.newworld.co.nz",
                 "api": "https://api-prod.newworld.co.nz/v1/edge",
                 "product_url": "https://www.newworld.co.nz/shop/product/{pid}",
                 "search_store_hint": "Bishopdale"},
    "paknsave": {"label": "PAK'nSAVE", "web": "https://www.paknsave.co.nz",
                 "api": "https://api-prod.paknsave.co.nz/v1/edge",
                 "product_url": "https://www.paknsave.co.nz/shop/product/{pid}",
                 "search_store_hint": "Moorhouse"},
}

def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

def log(*a):
    print(*a, flush=True)

def load_json(name, default):
    p = os.path.join(DATA_DIR, name)
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default

def save_json(name, obj):
    os.makedirs(DATA_DIR, exist_ok=True)
    p = os.path.join(DATA_DIR, name)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, sort_keys=False)
        f.write("\n")
    os.replace(tmp, p)

def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())

def slug(s):
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", (s or "").lower())).strip("-")

def pid_url(pid):  # 5094108-EA-000 -> 5094108_ea_000nw style used by the site
    return pid.lower().replace("-", "_")

# ---------------------------------------------------------------- Foodstuffs
class Blocked(Exception):
    pass

class Foodstuffs:
    def __init__(self, cfg):
        self.cfg, self.token, self.token_at, self.requests = cfg, None, 0, 0

    def _http(self, method, url, body=None, auth=True):
        if self.requests:
            time.sleep(DELAY + random.uniform(0, 0.4))
        self.requests += 1
        h = {"User-Agent": UA, "Origin": self.cfg["web"], "Referer": self.cfg["web"] + "/",
             "Accept": "application/json"}
        if auth and self.token:
            h["Authorization"] = "Bearer " + self.token
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data, h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            snippet = e.read()[:200].decode("utf-8", "replace")
            if e.code in (403, 429, 503) or "Just a moment" in snippet:
                raise Blocked(f"HTTP {e.code} from {url}") from None
            raise RuntimeError(f"HTTP {e.code} from {url}: {snippet}") from None
        try:
            return json.loads(raw or b"null")
        except ValueError:
            raise Blocked(f"non-JSON response from {url} (bot challenge?)") from None

    def ensure_token(self):
        if self.token and time.time() - self.token_at < 20 * 60:
            return
        d = self._http("POST", self.cfg["web"] + "/api/user/get-current-user",
                       {"fingerprintUser": "cyclops-stockists", "fingerprintGuest": UA}, auth=False)
        tok = d.get("access_token") if isinstance(d, dict) else None
        if not tok:
            raise RuntimeError("no guest access_token returned")
        self.token, self.token_at = tok, time.time()

    def api(self, method, path, body=None):
        self.ensure_token()
        try:
            return self._http(method, self.cfg["api"] + path, body)
        except RuntimeError as e:
            if "HTTP 401" in str(e):
                self.token = None
                self.ensure_token()
                return self._http(method, self.cfg["api"] + path, body)
            raise

    def stores(self):
        d = self.api("GET", "/store")
        s = d.get("stores") if isinstance(d, dict) else d
        if not isinstance(s, list) or not s:
            raise RuntimeError("empty store list")
        return s

    def search(self, store_id, query):
        body = {
            "algoliaQuery": {"attributesToHighlight": [], "attributesToRetrieve": ["productID", "Type"],
                             "facets": ["brand"], "filters": f"stores:{store_id}", "hitsPerPage": 50,
                             "maxValuesPerFacet": 100, "page": 0, "query": query,
                             "analyticsTags": ["fs#WEB:desktop"]},
            "algoliaFacetQueries": [], "storeId": store_id, "hitsPerPage": 50, "page": 0,
            "sortOrder": "NI_POPULARITY_ASC", "tobaccoQuery": False,
            "precisionMedia": {"adDomain": "SEARCH_PAGE", "adPositions": [], "publishImpressionEvent": False,
                               "disableAds": True}}
        d = self.api("POST", "/search/paginated/products", body)
        return (d or {}).get("products") or []

    def decorate(self, store_id, ids):
        d = self.api("POST", f"/store/{store_id}/decorateProducts", {"productIds": ids})
        if not isinstance(d, dict) or "products" not in d:
            raise RuntimeError("unexpected decorateProducts response")
        return d["products"]

def is_brand(p):
    return norm(p.get("brand")) == norm(CONFIG["brand"]) or norm(p.get("name", "")).startswith(norm(CONFIG["brand"]))

def product_label(p):
    name = re.sub(r"^\s*" + re.escape(CONFIG["brand"]) + r"\s+", "", p.get("name") or "", flags=re.I)
    size = p.get("displayName") or ""
    return (name + " " + size).strip()

def run_banner(bkey, id_to_var, state, summary):
    cfg = BANNERS[bkey]
    fs = Foodstuffs(cfg)
    out, errors = {}, []
    stores = fs.stores()
    if os.environ.get("MAX_STORES"):
        stores = stores[: int(os.environ["MAX_STORES"])]
    summary["banners"][bkey] = {"stores_checked": 0, "stores_with_cyclops": 0, "lines": 0, "errors": 0}

    # Discovery: search "cyclops" at one store to pick up new product IDs.
    hint = next((s for s in stores if cfg["search_store_hint"].lower() in s.get("name", "").lower()), stores[0])
    try:
        found = [p for p in fs.search(hint["id"], CONFIG["search_term"]) if is_brand(p)]
        for p in found:
            pid = p.get("productId")
            if pid and pid not in id_to_var:
                key = "auto-" + slug(product_label(p) or pid)
                state["discovered"][pid] = {"key": key, "label": product_label(p), "first_seen": now_iso(),
                                            "found_at": hint.get("name")}
                id_to_var[pid] = key
                summary["new_product_ids"].append({"id": pid, "label": product_label(p), "banner": cfg["label"]})
    except (Blocked, RuntimeError) as e:
        summary["warnings"].append(f"{cfg['label']}: discovery search failed ({e})")

    ids = sorted(id_to_var)
    for s in stores:
        sid = s.get("id")
        if not sid:
            continue
        summary["banners"][bkey]["stores_checked"] += 1
        try:
            prods = fs.decorate(sid, ids)
        except Blocked as e:
            errors.append(sid)
            summary["warnings"].append(f"{cfg['label']}: blocked at {s.get('name')} ({e}); stopping banner")
            # Treat as banner failure: stop hammering a site that is challenging us.
            raise
        except (RuntimeError, OSError) as e:
            errors.append(sid)
            summary["banners"][bkey]["errors"] += 1
            out[f"{bkey}-{sid}"] = {"_error": str(e)}
            continue
        vars_here, lines = set(), []
        for p in prods:
            pid = p.get("productId")
            if not pid or not p.get("name"):
                continue  # null entry = not ranged at this store
            key = id_to_var.get(pid)
            if not key:
                continue
            vars_here.add(key)
            lines.append(pid)
            # Learn labels for auto/watch IDs.
            if pid in state["discovered"] and not state["discovered"][pid].get("label"):
                state["discovered"][pid]["label"] = product_label(p)
        rec = {
            "id": f"{bkey}-{sid}", "name": s.get("name", "").strip(), "banner": cfg["label"],
            "address": (s.get("address") or "").strip(), "phone": (s.get("localPhone") or "").strip(), "lat": s.get("latitude"), "lng": s.get("longitude"),
            "varieties": sorted(vars_here), "product_ids": sorted(lines), "source": bkey,
            "url": cfg["web"],
        }
        out[rec["id"]] = rec
        if vars_here:
            summary["banners"][bkey]["stores_with_cyclops"] += 1
            summary["banners"][bkey]["lines"] += len(lines)
    if stores and len(errors) / len(stores) > MAX_ERROR_RATIO:
        raise RuntimeError(f"{len(errors)} of {len(stores)} store requests failed")
    summary["banners"][bkey]["requests"] = fs.requests
    return out

# ---------------------------------------------------------------- Google Sheet
TRUTHY = {"y", "yes", "x", "1", "true", "✓", "✔", "tick"}

def read_sheet(url, label_to_key):
    if re.match(r"^https?://", url):
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=40) as r:
            text = r.read().decode("utf-8-sig")
        if text.lstrip().startswith("<"):
            raise RuntimeError("sheet URL returned HTML, not CSV (is it published as CSV?)")
    else:
        with open(url, encoding="utf-8-sig") as f:
            text = f.read()
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise RuntimeError("sheet is empty")
    header = [norm(h) for h in rows[0]]
    def col(*names):
        for n in names:
            if norm(n) in header:
                return header.index(norm(n))
        return None
    ci = {"name": col("store name", "name", "store"), "banner": col("banner", "chain"),
          "address": col("address"), "phone": col("phone"), "lat": col("lat", "latitude"), "lng": col("lng", "lon", "long", "longitude")}
    if ci["name"] is None or ci["lat"] is None or ci["lng"] is None:
        raise RuntimeError("sheet needs 'store name', 'lat' and 'lng' columns")
    var_cols = {i: label_to_key[h] for i, h in enumerate(header) if h in label_to_key}
    unknown = [rows[0][i] for i, h in enumerate(header) if h and h not in label_to_key
               and i not in ci.values() and h not in ("notes", "note")]
    out, bad = {}, 0
    for r in rows[1:]:
        r = r + [""] * (len(header) - len(r))
        name = r[ci["name"]].strip()
        if not name:
            continue
        try:
            lat, lng = float(r[ci["lat"]]), float(r[ci["lng"]])
        except ValueError:
            bad += 1
            continue
        vs = sorted({k for i, k in var_cols.items() if r[i].strip().lower() in TRUTHY})
        banner = r[ci["banner"]].strip() if ci["banner"] is not None else ""
        sid = "sheet-" + slug(banner + " " + name)
        n = 2
        while sid in out:
            sid = "sheet-" + slug(banner + " " + name) + f"-{n}"; n += 1
        out[sid] = {"id": sid, "name": name, "banner": banner or "Other",
                    "address": r[ci["address"]].strip() if ci["address"] is not None else "",
                    "phone": r[ci["phone"]].strip() if ci["phone"] is not None else "",
                    "lat": lat, "lng": lng, "varieties": vs, "source": "sheet",
                    "range_listed": bool(vs)}
    return out, {"rows": len(out), "rows_with_varieties": sum(1 for s in out.values() if s["varieties"]),
                 "bad_rows": bad, "unknown_columns": unknown, "variety_columns": len(var_cols)}

# ---------------------------------------------------------------- main
def main():
    started = time.time()
    prev = load_json("stockists.json", {"stores": []})
    prev_by_id = {s["id"]: s for s in prev.get("stores", [])}
    state = load_json("state.json", {})
    state.setdefault("empty_runs", {})
    state.setdefault("discovered", {})
    state.setdefault("last_success", {})
    summary = {"started_at": now_iso(), "banners": {}, "sources": {}, "warnings": [], "new_product_ids": [],
               "dropped_stores": [], "pending_removal": [], "added_stores": [], "variety_changes": 0}

    varieties = [dict(v) for v in CONFIG["varieties"]]
    id_to_var = {pid: v["key"] for v in varieties for pid in v.get("foodstuffs_ids", [])}
    for pid in CONFIG.get("watch_ids", []):
        if pid not in id_to_var:
            state["discovered"].setdefault(pid, {"key": "auto-" + slug(pid), "label": "", "first_seen": now_iso(),
                                                 "found_at": "watch list"})
    for pid, d in state["discovered"].items():
        id_to_var.setdefault(pid, d["key"])

    new_stores = {}
    banners = [b.strip() for b in os.environ.get("BANNERS", "newworld,paknsave").split(",") if b.strip()]
    for bkey in banners:
        try:
            got = run_banner(bkey, id_to_var, state, summary)
            summary["sources"][bkey] = "ok"
            state["last_success"][bkey] = now_iso()
        except Exception as e:  # source failed: keep last good data for this banner
            log(f"!! {bkey} failed: {e}")
            summary["sources"][bkey] = f"failed: {e}"
            for sid, s in prev_by_id.items():
                if s.get("source") == bkey:
                    new_stores[sid] = s
            continue
        for sid, rec in got.items():
            old = prev_by_id.get(sid)
            if "_error" in rec:  # single store error: keep previous record
                if old:
                    new_stores[sid] = old
                continue
            if rec["varieties"]:
                state["empty_runs"].pop(sid, None)
                rec["last_seen"] = now_iso()
                new_stores[sid] = rec
                if not old:
                    summary["added_stores"].append(rec["name"])
                elif set(old.get("varieties", [])) != set(rec["varieties"]):
                    summary["variety_changes"] += 1
            elif old:  # was listed, now empty: count before dropping
                n = state["empty_runs"].get(sid, 0) + 1
                if n >= EMPTY_RUNS_BEFORE_DROP:
                    state["empty_runs"].pop(sid, None)
                    summary["dropped_stores"].append(old["name"])
                else:
                    state["empty_runs"][sid] = n
                    keep = dict(old); keep["empty_runs"] = n
                    new_stores[sid] = keep
                    summary["pending_removal"].append(f"{old['name']} ({n}/{EMPTY_RUNS_BEFORE_DROP})")

    # Varieties list for the front end (configured + discovered).
    known_keys = {v["key"] for v in varieties}
    for pid, d in state["discovered"].items():
        if d["key"] not in known_keys and any(d["key"] in s.get("varieties", []) for s in new_stores.values()):
            varieties.append({"key": d["key"], "label": d.get("label") or pid, "group": "New",
                              "foodstuffs_ids": [pid]})
            known_keys.add(d["key"])

    label_to_key = {}
    for v in varieties:
        label_to_key[norm(v["label"])] = v["key"]
        label_to_key[norm(v["key"])] = v["key"]

    sheet_url = os.environ.get("SHEET_CSV_URL", "").strip()
    seed = os.path.join(HERE, "..", "sheet", "seed_sheet.csv")
    using_seed = False
    if not sheet_url and os.path.exists(seed):
        # No published sheet yet: fall back to the seed sheet committed in the repo.
        sheet_url, using_seed = seed, True
        log("SHEET_CSV_URL not set; using sheet/seed_sheet.csv")
    if sheet_url:
        try:
            got, info = read_sheet(sheet_url, label_to_key)
            if not got:
                raise RuntimeError("no valid rows")
            summary["sources"]["sheet"] = "ok (sheet/seed_sheet.csv; SHEET_CSV_URL not set)" if using_seed else "ok"
            summary["sheet"] = info
            state["last_success"]["sheet"] = now_iso()
            new_stores.update(got)  # sheet is authoritative: Clare deletes rows to remove stores
        except Exception as e:
            log(f"!! sheet failed: {e}")
            summary["sources"]["sheet"] = f"failed: {e}"
            new_stores.update({k: s for k, s in prev_by_id.items() if s.get("source") == "sheet"})
    else:
        summary["sources"]["sheet"] = "not configured (SHEET_CSV_URL unset); kept previous sheet rows"
        new_stores.update({k: s for k, s in prev_by_id.items() if s.get("source") == "sheet"})

    if not new_stores and prev_by_id:
        summary["warnings"].append("all sources empty: kept previous stockists.json unchanged")
        new_stores = prev_by_id

    stores = sorted(new_stores.values(), key=lambda s: (s.get("banner", ""), s.get("name", "")))
    for s in stores:
        s.pop("_error", None)
    used = {k for s in stores for k in s.get("varieties", [])}
    output = {
        "generated_at": now_iso(), "brand": CONFIG["brand"],
        "varieties": [{"key": v["key"], "label": v["label"], "group": v.get("group", "")}
                      for v in varieties],
        "banners": sorted({s.get("banner", "") for s in stores}),
        "stores": stores,
        "sources": {k: {"status": v, "last_success": state["last_success"].get(k)}
                    for k, v in summary["sources"].items()},
    }
    save_json("stockists.json", output)
    save_json("state.json", state)

    summary.update({"finished_at": now_iso(), "duration_s": round(time.time() - started),
                    "stores_total": len(stores),
                    "stores_by_banner": {b: sum(1 for s in stores if s.get("banner") == b) for b in output["banners"]},
                    "varieties_in_use": len(used)})
    save_json("run_summary.json", summary)
    md = render_md(summary)
    with open(os.path.join(DATA_DIR, "run_summary.md"), "w", encoding="utf-8") as f:
        f.write(md)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(md)
    log(md)
    failed = [k for k, v in summary["sources"].items() if v.startswith("failed")]
    return 1 if failed and os.environ.get("FAIL_ON_SOURCE_ERROR", "1") == "1" else 0

def render_md(s):
    L = [f"## Cyclops stockists run {s['started_at']}", "",
         f"- Stores on map: **{s['stores_total']}** ({', '.join(f'{k} {v}' for k, v in s['stores_by_banner'].items())})",
         f"- Duration: {s['duration_s']} s"]
    for k, v in s["sources"].items():
        L.append(f"- Source `{k}`: {v}")
    for k, b in s["banners"].items():
        L.append(f"- {k}: {b.get('stores_checked')} stores checked, {b.get('stores_with_cyclops')} with Cyclops, "
                 f"{b.get('lines')} lines, {b.get('errors')} errors, {b.get('requests', '?')} requests")
    if s.get("sheet"):
        sh = s["sheet"]
        L.append(f"- Sheet: {sh['rows']} rows, {sh['rows_with_varieties']} with varieties ticked, "
                 f"{sh['bad_rows']} rows skipped (bad lat/lng)"
                 + (f", unrecognised columns: {', '.join(sh['unknown_columns'])}" if sh["unknown_columns"] else ""))
    for title, key in [("New stores", "added_stores"), ("Pending removal", "pending_removal"),
                       ("Dropped stores", "dropped_stores"), ("Warnings", "warnings")]:
        if s[key]:
            L.append(f"- {title} ({len(s[key])}): " + "; ".join(map(str, s[key][:40])))
    if s["new_product_ids"]:
        L.append("- **New Cyclops product IDs found** (add to job/varieties.json): " +
                 "; ".join(f"{p['id']} {p['label']}" for p in s["new_product_ids"]))
    L.append(f"- Stores whose variety list changed: {s['variety_changes']}")
    return "\n".join(L) + "\n"

if __name__ == "__main__":
    sys.exit(main())
