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
    chosen = next((a for a in rel.get("assets", []) if "linux" in a.get("name", "").lower() and "x86_64" in a.get("name", "").lower() and not a.get("name", "").endswith((".deb", ".rpm"))), None)
    if not chosen: chosen = next((a for a in rel.get("assets", []) if "linux" in a.get("name", "").lower() and not a.get("name", "").endswith((".deb", ".rpm"))), None)
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

def apkeep_download(apkeep, pkg, version, arch, outdir):
    os.makedirs(outdir, exist_ok=True)
    spec = f"{pkg}@{version}" if version and version != "latest" else pkg
    for cmd in [[apkeep, "-a", spec, "-d", "apk-pure", "-o", f"arch={arch}", outdir], [apkeep, "-a", spec, "-o", f"arch={arch}", outdir], [apkeep, "-a", spec, outdir]]:
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

def repo_from_url(url):
    for pat in [r"raw\.githubusercontent\.com/([^/]+/[^/]+)/", r"github\.com/([^/]+/[^/]+)/", r"bundle/([^/]+/[^/]+)/", r"gitlab\.com/([^/]+/[^/]+)/"]:
        m = re.search(pat, url)
        if m: return m.group(1)
    return None

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
    RAW_CACHE[key] = result
    return result

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
    found, all_wanted = set(), set()
    for i, bundle in enumerate(data):
        wanted = per_bundle[i] if i < len(per_bundle) else {}
        all_wanted |= set(wanted)
        for name, entry in (bundle.get("patches") or {}).items():
            if name in wanted: found.add(name); enable_entry(entry, wanted[name])
            else: entry["enabled"] = False
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

def parse_patches_info(bundles):
    cmd = ["java", "-jar", "build/cli.jar", "list-patches"]
    for b in bundles: cmd += ["-p", b]
    cmd += ["--with-packages", "--with-options"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    info, cur, pending_required = [], None, False
    for raw in r.stdout.splitlines():
        line = raw.strip()
        if line.startswith("Index:"): cur = {"name": None, "packages": [], "required_opts": [], "last_key": None}; info.append(cur)
        elif cur is None: continue
        elif line.startswith("Name:"): cur["name"] = line.split(":", 1)[1].strip()
        elif line.startswith("Package name:"): cur["packages"].append(line.split(":", 1)[1].strip())
        elif line.startswith("Key:"):
            cur["last_key"] = line.split(":", 1)[1].strip()
            if pending_required: cur["required_opts"].append(cur["last_key"]); pending_required = False
        elif line.startswith("Required:"):
            req = line.split(":", 1)[1].strip().lower() == "true"
            if req and cur.get("last_key"): cur["required_opts"].append(cur["last_key"])
            else: pending_required = req
    return [x for x in info if x.get("name")]

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

def find_asset(release):
    for a in release.get("assets", []):
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

def pick_candidates(releases, channel, latest_stable):
    stable_tag = latest_stable.get("tag_name") if latest_stable else None
    stable_ver = parse_ver(stable_tag)
    stable_cand = None
    if latest_stable:
        url = find_asset(latest_stable)
        if url: stable_cand = (stable_tag, url)
    old_stables, betas, nightlies, older = [], [], [], []
    for r in releases:
        tag = r.get("tag_name") or ""
        if tag == stable_tag: continue
        url = find_asset(r)
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

def build_extra_app(app, alias, ks_fp, notes):
    aid = app["id"]
    spec = app["apk"]
    arch = spec.get("arch", "arm64-v8a")
    dens = app.get("density", ["xxhdpi"])
    if isinstance(dens, str): dens = [dens]
    languages = [x.lower() for x in app.get("languages", ["en"])]
    keep_all_abis = app.get("keep_all_abis", False)
    variants = app.get("variants", [])
    for v in variants:
        mpps, ok = [], True
        for b in v.get("bundles", []):
            mpp = f"bundles/{aid}_{safe_name(b['label'])}.mpp"
            try:
                ver = download_bundle_smart(b["url"], mpp)
                b["_ver"] = ver
                b["_status"] = get_release_status(repo_from_url(b["url"]), ver)
                mpps.append(mpp)
            except Exception as e: ok = False; break
        v["_ok"] = ok
        v["_mpps"] = mpps

    for v in variants:
        vid = v["id"]
        if not v.get("_ok"): notes.append(f"## {vid}\nStatus: Failed (bundle download)\n\n"); continue
        ver = str(v.get("apk_version") or spec.get("version") or "latest")
        vsource = v.get("apk_source", spec.get("source", "apkeep"))
        bases = prepare_bases(app, aid, ver, vsource, arch, dens, languages, keep_all_abis)
        if not bases: notes.append(f"## {vid}\nStatus: Failed (apk source {ver})\n\n"); continue
        mpps = v["_mpps"]
        gen = generate_options_file(mpps, f"build/gen_{safe_name(vid)}.json")
        if gen is None: notes.append(f"## {vid}\nStatus: Failed (options generation)\n\n"); continue
        per_bundle, seen_lower, dup_skipped = [], set(), []
        for i, g in enumerate(gen):
            bundle_cfg = v.get("bundles", [])[i] if i < len(v.get("bundles", [])) else {}
            allow = bundle_cfg.get("patches")
            allow_l = {x.lower() for x in allow} if allow else None
            wanted = {}
            for name in sorted((g.get("patches") or {}).keys()):
                nl = name.lower()
                if allow_l is not None and nl not in allow_l: continue
                if v.get("merge_exclusive") and nl in seen_lower: dup_skipped.append(name); continue
                wanted[name] = {}
                seen_lower.add(nl)
            per_bundle.append(wanted)
        excludes = {x.lower() for x in v.get("exclude_patches", [])}
        for d in per_bundle:
            for n in list(d):
                if n.lower() in excludes: del d[n]
        for patch_name, opts in (v.get("options") or {}).items():
            pl = patch_name.lower()
            for i, g in enumerate(gen):
                for real in (g.get("patches") or {}):
                    if real.lower() == pl: per_bundle[i].setdefault(real, {}).update(opts)
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
        if not bp: notes.append(f"## {vid}\nStatus: Failed (base extraction)\n\n"); continue
        out = f"build/out_{safe_name(vid)}.apk"
        ok, applied, dropped, missing = heal_patch(bp, out, gen, per_bundle, vid, alias, mpps, keep_all_abis)
        final = f"build/{app.get('output_base', aid)}-{ver}-{joined}-patched.apk"
        if ok and os.path.exists(out):
            shutil.copyfile(out, final)
            fp = verify_signature(final, ks_fp)
            note = f"## {vid}\nApp version: {ver}\nBundles: {', '.join(parts)}\nBuild mode: {bmode}\n"
            if bases["single"]: note += "Note: base is a single APK; no dynamic-feature splits to merge.\n"
            if keep_all_abis: note += "Note: kept all ABIs to satisfy multiArch manifest.\n"
            if fp: note += f"Signing fingerprint: {fp}\n"
            note += "\nApplied patches:\n" + ("\n".join(f"- {x}" for x in applied) if applied else "- none") + "\n"
            if dropped: note += "\nDropped after failure:\n" + "\n".join(f"- {x}" for x in dropped) + "\n"
            note += "\nStatus: Success\n\n"
            notes.append(note)
        else: notes.append(f"## {vid}\nStatus: Failed\n\n")

def main():
    with open("config.yaml", "r") as f: config = yaml.safe_load(f)
    if os.path.exists("build"): shutil.rmtree("build")
    if os.path.exists("bundles"): shutil.rmtree("bundles")
    os.makedirs("build")
    os.makedirs("bundles")
    alias = detect_alias()
    ks_fp = keystore_fingerprint(alias)
    get_latest_cli_jar()
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
    cache, notes = {}, []
    for channel in ["stable", "nightly", "beta"]:
        cands = pick_candidates(brave_releases, channel, latest_stable)
        if not cands: continue
        configurable = set(brave_base)
        if name_patch: configurable.add(name_patch)
        if clone_patch: configurable.add(clone_patch)
        auto = compute_auto(info_dh6k, CHANNEL_PKG[channel], config.get("exclude_patches", []), configurable, needs_brave) if config.get("auto_include_new_patches", True) else []
        for variant in [v for v in config["variants"] if v["type"] == channel]:
            bundles, gen, names, n_patch, c_patch = BUNDLES, gen_brave, all_names, name_patch, clone_patch
            if variant.get("bundles"):
                bundles = [extra_bundles[bid] if bid in extra_bundles else f"bundles/{bid}.mpp" for bid in variant["bundles"] if bid in extra_bundles or os.path.exists(f"bundles/{bid}.mpp")]
                gen = generate_options_file(bundles, f"build/gen_{variant['id']}.json")
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
                except Exception: notes.append(f"## {variant['output_name']}\nStatus: Failed (exact_asset download)\n\n"); continue
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
                            if n in brave_base: d[n] = brave_base[n]
                            elif n in auto: d[n] = {}
                        per_bundle.append(d)
                if variant.get("app_name") and n_patch:
                    for i, g in enumerate(gen):
                        if n_patch in (g.get("patches") or {}): per_bundle[i][n_patch] = {"appName": variant["app_name"]}; break
                if variant.get("clone_package") and c_patch:
                    for i, g in enumerate(gen):
                        if c_patch in (g.get("patches") or {}): per_bundle[i][c_patch] = {"packageName": variant["clone_package"]}; break
                out = f"build/out_{safe_name(variant['id'])}_{safe_name(target_tag)}.apk"
                ok, applied, dropped, missing = heal_patch(apk_path, out, gen, per_bundle, variant["id"], alias, bundles)
                final = f"build/{variant['output_name']}-{target_tag}-{dh6k_tag}-patched.apk"
                if ok and os.path.exists(out): shutil.copyfile(out, final); fp = verify_signature(final, ks_fp)
                else: fp = None
                note = f"## {variant['output_name']}\nBrave version: {target_tag} (Exact Asset URL)\nPatch bundles: {dh6k_tag}, official {official_tag}\n"
                if fp: note += f"Signing fingerprint: {fp}\n"
                note += "\nApplied patches:\n" + ("\n".join(f"- {x}" for x in applied) if applied else "- none") + "\n"
                if dropped: note += "\nDropped after failure:\n" + "\n".join(f"- {x}" for x in dropped) + "\n"
                note += "\nStatus: " + ("Success" if ok else "Failed") + "\n\n"
                notes.append(note)
                continue
            per_bundle = []
            for g in gen:
                d = {}
                for n in sorted((g.get("patches") or {}).keys()):
                    if n in brave_base: d[n] = brave_base[n]
                    elif n in auto: d[n] = {}
                per_bundle.append(d)
            if variant.get("app_name") and n_patch:
                for i, g in enumerate(gen):
                    if n_patch in (g.get("patches") or {}): per_bundle[i][n_patch] = {"appName": variant["app_name"]}; break
            if variant.get("clone_package") and c_patch:
                for i, g in enumerate(gen):
                    if c_patch in (g.get("patches") or {}): per_bundle[i][c_patch] = {"packageName": variant["clone_package"]}; break
            tag, out, applied, dropped, best_effort = find_working_brave_version(cands, per_bundle, gen, alias, cache, variant["id"], bundles)
            final = f"build/{variant['output_name']}-{tag}-{dh6k_tag}-patched.apk"
            if os.path.exists(out): shutil.copyfile(out, final); fp = verify_signature(final, ks_fp)
            else: fp = None
            note = f"## {variant['output_name']}\nBrave version: {tag}\nPatch bundles: {dh6k_tag}, official {official_tag}\n"
            if fp: note += f"Signing fingerprint: {fp}\n"
            note += "\nApplied patches:\n" + ("\n".join(f"- {x}" for x in applied) if applied else "- none") + "\n"
            if dropped: note += "\nDropped after failure:\n" + "\n".join(f"- {x}" for x in dropped) + "\n"
            note += "\nStatus: " + ("Best effort" if best_effort else "Success") + "\n\n"
            notes.append(note)
    for app in config.get("extra_apps", []): build_extra_app(app, alias, ks_fp, notes)
    with open("release_notes.md", "w") as f: f.write("# Morphe AutoBuilds Release\n\n" + "".join(notes))
    
    # CRITICAL: fail loudly if nothing was built
    if not glob.glob("build/*-patched.apk"):
        print("\n" + "=" * 60)
        print("❌ FATAL: No patched APKs were generated!")
        print("Internal status log:")
        print("".join(notes))
        print("=" * 60 + "\n")
        raise SystemExit(1)

if __name__ == "__main__":
    main()