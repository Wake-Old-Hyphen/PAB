import os
import re
import io
import json
import yaml
import copy
import glob
import stat
import shutil
import tarfile
import zipfile
import hashlib
import subprocess
import sys
import time
import calendar
import xml.etree.ElementTree as ET
from urllib.parse import quote_plus, urljoin

import requests

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except Exception:
    HAS_BS4 = False

try:
    from curl_cffi import requests as cffi_requests
    HAS_CURL_CFFI = True
except Exception:
    HAS_CURL_CFFI = False

MAX_ATTEMPTS = 5
PINNED = {"stable": "", "nightly": "", "beta": ""}
CHANNEL_PKG = {
    "stable": "com.brave.browser",
    "beta": "com.brave.browser_beta",
    "nightly": "com.brave.browser_nightly",
}
BUNDLES = []
ABI_QUALS = {"arm64_v8a", "armeabi_v7a", "armeabi", "x86", "x86_64", "mips"}
DPI_QUALS = {"ldpi", "mdpi", "hdpi", "xhdpi", "xxhdpi", "xxxhdpi", "nodpi", "anydpi", "tvdpi"}
UA = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
RAW_CACHE = {}
BASE_CACHE = {}
DETECTED_VERSIONS = {}
RESOLUTION = []
BUILT = []
SKIPPED = []
DECLARED_CACHE = {}
TAGS_CACHE = {}
UPLOAD_CACHE = {}
PROBE_CACHE = {}
SCHEMA = 2
CACHE_SCHEMA = 1
CACHE_FILE = ".pab-cache.json"

ANDROID_NS = "http://schemas.android.com/apk/res/android"
ET.register_namespace("android", ANDROID_NS)
ET.register_namespace("tools", "http://schemas.android.com/tools")

FGS_PERM_TO_TYPE = {
    "android.permission.FOREGROUND_SERVICE_MICROPHONE": "microphone",
    "android.permission.FOREGROUND_SERVICE_CAMERA": "camera",
    "android.permission.FOREGROUND_SERVICE_LOCATION": "location",
    "android.permission.FOREGROUND_SERVICE_MEDIA_PLAYBACK": "mediaPlayback",
    "android.permission.FOREGROUND_SERVICE_MEDIA_PROJECTION": "mediaProjection",
    "android.permission.FOREGROUND_SERVICE_PHONE_CALL": "phoneCall",
    "android.permission.FOREGROUND_SERVICE_CONNECTED_DEVICE": "connectedDevice",
    "android.permission.FOREGROUND_SERVICE_DATA_SYNC": "dataSync",
    "android.permission.FOREGROUND_SERVICE_HEALTH": "health",
    "android.permission.FOREGROUND_SERVICE_REMOTE_MESSAGING": "remoteMessaging",
    "android.permission.FOREGROUND_SERVICE_SYSTEM_EXEMPTED": "systemExempted",
    "android.permission.FOREGROUND_SERVICE_SPECIAL_USE": "specialUse",
    "android.permission.RECORD_AUDIO": "microphone",
    "android.permission.CAMERA": "camera",
    "android.permission.ACCESS_FINE_LOCATION": "location",
    "android.permission.ACCESS_COARSE_LOCATION": "location",
    "android.permission.ACCESS_BACKGROUND_LOCATION": "location",
}

# ============================================================
# CACHE
# ============================================================
def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def compute_config_hash(variant):
    h = hashlib.sha256()
    h.update(json.dumps({
        "id": variant.get("id"),
        "clone_package": variant.get("clone_package"),
        "app_name": variant.get("app_name"),
        "exclude_patches": sorted(variant.get("exclude_patches") or []),
        "options": variant.get("options") or {},
        "bundles": [{"label": b.get("label"), "patches": b.get("patches"), "options": b.get("options"), "pin": b.get("pin")} for b in variant.get("bundles", [])],
        "profile": variant.get("_profile"),
        "apk_version": variant.get("apk_version"),
        "_policy": variant.get("_policy"),
    }, sort_keys=True, separators=(',', ':')).encode())
    return h.hexdigest()

def load_cache():
    if not os.path.exists(CACHE_FILE):
        return {"schema": CACHE_SCHEMA, "variants": {}}
    try:
        with open(CACHE_FILE) as f:
            c = json.load(f)
            if c.get("schema") != CACHE_SCHEMA:
                return {"schema": CACHE_SCHEMA, "variants": {}}
            return c
    except Exception:
        return {"schema": CACHE_SCHEMA, "variants": {}}

def save_cache(cache):
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)

def should_skip_variant(variant_id, cache, current, max_age_days=0):
    if not cache or variant_id not in cache.get("variants", {}):
        return False, "no cache entry"
    entry = cache["variants"][variant_id]
    if entry.get("status") == "failed":
        return False, "last build failed (auto-retry)"
    if entry.get("resolved") != current.get("resolved"):
        return False, f"version changed: {entry.get('resolved')} -> {current.get('resolved')}"
    if str(current.get("resolved")) == "latest" and max_age_days:
        try:
            bt = time.strptime(str(entry.get("built_at", ""))[:19], "%Y-%m-%dT%H:%M:%S")
            age = (time.time() - calendar.timegm(bt)) / 86400
            if age >= max_age_days:
                return False, f"refresh interval reached ({max_age_days}d)"
        except Exception:
            return False, "no valid built_at (refresh)"
    if entry.get("config_hash") != current.get("config_hash"):
        return False, "config changed"
    if entry.get("bundles") != current.get("bundles"):
        return False, "bundle tags changed"
    return True, f"unchanged (last built {str(entry.get('built_at', '?'))[:10]}, release {entry.get('release_tag', '?')})"

def update_cache_entry(cache, variant_id, data):
    cache.setdefault("variants", {})[variant_id] = {
        "schema": CACHE_SCHEMA,
        "resolved": data.get("resolved"),
        "config_hash": data.get("config_hash"),
        "bundles": data.get("bundles"),
        "status": data.get("status", "success"),
        "output": data.get("output"),
        "release_tag": data.get("release_tag"),
        "built_at": data.get("built_at") or now_iso(),
    }

# ============================================================
# CONFIG v2 LOADER
# ============================================================
def gh_api_get(url, timeout=60):
    headers = dict(UA)
    tok = os.environ.get("GITHUB_TOKEN", "")
    if tok: headers["Authorization"] = f"Bearer {tok}"
    return requests.get(url, timeout=timeout, headers=headers)

def parse_ver(tag):
    try: return tuple(int(x) for x in re.findall(r"\d+", str(tag))[:4])
    except Exception: return (0,)

def norm_key(s): return re.sub(r"[^a-z0-9]", "", (s or "").lower())
def safe_name(s): return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(s))
def clean_name(p): return re.sub(r"\s+", " ", (p or "")).strip()

def normalize_bundle_url(url):
    u = (url or "").strip()
    m = re.match(r"^https?://(?:www\.)?github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", u)
    if m:
        return f"https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}/main/patches-bundle.json"
    m = re.match(r"^https?://(?:www\.)?github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+?)/?$", u)
    if m:
        return f"https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}/{m.group(3)}/{m.group(4)}"
    return u

def repo_from_url(url):
    for pat in [r"raw\.githubusercontent\.com/([^/]+/[^/]+)/", r"github\.com/([^/]+/[^/]+)/", r"bundle/([^/]+/[^/]+)/", r"gitlab\.com/([^/]+/[^/]+)/"]:
        m = re.search(pat, url)
        if m: return m.group(1)
    return None

def _expand_bundle(ref, registry):
    if isinstance(ref, str): bid, opts = ref, {}
    elif isinstance(ref, dict):
        if "url" in ref:
            b = {"url": normalize_bundle_url(ref["url"])}
            b["label"] = ref.get("label") or (repo_from_url(b["url"]) or "/x").split("/")[-1]
            for k in ("patches", "options", "pin", "authority", "auto_include_new"):
                if k in ref: b[k] = ref[k]
            b.setdefault("auto_include_new", True)
            return b
        bid = list(ref.keys())[0]; opts = ref[bid] or {}
    else: return None
    src = registry.get(bid) or {}
    b = {"url": src.get("url", ""), "label": src.get("label") or bid}
    for k in ("patches", "options", "pin", "authority", "auto_include_new"):
        if k in opts: b[k] = opts[k]
        elif k in src: b[k] = src[k]
    b.setdefault("auto_include_new", True)
    return b if b.get("url") else None

def normalize_v2(raw):
    d = raw.get("defaults") or {}
    registry = {}
    for bid, b in (raw.get("bundles") or {}).items():
        b = b if isinstance(b, dict) else {"url": b}
        entry = {"url": normalize_bundle_url(b.get("url", "")), "label": b.get("label") or bid}
        for k in ("patches", "options", "pin", "authority", "auto_include_new"):
            if k in b: entry[k] = b[k]
        entry.setdefault("auto_include_new", True)
        registry[bid] = entry
    apps = raw.get("apps") or {}
    cfg = {"auto_include_new_patches": d.get("auto_include_new_patches", True),
           "exclude_patches": d.get("exclude_patches", []), "extra_bundles": {},
           "variants": [], "extra_apps": [], "_v2": True, "_defaults": d, "_registry": registry}
    apps_out = {}
    for bd in raw.get("builds") or []:
        app_id = bd.get("app"); app = apps.get(app_id) or {}
        bundles = [x for x in (_expand_bundle(r, registry) for r in bd.get("bundles") or []) if x]
        ver = str(bd.get("version") or app.get("version_policy") or d.get("version_policy") or "pin")
        common = {"id": bd.get("id") or f"{app_id}-{len(bundles)}", "bundles": bundles,
                  "clone_package": bd.get("clone") or app.get("clone") or "",
                  "app_name": bd.get("name") or app.get("name") or "",
                  "exclude_patches": bd.get("exclude") or [], "options": bd.get("options") or {},
                  "apk_version": ver, "_policy": ver,
                  "fallback_version": bd.get("fallback_version") or app.get("fallback_version") or "",
                  "auto_include_new": bd.get("auto_include_new", app.get("auto_include_new", True)),
                  "gh_assets": bd.get("gh_assets") or app.get("source", {}).get("assets") or [],
                  "_authorities": [b["label"] for b in bundles if b.get("authority")]}
        if bd.get("profile"): common["_profile"] = bd["profile"]
        if ver.startswith("channel:"):
            e = dict(common); e["type"] = ver.split(":", 1)[1]
            e["output_name"] = bd.get("output_name") or common["id"]
            cfg["variants"].append(e)
            continue
        src = app.get("source") or {}
        kind = src.get("kind", "apkeep")
        apk = {"source": {"upload": "upload", "apkeep": "apkeep", "github-releases": "github-rel"}.get(kind, kind),
               "package": app.get("package", ""), "arch": app.get("arch", "arm64-v8a"),
               "upload_tag": src.get("tag", ""), "gh_repo": src.get("repo", ""),
               "gh_assets": src.get("assets") or [], "fallback": src.get("fallback", ""),
               "fallbacks": src.get("fallbacks") or []}
        ae = apps_out.setdefault(app_id, {
            "id": app_id, "output_base": app.get("output_base") or app_id.title(),
            "density": app.get("density") or d.get("density") or ["xxhdpi"],
            "languages": app.get("languages") or d.get("languages") or ["en"],
            "keep_all_abis": app.get("keep_all_abis", d.get("keep_all_abis", False)),
            "apk": apk, "variants": []})
        ae["variants"].append(common)
    cfg["extra_apps"] = list(apps_out.values())
    return cfg

def normalize_legacy(raw):
    cfg = dict(raw)
    cfg.setdefault("auto_include_new_patches", True)
    cfg.setdefault("exclude_patches", [])
    cfg.setdefault("extra_bundles", {})
    cfg.setdefault("variants", [])
    cfg.setdefault("extra_apps", [])
    for v in cfg["variants"]:
        v["_policy"] = "channel:" + v.get("type", "stable")
    for ae in cfg["extra_apps"]:
        for v in ae.get("variants", []):
            v.setdefault("_policy", str(v.get("apk_version") or "latest"))
            v.setdefault("_authorities", [b.get("label") for b in v.get("bundles", []) if b.get("authority")])
    return cfg

def load_config(path="config.yaml"):
    with open(path) as f: raw = yaml.safe_load(f) or {}
    if "builds" in raw or (raw.get("meta") or {}).get("version") == 2:
        return normalize_v2(raw)
    return normalize_legacy(raw)

# ============================================================
# VERSION RESOLUTION
# ============================================================
def parse_patches_info(bundles):
    cmd = ["java", "-jar", "build/cli.jar", "list-patches"]
    for b in bundles: cmd += ["-p", b]
    cmd += ["--with-packages", "--with-options", "--with-versions"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    try:
        os.makedirs("build", exist_ok=True)
        tag = "_".join(os.path.basename(b).replace(".mpp", "") for b in bundles)
        with open(f"build/cli_debug_{safe_name(tag)}.txt", "w") as f:
            f.write(r.stdout or "")
            f.write("\n===== STDERR =====\n")
            f.write(r.stderr or "")
    except Exception: pass
    PKG_RE = re.compile(r"([a-z][a-z0-9_]*(?:\.[a-z0-9_]+){2,})")
    VER_RE = re.compile(r"(\d+\.\d+(?:\.\d+)*)")
    info, cur, pending_required, cur_pkg = [], None, False, None
    for rawline in (r.stdout or "").splitlines():
        line = rawline.strip()
        if line.startswith("Index:"):
            cur = {"name": None, "packages": [], "required_opts": [], "last_key": None, "versions": {}}
            info.append(cur); cur_pkg = None
        elif cur is None: continue
        elif line.startswith("Name:"): cur["name"] = line.split(":", 1)[1].strip()
        elif line.startswith("Key:"):
            cur["last_key"] = line.split(":", 1)[1].strip()
            if pending_required: cur["required_opts"].append(cur["last_key"]); pending_required = False
        elif line.startswith("Required:"):
            req = line.split(":", 1)[1].strip().lower() == "true"
            if req and cur.get("last_key"): cur["required_opts"].append(cur["last_key"])
            else: pending_required = req
        elif line.startswith(("Package name:", "Compatible packages:")):
            val = line.split(":", 1)[1].strip()
            mpar = re.search(r"\(([^)]*)\)", val)
            if mpar:
                vs = set(VER_RE.findall(mpar.group(1)))
                val = val[:mpar.start()].strip()
                if vs and val: cur["versions"].setdefault(val, set()).update(vs)
            pm = PKG_RE.search(val)
            if pm:
                cur_pkg = pm.group(1)
                if cur_pkg not in cur["packages"]: cur["packages"].append(cur_pkg)
        elif line.startswith("Supported versions:"):
            vs = {v.strip() for v in line.split(":", 1)[1].split(",") if v.strip()}
            if vs and cur_pkg: cur["versions"].setdefault(cur_pkg, set()).update(vs)
        else:
            pm = PKG_RE.search(line)
            if pm and re.fullmatch(r"[\w.]+:?", line):
                cur_pkg = pm.group(1)
                if cur_pkg not in cur["packages"]: cur["packages"].append(cur_pkg)
            elif re.fullmatch(r"[\d][\d.,\s]*", line):
                vs = set(VER_RE.findall(line))
                if vs and cur_pkg: cur["versions"].setdefault(cur_pkg, set()).update(vs)
            elif "version" in line.lower() and ":" in line:
                vs = set(VER_RE.findall(line))
                pm2 = PKG_RE.search(line)
                tgt = pm2.group(1) if pm2 else cur_pkg
                if vs and tgt: cur["versions"].setdefault(tgt, set()).update(vs)
    return [x for x in info if x.get("name")]

def declared_versions(mpp, package):
    key = (mpp, package)
    if key in DECLARED_CACHE: return DECLARED_CACHE[key]
    vers = set()
    for p in parse_patches_info([mpp]):
        for pkg, vs in (p.get("versions") or {}).items():
            if pkg == package: vers |= vs
    DECLARED_CACHE[key] = vers
    return vers

def github_tags(repo):
    if repo not in TAGS_CACHE:
        try:
            rels = gh_api_get(f"https://api.github.com/repos/{repo}/releases?per_page=100").json()
            TAGS_CACHE[repo] = {(r.get("tag_name") or "").lower().lstrip("v") for r in rels}
        except Exception:
            TAGS_CACHE[repo] = None
    return TAGS_CACHE[repo]

def upload_names(tag):
    if tag not in UPLOAD_CACHE:
        own = os.environ.get("GITHUB_REPOSITORY", "")
        names = set()
        try:
            r = gh_api_get(f"https://api.github.com/repos/{own}/releases/tags/{tag}")
            if r.status_code == 200:
                names = {a.get("name", "") for a in r.json().get("assets", [])}
        except Exception: pass
        UPLOAD_CACHE[tag] = names
    return UPLOAD_CACHE[tag]

def probe_store_version(apk_spec):
    pkg = apk_spec.get("package", "")
    if not pkg: return None
    if pkg in PROBE_CACHE: return PROBE_CACHE[pkg]
    ver = None
    names = [pkg.split(".")[-1], pkg.replace(".", "-")]
    for host in ("apkpure.net", "apkpure.com"):
        for n in names:
            html = cf_get(f"https://{host}/{n}/{pkg}", timeout=20)
            if not html: continue
            m = re.search(r'"softwareVersion"\s*:\s*"([\d][\d.]*)"', html) or re.search(r'"version"\s*:\s*"([\d][\d.]*)"', html)
            if m: ver = m.group(1); break
        if ver: break
    if not ver:
        for slug in names:
            html = cf_get(f"https://{slug}.en.uptodown.com/android", timeout=20)
            if not html: continue
            m = re.search(r"version[\"'>\s]+([\d][\d.]*)", html, re.I)
            if m: ver = m.group(1); break
    PROBE_CACHE[pkg] = ver
    return ver

def source_has_version(apk_spec, ver):
    kind = apk_spec.get("source")
    if kind == "upload":
        pkg = apk_spec.get("package", "")
        return any(pkg in n and ver in n for n in upload_names(apk_spec.get("upload_tag", "")))
    if kind == "github-rel":
        tags = github_tags(apk_spec.get("gh_repo", ""))
        return tags is not None and ver.lower() in tags
    return True

def resolve_variant(v, apk_spec):
    pol = str(v.get("_policy") or "pin")
    pkg = apk_spec.get("package", "")
    if pol == "latest":
        if apk_spec.get("source") == "github-rel" and apk_spec.get("gh_repo"):
            try:
                rel = gh_api_get(f"https://api.github.com/repos/{apk_spec['gh_repo']}/releases/latest").json()
                tag = (rel.get("tag_name") or "").lstrip("v")
                if tag: return tag, f"github-latest={tag} ({apk_spec['gh_repo']})"
            except Exception: pass
        probe = probe_store_version(apk_spec)
        if probe: return probe, f"store-latest={probe} (probed)"
        return "latest", "store-latest via apkeep (probe failed; age-refresh applies)"
    if pol not in ("follow", "follow_any", "latest_available"):
        return pol, f"pinned {pol}"
    declared = {}
    for mpp, label in (v.get("_mpps") or []):
        vs = declared_versions(mpp, pkg)
        if vs and (not v.get("_authorities") or label in v["_authorities"]):
            declared[label] = vs
    if not declared:
        return None, f"no authority bundle declares versions for {pkg}"
    sets = list(declared.values())
    pool = set.intersection(*sets) if pol != "follow_any" else set.union(*sets)
    if not pool:
        if pol == "follow":
            return None, "authority conflict: " + "; ".join(f"{k}->{sorted(x, key=parse_ver)[-1]}" for k, x in declared.items())
        pool = set.union(*sets)
    order = sorted(pool, key=parse_ver, reverse=True)
    who = ", ".join(declared)
    if pol == "latest_available":
        for cand in order:
            if source_has_version(apk_spec, cand):
                return cand, f"latest_available={cand} (declared by {who})"
        return None, f"declared {order[0]} not present at source ({apk_spec.get('source')})"
    return order[0], f"follow={order[0]} (declared by {who})"

def preflight(cfg):
    for ae in cfg.get("extra_apps", []):
        for v in ae.get("variants", []):
            mpps = []
            for b in v.get("bundles", []):
                mpp = f"bundles/{ae['id']}_{safe_name(b['label'])}.mpp"
                try:
                    if b.get("pin"): ver = download_pinned_mpp(repo_from_url(b["url"]), b["pin"], mpp)
                    else: ver = download_bundle_smart(b["url"], mpp)
                    b["_ver"] = ver
                    b["_status"] = get_release_status(repo_from_url(b["url"]), ver)
                    mpps.append((mpp, b["label"]))
                except Exception as e:
                    v["_skip"] = f"bundle download failed ({b.get('label')}): {e}"
                    break
            v["_mpps"] = mpps
            if v.get("_skip"):
                RESOLUTION.append(f"- {v['id']}: SKIP ({v['_skip']})")
                continue
            pol = str(v.get("_policy") or "")
            if pol in ("follow", "follow_any", "latest_available", "latest"):
                ver, why = resolve_variant(v, ae["apk"])
                if ver is None and str(v.get("fallback_version") or "").strip():
                    why = f"FALLBACK {v['fallback_version']} used because: {why}"
                    ver = str(v["fallback_version"])
                RESOLUTION.append(f"- {v['id']}: {why}")
                if ver is None: v["_skip"] = why
                else: v["apk_version"] = ver; v["_resolved_why"] = why
            else:
                RESOLUTION.append(f"- {v['id']}: pinned {v.get('apk_version')}")
    for bv in cfg.get("variants", []):
        RESOLUTION.append(f"- {bv.get('id')}: channel {bv.get('type')} (heal-loop discovers version)")

# ============================================================
# CORE HELPERS
# ============================================================
def is_zip(path):
    try:
        with open(path, "rb") as f: return f.read(2) == b"PK"
    except Exception: return False

def download_file(url, dest, timeout=1200):
    print(f"Downloading {url} ...")
    with requests.get(url, stream=True, timeout=timeout, headers=UA) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                if chunk: f.write(chunk)

def download_browser(url, dest, timeout=1200):
    try:
        with requests.get(url, stream=True, timeout=timeout, headers=UA, allow_redirects=True) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    if chunk: f.write(chunk)
        if os.path.exists(dest) and os.path.getsize(dest) > 1024 and is_zip(dest): return True
    except Exception as e: print(f"requests download failed: {e}")
    if HAS_CURL_CFFI:
        for imp in ["chrome136", "chrome133", "chrome131", "chrome124", "chrome120", "chrome110"]:
            try:
                s = cffi_requests.Session(impersonate=imp)
                r = s.get(url, timeout=timeout, headers=UA, allow_redirects=True)
                if r.status_code == 200 and r.content:
                    with open(dest, "wb") as f: f.write(r.content)
                    if os.path.exists(dest) and os.path.getsize(dest) > 1024 and is_zip(dest): return True
            except Exception: pass
    return False

def cf_get(url, timeout=30):
    if HAS_CURL_CFFI:
        for imp in ["chrome136", "chrome133", "chrome131", "chrome124", "chrome120", "chrome110"]:
            try:
                s = cffi_requests.Session(impersonate=imp)
                r = s.get(url, timeout=timeout, headers=UA, allow_redirects=True)
                text = r.text or ""
                if r.status_code == 200 and not any(x in text[:1000].lower() for x in ["just a moment", "attention required", "turnstile"]): return text
            except Exception: continue
    try:
        r = requests.get(url, timeout=timeout, headers=UA, allow_redirects=True)
        if r.status_code == 200 and not any(x in r.text[:1000].lower() for x in ["just a moment", "attention required", "turnstile"]): return r.text
    except Exception: pass
    return None

def ensure_apkeep():
    path = "build/apkeep"
    if os.path.exists(path):
        os.chmod(path, 0o755)
        return path
    rel = gh_api_get("https://api.github.com/repos/EFForg/apkeep/releases/latest").json()
    chosen = next((a for a in rel.get("assets", []) if "unknown-linux-gnu" in a.get("name", "").lower() and "x86_64" in a.get("name", "").lower() and not a.get("name", "").endswith((".deb", ".rpm", ".sig"))), None)
    if not chosen:
        chosen = next((a for a in rel.get("assets", []) if "linux" in a.get("name", "").lower() and "x86_64" in a.get("name", "").lower() and not a.get("name", "").endswith((".deb", ".rpm", ".sig"))), None)
    if not chosen: raise Exception("Could not find Linux apkeep release asset")
    raw = "build/apkeep_download"
    download_file(chosen["browser_download_url"], raw)
    extracted = False
    try:
        if tarfile.is_tarfile(raw):
            with tarfile.open(raw, "r:*") as t:
                target = next((m for m in t.getmembers() if os.path.basename(m.name) == "apkeep"), None)
                if not target: target = next((m for m in t.getmembers() if m.isfile()), None)
                with open(path, "wb") as out: out.write(t.extractfile(target).read())
                extracted = True
        elif zipfile.is_zipfile(raw):
            with zipfile.ZipFile(raw) as z:
                names = [n for n in z.namelist() if os.path.basename(n) == "apkeep"] or [n for n in z.namelist() if not n.endswith("/")]
                with open(path, "wb") as out: out.write(z.read(names[0]))
                extracted = True
    except Exception as e: print(f"apkeep archive extraction failed: {e}")
    if not extracted: shutil.copyfile(raw, path)
    os.chmod(path, 0o755)
    return path

def apkeep_download(apkeep, pkg, version, arch, outdir, apkeep_source=None):
    os.makedirs(outdir, exist_ok=True)
    spec = f"{pkg}@{version}" if version and version != "latest" else pkg
    if apkeep_source == "f-droid":
        cmds = [[apkeep, "-a", spec, "-d", "f-droid", outdir], [apkeep, "-a", spec, outdir]]
    else:
        cmds = [[apkeep, "-a", spec, "-d", "apk-pure", "-o", f"arch={arch}", outdir],
                [apkeep, "-a", spec, "-o", f"arch={arch}", outdir],
                [apkeep, "-a", spec, outdir]]
    for cmd in cmds:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
            files = [os.path.join(outdir, f) for f in os.listdir(outdir) if os.path.isfile(os.path.join(outdir, f)) and is_zip(os.path.join(outdir, f))]
            if files:
                files.sort(key=os.path.getsize, reverse=True)
                return files[0]
        except Exception: pass
    return None

def select_splits(entries, arch, densities, languages, keep_all_abis=False):
    arch_q = arch.replace("-", "_")
    dens = [d.lower() for d in densities]
    langs = [x.lower() for x in languages]
    keep = []
    for n in entries:
        b = os.path.basename(n).lower()
        if ".config." not in b: keep.append(n); continue
        qual = b.split(".config.")[-1].replace(".apk", "").lower()
        if qual in ABI_QUALS:
            if keep_all_abis or qual == arch_q: keep.append(n)
        elif qual in dens: keep.append(n)
        elif qual.isalpha() and len(qual) <= 3:
            if qual in langs: keep.append(n)
        else: keep.append(n)
    return keep

def raw_kind(raw):
    if not raw or not os.path.exists(raw) or not is_zip(raw): return None
    with zipfile.ZipFile(raw) as z: names = z.namelist()
    if any(n.lower().endswith(".apk") for n in names): return "bundle"
    if any(n.endswith("AndroidManifest.xml") for n in names): return "single"
    return None

def base_manifest_package_ok(raw_path, expected_pkg):
    if not expected_pkg: return True
    try:
        with zipfile.ZipFile(raw_path) as z:
            names = z.namelist()
            apk_entries = [n for n in names if n.lower().endswith(".apk")]
            if apk_entries:
                base_entry = next((n for n in apk_entries if not os.path.basename(n).lower().startswith(("split_", "config."))), apk_entries[0])
                with zipfile.ZipFile(io.BytesIO(z.read(base_entry))) as za: data = za.read("AndroidManifest.xml")
            else: data = z.read("AndroidManifest.xml")
        return (expected_pkg.encode("utf-8") in data) or (expected_pkg.encode("utf-16-le") in data)
    except Exception: return True

def save_base(raw, out_apkm, out_single, arch, densities, languages, keep_all_abis=False):
    kind = raw_kind(raw)
    if kind == "bundle":
        with zipfile.ZipFile(raw) as z:
            entries = [n for n in z.namelist() if n.lower().endswith(".apk")]
            keep = select_splits(entries, arch, densities, languages, keep_all_abis)
            if not keep: return None, None
            with zipfile.ZipFile(out_apkm, "w", zipfile.ZIP_DEFLATED) as zo:
                for n in keep: zo.writestr(os.path.basename(n), z.read(n))
        return out_apkm, f"split subset ({arch}, {'/'.join(densities)})"
    if kind == "single":
        shutil.copyfile(raw, out_single)
        return out_single, f"single apk ({arch})"
    return None, None

def get_releases(repo):
    r = gh_api_get(f"https://api.github.com/repos/{repo}/releases?per_page=100")
    r.raise_for_status()
    return r.json()

def get_latest_stable(repo):
    r = gh_api_get(f"https://api.github.com/repos/{repo}/releases/latest")
    r.raise_for_status()
    return r.json()

def get_release_status(repo, version):
    if not repo: return "unknown"
    try:
        for r in get_releases(repo):
            if (r.get("tag_name") or "").lower().lstrip("v") == str(version).lower().lstrip("v"):
                return "prerelease" if r.get("prerelease") else "stable"
    except Exception: pass
    return "unknown"

def download_bundle_from_json(url, dest):
    j = requests.get(url, timeout=120).json()
    dl = j.get("download_url")
    if not dl: raise Exception(f"bundle json has no download_url: {url}")
    download_file(dl, dest)
    return j.get("version", "unknown")

def download_bundle_smart(url, dest):
    try:
        ver = download_bundle_from_json(url, dest)
        if os.path.exists(dest) and os.path.getsize(dest) > 100_000 and is_zip(dest): return ver
    except Exception as e: print(f"bundle json failed for {url}: {e}")
    repo = repo_from_url(url)
    if repo: return download_mpp_from_github(repo, dest)
    raise Exception(f"Could not download bundle from {url}")

def download_mpp_from_github(repo, dest):
    try:
        rels = gh_api_get(f"https://api.github.com/repos/{repo}/releases?per_page=10").json()
        for r in rels:
            if r.get("draft"): continue
            for a in r.get("assets", []):
                if a.get("name", "").endswith(".mpp"):
                    download_file(a["browser_download_url"], dest)
                    return r.get("tag_name", "unknown")
    except Exception: pass
    try:
        j = requests.get(f"https://raw.githubusercontent.com/{repo}/main/patches-bundle.json", timeout=30).json()
        if j.get("download_url"):
            download_file(j["download_url"], dest)
            return j.get("version", "unknown")
    except Exception: pass
    raise Exception(f"Could not find .mpp for {repo}")

def download_stable_mpp(repo, dest):
    rels = get_releases(repo)
    for prefer_stable in (True, False):
        for r in rels:
            if r.get("draft"): continue
            if prefer_stable and r.get("prerelease"): continue
            for a in r.get("assets", []):
                if a.get("name", "").endswith(".mpp"):
                    download_file(a["browser_download_url"], dest)
                    return r.get("tag_name", "unknown")
    raise Exception(f"Could not find .mpp for {repo}")

def download_pinned_mpp(repo, tag, dest):
    rel = gh_api_get(f"https://api.github.com/repos/{repo}/releases/tags/{tag}").json()
    for a in rel.get("assets", []):
        if a.get("name", "").endswith(".mpp"):
            download_file(a["browser_download_url"], dest)
            return tag
    raise Exception(f"No .mpp asset in {repo} release {tag}")

def find_uploaded_asset(own_repo, up_tag, aid, pkg, version):
    releases = []
    if up_tag:
        r = gh_api_get(f"https://api.github.com/repos/{own_repo}/releases/tags/{up_tag}")
        if r.status_code == 200: releases.append(r.json())
    r = gh_api_get(f"https://api.github.com/repos/{own_repo}/releases?per_page=100")
    if r.status_code == 200: releases.extend(r.json())
    seen, uniq = set(), []
    for rel in releases:
        if isinstance(rel, dict) and rel.get("id") not in seen:
            seen.add(rel.get("id"))
            uniq.append(rel)
    pkg_lower, ver_lower = (pkg or "").lower(), (version or "").lower()
    def ok(name):
        n = name.lower()
        if "patched" in n: return False
        if not n.endswith((".apk", ".apkm", ".xapk", ".zip")): return False
        if pkg_lower and pkg_lower not in n: return False
        if ver_lower and ver_lower not in n: return False
        return True
    candidates = [a for rel in uniq for a in rel.get("assets", []) if ok(a.get("name", ""))]
    if not candidates: return None, None
    multi_arch = [c for c in candidates if "arm64-v8a" in c["name"] and "armeabi-v7a" in c["name"]]
    best = multi_arch[0] if multi_arch else candidates[0]
    tag_name = "unknown"
    for rel in uniq:
        for a in rel.get("assets", []):
            if a.get("id") == best.get("id"):
                tag_name = rel.get("tag_name", "unknown")
                break
        if tag_name != "unknown": break
    return best, tag_name

def fetch_raw(app, aid, version, source, bundle_only=False):
    key = (aid, version, bundle_only)
    if key in RAW_CACHE: return RAW_CACHE[key]
    spec = app["apk"]
    arch = spec.get("arch", "arm64-v8a")
    result = (None, None)
    def accept(raw):
        k = raw_kind(raw)
        if k is None: return None
        if bundle_only and k != "bundle": return None
        return k

    chain = [source] + [f for f in (spec.get("fallbacks") or []) if f != source]

    if "github-rel" in chain:
        repo = spec.get("gh_repo", "")
        raw = f"build/raw_{aid}_{safe_name(version)}_ghrel.bin"
        for tag in (f"v{version}", str(version)):
            try:
                rel = gh_api_get(f"https://api.github.com/repos/{repo}/releases/tags/{tag}").json()
            except Exception:
                continue
            assets = rel.get("assets") or []
            chosen = None
            for pat in spec.get("gh_assets") or []:
                chosen = next((a for a in assets if a.get("name", "") == pat), None)
                if chosen: break
            if not chosen:
                chosen = next((a for a in assets if a.get("name", "").lower().endswith(".apk") and "arm64" in a.get("name", "").lower()), None)
            if not chosen:
                chosen = next((a for a in assets if a.get("name", "").lower().endswith(".apk")), None)
            if chosen:
                try:
                    download_file(chosen["browser_download_url"], raw)
                    k = accept(raw)
                    if k: result = (raw, k)
                except Exception as e: print(f"{aid}: github-rel asset failed: {e}")
                break
        if result[0] is not None:
            RAW_CACHE[key] = result
            return result

    if not bundle_only and source == "upload":
        up_tag = (spec.get("upload_tag") or "").strip()
        own_repo = os.environ.get("GITHUB_REPOSITORY", "")
        if up_tag and own_repo:
            asset, tag = find_uploaded_asset(own_repo, up_tag, aid, spec.get("package", ""), version)
            if asset:
                print(f"{aid}: using uploaded asset {asset['name']} from release {tag}")
                raw = f"build/raw_{aid}_{safe_name(version)}_upload.bin"
                try:
                    download_file(asset["browser_download_url"], raw)
                    k = accept(raw)
                    if k: result = (raw, k)
                except Exception as e: print(f"{aid}: uploaded asset failed: {e}")
    if source == "upload":
        RAW_CACHE[key] = result
        return result

    if result[0] is None and (source in ("apkeep", "scraper") or "scraper" in chain or "apkmirror" in chain):
        raw = f"build/raw_{aid}_{safe_name(version)}_scraper.bin"
        expected_pkg = spec.get("package", "")
        def scraper_candidates():
            try:
                for u in scrape_apkmirror_links(spec, version, arch, app.get("density", "xxhdpi")):
                    yield ("apkmirror", u)
            except Exception as e: print(f"{aid}: apkmirror scrape failed: {e}")
            try:
                u = scrape_apkcombo(spec, version, arch)
                if u: yield ("apkcombo", u)
            except Exception as e: print(f"{aid}: apkcombo scrape failed: {e}")
            try:
                u = scrape_apkpure_net(spec, version)
                if u: yield ("apkpure.net", u)
                else: print(f"{aid}: apkpure.net did not find a link for {version}")
            except Exception as e: print(f"{aid}: apkpure.net scrape failed: {e}")
            try:
                u = scrape_uptodown(spec, version, arch)
                if u: yield ("uptodown", u)
                else: print(f"{aid}: uptodown did not find a link for {version}")
            except Exception as e: print(f"{aid}: uptodown scrape failed: {e}")
        for label, dl in scraper_candidates():
            if not download_browser(dl, raw):
                print(f"{aid}: {label} download invalid")
                continue
            if not base_manifest_package_ok(raw, expected_pkg):
                print(f"{aid}: {label} base package is NOT {expected_pkg} - skipping this candidate")
                continue
            k = accept(raw)
            if k:
                print(f"{aid}: {label} provided {k} for {version}")
                result = (raw, k)
                break
            else:
                print(f"{aid}: {label} result not acceptable (bundle_only={bundle_only})")

    if result[0] is None and "f-droid" in chain:
        try:
            apkeep = ensure_apkeep()
            raw = apkeep_download(apkeep, spec.get("package", ""), version, arch,
                                  f"build/apkeep_{aid}_{safe_name(version)}_fdroid", apkeep_source="f-droid")
            if raw:
                k = accept(raw)
                if k: result = (raw, k)
        except Exception as e: print(f"{aid}: f-droid failed: {e}")

    if result[0] is None and "apkeep" in chain:
        try:
            apkeep = ensure_apkeep()
            raw = apkeep_download(apkeep, spec.get("package", ""), version, arch, f"build/apkeep_{aid}_{safe_name(version)}")
            if raw:
                k = accept(raw)
                if k: result = (raw, k)
        except Exception as e: print(f"{aid}: apkeep failed: {e}")

    if result[0] is None and source in ("apkeep", "scraper") and version == "latest":
        vc = str(spec.get("version_code") or "").strip()
        pkg = spec.get("package", "")
        if vc and pkg:
            for host in ["d.apkpure.net", "d.apkpure.com"]:
                try:
                    url = f"https://{host}/b/XAPK/{pkg}?versionCode={vc}"
                    raw = f"build/raw_{aid}_{safe_name(version)}_direct.bin"
                    if download_browser(url, raw):
                        k = accept(raw)
                        if k:
                            result = (raw, k)
                            break
                except Exception as e: print(f"{aid}: direct APKPure failed: {e}")

    RAW_CACHE[key] = result
    return result

def scrape_apkmirror_links(spec, version, arch, density, limit=6):
    base = "https://www.apkmirror.com"
    btype = (spec.get("apkmirror_type") or "ANY").upper()
    pages = apkmirror_candidate_pages(spec, version)
    results = []
    for page_url in pages:
        page = cf_get(page_url)
        if not page: continue
        rows = re.split(r'<div[^>]*class="[^"]*table-row[^"]*"[^>]*>', page, flags=re.I)
        for row in rows:
            row_text = re.sub(r"<[^>]+>", " ", row).lower()
            arch_match = (arch.replace("-", "_") in row_text or arch in row_text or "arm64" in row_text or "universal" in row_text or "noarch" in row_text)
            if not arch_match: continue
            is_bundle = "bundle" in row_text and "apkm" in row_text
            is_apk = ("apk" in row_text and not is_bundle) or "forcebaseapk" in row_text
            if btype == "APK" and not is_apk: continue
            if btype == "BUNDLE" and not is_bundle: continue
            btn_match = re.search(r'<a[^>]+class="[^"]*downloadButton[^"]*"[^>]*href="([^"]+)"', row, re.I)
            if not btn_match:
                btn_match = re.search(r'<a[^>]+href="([^"]+)"[^>]*class="[^"]*downloadButton', row, re.I)
            if not btn_match:
                btn_match = re.search(r'href="([^"]*-android-apk-download/[^"]*)"', row, re.I)
            if not btn_match: continue
            vurl = btn_match.group(1)
            if not vurl.startswith("http"): vurl = base + vurl
            vpage = cf_get(vurl)
            if not vpage: continue
            final_match = re.search(r'<a[^>]+id="download-link"[^>]*href="([^"]+)"', vpage, re.I)
            if not final_match:
                final_match = re.search(r'<a[^>]+href="([^"]+)"[^>]*id="download-link"', vpage, re.I)
            if not final_match:
                final_match = re.search(r'href="([^"]*download\.php\?id=[^"]+)"', vpage, re.I)
            if not final_match: continue
            out = final_match.group(1)
            if out.startswith("/"): out = base + out
            out = out.replace("&amp;", "&")
            if btype == "APK" and "forcebaseapk=true" not in out and "bundle" in out.lower(): continue
            if btype == "BUNDLE" and "forcebaseapk=true" in out: continue
            if out not in results:
                results.append(out)
            if len(results) >= limit: return results
    return results

def apkmirror_candidate_pages(spec, version):
    base = "https://www.apkmirror.com"
    org = spec.get("apkmirror_org", "google-inc")
    names = spec.get("apkmirror_names") or [spec.get("apkmirror_name", "")]
    names = [n for n in names if n]
    found, seen = [], set()
    ver_slug = version.replace(".", "-") if version and version != "latest" else ""
    def add(href):
        if not href: return
        if href.startswith("/"): href = base + href
        if href not in seen:
            seen.add(href)
            found.append(href)
    for name in names:
        html = cf_get(f"{base}/apk/{org}/{name}/")
        if html:
            for href in re.findall(r'href="([^"]+/apk/[^"]+)"', html):
                if ver_slug and ver_slug not in href: continue
                add(href)
            for href in re.findall(r'href="(/apk/[^"]+)"', html):
                if ver_slug and ver_slug not in href: continue
                add(href)
    if ver_slug:
        q = quote_plus(" ".join(names + [version]))
        for su in [f"{base}/?post_type=app_release&searchtype=apk&s={q}", f"{base}/?s={q}"]:
            html = cf_get(su)
            if html:
                for href in re.findall(r'href="(/apk/[^"]+)"', html):
                    if ver_slug in href: add(href)
    return found[:20]

def scrape_apkcombo(spec, version, arch):
    pkg = spec.get("package", "")
    if not pkg: return None
    urls_to_try = []
    if version and version != "latest":
        safe_ver = version.replace(" ", "-")
        urls_to_try.append(f"https://apkcombo.com/search/{pkg}/download/phone-{safe_ver}-apk")
        urls_to_try.append(f"https://apkcombo.com/search/{pkg}/download/phone-{safe_ver}-xapk")
        urls_to_try.append(f"https://apkcombo.com/{pkg}/download/phone-{safe_ver}-apk")
    urls_to_try.append(f"https://apkcombo.com/search/{pkg}/download/apk")
    urls_to_try.append(f"https://apkcombo.com/search/{pkg}/download/xapk")
    urls_to_try.append(f"https://apkcombo.com/{pkg}/download/apk")
    for url in urls_to_try:
        html = cf_get(url)
        if not html: continue
        m = re.search(r'href="(https://download\.apkcombo\.com/[^"]+)"', html, re.I)
        if m: return m.group(1).replace('&amp;', '&')
        m = re.search(r'"download_url"\s*:\s*"(https://download\.apkcombo\.com/[^"]+)"', html, re.I)
        if m: return m.group(1).replace('\\u0026', '&').replace('&amp;', '&')
        m = re.search(r'href="(/r2\?u=[^"]+)"', html, re.I)
        if m: return "https://apkcombo.com" + m.group(1).replace('&amp;', '&')
    return None

def scrape_apkpure_net(spec, version):
    if not HAS_BS4: return None
    pkg = spec.get("package", "")
    candidates = []
    for x in [spec.get("apkpure_name"), pkg.split(".")[-1], pkg.replace(".", "-")]:
        if x and x not in candidates: candidates.append(x)
    for name in candidates:
        urls = []
        if version and version != "latest":
            urls.append(f"https://apkpure.net/{name}/{pkg}/download/{version}")
        urls.append(f"https://apkpure.net/{name}/{pkg}")
        urls.append(f"https://apkpure.net/{name}/{pkg}/versions")
        for url in urls:
            try:
                html = cf_get(url)
                if not html: continue
                soup = BeautifulSoup(html, "html.parser")
                a = soup.find("a", id="download_link")
                if a and a.get("href"):
                    href = a["href"]
                    if not href.startswith("http"): href = urljoin("https://apkpure.net", href)
                    return href
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    txt = a.get_text(" ", strip=True).lower()
                    if version and version != "latest" and version not in href and version not in txt: continue
                    if "/download/" in href:
                        if not href.startswith("http"): href = urljoin("https://apkpure.net", href)
                        h2 = cf_get(href)
                        if h2:
                            s2 = BeautifulSoup(h2, "html.parser")
                            b = s2.find("a", id="download_link")
                            if b and b.get("href"):
                                out = b["href"]
                                if not out.startswith("http"): out = urljoin("https://apkpure.net", out)
                                return out
            except Exception as e: print(f"APKPure scrape failed for {url}: {e}")
    return None

def scrape_uptodown(spec, version, arch):
    if not HAS_BS4: return None
    pkg = spec.get("package", "")
    slugs = []
    for x in [spec.get("uptodown_slug"), pkg.split(".")[-1], pkg.replace(".", "-")]:
        if x and x not in slugs: slugs.append(x)
    locales = ["en", "de", "fr", "in", "it", "ru", "jp", "kr"]
    for slug in slugs:
        for loc in locales:
            base = f"https://{slug}.{loc}.uptodown.com/android"
            try:
                html = cf_get(base)
                if not html: continue
                soup = BeautifulSoup(html, "html.parser")
                h1 = soup.find("h1", id="detail-app-name")
                if not h1: continue
                code = h1.get("data-code")
                if not code: continue
                for page in range(1, 4):
                    api = f"{base}/apps/{code}/versions/{page}"
                    js = cf_get(api)
                    if not js: break
                    try: entries = (json.loads(js) or {}).get("data") or []
                    except Exception: break
                    if not entries: break
                    for ent in entries:
                        ev = ent.get("version", "")
                        if version and version != "latest" and ev != version: continue
                        parts = ent.get("versionURL") or {}
                        vu = "/".join(str(parts.get(k, "")).strip("/") for k in ["url", "extraURL", "versionID"])
                        if not vu: continue
                        if not vu.startswith("http"): vu = urljoin(base, vu)
                        vhtml = cf_get(vu)
                        if not vhtml: continue
                        vsoup = BeautifulSoup(vhtml, "html.parser")
                        variant_id = None
                        vbtn = vsoup.select_one(".button.variants[data-version]")
                        if vbtn:
                            data_version = vbtn.get("data-version")
                            cat = f"{base.rsplit('/android', 1)[0]}/app/{code}/version/{data_version}/files"
                            cj = cf_get(cat)
                            if cj:
                                try: content = (json.loads(cj) or {}).get("content") or ""
                                except Exception: content = ""
                                csoup = BeautifulSoup(content, "html.parser")
                                cur_arch = ""
                                fallback = None
                                for node in csoup.select("section.variants > .content > *"):
                                    if node.name == "p":
                                        cur_arch = node.get_text(" ", strip=True).lower()
                                        continue
                                    rep = node.select_one(".v-report[data-file-id]")
                                    if not rep: continue
                                    fid = rep.get("data-file-id")
                                    if not fallback: fallback = fid
                                    if arch in cur_arch or arch.replace("-", "_") in cur_arch or "universal" in cur_arch:
                                        variant_id = fid
                                        break
                                if not variant_id: variant_id = fallback
                        if variant_id:
                            dx = cf_get(f"{base}/download/{variant_id}-x")
                            if dx:
                                dsoup = BeautifulSoup(dx, "html.parser")
                                btn = dsoup.find(id="detail-download-button")
                                if btn and btn.get("data-url"):
                                    return urljoin("https://dw.uptodown.com/dwn/", btn["data-url"])
                        btn = vsoup.find(id="detail-download-button")
                        if btn and btn.get("data-url"):
                            return urljoin("https://dw.uptodown.com/dwn/", btn["data-url"])
                    if version and version != "latest":
                        try:
                            target = parse_ver(version)
                            if all(parse_ver(e.get("version", "")) < target for e in entries): break
                        except Exception: pass
            except Exception as e: print(f"Uptodown scrape failed for {base}: {e}")
    return None

def prepare_bases(app, aid, version, source, arch, densities, languages, keep_all_abis=False):
    key = (aid, version)
    if key in BASE_CACHE: return BASE_CACHE[key]
    sv = safe_name(version)
    out_apkm = f"build/base_{aid}_{sv}.apkm"
    out_single = f"build/base_{aid}_{sv}.apk"
    raw, kind = fetch_raw(app, aid, version, source, bundle_only=False)
    if raw is None:
        BASE_CACHE[key] = None
        return None
    if kind == "bundle":
        res_tuple = save_base(raw, out_apkm, out_single, arch, densities, languages, keep_all_abis)
        res = {"base": res_tuple, "single": False}
    elif source == "upload":
        res_tuple = save_base(raw, out_apkm, out_single, arch, densities, languages, keep_all_abis)
        res = {"base": res_tuple, "single": True}
    else:
        raw2, kind2 = fetch_raw(app, aid, version, source, bundle_only=True)
        if kind2 == "bundle":
            res_tuple = save_base(raw2, out_apkm, out_single, arch, densities, languages, keep_all_abis)
            res = {"base": res_tuple, "single": False}
        else:
            res_tuple = save_base(raw, out_apkm, out_single, arch, densities, languages, keep_all_abis)
            res = {"base": res_tuple, "single": True}
    BASE_CACHE[key] = res
    return res

def generate_options_file(bundles, out_path):
    for sub in ["options", "options-create"]:
        cmd = ["java", "-jar", "build/cli.jar", sub]
        for b in bundles: cmd += ["-p", b]
        cmd += ["-o", out_path]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0 and os.path.exists(out_path):
            with open(out_path) as f: return json.load(f)
    return None

def set_option_value(opts, key, value):
    cur = opts.get(key)
    if isinstance(cur, dict): cur["value"] = value
    else: opts[key] = value

def apply_option(entry, key, value):
    opts = entry.setdefault("options", {})
    nk = norm_key(key)
    for k in list(opts):
        if k == key or norm_key(k) == nk: set_option_value(opts, k, value); return
    for k in list(opts):
        ok = norm_key(k)
        if nk and ok and (nk in ok or ok in nk): set_option_value(opts, k, value); return
    if len(opts) == 1: set_option_value(opts, list(opts)[0], value); return
    set_option_value(opts, key, value)

def enable_entry(entry, vals):
    entry["enabled"] = True
    vals = vals or {}
    for k, v in vals.items(): apply_option(entry, k, v)
    pkg = vals.get("packageName") or vals.get("packagename")
    if pkg:
        for k in list((entry.get("options") or {})):
            kl = k.lower()
            raw = entry["options"][k]
            cur = raw.get("value") if isinstance(raw, dict) else raw
            if "update" in kl and isinstance(cur, bool): set_option_value(entry["options"], k, True)
            elif "package" in kl and "update" not in kl: set_option_value(entry["options"], k, pkg)

def make_variant_options(gen_data, per_bundle):
    data = copy.deepcopy(gen_data)
    found = set()
    all_wanted = set()
    for i, bundle in enumerate(data):
        wanted = per_bundle[i] if i < len(per_bundle) else {}
        all_wanted |= set(wanted)
        entries = bundle.get("patches") or {}
        lookup = {}
        for real in entries:
            lookup.setdefault(clean_name(real).lower(), real)
        resolved = {}
        for wname, wvals in wanted.items():
            key = clean_name(wname).lower()
            real = lookup.get(key)
            if real is None:
                cands = sorted({r for k, r in lookup.items() if key in k or k in key})
                if len(cands) == 1:
                    real = cands[0]
            if real is not None:
                resolved[real] = wvals
                found.add(wname)
        for name, entry in entries.items():
            if name in resolved:
                enable_entry(entry, resolved[name])
            else:
                entry["enabled"] = False
    return data, sorted(all_wanted - found)

def needs_value_names(gen_data):
    need = set()
    for bundle in gen_data or []:
        for name, entry in (bundle.get("patches") or {}).items():
            for v in (entry.get("options") or {}).values():
                if v is None or (isinstance(v, dict) and v.get("value") is None): need.add(name)
    return need

def detect_alias():
    ks = "signing/keystore.jks"
    pw = os.environ.get("KEYSTORE_PASSWORD", "")
    preferred = os.environ.get("KEY_ALIAS", "")
    try:
        out = subprocess.run(["keytool", "-list", "-keystore", ks, "-storepass", pw], capture_output=True, text=True)
        aliases = [line.split(",")[0].strip() for line in out.stdout.splitlines() if "PrivateKeyEntry" in line or "trustedCertEntry" in line]
        if preferred in aliases: return preferred
        if aliases: return aliases[0]
    except Exception: pass
    return preferred

def keystore_fingerprint(alias):
    try:
        r = subprocess.run(["keytool", "-list", "-v", "-keystore", "signing/keystore.jks", "-storepass", os.environ.get("KEYSTORE_PASSWORD", ""), "-alias", alias], capture_output=True, text=True)
        m = re.search(r"SHA-?256:\s*([0-9A-Fa-f:]+)", r.stdout)
        return m.group(1).upper() if m else None
    except Exception: return None

def apk_fingerprint(path):
    try:
        r = subprocess.run(["keytool", "-printcert", "-jarfile", path], capture_output=True, text=True)
        m = re.search(r"SHA-?256:\s*([0-9A-Fa-f:]+)", r.stdout)
        return m.group(1).upper() if m else None
    except Exception: return None

def verify_signature(path, ks_fp):
    fp = apk_fingerprint(path)
    if fp: print(f"signature check: apk={fp} keystore={ks_fp} match={fp == ks_fp if ks_fp else None}")
    return fp

def compute_auto(info, pkg, exclude, configured, needs_value):
    ex = {x.lower() for x in exclude}
    conf = {x.lower() for x in configured}
    needs = {x.lower() for x in needs_value}
    out = []
    for p in info:
        n, nl = p["name"], p["name"].lower()
        if nl in ex or nl in conf or nl in needs: continue
        if p.get("required_opts"): continue
        if pkg in p.get("packages", []): out.append(n)
    return out

def run_patch(apk_path, out_apk, gen_data, per_bundle, label, alias, bundles, keep_all_abis=False):
    data, missing = make_variant_options(gen_data, per_bundle)
    opts_path = f"build/options_{safe_name(label)}.json"
    with open(opts_path, "w") as f: json.dump(data, f, indent=2)
    cmd = ["java", "-jar", "build/cli.jar", "patch"]
    for b in bundles: cmd += ["-p", b]
    cmd += ["--options-file", opts_path, "--force"]
    ks = "signing/keystore.jks"
    if os.path.exists(ks):
        cmd += ["--keystore", ks, "--keystore-password", os.environ.get("KEYSTORE_PASSWORD", ""), "--keystore-entry-alias", alias, "--keystore-entry-password", os.environ.get("KEY_PASSWORD", "")]
    if not keep_all_abis: cmd += ["--striplibs", "arm64-v8a"]
    cmd += ["-o", out_apk, apk_path]
    r = subprocess.run(cmd, capture_output=True, text=True)
    m = re.search(r"Filtering patches for\s+[\w.]+\s+v([0-9][0-9A-Za-z_\-]*(?:\.[0-9A-Za-z_\-]+)*)", r.stdout or "")
    if m:
        DETECTED_VERSIONS[label] = m.group(1)
    applied = list(dict.fromkeys(m.strip() for m in re.findall(r"Applied:\s*(.+)", r.stdout)))
    failed = list(dict.fromkeys(m.strip() for m in re.findall(r"FAILED:\s*(.+)", r.stdout + "\n" + r.stderr)))
    return r.returncode == 0, applied, failed, missing

def heal_patch(apk_path, out_apk, gen_data, per_bundle, label, alias, bundles, keep_all_abis=False):
    pb = [dict(x) for x in per_bundle]
    dropped = []
    while True:
        ok, applied, failed, missing = run_patch(apk_path, out_apk, gen_data, pb, label, alias, bundles, keep_all_abis)
        if ok and failed: ok = False
        if ok: return True, applied, dropped, missing
        if not failed: return False, applied, dropped, missing
        removed = False
        for f in failed:
            fl = f.lower()
            for d in pb:
                for n in list(d):
                    if n.lower() == fl: dropped.append(n); del d[n]; removed = True
        if not removed: return False, applied, dropped, missing

def find_asset(release, names=None):
    assets = release.get("assets", [])
    if names:
        low = [n.lower() for n in names]
        for a in assets:
            if a.get("name", "").lower() in low: return a.get("browser_download_url")
    for a in assets:
        n = a.get("name", "").lower()
        if "arm64" in n and "universal" in n: return a.get("browser_download_url")
    return None

def classify(r, stable_tag):
    name = (r.get("name") or "").strip().lower()
    tag = r.get("tag_name") or ""
    if tag == stable_tag or name.startswith("release"): return "stable"
    if name.startswith("nightly"): return "nightly"
    if name.startswith("beta"): return "beta"
    return None

def pick_candidates(releases, channel, latest_stable, asset_names=None):
    stable_tag = latest_stable.get("tag_name") if latest_stable else None
    stable_ver = parse_ver(stable_tag)
    stable_cand = None
    if latest_stable:
        url = find_asset(latest_stable, asset_names)
        if url: stable_cand = (stable_tag, url)
    old_stables, betas, nightlies, older = [], [], [], []
    for r in releases:
        tag = r.get("tag_name") or ""
        if tag == stable_tag: continue
        url = find_asset(r, asset_names)
        if not url: continue
        entry = (tag, url)
        c = classify(r, stable_tag)
        if c == "stable": old_stables.append(entry)
        elif c == "beta": betas.append(entry)
        elif c == "nightly": nightlies.append(entry)
        if parse_ver(tag) < stable_ver: older.append(entry)
    if channel == "stable": cands = ([stable_cand] if stable_cand else []) + old_stables + older
    elif channel == "beta": cands = betas + ([stable_cand] if stable_cand else []) + older
    else: cands = nightlies + betas + ([stable_cand] if stable_cand else []) + older
    out, seen = [], set()
    for tag, url in cands:
        if tag and tag not in seen: seen.add(tag); out.append((tag, url))
    return out[:MAX_ATTEMPTS]

def get_apk(tag, url, cache):
    if tag not in cache:
        path = f"build/brave_{safe_name(tag)}.apk"
        download_file(url, path)
        cache[tag] = path
    return cache[tag]

def find_working_brave_version(cands, per_bundle, gen_data, alias, cache, label, bundles):
    for tag, url in cands:
        apk = get_apk(tag, url, cache)
        out = f"build/out_{safe_name(label)}_{safe_name(tag)}.apk"
        ok, applied, dropped, missing = heal_patch(apk, out, gen_data, per_bundle, f"{label}_{tag}", alias, bundles)
        if ok: return tag, out, applied, dropped, False
    tag, url = cands[0]
    apk = get_apk(tag, url, cache)
    out = f"build/out_{safe_name(label)}_{safe_name(tag)}.apk"
    ok, applied, dropped, missing = heal_patch(apk, out, gen_data, per_bundle, f"{label}_besteffort", alias, bundles)
    return tag, out, applied, dropped, True

def get_latest_cli_jar():
    rel = gh_api_get("https://api.github.com/repos/MorpheApp/morphe-desktop/releases/latest").json()
    for a in rel.get("assets", []):
        if a.get("name", "").endswith("-all.jar"):
            download_file(a["browser_download_url"], "build/cli.jar")
            return
    raise Exception("Could not find Morphe CLI all.jar")

def find_android_tool(name):
    p = shutil.which(name)
    if p: return p
    roots = []
    for env in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        v = os.environ.get(env)
        if v: roots.append(v)
    roots.append("/usr/local/lib/android/sdk")
    cands = []
    for r in roots:
        cands += glob.glob(os.path.join(r, "build-tools", "*", name))
    cands = [c for c in cands if os.path.isfile(c) and os.access(c, os.X_OK)]
    if cands:
        cands.sort(key=lambda x: parse_ver(os.path.basename(os.path.dirname(x))), reverse=True)
        return cands[0]
    return None

def ensure_apkeditor():
    jar = "build/APKEditor.jar"
    if os.path.exists(jar) and os.path.getsize(jar) > 500_000:
        return jar
    rel = gh_api_get("https://api.github.com/repos/REAndroid/APKEditor/releases/latest").json()
    asset = next((a for a in rel.get("assets", []) if (a.get("name") or "").endswith(".jar")), None)
    if not asset: raise Exception("No APKEditor jar asset found on latest release")
    download_file(asset["browser_download_url"], jar)
    if not (os.path.exists(jar) and os.path.getsize(jar) > 500_000):
        raise Exception("APKEditor jar download invalid")
    return jar
    
def strip_permissions_from_apk(apk_path, allowlist, ks_path, ks_password, ks_alias, ks_key_password):
    print(f"\n🔒 Stripping permissions from {os.path.basename(apk_path)}")
    print(f"Allowlist ({len(allowlist)} permissions):")
    for p in allowlist: print(f"  ✓ {p}")
    decoded_dir = "build/apkeditor_decoded"
    stripped_apk = "build/stripped_unsigned.apk"
    aligned_apk = "build/stripped_aligned.apk"
    allow_set = {a.strip().lower() for a in allowlist}
    def is_allowed(perm_name): return perm_name.strip().lower() in allow_set
    try:
        apkeditor_jar = ensure_apkeditor()
        print("\n  → Decoding with APKEditor...")
        if os.path.exists(decoded_dir): shutil.rmtree(decoded_dir)
        subprocess.run(["java", "-jar", apkeditor_jar, "d", "-i", apk_path, "-o", decoded_dir, "-f"], check=True, capture_output=True, text=True)
        manifest_path = os.path.join(decoded_dir, "AndroidManifest.xml")
        if not os.path.exists(manifest_path):
            print("  ⚠️ AndroidManifest.xml not found, skipping permission strip")
            return False
        print("  → Parsing AndroidManifest.xml...")
        ET.register_namespace('android', 'http://schemas.android.com/apk/res/android')
        ET.register_namespace('tools', 'http://schemas.android.com/tools')
        tree = ET.parse(manifest_path)
        root = tree.getroot()
        ns = "{http://schemas.android.com/apk/res/android}"
        removed = []
        for elem in list(root.iter()):
            if elem.tag.endswith("uses-permission") or elem.tag.endswith("uses-permission-sdk-23"):
                perm_name = elem.get(f"{ns}name") or elem.get("{http://schemas.android.com/apk/res/android}name") or elem.get("name")
                if not perm_name: continue
                if not is_allowed(perm_name):
                    parent = None
                    for p in root.iter():
                        if elem in list(p): parent = p; break
                    if parent is not None:
                        parent.remove(elem)
                        removed.append(perm_name)
        print(f"  → Removed {len(removed)} non-allowlisted permissions.")
        affected = {FGS_PERM_TO_TYPE[p] for p in removed if p in FGS_PERM_TO_TYPE}
        if affected:
            fixed = 0
            for elem in root.iter():
                if elem.tag.endswith("service"):
                    fgs = elem.get(f"{ns}foregroundServiceType") or elem.get("foregroundServiceType")
                    if not fgs: continue
                    tokens = [t for t in fgs.split("|") if t and t not in affected]
                    if tokens: elem.set(f"{ns}foregroundServiceType", "|".join(tokens))
                    else:
                        if f"{ns}foregroundServiceType" in elem.attrib: del elem.attrib[f"{ns}foregroundServiceType"]
                        if "foregroundServiceType" in elem.attrib: del elem.attrib["foregroundServiceType"]
                    fixed += 1
            print(f"  → Sanitized foregroundServiceType on {fixed} service(s)")
        root.set("xmlns:android", "http://schemas.android.com/apk/res/android")
        root.set("xmlns:tools", "http://schemas.android.com/tools")
        print("  → Writing modified manifest...")
        tree.write(manifest_path, encoding="utf-8", xml_declaration=True)
        print("  → Rebuilding with APKEditor...")
        subprocess.run(["java", "-jar", apkeditor_jar, "b", "-i", decoded_dir, "-o", stripped_apk], check=True, capture_output=True, text=True)
        sign_src = stripped_apk
        zipalign = find_android_tool("zipalign")
        if zipalign:
            print("  → Running zipalign...")
            subprocess.run([zipalign, "-f", "-p", "4", stripped_apk, aligned_apk], check=True, capture_output=True, text=True)
            sign_src = aligned_apk
        else:
            print("  ⚠️ zipalign not found; signing without alignment")
        apksigner = find_android_tool("apksigner")
        if not apksigner: raise Exception("apksigner not found")
        print("  → Re-signing with apksigner...")
        subprocess.run([apksigner, "sign", "--ks", ks_path, "--ks-key-alias", ks_alias,
                        "--ks-pass", f"pass:{ks_password}", "--key-pass", f"pass:{ks_key_password}",
                        "--out", apk_path, sign_src], check=True, capture_output=True, text=True)
        aapt = find_android_tool("aapt") or find_android_tool("aapt2")
        if aapt:
            print("  → Verifying final manifest...")
            result = subprocess.run([aapt, "dump", "permissions", apk_path], capture_output=True, text=True)
            final_perms = []
            for line in result.stdout.splitlines():
                line = line.strip()
                if line.startswith("uses-permission:"):
                    m2 = re.search(r"name='([^']+)'", line) or re.search(r'name="([^"]+)"', line)
                    if m2: final_perms.append(m2.group(1))
            print(f"\n  ✅ FINAL RESULT: APK contains {len(final_perms)} permissions:")
            for p in final_perms: print(f"     ✓ {p}")
        else:
            print("  ⚠️ aapt not found; skipping verification")
        shutil.rmtree(decoded_dir, ignore_errors=True)
        for f in [stripped_apk, aligned_apk]:
            if os.path.exists(f): os.remove(f)
        return True
    except Exception as e:
        print(f"  ⚠️ Permission stripping error: {e}")
        print("  → Keeping original patched APK")
        for p in [decoded_dir, stripped_apk, aligned_apk]:
            if os.path.exists(p):
                if os.path.isdir(p): shutil.rmtree(p, ignore_errors=True)
                else: os.remove(p)
        return False

def format_patch_list(patches, label, threshold=10):
    if not patches:
        return f"\n{label}: none\n"
    if len(patches) <= threshold:
        return f"\n{label}:\n" + "\n".join(f"- {x}" for x in patches) + "\n"
    return f'\n<details>\n<summary>{label} ({len(patches)} patches, click to expand)</summary>\n\n' + \
           "\n".join(f"- {x}" for x in patches) + "\n\n</details>\n"

def build_extra_app(app, alias, ks_fp, notes, cache, force_rebuild=False, release_tag="", global_excludes=None, max_age_days=0):
    aid = app["id"]
    spec = app["apk"]
    arch = spec.get("arch", "arm64-v8a")
    dens = app.get("density", ["xxhdpi"])
    if isinstance(dens, str): dens = [dens]
    languages = [x.lower() for x in app.get("languages", ["en"])]
    keep_all_abis = app.get("keep_all_abis", False)
    variants = app.get("variants", [])
    for v in variants:
        if v.get("_mpps"):
            v["_ok"] = True
            v["_mpps_paths"] = [p for p, _ in v["_mpps"]]
            continue
        mpps, ok = [], True
        for b in v.get("bundles", []):
            mpp = f"bundles/{aid}_{safe_name(b['label'])}.mpp"
            try:
                url_n = normalize_bundle_url(b["url"])
                if b.get("pin"): ver = download_pinned_mpp(repo_from_url(url_n), b["pin"], mpp)
                else: ver = download_bundle_smart(url_n, mpp)
                b["_ver"] = ver
                b["_status"] = get_release_status(repo_from_url(url_n), ver)
                mpps.append(mpp)
            except Exception: ok = False; break
        v["_ok"] = ok
        v["_mpps_paths"] = mpps

    for v in variants:
        vid = v["id"]
        if v.get("_skip"):
            SKIPPED.append({"id": vid, "reason": v["_skip"]})
            notes.append(f"## {vid}\nStatus: Skipped ({v['_skip']})\n\n")
            continue

        bundles_tags = {b.get("label"): str(b.get("_ver", "?")).lstrip("v") for b in v["bundles"]}
        config_hash = compute_config_hash(v)
        resolved = str(v.get("apk_version") or spec.get("version") or "latest")
        skip, reason = should_skip_variant(vid, cache, {"resolved": resolved, "config_hash": config_hash, "bundles": bundles_tags}, max_age_days=max_age_days)
        if skip and not force_rebuild:
            SKIPPED.append({"id": vid, "reason": reason})
            notes.append(f"## {vid}\nStatus: Skipped (unchanged)\nReason: {reason}\n\n")
            continue

        if not v.get("_ok"):
            notes.append(f"## {vid}\nStatus: Failed (bundle download)\n\n")
            update_cache_entry(cache, vid, {"resolved": resolved, "config_hash": config_hash, "bundles": bundles_tags, "status": "failed", "release_tag": release_tag})
            continue

        ver = str(v.get("apk_version") or spec.get("version") or "latest")
        vsource = v.get("apk_source", spec.get("source", "apkeep"))
        bases = prepare_bases(app, aid, ver, vsource, arch, dens, languages, keep_all_abis)
        if not bases:
            notes.append(f"## {vid}\nStatus: Failed (apk source {ver})\n\n")
            update_cache_entry(cache, vid, {"resolved": resolved, "config_hash": config_hash, "bundles": bundles_tags, "status": "failed", "release_tag": release_tag})
            continue

        mpps = v["_mpps_paths"]
        gen = generate_options_file(mpps, f"build/gen_{safe_name(vid)}.json")
        if gen is None:
            notes.append(f"## {vid}\nStatus: Failed (options generation)\n\n")
            update_cache_entry(cache, vid, {"resolved": resolved, "config_hash": config_hash, "bundles": bundles_tags, "status": "failed", "release_tag": release_tag})
            continue

        excl_var = {clean_name(x).lower() for x in (v.get("exclude_patches") or [])}
        excl_glob = set(global_excludes or [])
        per_bundle, seen_lower, dup_skipped = [], set(), []
        for i, g in enumerate(gen):
            bundle_cfg = v.get("bundles", [])[i] if i < len(v.get("bundles", [])) else {}
            allow = bundle_cfg.get("patches")
            allow_l = {clean_name(x).lower() for x in allow} if allow else None
            auto_new = bundle_cfg.get("auto_include_new")
            if auto_new is None: auto_new = v.get("auto_include_new", True)
            auto_new = bool(auto_new)
            wanted = {}
            for name in sorted((g.get("patches") or {}).keys()):
                nl = clean_name(name).lower()
                if allow_l is not None and nl not in allow_l:
                    if not auto_new: continue
                    if nl in excl_glob or nl in excl_var: continue
                if v.get("merge_exclusive") and nl in seen_lower: dup_skipped.append(name); continue
                wanted[name] = {}
                seen_lower.add(nl)
            per_bundle.append(wanted)
        for d in per_bundle:
            for n in list(d):
                if clean_name(n).lower() in excl_var: del d[n]

        for patch_name, opts in (v.get("options") or {}).items():
            pl = clean_name(patch_name).lower()
            for i, g in enumerate(gen):
                for real in (g.get("patches") or {}):
                    if clean_name(real).lower() == pl: per_bundle[i].setdefault(real, {}).update(opts)
        cp = (v.get("clone_package") or "").strip()
        if cp:
            for i, g in enumerate(gen):
                for real in (g.get("patches") or {}):
                    if real.lower() == "clone app": per_bundle[i][real] = {"packageName": cp}; break
        an = (v.get("app_name") or "").strip()
        if an:
            for i, g in enumerate(gen):
                for real in (g.get("patches") or {}):
                    if real.lower() in ("custom branding", "change app name"): per_bundle[i].setdefault(real, {})["appName"] = an
        parts = [f"{b['label']}_v{str(b.get('_ver', '?')).lstrip('v')}-{b.get('_status', '?')}" for b in v["bundles"]]
        joined = "_X_".join(parts)
        bp, bmode = bases["base"]
        if not bp:
            notes.append(f"## {vid}\nStatus: Failed (base extraction)\n\n")
            update_cache_entry(cache, vid, {"resolved": resolved, "config_hash": config_hash, "bundles": bundles_tags, "status": "failed", "release_tag": release_tag})
            continue

        out = f"build/out_{safe_name(vid)}.apk"
        ok, applied, dropped, missing = heal_patch(bp, out, gen, per_bundle, vid, alias, mpps, keep_all_abis)
        if (not ver) or ver.lower() == "latest":
            ver = DETECTED_VERSIONS.get(vid, ver)
        final = f"build/{app.get('output_base', aid)}-{ver}-{joined}-patched.apk"
        if ok and os.path.exists(out):
            shutil.copyfile(out, final)
            fp = verify_signature(final, ks_fp)
            note = f"## {vid}\nApp version: {ver}\nBundles: {', '.join(parts)}\nBuild mode: {bmode}\n"
            if v.get("_resolved_why"): note += f"Version resolution: {v['_resolved_why']}\n"
            if bases["single"]: note += "Note: base is a single APK; no dynamic-feature splits to merge.\n"
            if keep_all_abis: note += "Note: kept all ABIs to satisfy multiArch manifest.\n"
            if fp: note += f"Signing fingerprint: {fp}\n"
            note += format_patch_list(applied, "Applied patches", threshold=10)
            note += format_patch_list(dropped, "Dropped after failure", threshold=5)
            note += format_patch_list(missing, "Requested but NOT FOUND", threshold=5)
            if dup_skipped: note += "\nDuplicate patches skipped:\n" + "\n".join(f"- {x}" for x in dup_skipped) + "\n"
            prof = v.get("_profile")
            if prof:
                ppath = f"profiles/{prof}.yaml"
                pdata = None
                if os.path.exists(ppath):
                    with open(ppath) as f: pdata = yaml.safe_load(f) or {}
                if pdata and pdata.get("strip_permissions"):
                    allow = [clean_name(p) for p in pdata["strip_permissions"]]
                    if strip_permissions_from_apk(final, allow, "signing/keystore.jks", os.environ.get("KEYSTORE_PASSWORD", ""), alias, os.environ.get("KEY_PASSWORD", "")):
                        note += f"\nPermission stripping: applied ({len(allow)} allowlisted)\n"
                        fp2 = verify_signature(final, ks_fp)
                        if fp2: note += f"Post-strip fingerprint: {fp2}\n"
                    else:
                        note += "\nPermission stripping: failed, kept unstripped APK\n"
            note += "\nStatus: Success\n\n"
            notes.append(note)
            BUILT.append({"id": vid, "output": final})
            update_cache_entry(cache, vid, {"resolved": resolved, "config_hash": config_hash, "bundles": bundles_tags, "status": "success", "output": final, "release_tag": release_tag})
        else:
            notes.append(f"## {vid}\nStatus: Failed\n\n")
            update_cache_entry(cache, vid, {"resolved": resolved, "config_hash": config_hash, "bundles": bundles_tags, "status": "failed", "release_tag": release_tag})

def main():
    # ==========================================
    # 🚀 CUSTOM BUILD MODE (no cache) 🚀
    # ==========================================
    if os.environ.get("CUSTOM_BUILD"):
        print("🚀 RUNNING IN CUSTOM BUILD MODE 🚀")
        app_id = os.environ.get("CUSTOM_APP", "custom")
        default_pkgs = {
            "tiktok": "com.zhiliaoapp.musically", "youtube": "com.google.android.youtube",
            "ytmusic": "com.google.android.apps.youtube.music", "google": "com.google.android.googlequicksearchbox",
            "gemini": "com.google.android.apps.bard", "windscribe": "com.windscribe.vpn",
            "protonmail": "ch.protonmail.android", "protonvpn": "ch.protonvpn.android", "brave": "com.brave.browser",
            "reddit": "com.reddit.frontpage", "mindicator": "com.mobond.mindicator"
        }
        profile_name = os.environ.get("CUSTOM_PROFILE", "").strip()
        profile, profile_src = None, None
        if profile_name:
            base = profile_name
            if base.lower().endswith((".yaml", ".yml")): base = base.rsplit(".", 1)[0]
            cand_paths = [f"profiles/{base}.yaml", f"profiles/{base}.yml"]
            if f"profiles/{profile_name}" not in cand_paths: cand_paths.append(f"profiles/{profile_name}")
            for p in cand_paths:
                if os.path.exists(p):
                    with open(p) as f: profile = yaml.safe_load(f) or {}
                    profile_src = f"repo:{p}"
                    break
            if profile is None:
                own_repo = os.environ.get("GITHUB_REPOSITORY", "")
                if own_repo:
                    try:
                        rel = gh_api_get(f"https://api.github.com/repos/{own_repo}/releases/tags/custom-profiles").json()
                        cand_names = {f"{base}.yaml", f"{base}.yml", profile_name}
                        for a in rel.get("assets", []):
                            if a.get("name", "") in cand_names:
                                pdest = f".profile_tmp_{safe_name(base)}.yaml"
                                download_file(a["browser_download_url"], pdest)
                                with open(pdest) as f: profile = yaml.safe_load(f) or {}
                                profile_src = "release-tag:custom-profiles"
                                break
                    except Exception as e: print(f"profile release-tag lookup failed: {e}")
            if profile is None:
                print(f"❌ ERROR: profile '{profile_name}' not found in profiles/ nor on release tag 'custom-profiles'")
                raise SystemExit(1)
            profile_name = base
            print(f"📄 Using profile '{profile_name}' from {profile_src}")

        ver = os.environ.get("CUSTOM_VER", "").strip() or str((profile or {}).get("apk_version", "") or "").strip() or "latest"
        src_form = os.environ.get("CUSTOM_SOURCE", "profile-default").strip()
        source_choice = src_form if src_form not in ("", "profile-default") else str((profile or {}).get("source", "auto") or "auto")
        abi_form = os.environ.get("CUSTOM_ABI", "profile-default").strip()
        abi = abi_form if abi_form not in ("", "profile-default") else str((profile or {}).get("abi", "arm64-v8a") or "arm64-v8a")
        pkg = os.environ.get("CUSTOM_PKG", "").strip() or str((profile or {}).get("package", "") or "").strip() or default_pkgs.get(app_id, f"com.custom.{app_id}")
        clone_pkg = os.environ.get("CUSTOM_CLONE", "").strip() or str((profile or {}).get("clone_package", "") or "")
        app_name = os.environ.get("CUSTOM_NAME", "").strip() or str((profile or {}).get("app_name", "") or "")

        upload_first_apps = ["tiktok", "youtube", "ytmusic", "google", "gemini"]
        source = "upload" if (source_choice == "auto" and app_id in upload_first_apps) else ("apkeep" if source_choice == "auto" else source_choice)

        bundles_cfg, options_cfg = [], {}
        global_inc = [clean_name(p) for p in os.environ.get("CUSTOM_INCLUDE", "").split(",") if clean_name(p)]
        if profile:
            if os.environ.get("CUSTOM_BUNDLES", "").strip():
                print("⚠️ profile provides bundles; ignoring form bundle_urls")
            for i, b in enumerate(profile.get("bundles") or []):
                url = normalize_bundle_url((b.get("url") or "").strip())
                if not url: continue
                repo = repo_from_url(url)
                label = (b.get("label") or (repo.split("/")[-1] if repo else f"Bundle{i}"))
                bc = {"url": url, "label": label}
                if b.get("pin"): bc["pin"] = str(b["pin"])
                if b.get("patches"): bc["patches"] = [clean_name(x) for x in (b["patches"] or [])]
                bundles_cfg.append(bc)
                for pn, ov in (b.get("options") or {}).items():
                    options_cfg.setdefault(pn, {}).update(ov or {})
            if global_inc:
                for bc in bundles_cfg:
                    if "patches" not in bc: bc["patches"] = global_inc
        else:
            raw_entries = [u.strip() for u in os.environ.get("CUSTOM_BUNDLES", "").split(",") if u.strip()]
            any_per_bundle = False
            for i, entry in enumerate(raw_entries):
                own = None
                if "::" in entry:
                    entry, own_str = entry.split("::", 1)
                    own = [clean_name(p) for p in own_str.split(";") if clean_name(p)]
                    if own: any_per_bundle = True
                url = normalize_bundle_url(entry)
                repo = repo_from_url(url)
                label = repo.split("/")[-1].replace("-patches", "").replace("-morphe", "").replace("revanced-", "").title() if repo else f"Bundle{i}"
                bc = {"url": url, "label": label}
                if own: bc["patches"] = own
                bundles_cfg.append(bc)
            if global_inc:
                for bc in bundles_cfg:
                    if "patches" not in bc: bc["patches"] = global_inc
                if any_per_bundle:
                    print("ℹ️ global include box applied only to bundles without their own '::' list")

        for line in os.environ.get("CUSTOM_OPTIONS", "").splitlines():
            line = line.strip()
            if not line or "=" not in line: continue
            patch_key, val = line.split("=", 1)
            patch_name, opt_key = patch_key.rsplit(".", 1) if "." in patch_key else (patch_key, "value")
            options_cfg.setdefault(clean_name(patch_name), {})[opt_key.strip()] = val.strip()

        exclude_patches = [clean_name(p) for p in ((profile or {}).get("exclude_patches") or [])] + \
                          [clean_name(p) for p in os.environ.get("CUSTOM_EXCLUDE", "").split(",") if clean_name(p)]

        keep_all_abis = (abi == "all")
        arch = "armeabi-v7a" if abi == "armeabi-v7a" else "arm64-v8a"

        config = {
            "auto_include_new_patches": False,
            "exclude_patches": [],
            "extra_apps": [{
                "id": app_id, "output_base": app_id.title(), "density": ["nodpi", "xxhdpi"], "languages": ["en"],
                "keep_all_abis": keep_all_abis,
                "apk": {"source": source, "package": pkg, "arch": arch, "upload_tag": "stock-tiktok-apk"},
                "variants": [{
                    "id": f"Custom-{app_id.title()}", "apk_version": ver, "clone_package": clone_pkg,
                    "app_name": app_name, "exclude_patches": exclude_patches,
                    "bundles": bundles_cfg, "options": options_cfg
                }]
            }]
        }

        if os.path.exists("build"): shutil.rmtree("build")
        if os.path.exists("bundles"): shutil.rmtree("bundles")
        os.makedirs("build"); os.makedirs("bundles")
        alias = detect_alias(); ks_fp = keystore_fingerprint(alias); get_latest_cli_jar()
        notes = []
        for app in config.get("extra_apps", []):
            build_extra_app(app, alias, ks_fp, notes, {"variants": {}}, force_rebuild=True, release_tag="",
                            global_excludes=config.get("exclude_patches", []))

        if profile and profile.get("strip_permissions"):
            allowlist = [clean_name(p) for p in profile["strip_permissions"]]
            patched_apks = glob.glob("build/*-patched.apk")
            if patched_apks:
                apk_path = patched_apks[0]
                success = strip_permissions_from_apk(apk_path, allowlist, "signing/keystore.jks",
                                                     os.environ.get("KEYSTORE_PASSWORD", ""), alias, os.environ.get("KEY_PASSWORD", ""))
                if success:
                    fp2 = verify_signature(apk_path, ks_fp)
                    notes.insert(0, f"## Permission Stripping\n✅ Permissions stripped to {len(allowlist)} allowlisted entries; re-signed (fingerprint match: {fp2 == ks_fp if ks_fp else 'n/a'}).\n\n")
                else:
                    notes.insert(0, "## Permission Stripping\n⚠️ Permission stripping failed. Original patched APK kept.\n\n")

        head = "# Custom Build Release\n\n"
        if profile_name: head += f"Profile: {profile_name} (from {profile_src})\n\n"
        with open("release_notes.md", "w") as f:
            f.write(head + "".join(notes))
        if not glob.glob("build/*-patched.apk"):
            print("\n❌ FATAL: Custom build failed to produce an APK!\n" + "".join(notes))
            raise SystemExit(1)
        print("✅ Custom build successful!")
        return

    # ==========================================
    # 📦 STANDARD BATCH MODE (v2 + cache) 📦
    # ==========================================
    config = load_config("config.yaml")
    if os.path.exists("build"): shutil.rmtree("build")
    if os.path.exists("bundles"): shutil.rmtree("bundles")
    os.makedirs("build"); os.makedirs("bundles")
    alias = detect_alias(); ks_fp = keystore_fingerprint(alias); get_latest_cli_jar()

    preflight(config)

    if "--plan" in sys.argv:
        plan = {"schema": SCHEMA,
                "resolution": RESOLUTION,
                "builds": [v["id"] for ae in config["extra_apps"] for v in ae["variants"] if not v.get("_skip")],
                "skips": [{"id": v["id"], "reason": v["_skip"]} for ae in config["extra_apps"] for v in ae["variants"] if v.get("_skip")]}
        with open("plan.json", "w") as f: json.dump(plan, f, indent=2)
        with open("resolution_report.md", "w") as f: f.write("# Version resolution\n\n" + "\n".join(RESOLUTION) + "\n")
        print("\n📋 PLAN (no builds executed):")
        print("\n".join(RESOLUTION))
        print("Builds:", ", ".join(plan["builds"]) or "(none)")
        return

    cache = load_cache()
    force_rebuild = "--force-rebuild" in sys.argv
    refresh_days = int((config.get("_defaults") or {}).get("refresh_after_days", 7) or 0)
    release_tag = "build-" + time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    with open("build/release_tag.txt", "w") as f: f.write(release_tag)

    dh6k_tag = "unknown"
    try: dh6k_tag = download_stable_mpp("dh6k/morphe-patches", "bundles/dh6k.mpp")
    except Exception: pass
    official_tag = "unknown"
    try: official_tag = download_stable_mpp("MorpheApp/morphe-patches", "bundles/official.mpp")
    except Exception: pass
    extra_bundles = {}
    for eb in config.get("extra_bundles", []):
        try:
            mpp = f"bundles/{eb['id']}.mpp"
            ver = download_bundle_smart(eb["url"], mpp)
            extra_bundles[eb["id"]] = mpp
        except Exception: pass
    BUNDLES.clear()
    BUNDLES.append("bundles/dh6k.mpp")
    if os.path.exists("bundles/official.mpp"): BUNDLES.append("bundles/official.mpp")
    gen_brave = generate_options_file(BUNDLES, "build/gen_brave.json")
    info_dh6k = parse_patches_info(["bundles/dh6k.mpp"])
    needs_brave = needs_value_names(gen_brave)
    all_names = set()
    for g in gen_brave or []: all_names |= set((g.get("patches") or {}).keys())
    def resolve(target, names):
        target = target.lower()
        for n in names:
            if n.lower() == target: return n
        for n in names:
            if target in n.lower(): return n
        return None
    brave_base = {"Brave Origin": {}, "Change app icon": {"customIcon": "assets/isoamoledbraveicon.png"}, "Disable analytics": {}}
    name_patch = resolve("change app name", all_names)
    clone_patch = resolve("clone app", all_names)
    brave_releases = get_releases("brave/brave-browser")
    latest_stable = get_latest_stable("brave/brave-browser")
    apk_cache, notes = {}, []
    for channel in ["stable", "nightly", "beta"]:
        for variant in [v for v in config["variants"] if v.get("type") == channel]:
            cands = pick_candidates(brave_releases, channel, latest_stable, variant.get("gh_assets"))
            if not cands: continue
            configurable = set(brave_base)
            if name_patch: configurable.add(name_patch)
            if clone_patch: configurable.add(clone_patch)
            auto = compute_auto(info_dh6k, CHANNEL_PKG[channel], config.get("exclude_patches", []), configurable, needs_brave) if config.get("auto_include_new_patches", True) else []
            vid = variant["id"]
            btags = {"dh6k": dh6k_tag, "official": official_tag}
            chash = compute_config_hash(variant)
            channel_latest = cands[0][0]
            skip, reason = should_skip_variant(vid, cache, {"resolved": channel_latest, "config_hash": chash, "bundles": btags})
            if skip and not force_rebuild:
                SKIPPED.append({"id": vid, "reason": reason})
                notes.append(f"## {variant.get('output_name', vid)}\nStatus: Skipped (unchanged)\nReason: {reason}\n\n")
                continue
            bundles, gen, names, n_patch, c_patch = BUNDLES, gen_brave, all_names, name_patch, clone_patch
            if variant.get("bundles"):
                bundles = []
                for bb in variant["bundles"]:
                    if isinstance(bb, dict):
                        mpp = f"bundles/{safe_name(bb.get('label', 'b'))}.mpp"
                        if not os.path.exists(mpp): download_bundle_smart(bb["url"], mpp)
                        bundles.append(mpp)
                    elif bb in extra_bundles: bundles.append(extra_bundles[bb])
                    elif os.path.exists(f"bundles/{bb}.mpp"): bundles.append(f"bundles/{bb}.mpp")
                gen = generate_options_file(bundles, f"build/gen_{vid}.json")
                names = set()
                for g in gen or []: names |= set((g.get("patches") or {}).keys())
                n_patch = resolve("change app name", names)
                c_patch = resolve("clone app", names)
            exact_asset_url = variant.get("exact_asset_url")
            if exact_asset_url:
                m = re.search(r"/download/(v[^/]+)/", exact_asset_url)
                target_tag = m.group(1) if m else "unknown"
                apk_path = f"build/brave_{safe_name(target_tag)}_exact.apk"
                try: download_file(exact_asset_url, apk_path)
                except Exception:
                    notes.append(f"## {variant['output_name']}\nStatus: Failed (exact_asset download)\n\n")
                    update_cache_entry(cache, vid, {"resolved": channel_latest, "config_hash": chash, "bundles": btags, "status": "failed", "release_tag": release_tag})
                    continue
                exact_patches = variant.get("exact_patches", {})
                if exact_patches:
                    per_bundle = [{} for _ in gen]
                    for patch_name, opts in exact_patches.items():
                        for i, g in enumerate(gen):
                            if patch_name in (g.get("patches") or {}): per_bundle[i][patch_name] = opts; break
                else:
                    per_bundle = []
                    for g in gen:
                        d = {}
                        for n in sorted((g.get("patches") or {}).keys()):
                            if n in brave_base: d[n] = dict(brave_base[n])
                            elif n in auto: d[n] = {}
                        per_bundle.append(d)
                if variant.get("app_name") and n_patch:
                    for i, g in enumerate(gen):
                        if n_patch in (g.get("patches") or {}): per_bundle[i][n_patch] = {"appName": variant["app_name"]}; break
                if variant.get("clone_package") and c_patch:
                    for i, g in enumerate(gen):
                        if c_patch in (g.get("patches") or {}): per_bundle[i][c_patch] = {"packageName": variant["clone_package"]}; break
                out = f"build/out_{safe_name(vid)}_{safe_name(target_tag)}.apk"
                ok, applied, dropped, missing = heal_patch(apk_path, out, gen, per_bundle, vid, alias, bundles)
                final = f"build/{variant['output_name']}-{target_tag}-{dh6k_tag}-patched.apk"
                if ok and os.path.exists(out): shutil.copyfile(out, final); fp = verify_signature(final, ks_fp)
                else: fp = None
                note = f"## {variant['output_name']}\nBrave version: {target_tag} (Exact Asset URL)\nPatch bundles: {dh6k_tag}, official {official_tag}\n"
                if fp: note += f"Signing fingerprint: {fp}\n"
                note += format_patch_list(applied, "Applied patches", threshold=10)
                note += format_patch_list(dropped, "Dropped after failure", threshold=5)
                note += format_patch_list(missing, "Requested but NOT FOUND", threshold=5)
                note += "\nStatus: " + ("Success" if ok else "Failed") + "\n\n"
                notes.append(note)
                update_cache_entry(cache, vid, {"resolved": channel_latest, "config_hash": chash, "bundles": btags, "status": "success" if ok else "failed", "output": final, "release_tag": release_tag})
                continue
            # ---- Explicit patch lists (from config) or legacy brave_base+auto ----
            bcfgs = [b for b in (variant.get("bundles") or []) if isinstance(b, dict)]
            if any(b.get("patches") for b in bcfgs):
                excl = {clean_name(x).lower() for x in (variant.get("exclude_patches") or [])}
                per_bundle = []
                for i, g in enumerate(gen):
                    bc = bcfgs[i] if i < len(bcfgs) else {}
                    allow = bc.get("patches")
                    allow_l = {clean_name(x).lower() for x in allow} if allow else None
                    auto_new = bc.get("auto_include_new")
                    if auto_new is None: auto_new = variant.get("auto_include_new", True)
                    auto_new = bool(auto_new)
                    wanted = {}
                    for name in sorted((g.get("patches") or {}).keys()):
                        nl = clean_name(name).lower()
                        if allow_l is not None and nl not in allow_l:
                            if not auto_new: continue
                            if nl in excl: continue
                        wanted[name] = {}
                    per_bundle.append(wanted)
                for patch_name, opts in (variant.get("options") or {}).items():
                    pl = clean_name(patch_name).lower()
                    for i, g in enumerate(gen):
                        for real in (g.get("patches") or {}):
                            if clean_name(real).lower() == pl: per_bundle[i].setdefault(real, {}).update(opts)
            else:
                per_bundle = []
                for g in gen:
                    d = {}
                    for n in sorted((g.get("patches") or {}).keys()):
                        if n in brave_base: d[n] = dict(brave_base[n])
                        elif n in auto: d[n] = {}
                    per_bundle.append(d)
            if variant.get("app_name") and n_patch:
                for i, g in enumerate(gen):
                    if n_patch in (g.get("patches") or {}): per_bundle[i][n_patch] = {"appName": variant["app_name"]}; break
            if variant.get("clone_package") and c_patch:
                for i, g in enumerate(gen):
                    if c_patch in (g.get("patches") or {}): per_bundle[i][c_patch] = {"packageName": variant["clone_package"]}; break
            tag, out, applied, dropped, best_effort = find_working_brave_version(cands, per_bundle, gen, alias, apk_cache, vid, bundles)
            final = f"build/{variant['output_name']}-{tag}-{dh6k_tag}-patched.apk"
            if os.path.exists(out): shutil.copyfile(out, final); fp = verify_signature(final, ks_fp)
            else: fp = None
            note = f"## {variant['output_name']}\nBrave version: {tag}\nPatch bundles: {dh6k_tag}, official {official_tag}\n"
            if fp: note += f"Signing fingerprint: {fp}\n"
            note += format_patch_list(applied, "Applied patches", threshold=10)
            note += format_patch_list(dropped, "Dropped after failure", threshold=5)
            note += "\nStatus: " + ("Best effort" if best_effort else "Success") + "\n\n"
            notes.append(note)
            BUILT.append({"id": vid, "output": final})
            update_cache_entry(cache, vid, {"resolved": channel_latest, "config_hash": chash, "bundles": btags, "status": "success", "output": final, "release_tag": release_tag})
    for app in config.get("extra_apps", []):
        build_extra_app(app, alias, ks_fp, notes, cache, force_rebuild, release_tag,
                        global_excludes=config.get("exclude_patches", []), max_age_days=refresh_days)

    skip_section = ""
    if SKIPPED:
        if len(SKIPPED) > 5:
            skip_section = f'\n\n## Skipped (unchanged)\n<details>\n<summary>{len(SKIPPED)} variants skipped (click to expand)</summary>\n\n' + \
                          "\n".join(f"- **{s['id']}**: {s['reason']}" for s in SKIPPED) + "\n\n</details>\n"
        else:
            skip_section = "\n\n## Skipped (unchanged)\n" + "\n".join(f"- **{s['id']}**: {s['reason']}" for s in SKIPPED) + "\n"
    head = f"# Morphe AutoBuilds Release\n\nBuilt: {len(BUILT)} · Skipped: {len(SKIPPED)}\n\n## Version resolution\n"
    with open("release_notes.md", "w") as f:
        f.write(head + "\n".join(RESOLUTION) + skip_section + "\n\n" + "".join(notes))

    save_cache(cache)
    print(f"\n📦 Built {len(BUILT)} · Skipped: {len(SKIPPED)}")

    if not glob.glob("build/*-patched.apk"):
        print("\n" + "=" * 60)
        print("❌ FATAL: No patched APKs were generated!")
        print("Internal status log:")
        print("".join(notes))
        print("=" * 60 + "\n")
        raise SystemExit(1)

if __name__ == "__main__":
    main()